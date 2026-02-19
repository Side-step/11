"""
Polymarket v8.0 — Notification Module.
Wraps TelegramNotifier for the v8.0 bot interface.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.telegram_bot import TelegramNotifier

log = logging.getLogger("notifier")


class Notifier:
    """v8.0 notification interface wrapping TelegramNotifier."""

    def __init__(self):
        self._tg = TelegramNotifier()
        self._poll_task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self._tg.enabled

    async def start(self):
        """Start Telegram polling (for commands)."""
        if self._tg.enabled:
            log.info("Notifier started (Telegram enabled)")
        else:
            log.info("Notifier started (Telegram disabled)")

    async def stop(self):
        """Stop polling."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        log.info("Notifier stopped")

    async def send(self, text: str):
        """Send a raw message."""
        await self._tg.send(text, "P2")

    async def error(self, msg: str):
        """Send an error notification."""
        text = f"<b>Error:</b> {_esc(msg)}"
        await self._tg.send(text, "P1")

    async def trade_opened(
        self, coin: str, direction: str, amount: float,
        price: float, score: float,
    ):
        """Notify single-direction trade opened."""
        text = (
            f"<b>TRADE OPENED</b>\n"
            f"Coin: {coin.upper()} {direction}\n"
            f"Amount: ${amount:.2f} @ {price:.3f}\n"
            f"Score: {score:+.2f}"
        )
        await self._tg.send(text, "P2")

    async def hedge_opened(
        self, coin: str, direction: str, primary: float,
        hedge: float, primary_price: float, hedge_price: float,
        score: float, guaranteed: float,
    ):
        """Notify hedge pair opened."""
        text = (
            f"<b>HEDGE OPENED</b>\n"
            f"Coin: {coin.upper()} {direction}\n"
            f"Primary: ${primary:.2f} @ {primary_price:.3f}\n"
            f"Hedge: ${hedge:.2f} @ {hedge_price:.3f}\n"
            f"Score: {score:+.2f}\n"
            f"Guaranteed: ${guaranteed:.2f}"
        )
        await self._tg.send(text, "P2")

    async def staggered_primary_placed(
        self, coin: str, direction: str, primary_amount: float,
        primary_price: float, hedge_amount: float,
        hedge_price: float, score: float,
    ):
        """Notify staggered primary leg placed."""
        text = (
            f"<b>STAGGER PRIMARY</b>\n"
            f"Coin: {coin.upper()} {direction}\n"
            f"Primary: ${primary_amount:.2f} @ {primary_price:.3f}\n"
            f"Hedge pending: ${hedge_amount:.2f} (initial {hedge_price:.3f})\n"
            f"Score: {score:+.2f}"
        )
        await self._tg.send(text, "P2")

    async def staggered_hedge_placed(
        self, coin: str, hedge_dir: str, amount: float,
        initial_price: float, final_price: float,
        improvement: object,
    ):
        """Notify staggered hedge leg placed."""
        imp = getattr(improvement, "price_improvement", 0)
        odds = getattr(improvement, "odds_profit", 0)
        text = (
            f"<b>STAGGER HEDGE PLACED</b>\n"
            f"Coin: {coin.upper()} {hedge_dir}\n"
            f"Amount: ${amount:.2f}\n"
            f"Price: {initial_price:.3f} -> {final_price:.3f}\n"
            f"Improvement: {imp:.3f} (extra ${odds:.2f})"
        )
        await self._tg.send(text, "P2")

    async def trade_settled(self, coin: str, direction: str, pnl: float):
        """Notify trade settled."""
        emoji = "+" if pnl >= 0 else ""
        text = (
            f"<b>SETTLED</b>\n"
            f"Coin: {coin.upper()}\n"
            f"PnL: {emoji}${pnl:.2f}"
        )
        await self._tg.send(text, "P2")

    async def status(self, bankroll: float, positions: int, total_pnl: float):
        """Periodic status update."""
        text = (
            f"<b>STATUS</b>\n"
            f"Bankroll: ${bankroll:.2f}\n"
            f"Positions: {positions}\n"
            f"Total PnL: ${total_pnl:+.2f}"
        )
        await self._tg.send(text, "P3")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
