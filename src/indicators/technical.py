"""
Technical indicators calculated from OHLCV candle data.
Supports RSI, EMA, VWAP, Bollinger Bands, MACD, Volume Ratio, ATR.
All calculations operate on lists of candle dicts with keys:
  open, high, low, close, volume, timestamp
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

Candles = List[dict]


@dataclass
class RSIResult:
    value: float               # current RSI
    is_oversold: bool          # < 30
    is_overbought: bool        # > 70
    signal: str                # "buy" | "sell" | "neutral"
    strength: str              # "strong" | "normal" | "none"


@dataclass
class EMAResult:
    ema_fast: float
    ema_slow: float
    cross: str                 # "golden" | "dead" | "none"
    cross_bars_ago: int
    trend: str                 # "up" | "down" | "flat"
    signal: str
    strength: str


@dataclass
class VWAPResult:
    vwap: float
    position_pct: float        # (price - vwap) / vwap * 100
    signal: str
    strength: str


@dataclass
class BollingerResult:
    upper: float
    middle: float
    lower: float
    bandwidth: float
    position: float            # -1 = lower band, 0 = middle, +1 = upper
    squeeze: bool
    signal: str
    strength: str


@dataclass
class MACDResult:
    macd_line: float
    signal_line: float
    histogram: float
    cross: str                 # "bullish" | "bearish" | "none"
    signal: str
    strength: str


@dataclass
class VolumeResult:
    current_volume: float
    avg_volume: float
    ratio: float
    signal: str
    strength: str


@dataclass
class ATRResult:
    atr: float
    atr_pct: float             # atr / price * 100
    volatility: str            # "high" | "normal" | "low"


def compute_rsi(candles: Candles, period: int = 14) -> Optional[RSIResult]:
    """Compute RSI using Wilder's smoothing method."""
    if len(candles) < period + 1:
        return None

    closes = [c["close"] for c in candles]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period

    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    is_oversold = rsi < 30
    is_overbought = rsi > 70

    if is_oversold:
        signal = "buy"
    elif is_overbought:
        signal = "sell"
    else:
        signal = "neutral"

    # Strength: check 5m context if available (caller should combine)
    if rsi < 20 or rsi > 80:
        strength = "strong"
    elif rsi < 30 or rsi > 70:
        strength = "normal"
    else:
        strength = "none"

    return RSIResult(
        value=round(rsi, 2),
        is_oversold=is_oversold,
        is_overbought=is_overbought,
        signal=signal,
        strength=strength,
    )


def _ema(values: List[float], period: int) -> List[float]:
    """Calculate EMA series."""
    if not values:
        return []
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def compute_ema_cross(candles: Candles, fast: int = 9, slow: int = 21) -> Optional[EMAResult]:
    """Compute EMA cross (golden/dead)."""
    if len(candles) < slow + 5:
        return None

    closes = [c["close"] for c in candles]
    ema_fast_series = _ema(closes, fast)
    ema_slow_series = _ema(closes, slow)

    ema_f = ema_fast_series[-1]
    ema_s = ema_slow_series[-1]

    # Detect cross: look back for direction change
    cross = "none"
    cross_bars_ago = 0
    for i in range(len(ema_fast_series) - 2, max(0, len(ema_fast_series) - 20), -1):
        prev_diff = ema_fast_series[i] - ema_slow_series[i]
        curr_diff = ema_f - ema_s
        if prev_diff <= 0 < curr_diff:
            cross = "golden"
            cross_bars_ago = len(ema_fast_series) - 1 - i
            break
        elif prev_diff >= 0 > curr_diff:
            cross = "dead"
            cross_bars_ago = len(ema_fast_series) - 1 - i
            break

    trend = "up" if ema_f > ema_s else "down" if ema_f < ema_s else "flat"

    if cross == "golden" and cross_bars_ago <= 3:
        signal, strength = "buy", "strong"
    elif cross == "golden":
        signal, strength = "buy", "normal"
    elif cross == "dead" and cross_bars_ago <= 3:
        signal, strength = "sell", "strong"
    elif cross == "dead":
        signal, strength = "sell", "normal"
    elif trend == "up":
        signal, strength = "buy", "none"
    elif trend == "down":
        signal, strength = "sell", "none"
    else:
        signal, strength = "neutral", "none"

    return EMAResult(
        ema_fast=round(ema_f, 4),
        ema_slow=round(ema_s, 4),
        cross=cross,
        cross_bars_ago=cross_bars_ago,
        trend=trend,
        signal=signal,
        strength=strength,
    )


