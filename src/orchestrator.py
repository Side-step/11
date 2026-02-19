"""
메인 봇 오케스트레이터.
모든 모듈을 통합하여 10초 주기 트레이딩 루프를 실행합니다.

의사결정 우선순위: 5분봉 > 전략A > 전략B > 전략C
동시 포지션: 1개만
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from src import config
from src.adaptive_learning import AdaptiveLearningEngine
from src.auto_redeem import AutoRedeemer
from src.binance_feed import BinanceFeed
from src.bot_state import BotPhase, BotState, ExitReason, Position
from src.confluence import IndicatorSnapshot, build_snapshot, compute_confluence
from src.polymarket_api import MarketInfo, PolymarketClient
from src.risk_manager import RiskManager
from src.strategies.five_min import FiveMinSignal, evaluate_five_min
from src.strategies.strategy_a import StrategyASignal, evaluate_strategy_a
from src.strategies.strategy_b import StrategyBSignal, evaluate_strategy_b
from src.strategies.strategy_c import StrategyCSignal, evaluate_strategy_c
from src.telegram_bot import TelegramNotifier

logger = logging.getLogger(__name__)


class ScalpingOrchestrator:
    """Polymarket 크립토 복리 스캘핑 봇 오케스트레이터."""

    def __init__(self):
        # Core components
        self.bot = BotState()
        self.poly = PolymarketClient()
        self.feed = BinanceFeed()
        self.risk = RiskManager(self.bot, self.feed, self.poly)
        self.learning = AdaptiveLearningEngine()
        self.redeemer = AutoRedeemer()
        self.telegram = TelegramNotifier()

        # Market cache
        self._markets: list[MarketInfo] = []
        self._last_market_scan: float = 0.0
        self._last_full_scan: float = 0.0

        # Loop control
        self._running = False
        self._loop_interval = 10  # seconds

    # ── 부팅 시퀀스 ──────────────────────────────────────────

    async def boot(self) -> bool:
        """
        봇 부팅 시퀀스:
        1. 잔고 확인
        2. 시장 스캔
        3. BTC/ETH 데이터 연결
        4. 트레이딩 루프 시작
        """
        logger.info("=" * 60)
        logger.info("Polymarket Compound Scalping Bot - BOOTING")
        logger.info("Mode: %s", config.OPERATION_MODE)
        logger.info("=" * 60)

        # STEP 1: Polymarket 클라이언트 초기화
        if not self.poly.initialize():
            logger.error("BOOT FAILED: Polymarket client init failed")
            return False

        # STEP 2: 잔고 확인
        balance = self.poly.get_balance()
        initial = float(os.environ.get("INITIAL_CAPITAL", 0)) or balance or 200.0
        self.bot = BotState(initial_balance=initial)
        self.risk = RiskManager(self.bot, self.feed, self.poly)
        self.telegram = TelegramNotifier()
        logger.info("STEP 1: Balance = $%.2f", self.bot.balance)

        # STEP 3: 시장 스캔
        self._markets = self.poly.fetch_crypto_markets()
        logger.info("STEP 2: Found %d crypto markets", len(self._markets))

        # STEP 4: Binance 과거 데이터 로드
        self.feed.load_historical()
        logger.info("STEP 3: Binance historical data loaded")

        # STEP 5: 학습 엔진 초기화
        self.learning.initialize()
        logger.info("STEP 4: Learning engine initialized (%d trades)", self.learning._trade_count)

        # STEP 6: Auto-Redeemer 초기화
        self.redeemer.initialize()

        self.bot.phase = BotPhase.RUNNING
        logger.info("BOOT COMPLETE - Trading loop starting")
        return True

    # ── 메인 루프 ─────────────────────────────────────────────

    async def run(self):
        """메인 트레이딩 루프."""
        if not await self.boot():
            return

        self._running = True

        # 백그라운드 태스크 시작
        tasks = [
            asyncio.create_task(self.feed.start_websocket()),
            asyncio.create_task(self.redeemer.start_loop()),
            asyncio.create_task(self._trading_loop()),
            asyncio.create_task(self._daily_tasks()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Orchestrator shutting down")
        finally:
            self._running = False
            self.feed.stop()
            self.redeemer.stop()

    async def _trading_loop(self):
        """10초 주기 트레이딩 루프."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Trading loop error: %s", e, exc_info=True)
            await asyncio.sleep(self._loop_interval)

    async def _tick(self):
        """단일 트레이딩 틱 (10초마다 실행)."""

        # 1. 쿨다운 체크
        if self.bot.phase == BotPhase.COOLDOWN:
            if self.risk.check_restart():
                await self.telegram.notify_restart(True, self.bot.balance, self.bot.current_bet_pct)
            return

        if self.bot.phase != BotPhase.RUNNING:
            return

        # 2. 비상 체크
        emergency = self.risk.check_emergency()
        if emergency:
            self.risk.handle_emergency(emergency)
            cooldown_until = time.time() + 900  # 15분 쿨다운
            await self.telegram.notify_emergency(emergency, self.bot.balance, cooldown_until)
            return

        # 3. 포지션 관리 (보유 중일 때)
        if self.bot.has_position():
            await self._manage_position()
            return

        # 4. 시장 스캔 (주기적)
        await self._refresh_markets()

        # 5. 신호 평가 (우선순위: 5분봉 > A > B > C)
        await self._evaluate_signals()

    # ── 포지션 관리 ──────────────────────────────────────────

    async def _manage_position(self):
        """열린 포지션의 가격 업데이트 + 청산 판단."""
        pos = self.bot.position
        if not pos:
            return

        # 현재 가격 업데이트
        price = self.poly.get_midpoint(pos.token_id)
        if price > 0:
            pos.current_price = price

        # 15분 타임프레임 관리
        exit_reason = self.risk.manage_position()

        # 지표 역전 체크
        if not exit_reason:
            asset_state = self.feed.get_state(
                self._detect_asset(pos.market_question)
            )
            if asset_state:
                reverse_dir = "sell" if pos.direction == "Yes" else "buy"
                from src.indicators.orderbook import compute_ofi
                bids, asks = self.poly.get_orderbook(pos.token_id)
                ofi = compute_ofi(bids, asks) if bids and asks else None

                reverse_score = compute_confluence(
                    direction=reverse_dir,
                    rsi_1m=asset_state.rsi_1m,
                    rsi_5m=asset_state.rsi_5m,
                    ema=asset_state.ema,
                    vwap=asset_state.vwap,
                    bb=asset_state.bollinger,
                    macd=asset_state.macd,
                    volume=asset_state.volume_ratio,
                    ofi=ofi,
                )
                if reverse_score.total >= 7:
                    exit_reason = ExitReason.INDICATOR_REVERSAL

        if exit_reason:
            await self._close_position(exit_reason)

    async def _close_position(self, reason: ExitReason):
        """포지션을 청산하고 결과를 기록합니다."""
        pos = self.bot.position
        if not pos:
            return

        old_pct = self.bot.current_bet_pct
        self.risk.execute_exit(reason)

        # 학습 데이터 기록
        snapshot = {
            "trade_id": f"T-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_type": pos.market_type,
            "strategy": pos.strategy,
            "asset": self._detect_asset(pos.market_question) or "BTC",
            "direction": pos.direction,
            "indicators": {},  # snapshot was recorded at entry
            "context": {
                "hour_utc": datetime.now(timezone.utc).hour,
                "confluence_score": pos.confluence_score,
                "streak": self.bot.consecutive_wins or -self.bot.consecutive_losses,
                "hwm_drawdown": self.bot.hwm_drawdown_pct() * 100,
            },
            "result": {
                "outcome": "win" if pos.pnl_usd >= 0 else "loss",
                "pnl_pct": pos.pnl_pct * 100,
                "pnl_usd": pos.pnl_usd,
                "hold_time_sec": pos.hold_time_sec,
                "exit_type": reason.value,
            },
        }
        self.learning.record_trade(snapshot)

        # 텔레그램 알림
        await self.telegram.notify_exit(self.bot, pos, reason)

        # 베팅 비율 변경 알림
        new_pct = self.bot.current_bet_pct
        if new_pct != old_pct:
            reason_str = f"{self.bot.consecutive_wins}연승" if self.bot.consecutive_wins > 0 else f"{self.bot.consecutive_losses}연패"
            next_bet = self.bot.balance * new_pct
            await self.telegram.notify_bet_change(old_pct, new_pct, reason_str, next_bet)

    # ── 신호 평가 ─────────────────────────────────────────────

    async def _evaluate_signals(self):
        """전략 우선순위에 따라 진입 신호를 평가합니다."""
        if not self.bot.can_trade():
            return

        aw = self.learning.get_adaptive_weights()
        blend = self.learning.get_blend_ratio()

        # 5분봉 시장 우선
        five_min_signal = evaluate_five_min(
            self.bot, self.feed, self.poly, self._markets,
            adaptive_weights=aw, blend_ratio=blend,
        )
        if five_min_signal:
            await self._execute_entry_5min(five_min_signal)
            return

        # 전략 A: 크립토 가격 연동
        signal_a = evaluate_strategy_a(
            self.bot, self.feed, self.poly, self._markets,
            adaptive_weights=aw, blend_ratio=blend,
        )
        if signal_a:
            await self._execute_entry_a(signal_a)
            return

        # 전략 C: 오더북 불균형 (보조)
        if not self.bot.hwm_restricts_strategy():
            signal_c = evaluate_strategy_c(
                self.bot, self.feed, self.poly, self._markets,
                adaptive_weights=aw, blend_ratio=blend,
            )
            if signal_c:
                await self._execute_entry_c(signal_c)
                return

    # ── 진입 실행 ─────────────────────────────────────────────

    async def _execute_entry_a(self, signal: StrategyASignal):
        """전략 A 진입 실행."""
        bet_amount = self.bot.compute_bet_amount(boost=signal.confluence.boost_factor)
        if bet_amount <= 0:
            return

        # 패턴 체크
        loss_mult = self.learning.check_loss_pattern(
            signal.snapshot.to_dict(), {}
        )
        if loss_mult is None:
            logger.info("Strategy A blocked by loss pattern")
            return
        win_mult = self.learning.check_win_pattern(signal.snapshot.to_dict(), {})
        bet_amount *= loss_mult * win_mult

        # 리스크 검증
        ok, reason = self.risk.validate_entry("Type B", "A", bet_amount)
        if not ok:
            logger.debug("Entry blocked: %s", reason)
            return

        await self._place_entry(
            market=signal.market,
            direction=signal.direction,
            bet_amount=bet_amount,
            strategy="A",
            market_type=signal.market.market_type,
            confluence_score=signal.confluence.total,
            boost_factor=signal.confluence.boost_factor,
        )

    async def _execute_entry_5min(self, signal: FiveMinSignal):
        """5분봉 전략 진입 실행."""
        bet_amount = self.bot.compute_bet_amount(boost=signal.confluence.boost_factor)
        if bet_amount <= 0:
            return

        ok, reason = self.risk.validate_entry("Type A", "5min", bet_amount)
        if not ok:
            return

        await self._place_entry(
            market=signal.market,
            direction=signal.direction,
            bet_amount=bet_amount,
            strategy="5min",
            market_type="Type A",
            confluence_score=signal.confluence.total,
            boost_factor=signal.confluence.boost_factor,
        )

    async def _execute_entry_c(self, signal: StrategyCSignal):
        """전략 C 진입 실행 (보조, 베팅 축소)."""
        bet_amount = self.bot.compute_bet_amount() * config.STRATEGY_C_BET_MULTIPLIER
        if bet_amount <= 0:
            return

        ok, reason = self.risk.validate_entry("Type B", "C", bet_amount)
        if not ok:
            return

        await self._place_entry(
            market=signal.market,
            direction=signal.direction,
            bet_amount=bet_amount,
            strategy="C",
            market_type="Type B",
            confluence_score=signal.confluence.total,
            boost_factor=1.0,
        )

    async def _place_entry(
        self,
        market: MarketInfo,
        direction: str,
        bet_amount: float,
        strategy: str,
        market_type: str,
        confluence_score: float,
        boost_factor: float,
    ):
        """실제 주문을 제출하고 포지션을 생성합니다."""
        token_id = market.yes_token_id if direction == "Yes" else market.no_token_id
        tick_size = market.tick_size or 0.01

        # 진입 가격 결정
        price = self.poly.get_price(token_id, side="buy")
        if price <= 0:
            return

        # 주식 수 계산
        size = bet_amount / price if price > 0 else 0
        if size <= 0:
            return

        # tick_size 배수 정렬
        aligned_price = round(round(price / tick_size) * tick_size, 6)

        # GTC + postOnly 리밋 주문
        resp = self.poly.place_limit_order(
            token_id=token_id,
            price=aligned_price,
            size=size,
            side="buy",
            tick_size=tick_size,
            post_only=True,
        )

        if not resp:
            # postOnly 실패 시 FAK로 전환
            resp = self.poly.place_fak_order(
                token_id=token_id,
                price=aligned_price + tick_size * 2,
                size=size,
                side="buy",
                tick_size=tick_size,
            )

        if not resp:
            logger.warning("Entry order failed for %s", market.question[:40])
            return

        # 포지션 생성
        position = Position(
            market_id=market.condition_id,
            market_question=market.question,
            market_type=market_type,
            strategy=strategy,
            direction=direction,
            token_id=token_id,
            entry_price=aligned_price,
            size=size,
            bet_amount=bet_amount,
            confluence_score=confluence_score,
            boost_factor=boost_factor,
            current_price=aligned_price,
            peak_price=aligned_price,
        )

        if resp.get("orderID"):
            position.tp_order_id = None  # 별도 익절 오더 배치

        self.bot.position = position

        # 익절 오더 배치
        tp_price = round(
            round(position.tp_price / tick_size) * tick_size, 6
        )
        tp_resp = self.poly.place_limit_order(
            token_id=token_id,
            price=tp_price,
            size=size,
            side="sell",
            tick_size=tick_size,
            post_only=False,
        )
        if tp_resp and tp_resp.get("orderID"):
            position.tp_order_id = tp_resp["orderID"]

        logger.info(
            "ENTRY: %s %s @ %.4f, $%.2f, strategy=%s, confluence=%.1f",
            direction, market.question[:40], aligned_price,
            bet_amount, strategy, confluence_score,
        )

        await self.telegram.notify_entry(self.bot, position)

    # ── 시장 스캔 ─────────────────────────────────────────────

    async def _refresh_markets(self):
        """시장 목록을 주기적으로 갱신합니다."""
        now = time.time()

        # 전체 재스캔 (5분마다)
        if now - self._last_full_scan >= config.FULL_RESCAN_INTERVAL:
            self._markets = self.poly.fetch_crypto_markets()
            self._last_full_scan = now
            self._last_market_scan = now

            # XRP 필터 로그
            xrp_count = sum(1 for m in self._markets if m.is_xrp)
            if xrp_count > 0:
                logger.debug("Filtered %d XRP markets", xrp_count)
                self._markets = [m for m in self._markets if not m.is_xrp]

    # ── 일일 태스크 ──────────────────────────────────────────

    async def _daily_tasks(self):
        """매일 00:00 UTC에 일일 리포트 + 백업."""
        while self._running:
            now = datetime.now(timezone.utc)
            # 다음 00:00 UTC까지 대기
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now.hour != 0 or now.minute != 0:
                from datetime import timedelta
                tomorrow += timedelta(days=1)
            wait_sec = (tomorrow - now).total_seconds()
            await asyncio.sleep(min(wait_sec, 3600))

            if not self._running:
                break

            # 일일 리포트
            await self.telegram.send_daily_report(self.bot)

            # 학습 데이터 백업
            self.learning.backup_daily()

            # 일일 카운터 리셋
            self.bot.reset_daily()

    # ── 유틸리티 ──────────────────────────────────────────────

    def _detect_asset(self, question: str) -> Optional[str]:
        q = question.upper()
        for asset in config.MONITORED_ASSETS:
            if asset in q:
                return asset
        return None

    async def shutdown(self):
        """봇을 안전하게 종료합니다."""
        logger.info("Shutting down...")
        self._running = False

        if self.bot.has_position():
            logger.warning("Closing position before shutdown")
            self.risk.execute_exit(ExitReason.MANUAL)

        self.poly.cancel_all()
        self.feed.stop()
        self.redeemer.stop()
        logger.info("Shutdown complete")
