"""
Polymarket v8.0 — Data Collector.
Wraps BinanceFeed to provide real-time signals for the prediction engine.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import config as cfg
from src.binance_feed import AssetState, BinanceFeed

log = logging.getLogger("data_collector")


@dataclass
class SignalBundle:
    """All signals for a single coin at a point in time."""
    coin_key: str
    price: float = 0.0
    change_1m_pct: float = 0.0
    change_5m_pct: float = 0.0
    change_15m_pct: float = 0.0

    # Indicators (from AssetState)
    rsi_1m: Any = None
    rsi_5m: Any = None
    rsi_15m: Any = None
    ema: Any = None
    ema_15m: Any = None
    macd: Any = None
    vwap: Any = None
    bollinger: Any = None
    bollinger_15m: Any = None
    volume_ratio: Any = None
    atr: Any = None
    atr_15m: Any = None

    # Candle data
    candles_1m: list = field(default_factory=list)
    candles_5m: list = field(default_factory=list)
    candles_15m: list = field(default_factory=list)

    last_update: float = 0.0


class DataCollector:
    """Collects and manages real-time Binance data for all monitored coins."""

    def __init__(self):
        self.feed = BinanceFeed()
        self._ws_task: Optional[asyncio.Task] = None

    async def start(self):
        """Load historical data and start WebSocket stream."""
        self.feed.load_historical()
        self._ws_task = asyncio.create_task(self.feed.start_websocket())
        log.info("DataCollector started (%d assets)", len(self.feed.assets))

    async def stop(self):
        """Stop WebSocket stream."""
        self.feed.stop()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        log.info("DataCollector stopped")

    def is_stale(self, coin_key: str) -> bool:
        """Check if data for a coin is stale (too old)."""
        state = self.feed.get_state(coin_key.upper())
        if not state:
            return True
        if state.last_update == 0:
            return True
        return (time.time() - state.last_update) > cfg.DATA_STALE_SEC

    def get_signals(self, coin_key: str) -> Optional[SignalBundle]:
        """Get all current signals for a coin."""
        state = self.feed.get_state(coin_key.upper())
        if not state:
            return None
        if state.price <= 0:
            return None

        return SignalBundle(
            coin_key=coin_key,
            price=state.price,
            change_1m_pct=state.change_1m_pct,
            change_5m_pct=state.change_5m_pct,
            change_15m_pct=state.change_15m_pct,
            rsi_1m=state.rsi_1m,
            rsi_5m=state.rsi_5m,
            rsi_15m=state.rsi_15m,
            ema=state.ema,
            ema_15m=state.ema_15m,
            macd=state.macd,
            vwap=state.vwap,
            bollinger=state.bollinger,
            bollinger_15m=state.bollinger_15m,
            volume_ratio=state.volume_ratio,
            atr=state.atr,
            atr_15m=state.atr_15m,
            candles_1m=state.candles_1m,
            candles_5m=state.candles_5m,
            candles_15m=state.candles_15m,
            last_update=state.last_update,
        )

    def get_all_signals(self) -> Dict[str, SignalBundle]:
        """Get signals for all coins."""
        result = {}
        for key in cfg.COINS:
            sig = self.get_signals(key)
            if sig:
                result[key] = sig
        return result
