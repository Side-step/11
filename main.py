#!/usr/bin/env python3
"""
Polymarket v8.0 — 크립토 복리 스캘핑 봇.

v3.0 스캘핑 전략 (5분봉/15분봉/전략A/전략C + 컨플루언스 + 복리 + 적응학습)
+ v8.0 봇 라이프사이클 (깔끔한 시작/루프/종료, 마켓 발견 참고).

의사결정 우선순위: 5분봉 > 15분봉 > 전략A > 전략C
동시 포지션: 최대 4개
"""
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from src import config
from src.adaptive_learning import AdaptiveLearningEngine
from src.auto_redeem import AutoRedeemer
from src.binance_feed import BinanceFeed
from src.bot_state import BotPhase, BotState, ExitReason, Position
from src.confluence import compute_confluence, build_snapshot
from src.polymarket_api import MarketInfo, PolymarketClient
from src.risk_manager import RiskManager
from src.strategies.five_min import FiveMinSignal, evaluate_five_min
from src.strategies.fifteen_min import FifteenMinSignal, evaluate_fifteen_min
from src.strategies.strategy_a import StrategyASignal, evaluate_strategy_a
from src.strategies.strategy_c import StrategyCSignal, evaluate_strategy_c
from src.telegram_bot import TelegramNotifier

# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("bot.log", mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

log = logging.getLogger("bot")


