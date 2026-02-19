#!/usr/bin/env python3
"""
Polymarket v8.0 — AI Prediction-Based Auto-Trading Bot
10-source ensemble prediction model for Polymarket BTC/ETH/SOL/DOGE
5-min and 15-min binary (Up/Down) markets using Binance real-time data.
Supports hedged (both-side) and single-direction betting strategies.
"""
import asyncio
import logging
import signal
import sys
import time

import config as cfg
from data_collector import DataCollector
from predictor import Predictor, Direction
from risk_manager import RiskManager
from trader import Trader
from notifier import Notifier

# ── Logging setup ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(cfg.LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
# Silence noisy HTTP request logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

log = logging.getLogger("bot")


class Bot:
    def __init__(self):
        self.collector = DataCollector()
        self.predictor = Predictor()
        self.risk = RiskManager()
        self.trader = Trader()
        self.notifier = Notifier()
        self._running = False
        self._cycle = 0

        # Loss cooldown
        self._daily_start_bankroll: float = 0.0
        self._daily_start_pnl: float = 0.0
        self._daily_date: int = 0
        self._cooldown_until: float = 0.0
        self._cooldown_count: int = 0

        # Prediction accuracy
        self._predictions: dict = {}  # condition_id → predicted direction
        self._pred_correct: int = 0
        self._pred_total: int = 0

    async def run(self):
        self._running = True
        log.info("=" * 60)
        log.info("Polymarket v8.0 starting")
        log.info("Coins: %s", ", ".join(cfg.COINS.keys()))
        log.info(
            "Max positions: %d | Kelly fraction: %.0f%%",
            cfg.MAX_OPEN_POSITIONS, cfg.KELLY_FRACTION * 100,
        )
        log.info("=" * 60)

        # Start all components
        await self.collector.start()
        await self.trader.start()
        await self.notifier.start()

        # Restore cooldown state
        self._cooldown_until = getattr(self.trader, "_cooldown_until", 0.0)
        if self._cooldown_until > time.time():
            remaining = (self._cooldown_until - time.time()) / 60
            log.warning(
                "COOLDOWN RESTORED: %.0f min remaining (from previous session)",
                remaining,
            )

        # Wait for data to accumulate
        log.info("Warming up data streams (30s)...")
        await asyncio.sleep(30)

        # Startup notification
        try:
            bankroll = await self.trader.get_balance()
            positions = self.trader.open_position_count()
            pending = len(self.trader.pending_hedges)
            cooldown_msg = ""
            if self._cooldown_until > time.time():
                remaining = (self._cooldown_until - time.time()) / 60
                cooldown_msg = f"\nCooldown: {remaining:.0f}min remaining"

            startup_msg = (
                f"<b>pbot v8.0 started</b>\n"
                f"Bankroll: ${bankroll:.2f}\n"
                f"Open positions: {positions}\n"
                f"Pending hedges: {pending}\n"
                f"Coins: {', '.join(c.upper() for c in cfg.COINS)}\n"
                f"Hedge: {'ON' if cfg.HEDGE_ENABLED else 'OFF'} | "
                f"Stagger: {'ON' if cfg.STAGGER_ENABLED else 'OFF'}"
                f"{cooldown_msg}"
            )
            await self.notifier.send(startup_msg)
            if self.notifier.enabled:
                log.info("Startup notification sent (bankroll=$%.2f)", bankroll)
            else:
                log.info("Ready (bankroll=$%.2f) — Telegram disabled", bankroll)
        except Exception as e:
            log.warning("Startup notification failed: %s", e)

        try:
            while self._running:
                await self._cycle_once()
                if self.trader.pending_hedges:
                    await asyncio.sleep(cfg.STAGGER_POLL_SEC)
                else:
                    await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _cycle_once(self):
        self._cycle += 1
        log.info("--- Cycle %d ---", self._cycle)

        try:
            # 1. Clean up expired markets
            self.trader.cleanup_expired()

            # 2. Discover new markets
            new_markets = await self.trader.discover_markets()
            if new_markets:
                log.info("Found %d new market(s)", len(new_markets))
            elif self._cycle % 20 == 0:
                log.info("No new crypto markets found")

            # 3. Refresh prices on tracked markets
            await self.trader.refresh_prices()

            # 4. Check settlements on expired positions
            settled = await self.trader.check_settlements()
            if settled:
                market_pnl: dict[str, float] = {}
                for pos in settled:
                    cid = pos.market.condition_id
                    market_pnl[cid] = market_pnl.get(cid, 0.0) + pos.pnl

                self.risk.open_positions = self.trader.open_position_count()

                # Prediction accuracy tracking
                for cid in market_pnl:
                    predicted = self._predictions.pop(cid, None)
                    if predicted:
                        primary_pos = next(
                            (p for p in settled if p.market.condition_id == cid),
                            None,
                        )
                        if primary_pos:
                            self._pred_total += 1
                            if primary_pos.pnl > 0:
                                self._pred_correct += 1
                            accuracy = (
                                self._pred_correct / self._pred_total * 100
                                if self._pred_total > 0 else 0
                            )
                            log.info(
                                "PREDICTION: %s predicted=%s pnl=$%.2f | "
                                "accuracy=%d/%d (%.1f%%)",
                                primary_pos.market.coin_key, predicted,
                                primary_pos.pnl,
                                self._pred_correct, self._pred_total, accuracy,
                            )

                # Notify per market
                notified: set[str] = set()
                for pos in settled:
                    cid = pos.market.condition_id
                    if cid in notified:
                        continue
                    notified.add(cid)
                    combined = market_pnl[cid]
                    await self.notifier.trade_settled(
                        pos.market.coin_key, pos.direction, combined
                    )
                    log.info(
                        "Settlement: %s combined PnL=$%.2f (total=$%.2f)",
                        pos.market.coin_key, combined, self.trader.total_pnl,
                    )

            # 5. Monitor pending staggered hedges
            if cfg.STAGGER_ENABLED and cfg.HEDGE_ENABLED:
                placed = await self.trader.monitor_pending_hedges()
                for ph in placed:
                    if ph.hedge and ph.hedge.filled:
                        improvement = self.risk.calc_staggered_improvement(
                            primary_amount=ph.primary.amount_usd if ph.primary else 0,
                            primary_price=ph.primary.entry_price if ph.primary else 0,
                            hedge_amount=ph.hedge_amount,
                            initial_hedge_price=ph.initial_hedge_price,
                            current_hedge_price=ph.hedge.entry_price,
                        )
                        await self.notifier.staggered_hedge_placed(
                            coin=ph.market.coin_key,
                            hedge_dir=ph.hedge_dir,
                            amount=ph.hedge_amount,
                            initial_price=ph.initial_hedge_price,
                            final_price=ph.hedge.entry_price,
                            improvement=improvement,
                        )
                        log.info(
                            "STAGGER COMPLETE: %s hedge placed @ %.3f "
                            "(was %.3f, improvement=%.3f, extra=$%.2f)",
                            ph.market.coin_key, ph.hedge.entry_price,
                            ph.initial_hedge_price,
                            improvement.price_improvement,
                            improvement.odds_profit,
                        )
                    elif not ph.hedge or not ph.hedge.filled:
                        log.warning(
                            "STAGGER EXPOSED: %s hedge failed",
                            ph.market.coin_key,
                        )
                        await self.notifier.error(
                            f"STAGGER WARNING: {ph.market.coin_key} hedge failed. "
                            f"Primary ${ph.primary.amount_usd:.2f} unhedged."
                            if ph.primary else
                            f"STAGGER WARNING: {ph.market.coin_key} hedge failed."
                        )

            # 6. Evaluate each market for trading opportunity
            bankroll = await self.trader.get_balance()
            self.risk.open_positions = self.trader.open_position_count()

            # Loss cooldown circuit breaker
            now_ts = time.time()
            today = int(now_ts // 86400)
            if today != self._daily_date:
                self._daily_date = today
                self._daily_start_bankroll = bankroll
                self._daily_start_pnl = self.trader.total_pnl
                self._cooldown_count = 0
                log.info("New trading day — bankroll=$%.2f", bankroll)

            in_cooldown = self._cooldown_until > now_ts
            if not in_cooldown and self._cooldown_until > 0:
                log.info(
                    "COOLDOWN ENDED: resuming trades (was paused %.0f min)",
                    cfg.LOSS_COOLDOWN_SEC / 60,
                )
                self._cooldown_until = 0.0
                self._daily_start_pnl = self.trader.total_pnl

            if not in_cooldown:
                daily_loss = self._daily_start_pnl - self.trader.total_pnl
                if (self._daily_start_bankroll > 0
                        and daily_loss > self._daily_start_bankroll * cfg.MAX_DAILY_LOSS_PCT):
                    self._cooldown_until = now_ts + cfg.LOSS_COOLDOWN_SEC
                    self._cooldown_count += 1
                    in_cooldown = True
                    minutes = cfg.LOSS_COOLDOWN_SEC / 60
                    log.warning(
                        "LOSS COOLDOWN #%d: $%.2f loss (%.1f%% of $%.2f) — "
                        "pausing new trades for %.0f min (until %s)",
                        self._cooldown_count, daily_loss,
                        daily_loss / self._daily_start_bankroll * 100,
                        self._daily_start_bankroll, minutes,
                        time.strftime("%H:%M", time.localtime(self._cooldown_until)),
                    )
                    await self.notifier.error(
                        f"Loss cooldown #{self._cooldown_count}: "
                        f"${daily_loss:.2f} loss "
                        f"({daily_loss / self._daily_start_bankroll * 100:.1f}%). "
                        f"Pausing {minutes:.0f} min."
                    )

            for cid, market in list(self.trader.markets.items()):
                if in_cooldown:
                    break

                # Skip markets too close to expiry
                time_left = market.end_time - time.time()
                if market.end_time > 0 and time_left < cfg.MIN_TIME_TO_EXPIRY:
                    continue

                # Skip if already have position in this market
                if any(p.market.condition_id == cid and not p.settled
                       for p in self.trader.positions):
                    continue

                # Skip if pending staggered hedge exists
                if self.trader.has_pending_hedge(cid):
                    continue

                # Skip if Binance data is stale
                if self.collector.is_stale(market.coin_key):
                    log.debug(
                        "SKIP %s: data stale (>%ds old)",
                        market.coin_key, cfg.DATA_STALE_SEC,
                    )
                    continue

                # Get prediction
                signals = self.collector.get_signals(market.coin_key)
                prediction = self.predictor.predict(signals)

                if prediction.direction == Direction.SKIP:
                    continue

                # Determine prices for both sides
                if prediction.direction == Direction.UP:
                    primary_price = market.yes_bid if market.yes_bid > 0 else market.yes_price
                    hedge_price = market.no_bid if market.no_bid > 0 else market.no_price
                    hedge_dir = "DOWN"
                else:
                    primary_price = market.no_bid if market.no_bid > 0 else market.no_price
                    hedge_price = market.yes_bid if market.yes_bid > 0 else market.yes_price
                    hedge_dir = "UP"

                # Track prediction for accuracy
                self._predictions[cid] = prediction.direction.value

                if cfg.HEDGE_ENABLED:
                    if cfg.STAGGER_ENABLED and time_left > cfg.STAGGER_MIN_TIME_BEFORE_EXPIRY:
                        await self._execute_staggered_hedge(
                            market, prediction, primary_price, hedge_price,
                            hedge_dir, bankroll,
                        )
                    else:
                        await self._execute_hedge(
                            market, prediction, primary_price, hedge_price,
                            hedge_dir, bankroll,
                        )
                else:
                    await self._execute_single(
                        market, prediction, primary_price, bankroll,
                    )

            # 7. Periodic status update (every 20 cycles)
            if self._cycle % 20 == 0:
                accuracy = (
                    self._pred_correct / self._pred_total * 100
                    if self._pred_total > 0 else 0
                )
                daily_pnl = self.trader.total_pnl - self._daily_start_pnl
                await self.notifier.status(
                    bankroll=bankroll,
                    positions=self.risk.open_positions,
                    total_pnl=self.trader.total_pnl,
                )
                cooldown_status = (
                    f"cooldown until {time.strftime('%H:%M', time.localtime(self._cooldown_until))}"
                    if self._cooldown_until > time.time()
                    else "active"
                )
                log.info(
                    "STATUS: bankroll=$%.2f positions=%d total_pnl=$%.2f "
                    "daily_pnl=$%.2f prediction=%d/%d (%.1f%%) trading=%s",
                    bankroll, self.risk.open_positions,
                    self.trader.total_pnl, daily_pnl,
                    self._pred_correct, self._pred_total, accuracy,
                    cooldown_status,
                )

        except Exception as e:
            log.exception("Cycle error: %s", e)
            await self.notifier.error(str(e))

    async def _execute_hedge(self, market, prediction, primary_price,
                               hedge_price, hedge_dir, bankroll):
        """Place a hedged bet with fill-safety protocol."""
        decision = self.risk.evaluate_hedge(
            confidence=prediction.confidence,
            primary_price=primary_price,
            hedge_price=hedge_price,
            bankroll=bankroll,
        )
        if not decision.approved:
            log.info("Skip %s %s: %s",
                     market.coin_key, prediction.direction.value, decision.reason)
            return

        pair = await self.trader.buy_hedge_pair(
            market=market,
            primary_dir=prediction.direction.value,
            hedge_dir=hedge_dir,
            primary_amount=decision.primary_amount,
            hedge_amount=decision.hedge_amount,
        )
        if pair.primary and pair.primary.filled:
            self.risk.open_positions += 1
            fill_status = "BOTH" if pair.hedge_filled else "PRIMARY_ONLY"
            await self.notifier.hedge_opened(
                coin=market.coin_key,
                direction=prediction.direction.value,
                primary=decision.primary_amount,
                hedge=decision.hedge_amount,
                primary_price=primary_price,
                hedge_price=hedge_price,
                score=prediction.score,
                guaranteed=decision.guaranteed,
            )
            log.info(
                "HEDGE[%s]: %s %s primary=$%.2f@%.3f hedge=$%.2f@%.3f "
                "(ensemble=%+.4f, conf=%.2f, net=$%.2f)",
                fill_status, market.coin_key, prediction.direction.value,
                decision.primary_amount, primary_price,
                decision.hedge_amount, hedge_price,
                prediction.score, prediction.confidence,
                decision.net_profit,
            )
        elif not pair.primary:
            log.info("NO FILL: %s — both legs cancelled, no exposure",
                     market.coin_key)

    async def _execute_staggered_hedge(self, market, prediction, primary_price,
                                         hedge_price, hedge_dir, bankroll):
        """Place primary bet now, schedule hedge for later at better odds."""
        decision = self.risk.evaluate_hedge(
            confidence=prediction.confidence,
            primary_price=primary_price,
            hedge_price=hedge_price,
            bankroll=bankroll,
            stagger=True,
        )
        if not decision.approved:
            log.info("Skip %s %s: %s",
                     market.coin_key, prediction.direction.value, decision.reason)
            return

        pending = await self.trader.buy_staggered_primary(
            market=market,
            primary_dir=prediction.direction.value,
            primary_amount=decision.primary_amount,
            hedge_dir=hedge_dir,
            hedge_amount=decision.hedge_amount,
            hedge_price=hedge_price,
        )
        if pending and pending.primary:
            self.risk.open_positions += 1
            await self.notifier.staggered_primary_placed(
                coin=market.coin_key,
                direction=prediction.direction.value,
                primary_amount=decision.primary_amount,
                primary_price=primary_price,
                hedge_amount=decision.hedge_amount,
                hedge_price=hedge_price,
                score=prediction.score,
            )
            log.info(
                "STAGGER[PRIMARY]: %s %s primary=$%.2f@%.3f "
                "hedge=$%.2f pending (initial_price=%.3f)",
                market.coin_key, prediction.direction.value,
                decision.primary_amount, primary_price,
                decision.hedge_amount, hedge_price,
            )

    async def _execute_single(self, market, prediction, token_price, bankroll):
        """Place a single-direction bet (no hedge)."""
        decision = self.risk.evaluate(
            confidence=prediction.confidence,
            market_price=token_price,
            bankroll=bankroll,
        )
        if not decision.approved:
            log.info("Skip %s %s: %s",
                     market.coin_key, prediction.direction.value, decision.reason)
            return

        pos = await self.trader.buy(
            market=market,
            direction=prediction.direction.value,
            amount_usd=decision.bet_amount,
        )
        if pos:
            self.risk.open_positions += 1
            await self.notifier.trade_opened(
                coin=market.coin_key,
                direction=prediction.direction.value,
                amount=decision.bet_amount,
                price=token_price,
                score=prediction.score,
            )
            log.info(
                "TRADE: %s %s $%.2f @ %.3f (ensemble=%+.4f, conf=%.2f)",
                market.coin_key, prediction.direction.value,
                decision.bet_amount, token_price,
                prediction.score, prediction.confidence,
            )

    async def _shutdown(self):
        log.info("Shutting down...")
        self.trader._cooldown_until = self._cooldown_until
        self.trader.save_state()
        log.info("State saved to disk")
        await self.collector.stop()
        await self.trader.stop()
        await self.notifier.stop()
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
