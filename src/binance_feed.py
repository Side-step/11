"""
Binance 실시간 데이터 피드.
REST API로 과거 캔들 로드, WebSocket으로 실시간 업데이트.
모든 모니터링 자산에 대해 1분봉/5분봉 + 보조 지표를 실시간 계산합니다.
XRP는 절대 모니터링하지 않습니다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import aiohttp
import requests

from src import config
from src.indicators.technical import (
    ATRResult, BollingerResult, EMAResult, MACDResult,
    RSIResult, VWAPResult, VolumeResult,
    compute_atr, compute_bollinger, compute_ema_cross,
    compute_macd, compute_rsi, compute_volume_ratio, compute_vwap,
)

logger = logging.getLogger(__name__)


@dataclass
class AssetState:
    """단일 자산의 실시간 상태."""
    symbol: str
    price: float = 0.0
    change_1m_pct: float = 0.0
    change_5m_pct: float = 0.0
    high_15m: float = 0.0
    low_15m: float = 0.0

    # 캔들 저장소
    candles_1m: List[dict] = field(default_factory=list)
    candles_5m: List[dict] = field(default_factory=list)

    # 1분봉 지표 (1초마다 갱신)
    rsi_1m: Optional[RSIResult] = None
    ema: Optional[EMAResult] = None
    macd: Optional[MACDResult] = None
    volume_ratio: Optional[VolumeResult] = None
    vwap: Optional[VWAPResult] = None

    # 5분봉 지표 (5초마다 갱신)
    rsi_5m: Optional[RSIResult] = None
    bollinger: Optional[BollingerResult] = None
    atr: Optional[ATRResult] = None

    last_update: float = 0.0


class BinanceFeed:
    """Binance 실시간 시세 + 지표 계산 엔진."""

    def __init__(self):
        self.assets: Dict[str, AssetState] = {}
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False
        self._on_price_change: Optional[Callable] = None
        self._last_5m_update: Dict[str, float] = {}

        # 모니터링 자산 초기화 (XRP 제외)
        for symbol in config.MONITORED_ASSETS:
            pair = f"{symbol}USDT"
            self.assets[pair] = AssetState(symbol=symbol)

    # ── REST API: 과거 캔들 로드 ─────────────────────────────

    def load_historical(self):
        """봇 시작 시 REST API로 과거 캔들 100개 로드 (지표 초기화용)."""
        for pair, state in self.assets.items():
            try:
                # 1분봉 100개
                candles_1m = self._fetch_klines(pair, "1m", 100)
                state.candles_1m = candles_1m

                # 5분봉 50개
                candles_5m = self._fetch_klines(pair, "5m", 50)
                state.candles_5m = candles_5m

                if candles_1m:
                    state.price = candles_1m[-1]["close"]

                # 초기 지표 계산
                self._compute_1m_indicators(pair)
                self._compute_5m_indicators(pair)

                logger.info(
                    "Loaded %s: %d 1m candles, %d 5m candles, price=%.2f",
                    pair, len(candles_1m), len(candles_5m), state.price,
                )
            except Exception as e:
                logger.error("Failed to load %s: %s", pair, e)

    def _fetch_klines(self, symbol: str, interval: str, limit: int) -> List[dict]:
        """Binance REST API에서 OHLCV 캔들 데이터를 가져옵니다."""
        url = f"{config.BINANCE_REST}/api/v3/klines"
        resp = requests.get(
            url,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        candles = []
        for k in resp.json():
            candles.append({
                "timestamp": k[0] / 1000,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return candles

    # ── WebSocket: 실시간 스트림 ──────────────────────────────

    async def start_websocket(self, on_price_change: Optional[Callable] = None):
        """Combined WebSocket 스트림으로 모든 자산의 실시간 데이터를 수신합니다."""
        self._on_price_change = on_price_change
        self._running = True

        streams = []
        for pair in self.assets:
            p = pair.lower()
            streams.extend([
                f"{p}@kline_1m",
                f"{p}@kline_5m",
                f"{p}@aggTrade",
            ])

        url = f"{config.BINANCE_WS}/stream?streams={'/'.join(streams)}"

        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=20) as ws:
                        logger.info("Binance WebSocket connected (%d streams)", len(streams))
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_ws_message(json.loads(msg.data))
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                logger.error("WS error: %s", ws.exception())
                                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("WebSocket disconnected: %s, reconnecting in 5s", e)
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    def _handle_ws_message(self, data: dict):
        """WebSocket 메시지를 처리합니다."""
        stream = data.get("stream", "")
        payload = data.get("data", {})

        if "@kline_" in stream:
            self._handle_kline(stream, payload)
        elif "@aggTrade" in stream:
            self._handle_trade(stream, payload)

    def _handle_kline(self, stream: str, data: dict):
        """캔들 업데이트를 처리합니다."""
        kline = data.get("k", {})
        pair = kline.get("s", "").upper()
        interval = kline.get("i", "")
        is_closed = kline.get("x", False)

        if pair not in self.assets:
            return

        state = self.assets[pair]
        candle = {
            "timestamp": kline["t"] / 1000,
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
        }

        if interval == "1m":
            if is_closed:
                state.candles_1m.append(candle)
                state.candles_1m = state.candles_1m[-150:]
            else:
                # 현재 진행 중인 캔들 업데이트
                if state.candles_1m:
                    state.candles_1m[-1] = candle
                else:
                    state.candles_1m.append(candle)

            state.price = candle["close"]
            state.last_update = time.time()
            self._compute_1m_indicators(pair)
            self._check_price_change(pair)

        elif interval == "5m":
            if is_closed:
                state.candles_5m.append(candle)
                state.candles_5m = state.candles_5m[-80:]
            else:
                if state.candles_5m:
                    state.candles_5m[-1] = candle
                else:
                    state.candles_5m.append(candle)

            now = time.time()
            if now - self._last_5m_update.get(pair, 0) >= 5:
                self._compute_5m_indicators(pair)
                self._last_5m_update[pair] = now

    def _handle_trade(self, stream: str, data: dict):
        """체결 데이터로 최신 가격 업데이트."""
        pair = data.get("s", "").upper()
        if pair in self.assets:
            self.assets[pair].price = float(data.get("p", 0))

    # ── 지표 계산 ─────────────────────────────────────────────

    def _compute_1m_indicators(self, pair: str):
        """1분봉 기반 지표를 재계산합니다 (RSI, EMA, MACD, Volume, VWAP)."""
        state = self.assets[pair]
        candles = state.candles_1m
        if len(candles) < 30:
            return

        state.rsi_1m = compute_rsi(candles, period=14)
        state.ema = compute_ema_cross(candles, fast=9, slow=21)
        state.macd = compute_macd(candles, fast=12, slow=26, signal_period=9)
        state.volume_ratio = compute_volume_ratio(candles, lookback=20)
        state.vwap = compute_vwap(candles)

        # 1분/5분 변동률
        if len(candles) >= 2:
            prev = candles[-2]["close"]
            if prev > 0:
                state.change_1m_pct = (state.price - prev) / prev
        if len(candles) >= 6:
            prev5 = candles[-6]["close"]
            if prev5 > 0:
                state.change_5m_pct = (state.price - prev5) / prev5

        # 15분 고저
        recent = candles[-15:] if len(candles) >= 15 else candles
        state.high_15m = max(c["high"] for c in recent)
        state.low_15m = min(c["low"] for c in recent)

    def _compute_5m_indicators(self, pair: str):
        """5분봉 기반 지표를 재계산합니다 (RSI, BB, ATR)."""
        state = self.assets[pair]
        candles = state.candles_5m
        if len(candles) < 25:
            return

        state.rsi_5m = compute_rsi(candles, period=14)
        state.bollinger = compute_bollinger(candles, period=20, num_std=2.0)
        state.atr = compute_atr(candles, period=14)

    def _check_price_change(self, pair: str):
        """1분 내 1%+ 급변 감지 시 콜백 호출."""
        state = self.assets[pair]
        if abs(state.change_1m_pct) >= config.PRICE_CHANGE_THRESHOLD:
            if self._on_price_change:
                self._on_price_change(pair, state)

    # ── 공개 조회 ─────────────────────────────────────────────

    def get_state(self, symbol: str) -> Optional[AssetState]:
        """자산 상태를 조회합니다. symbol은 "BTC", "ETH" 등."""
        pair = f"{symbol}USDT"
        return self.assets.get(pair)

    def get_all_states(self) -> Dict[str, AssetState]:
        return dict(self.assets)

    def get_volatility_class(self, symbol: str = "BTC") -> str:
        """ATR% 기반 변동성 분류를 반환합니다."""
        state = self.get_state(symbol)
        if state and state.atr:
            return state.atr.volatility
        return "normal"

    def get_5m_allocation(self, symbol: str = "BTC") -> tuple[float, float]:
        """
        변동성 기반 5분봉 vs 일반 시장 비중을 반환합니다.
        Returns: (five_min_weight, general_weight)
        """
        vol = self.get_volatility_class(symbol)
        if vol == "high":
            return 0.70, 0.30
        elif vol == "normal":
            return 0.50, 0.50
        else:
            return 0.20, 0.80

    def detect_spike(self, symbol: str) -> Optional[dict]:
        """
        1분 내 1%+ 급변을 감지합니다.
        Returns: {"symbol": ..., "change_pct": ..., "direction": ...} or None
        """
        state = self.get_state(symbol)
        if not state:
            return None
        if abs(state.change_1m_pct) >= config.PRICE_CHANGE_THRESHOLD:
            return {
                "symbol": symbol,
                "change_pct": state.change_1m_pct,
                "direction": "up" if state.change_1m_pct > 0 else "down",
                "price": state.price,
            }
        return None

    def is_black_swan(self, symbol: str = "BTC") -> bool:
        """5분 내 ±5% 급변 (블랙스완) 감지."""
        state = self.get_state(symbol)
        if state and abs(state.change_5m_pct) >= config.BLACK_SWAN_THRESHOLD:
            return True
        return False