class Bot:
    """v8.0 라이프사이클 + v3.0 스캘핑 전략."""

    def __init__(self):
        # v3.0 핵심 컴포넌트
        self.poly = PolymarketClient()
        self.feed = BinanceFeed()
        self.bot = BotState()
        self.risk: Optional[RiskManager] = None
        self.learning = AdaptiveLearningEngine()
        self.redeemer = AutoRedeemer()
        self.telegram = TelegramNotifier()

        # 마켓 캐시
        self._markets: list[MarketInfo] = []
        self._last_full_scan: float = 0.0

        # 루프 제어
        self._running = False
        self._cycle = 0

    # ── 부팅 ────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        log.info("=" * 60)
        log.info("Polymarket Scalping Bot v8.0")
        log.info("Mode: %s | Max Positions: %d",
                 config.OPERATION_MODE, config.MAX_CONCURRENT_POSITIONS)
        log.info("=" * 60)

        # 1) Polymarket 클라이언트 초기화
        if not self.poly.initialize():
            log.error("BOOT FAILED: Polymarket client init failed")
            return

        # 2) 잔고 확인
        balance = self.poly.get_balance()
        env_capital = float(os.environ.get("INITIAL_CAPITAL", 0))
        if env_capital > 0:
            initial = env_capital
        elif balance > 0:
            initial = balance
        else:
            log.warning("Balance query returned $0 — using $200 default")
            initial = 200.0

        self.bot = BotState(initial_balance=initial)
        self.risk = RiskManager(self.bot, self.feed, self.poly)
        log.info("STEP 1: Balance = $%.2f (source: %s)",
                 self.bot.balance,
                 "env" if env_capital > 0 else ("api" if balance > 0 else "default"))

        # 3) 마켓 스캔
        self._markets = self.poly.fetch_crypto_markets()
        self._last_full_scan = time.time()
        log.info("STEP 2: Found %d crypto markets", len(self._markets))

        # 4) Binance 과거 데이터 로드
        self.feed.load_historical()
        log.info("STEP 3: Binance historical data loaded (1m/5m/15m)")

        # 5) 학습 엔진 초기화
        self.learning.initialize()
        log.info("STEP 4: Learning engine initialized (%d trades)",
                 self.learning._trade_count)

        # 6) Auto-Redeemer
        self.redeemer.initialize()

        self.bot.phase = BotPhase.RUNNING
        log.info("BOOT COMPLETE - Trading loop starting (max %d positions)",
                 config.MAX_CONCURRENT_POSITIONS)

        # 부팅 알림
        await self.telegram.notify_boot(self.bot)

        # 백그라운드 태스크 시작
        tasks = [
            asyncio.create_task(self.feed.start_websocket()),
            asyncio.create_task(self.redeemer.start_loop()),
            asyncio.create_task(self._trading_loop()),
            asyncio.create_task(self._daily_tasks()),
            asyncio.create_task(self.telegram.start_polling(self.bot)),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Bot shutting down")
        finally:
            self._running = False
            self.feed.stop()
            self.redeemer.stop()

    # ── 트레이딩 루프 ──────────────────────────────────────────

    async def _trading_loop(self):
        """10초 주기 트레이딩 루프."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                log.exception("Trading loop error: %s", e)

            self._cycle += 1

            # 5분(30틱)마다 하트비트
            if self._cycle % 30 == 0:
                log.info(
                    "HEARTBEAT: balance=$%.2f | positions=%d/%d | "
                    "markets=%d | phase=%s | streak=W%d/L%d",
                    self.bot.balance, self.bot.position_count(),
                    config.MAX_CONCURRENT_POSITIONS,
                    len(self._markets), self.bot.phase.value,
                    self.bot.consecutive_wins, self.bot.consecutive_losses,
                )

            await asyncio.sleep(10)

    async def _tick(self):
        """단일 트레이딩 틱."""

        # 1. 쿨다운 체크
        if self.bot.phase == BotPhase.COOLDOWN:
            if self.risk.check_restart():
                await self.telegram.notify_restart(
                    True, self.bot.balance, self.bot.current_bet_pct)
            return

        if self.bot.phase != BotPhase.RUNNING:
            return

        # 2. 비상 체크
        emergency = self.risk.check_emergency()
        if emergency:
            self.risk.handle_emergency(emergency)
            cooldown_until = time.time() + 900
            await self.telegram.notify_emergency(
                emergency, self.bot.balance, cooldown_until)
            return

        # 3. 모든 열린 포지션 관리
        await self._manage_all_positions()

        # 4. 추가 포지션 진입 가능 여부
        if not self.bot.can_trade():
            return

        # 5. 마켓 스캔 (5분마다)
        await self._refresh_markets()

        # 6. 신호 평가 (우선순위: 5분봉 > 15분봉 > A > C)
        await self._evaluate_signals()

    # ── 포지션 관리 ─────────────────────────────────────────────

    async def _manage_all_positions(self):
        """모든 열린 포지션을 순회하며 관리."""
        for pos in list(self.bot.positions):
            # 현재 가격 업데이트
            price = self.poly.get_midpoint(pos.token_id)
            if price > 0:
                pos.current_price = price

            # 포지션 관리 (TP/SL/트레일링/시간제한)
            exit_reason = self.risk.manage_position(pos)

            # 지표 역전 체크
            if not exit_reason:
                exit_reason = self._check_indicator_reversal(pos)

            if exit_reason:
                await self._close_position(pos, exit_reason)

    def _check_indicator_reversal(self, pos: Position) -> Optional[ExitReason]:
        """지표 역전으로 인한 청산 필요 여부를 확인."""
        asset_state = self.feed.get_state(
            config.detect_asset(pos.market_question) or "BTC"
        )
        if not asset_state:
            return None

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
            return ExitReason.INDICATOR_REVERSAL
        return None

    async def _close_position(self, pos: Position, reason: ExitReason):
        """포지션을 청산하고 결과를 기록."""
        old_pct = self.bot.current_bet_pct
        self.risk.execute_exit(pos, reason)

        # 학습 데이터 기록
        snapshot = {
            "trade_id": f"T-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_type": pos.market_type,
            "strategy": pos.strategy,
            "asset": config.detect_asset(pos.market_question) or "BTC",
            "direction": pos.direction,
            "indicators": {},
            "context": {
                "hour_utc": datetime.now(timezone.utc).hour,
                "confluence_score": pos.confluence_score,
                "streak": self.bot.consecutive_wins or -self.bot.consecutive_losses,
                "hwm_drawdown": self.bot.hwm_drawdown_pct() * 100,
                "open_positions": self.bot.position_count(),
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
            reason_str = (f"{self.bot.consecutive_wins}연승"
                          if self.bot.consecutive_wins > 0
                          else f"{self.bot.consecutive_losses}연패")
            next_bet = self.bot.balance * new_pct
            await self.telegram.notify_bet_change(
                old_pct, new_pct, reason_str, next_bet)

    # ── 신호 평가 (v3.0 전략 파이프라인) ───────────────────────

    async def _evaluate_signals(self):
        """전략 우선순위에 따라 진입 신호를 평가. 최대 4개 포지션."""
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
            if not self.bot.can_trade():
                return

        # 15분봉 시장
        fifteen_min_signal = evaluate_fifteen_min(
            self.bot, self.feed, self.poly, self._markets,
            adaptive_weights=aw, blend_ratio=blend,
        )
        if fifteen_min_signal:
            await self._execute_entry_15min(fifteen_min_signal)
            if not self.bot.can_trade():
                return

        # 전략 A: 크립토 가격 연동
        signal_a = evaluate_strategy_a(
            self.bot, self.feed, self.poly, self._markets,
            adaptive_weights=aw, blend_ratio=blend,
        )
        if signal_a:
            await self._execute_entry_a(signal_a)
            if not self.bot.can_trade():
                return

        # 전략 C: 오더북 불균형 (보조)
        if not self.bot.hwm_restricts_strategy():
            signal_c = evaluate_strategy_c(
                self.bot, self.feed, self.poly, self._markets,
                adaptive_weights=aw, blend_ratio=blend,
            )
            if signal_c:
                await self._execute_entry_c(signal_c)

    # ── 진입 실행 ──────────────────────────────────────────────

    async def _execute_entry_5min(self, signal: FiveMinSignal):
        """5분봉 전략 진입."""
        bet_amount = self.bot.compute_bet_amount(boost=signal.confluence.boost_factor)
        if bet_amount <= 0:
            return

        ok, reason = self.risk.validate_entry(
            "Type A", "5min", bet_amount,
            market_id=signal.market.condition_id,
        )
        if not ok:
            return

        await self._place_entry(
            market=signal.market, direction=signal.direction,
            bet_amount=bet_amount, strategy="5min", market_type="Type A",
            confluence_score=signal.confluence.total,
            boost_factor=signal.confluence.boost_factor,
        )

    async def _execute_entry_15min(self, signal: FifteenMinSignal):
        """15분봉 전략 진입."""
        bet_amount = self.bot.compute_bet_amount(boost=signal.confluence.boost_factor)
        if bet_amount <= 0:
            return

        ok, reason = self.risk.validate_entry(
            "Type A", "15min", bet_amount,
            market_id=signal.market.condition_id,
        )
        if not ok:
            return

        await self._place_entry(
            market=signal.market, direction=signal.direction,
            bet_amount=bet_amount, strategy="15min", market_type="Type A",
            confluence_score=signal.confluence.total,
            boost_factor=signal.confluence.boost_factor,
        )

    async def _execute_entry_a(self, signal: StrategyASignal):
        """전략 A 진입."""
        bet_amount = self.bot.compute_bet_amount(boost=signal.confluence.boost_factor)
        if bet_amount <= 0:
            return

        # 패턴 체크 (적응형 학습)
        loss_mult = self.learning.check_loss_pattern(
            signal.snapshot.to_dict(), {})
        if loss_mult is None:
            log.info("Strategy A blocked by loss pattern")
            return
        win_mult = self.learning.check_win_pattern(signal.snapshot.to_dict(), {})
        bet_amount *= loss_mult * win_mult

        ok, reason = self.risk.validate_entry(
            "Type B", "A", bet_amount,
            market_id=signal.market.condition_id,
        )
        if not ok:
            log.debug("Entry blocked: %s", reason)
            return

        await self._place_entry(
            market=signal.market, direction=signal.direction,
            bet_amount=bet_amount, strategy="A",
            market_type=signal.market.market_type,
            confluence_score=signal.confluence.total,
            boost_factor=signal.confluence.boost_factor,
        )

    async def _execute_entry_c(self, signal: StrategyCSignal):
        """전략 C 진입 (보조, 베팅 축소)."""
        bet_amount = self.bot.compute_bet_amount() * config.STRATEGY_C_BET_MULTIPLIER
        if bet_amount <= 0:
            return

        ok, reason = self.risk.validate_entry(
            "Type B", "C", bet_amount,
            market_id=signal.market.condition_id,
        )
        if not ok:
            return

        await self._place_entry(
            market=signal.market, direction=signal.direction,
            bet_amount=bet_amount, strategy="C", market_type="Type B",
            confluence_score=signal.confluence.total, boost_factor=1.0,
        )

    async def _place_entry(
        self, market: MarketInfo, direction: str, bet_amount: float,
        strategy: str, market_type: str, confluence_score: float,
        boost_factor: float,
    ):
        """실제 주문을 제출하고 포지션을 생성."""
        token_id = market.yes_token_id if direction == "Yes" else market.no_token_id
        if not token_id:
            log.warning("No token ID for %s %s", direction, market.question[:40])
            return
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

        # GTC + postOnly 리밋 주문 (메이커 수수료 0%)
        resp = self.poly.place_limit_order(
            token_id=token_id, price=aligned_price, size=size,
            side="buy", tick_size=tick_size, post_only=True,
        )

        if not resp:
            # postOnly 실패 시 FAK로 전환
            resp = self.poly.place_fak_order(
                token_id=token_id, price=aligned_price + tick_size * 2,
                size=size, side="buy", tick_size=tick_size,
            )

        if not resp:
            log.warning("Entry order failed for %s", market.question[:40])
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
            position.tp_order_id = None

        # 멀티 포지션 리스트에 추가
        self.bot.add_position(position)

        # 익절 오더 배치 (FAK 즉시체결인 경우만 — GTC 리밋은 미체결이므로 TP 불가)
        order_status = resp.get("status", "")
        if order_status == "matched":
            tp_raw = min(position.tp_price, 0.999)  # Polymarket 가격 상한
            tp_price = round(
                round(tp_raw / tick_size) * tick_size, 6)
            tp_price = min(tp_price, 0.999)  # tick 정렬 후 재확인
            tp_resp = self.poly.place_limit_order(
                token_id=token_id, price=tp_price, size=size,
                side="sell", tick_size=tick_size, post_only=False,
            )
            if tp_resp and tp_resp.get("orderID"):
                position.tp_order_id = tp_resp["orderID"]
        else:
            log.info("  TP order deferred (buy order status=%s, not yet filled)",
                     order_status)

        log.info(
            "ENTRY [%d/%d]: %s %s @ %.4f, $%.2f, strategy=%s, confluence=%.1f",
            self.bot.position_count(), config.MAX_CONCURRENT_POSITIONS,
            direction, market.question[:40], aligned_price,
            bet_amount, strategy, confluence_score,
        )

        await self.telegram.notify_entry(self.bot, position)

    # ── 마켓 스캔 ──────────────────────────────────────────────

    async def _refresh_markets(self):
        """마켓 목록을 주기적으로 갱신."""
        now = time.time()
        if now - self._last_full_scan < config.FULL_RESCAN_INTERVAL:
            return

        self._markets = self.poly.fetch_crypto_markets()
        self._last_full_scan = now

        # XRP 필터
        self._markets = [m for m in self._markets if not m.is_xrp]

        with_tokens = sum(1 for m in self._markets if m.yes_token_id)
        log.info("SCAN: %d markets total, %d with valid token IDs",
                 len(self._markets), with_tokens)
        if self._markets:
            sample = self._markets[0]
            log.info("SCAN sample: %s | yes_price=%.4f | vol24h=%.0f | "
                     "yes_token=%s...",
                     sample.question[:50], sample.yes_price,
                     sample.volume_24h, sample.yes_token_id[:20])

    # ── 일일 태스크 ────────────────────────────────────────────

    async def _daily_tasks(self):
        """매일 00:00 UTC에 리포트 + 백업."""
        while self._running:
            now = datetime.now(timezone.utc)
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now.hour != 0 or now.minute != 0:
                from datetime import timedelta
                tomorrow += timedelta(days=1)
            wait_sec = (tomorrow - now).total_seconds()
            await asyncio.sleep(min(wait_sec, 3600))

            if not self._running:
                break

            await self.telegram.send_daily_report(self.bot)
            self.learning.backup_daily()
            self.bot.reset_daily()

    # ── 종료 ───────────────────────────────────────────────────

    async def shutdown(self):
        """봇 안전 종료."""
        log.info("Shutting down...")
        self._running = False

        if self.bot.has_position():
            log.warning("Closing %d positions before shutdown",
                        self.bot.position_count())
            self.risk.close_all_positions(ExitReason.MANUAL)

        self.poly.cancel_all()
        self.feed.stop()
        self.redeemer.stop()
        log.info("Shutdown complete")

    def stop(self):
        self._running = False


# ── Entry point ─────────────────────────────────────────────────

def main():
    bot = Bot()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.stop)

    try:
        loop.run_until_complete(bot.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
