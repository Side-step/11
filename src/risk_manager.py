"""
리스크 매니저: 비상 규칙, 자동 재시작, 포지션 관리, 슬리피지 모니터링.
모든 비상 중단은 15분 쿨다운 후 자동 재시작합니다.
멀티 포지션(최대 4개) 관리를 지원합니다.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from src import config
from src.binance_feed import BinanceFeed
from src.bot_state import BotState, BotPhase, Position, ExitReason
from src.polymarket_api import PolymarketClient

logger = logging.getLogger(__name__)


class RiskManager:
    """위험 관리 + 비상 처리 + 자동 재시작."""

    def __init__(
        self,
        bot: BotState,
        feed: BinanceFeed,
        poly: PolymarketClient,
    ):
        self.bot = bot
        self.feed = feed
        self.poly = poly
        self._last_api_check: float = 0.0
        self._last_orderbook_ok: float = time.time()

    # ── 비상 규칙 체크 (매 루프마다) ─────────────────────────

    def check_emergency(self) -> Optional[str]:
        """
        비상 중단 조건을 체크합니다.
        Returns: 중단 사유 문자열 or None
        """
        # 1. 블랙스완 체크 (BTC/ETH 5분 내 ±5%)
        for symbol in ["BTC", "ETH"]:
            if self.feed.is_black_swan(symbol):
                return f"Black swan: {symbol} ±5% in 5min"

        # 2. HWM -50% 체크
        if self.bot.should_halt_hwm():
            return f"HWM drawdown >= 50%: {self.bot.hwm_drawdown_pct()*100:.1f}%"

        # 3. 8연패 체크
        if self.bot.consecutive_losses >= 8:
            return "8 consecutive losses"

        # 4. 잔고 부족 체크
        bet_pct = self.bot.compute_bet_pct()
        if bet_pct is not None:
            min_bet = self.bot.balance * bet_pct
            if min_bet < 1.0:
                return f"Balance too low for min bet: ${self.bot.balance:.2f}"

        # 5. 슬리피지 연속 체크
        if self.bot.slippage_consecutive >= config.SLIPPAGE_CONSECUTIVE:
            return f"Consecutive slippage > {config.SLIPPAGE_LIMIT_RATIO*100:.0f}%"

        # 6. 오더북 갱신 실패 체크
        if self.bot.orderbook_fail_count >= config.ORDERBOOK_FAIL_LIMIT:
            return f"Orderbook update failed {config.ORDERBOOK_FAIL_LIMIT} times"

        return None

    def handle_emergency(self, reason: str):
        """
        비상 중단을 처리합니다.
        1. 미체결 주문 취소
        2. 모든 열린 포지션 시장가 청산
        3. 쿨다운 진입
        """
        logger.warning("EMERGENCY: %s", reason)

        # 미체결 주문 전량 취소
        self.poly.cancel_all()

        # 모든 열린 포지션 강제 청산
        for pos in list(self.bot.positions):
            self._force_close_position(pos, ExitReason.EMERGENCY)

        # 쿨다운 진입
        self.bot.enter_cooldown(reason)

    # ── 자동 재시작 ──────────────────────────────────────────

    def check_restart(self) -> bool:
        """
        쿨다운 후 재시작 조건을 체크합니다.
        Returns: True if restart successful
        """
        if not self.bot.is_in_cooldown():
            return False

        # 쿨다운 시간이 아직 안 지났으면 대기
        if time.time() < self.bot.cooldown_until:
            return False

        # 재시작 체크리스트
        checks = {
            "api_responsive": self._check_api_health(),
            "orderbook_ok": self._check_orderbook_health(),
            "balance_sufficient": self.bot.balance >= 1.0,
            "btc_stable": self._check_btc_stability(),
        }

        all_pass = all(checks.values())

        if all_pass:
            self.bot.try_restart()
            logger.info("RESTART SUCCESS: checks=%s", checks)
            return True
        else:
            # 실패 시 추가 15분 대기
            self.bot.cooldown_until = time.time() + config.RESTART_CHECK_INTERVAL
            self.bot.restart_attempts += 1
            logger.warning(
                "RESTART FAILED (attempt %d): %s",
                self.bot.restart_attempts, checks,
            )
            return False

    def _check_api_health(self) -> bool:
        """API 정상 응답 확인 (3회 연속 성공)."""
        success = 0
        for _ in range(3):
            try:
                start = time.time()
                self.poly.get_price(
                    self.poly.fetch_crypto_markets()[0].yes_token_id
                    if self.poly.fetch_crypto_markets()
                    else ""
                )
                elapsed = time.time() - start
                if elapsed < config.API_TIMEOUT_SEC:
                    success += 1
            except Exception:
                pass
        return success >= 3

    def _check_orderbook_health(self) -> bool:
        """오더북 정상 갱신 확인."""
        try:
            markets = self.poly.fetch_crypto_markets()
            if markets:
                bids, asks = self.poly.get_orderbook(markets[0].yes_token_id)
                return len(bids) > 0 and len(asks) > 0
        except Exception:
            pass
        return False

    def _check_btc_stability(self) -> bool:
        """BTC 5분 변동률 < 3% 확인."""
        state = self.feed.get_state("BTC")
        if state:
            return abs(state.change_5m_pct) < config.RESTART_BTC_STABILITY
        return False

    # ── 포지션 관리 (멀티 포지션) ──────────────────────────────

    def manage_position(self, pos: Position) -> Optional[ExitReason]:
        """
        개별 포지션의 15분 타임프레임을 관리합니다.
        Returns: ExitReason if position should be closed, None otherwise
        """
        if not pos:
            return None

        phase = pos.get_phase()

        # 강제 청산 (14:30~15:00)
        if pos.should_force_close():
            return ExitReason.TIME_LIMIT

        # 손절 체크
        if pos.current_price <= pos.sl_price:
            return ExitReason.STOP_LOSS

        # 익절 체크
        tp_target = pos.tp_price
        if pos.should_reduce_tp():
            tp_target = pos.entry_price * (1 + config.REDUCED_TP_PCT)

        if pos.current_price >= tp_target:
            return ExitReason.TAKE_PROFIT

        # 트레일링 스탑 (중반 이후)
        if pos.should_activate_trailing():
            pos.trailing_active = True
            if pos.current_price > pos.peak_price:
                pos.peak_price = pos.current_price

        if pos.trailing_stop_hit():
            return ExitReason.TRAILING

        # 후반 (12:00~14:30): 수익 중이면 현재가 청산 고려
        if phase == "late" and pos.pnl_pct > 0:
            return ExitReason.TIME_LIMIT

        return None

    def execute_exit(self, pos: Position, reason: ExitReason):
        """
        특정 포지션을 청산합니다.
        """
        if not pos:
            return

        logger.info(
            "EXIT [%s]: %s, pnl=%.2f%%, $%.2f, held=%ds",
            reason.value, pos.market_question[:40],
            pos.pnl_pct * 100, pos.pnl_usd, pos.hold_time_sec,
        )

        # 기존 익절 주문 취소
        if pos.tp_order_id:
            self.poly.cancel_order(pos.tp_order_id)

        # FAK로 즉시 청산
        sell_price = 0.01  # 최저가로 즉시 체결
        self.poly.place_fak_order(
            token_id=pos.token_id,
            price=sell_price,
            size=pos.size,
            side="sell",
            tick_size=0.01,
        )

        # 결과 기록
        if pos.pnl_usd >= 0:
            self.bot.record_win(pos.pnl_usd)
        else:
            self.bot.record_loss(pos.pnl_usd)

        # 포지션 리스트에서 제거
        self.bot.remove_position(pos)

    def _force_close_position(self, pos: Position, reason: ExitReason):
        """비상 시 특정 포지션 강제 청산."""
        self.execute_exit(pos, reason)

    def close_all_positions(self, reason: ExitReason):
        """모든 포지션을 청산합니다."""
        for pos in list(self.bot.positions):
            self.execute_exit(pos, reason)

    # ── 슬리피지 모니터링 ────────────────────────────────────

    def record_slippage(self, expected_price: float, actual_price: float, target_move: float):
        """
        슬리피지를 기록하고 연속 초과 시 경고합니다.
        """
        slippage = abs(actual_price - expected_price)
        slippage_ratio = slippage / target_move if target_move > 0 else 0

        if slippage_ratio > config.SLIPPAGE_LIMIT_RATIO:
            self.bot.slippage_consecutive += 1
            logger.warning(
                "Slippage %.2f%% > limit (consecutive: %d)",
                slippage_ratio * 100, self.bot.slippage_consecutive,
            )
        else:
            self.bot.slippage_consecutive = 0

    def record_orderbook_result(self, success: bool):
        """오더북 갱신 결과를 기록합니다."""
        if success:
            self.bot.orderbook_fail_count = 0
        else:
            self.bot.orderbook_fail_count += 1

    # ── 진입 전 검증 ─────────────────────────────────────────

    def validate_entry(
        self,
        market_type: str,
        strategy: str,
        bet_amount: float,
        market_id: str = "",
    ) -> tuple[bool, str]:
        """
        진입 전 최종 검증을 수행합니다.
        Returns: (allowed, reason)
        """
        # 최대 동시 포지션 수 초과
        if not self.bot.can_open_more():
            return False, f"Max positions ({config.MAX_CONCURRENT_POSITIONS}) reached"

        # 동일 시장에 이미 포지션 존재
        if market_id and self.bot.has_market_position(market_id):
            return False, f"Already has position in market {market_id[:20]}"

        # 쿨다운 중
        if self.bot.is_in_cooldown():
            return False, "In cooldown"

        # 봇 상태
        if self.bot.phase != BotPhase.RUNNING:
            return False, f"Bot phase: {self.bot.phase.value}"

        # HWM 전략 제한
        if self.bot.hwm_restricts_strategy() and strategy not in ("A",):
            return False, "HWM drawdown restricts to strategy A only"

        # 총 노출 + 신규 베팅이 잔고 80% 초과
        total_exposure = self.bot.total_exposure() + bet_amount
        if total_exposure > self.bot.balance * 0.80:
            return False, f"Total exposure ${total_exposure:.2f} > 80% of balance"

        # 잔고 확인
        available = self.bot.balance - self.bot.total_exposure()
        if bet_amount > available:
            return False, f"Bet ${bet_amount:.2f} > available ${available:.2f}"

        if bet_amount < 1.0:
            return False, f"Bet too small: ${bet_amount:.2f}"

        return True, "OK"