def compute_vwap(candles: Candles) -> Optional[VWAPResult]:
    """Compute VWAP from daily candles."""
    if not candles:
        return None

    cum_pv = 0.0
    cum_v = 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical * c["volume"]
        cum_v += c["volume"]

    if cum_v == 0:
        return None

    vwap = cum_pv / cum_v
    price = candles[-1]["close"]
    position_pct = (price - vwap) / vwap * 100 if vwap != 0 else 0

    if position_pct < -0.1:
        signal = "buy"
        strength = "strong" if position_pct < -0.5 else "normal"
    elif position_pct > 0.1:
        signal = "sell"
        strength = "strong" if position_pct > 0.5 else "normal"
    else:
        signal = "neutral"
        strength = "none"

    return VWAPResult(
        vwap=round(vwap, 4),
        position_pct=round(position_pct, 4),
        signal=signal,
        strength=strength,
    )


def compute_bollinger(candles: Candles, period: int = 20, num_std: float = 2.0) -> Optional[BollingerResult]:
    """Compute Bollinger Bands."""
    if len(candles) < period:
        return None

    closes = [c["close"] for c in candles[-period:]]
    sma = sum(closes) / period
    variance = sum((c - sma) ** 2 for c in closes) / period
    std = math.sqrt(variance)

    upper = sma + num_std * std
    lower = sma - num_std * std
    bandwidth = (upper - lower) / sma if sma != 0 else 0

    price = candles[-1]["close"]
    band_range = upper - lower
    if band_range > 0:
        position = (price - lower) / band_range * 2 - 1  # -1 to +1
    else:
        position = 0

    squeeze = bandwidth < 0.01

    if position <= -0.8:
        signal = "buy"
        strength = "strong" if position <= -1.0 else "normal"
    elif position >= 0.8:
        signal = "sell"
        strength = "strong" if position >= 1.0 else "normal"
    else:
        signal = "neutral"
        strength = "none"

    return BollingerResult(
        upper=round(upper, 4),
        middle=round(sma, 4),
        lower=round(lower, 4),
        bandwidth=round(bandwidth, 6),
        position=round(position, 4),
        squeeze=squeeze,
        signal=signal,
        strength=strength,
    )


def compute_macd(
    candles: Candles,
    fast: int = 12, slow: int = 26, signal_period: int = 9,
) -> Optional[MACDResult]:
    """Compute MACD line, signal line, histogram."""
    if len(candles) < slow + signal_period:
        return None

    closes = [c["close"] for c in candles]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)

    macd_line_series = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_series = _ema(macd_line_series, signal_period)

    macd_val = macd_line_series[-1]
    signal_val = signal_series[-1]
    histogram = macd_val - signal_val

    # Detect cross
    cross = "none"
    if len(macd_line_series) >= 2 and len(signal_series) >= 2:
        prev_diff = macd_line_series[-2] - signal_series[-2]
        curr_diff = histogram
        if prev_diff <= 0 < curr_diff:
            cross = "bullish"
        elif prev_diff >= 0 > curr_diff:
            cross = "bearish"

    if cross == "bullish" or histogram > 0:
        signal = "buy"
    elif cross == "bearish" or histogram < 0:
        signal = "sell"
    else:
        signal = "neutral"

    if cross in ("bullish", "bearish"):
        strength = "strong"
    elif abs(histogram) > 0:
        strength = "normal"
    else:
        strength = "none"

    return MACDResult(
        macd_line=round(macd_val, 6),
        signal_line=round(signal_val, 6),
        histogram=round(histogram, 6),
        cross=cross,
        signal=signal,
        strength=strength,
    )


def compute_volume_ratio(candles: Candles, lookback: int = 20) -> Optional[VolumeResult]:
    """Current 1-min volume vs 20-min average."""
    if len(candles) < lookback + 1:
        return None

    current_vol = candles[-1]["volume"]
    avg_vol = sum(c["volume"] for c in candles[-(lookback + 1):-1]) / lookback

    ratio = current_vol / avg_vol if avg_vol > 0 else 0

    price_change = candles[-1]["close"] - candles[-2]["close"] if len(candles) >= 2 else 0

    if ratio >= 3.0:
        strength = "strong"
        signal = "buy" if price_change > 0 else "sell" if price_change < 0 else "neutral"
    elif ratio >= 2.0:
        strength = "normal"
        signal = "buy" if price_change > 0 else "sell" if price_change < 0 else "neutral"
    else:
        strength = "none"
        signal = "neutral"

    return VolumeResult(
        current_volume=current_vol,
        avg_volume=round(avg_vol, 2),
        ratio=round(ratio, 4),
        signal=signal,
        strength=strength,
    )


def compute_atr(candles: Candles, period: int = 14) -> Optional[ATRResult]:
    """Compute Average True Range (EMA-based) from 5-min candles."""
    if len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    # EMA of TRs
    atr_series = _ema(trs, period)
    atr = atr_series[-1]

    price = candles[-1]["close"]
    atr_pct = (atr / price * 100) if price > 0 else 0

    if atr_pct > 0.3:
        volatility = "high"
    elif atr_pct >= 0.15:
        volatility = "normal"
    else:
        volatility = "low"

    return ATRResult(
        atr=round(atr, 6),
        atr_pct=round(atr_pct, 4),
        volatility=volatility,
    )
