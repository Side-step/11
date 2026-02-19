"""
Polymarket v8.0 — Ensemble Prediction Engine.
Combines multiple technical indicators and candle patterns into a single
directional prediction with confidence score.

10-source ensemble:
  1. RSI 1m         6. Volume ratio
  2. RSI 5m         7. Bollinger Bands
  3. RSI 15m        8. ATR volatility
  4. EMA cross      9. Candle pattern (5m)
  5. MACD          10. Price momentum (multi-TF)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import config as cfg
from data_collector import SignalBundle
from src.confluence import compute_confluence
from src.indicators.orderbook import OFIResult

log = logging.getLogger("predictor")


class Direction(Enum):
    UP = "UP"
    DOWN = "DOWN"
    SKIP = "SKIP"


@dataclass
class Prediction:
    direction: Direction
    confidence: float   # 0.0 ~ 1.0
    score: float        # raw ensemble score (can be negative)
    details: str = ""   # human-readable breakdown


class Predictor:
    """
    10-source ensemble predictor.
    Returns UP/DOWN/SKIP with confidence.
    """

    def predict(self, signals: Optional[SignalBundle]) -> Prediction:
        """
        Predict direction from signal bundle.
        Returns Prediction with direction, confidence, and raw score.
        """
        if not signals:
            return Prediction(Direction.SKIP, 0.0, 0.0, "no signals")

        # --- Source 1-7: Confluence scoring (RSI, EMA, VWAP, BB, MACD, Volume, OFI) ---
        buy_score = compute_confluence(
            direction="buy",
            rsi_1m=signals.rsi_1m,
            rsi_5m=signals.rsi_5m,
            ema=signals.ema,
            vwap=signals.vwap,
            bb=signals.bollinger,
            macd=signals.macd,
            volume=signals.volume_ratio,
        )
        sell_score = compute_confluence(
            direction="sell",
            rsi_1m=signals.rsi_1m,
            rsi_5m=signals.rsi_5m,
            ema=signals.ema,
            vwap=signals.vwap,
            bb=signals.bollinger,
            macd=signals.macd,
            volume=signals.volume_ratio,
        )

        # --- Source 8: ATR volatility regime ---
        atr_bonus = 0.0
        if signals.atr:
            vol_class = getattr(signals.atr, "volatility", "normal")
            if vol_class == "high":
                atr_bonus = 0.3  # more conviction in high vol
            elif vol_class == "low":
                atr_bonus = -0.2  # less conviction in low vol

        # --- Source 9: 5m candle pattern ---
        candle_score = self._score_candle_pattern(signals)

        # --- Source 10: Multi-TF momentum ---
        momentum_score = self._score_momentum(signals)

        # --- Source bonus: 15m RSI confirmation ---
        rsi_15m_bonus = 0.0
        if signals.rsi_15m:
            rsi_val = signals.rsi_15m.value
            if rsi_val < 35:
                rsi_15m_bonus = 0.5   # oversold on 15m → bullish
            elif rsi_val > 65:
                rsi_15m_bonus = -0.5  # overbought on 15m → bearish

        # --- Combine ---
        raw_buy = buy_score.total + candle_score + momentum_score + rsi_15m_bonus + atr_bonus
        raw_sell = sell_score.total - candle_score - momentum_score - rsi_15m_bonus + atr_bonus

        net_score = raw_buy - raw_sell

        # Determine direction
        if net_score > 0:
            direction = Direction.UP
            dominant_score = raw_buy
        elif net_score < 0:
            direction = Direction.DOWN
            dominant_score = raw_sell
        else:
            return Prediction(Direction.SKIP, 0.0, 0.0, "neutral")

        # Confidence = sigmoid-like mapping of absolute score to 0-1
        abs_score = abs(net_score)
        confidence = min(abs_score / 10.0, 1.0)  # score of 10 = 100% confidence

        # Apply thresholds
        if confidence < cfg.SKIP_CONFIDENCE:
            return Prediction(Direction.SKIP, confidence, net_score,
                              f"confidence {confidence:.2f} < {cfg.SKIP_CONFIDENCE}")

        if confidence < cfg.MIN_CONFIDENCE:
            return Prediction(Direction.SKIP, confidence, net_score,
                              f"confidence {confidence:.2f} < {cfg.MIN_CONFIDENCE}")

        details = (
            f"buy={buy_score.total:.1f} sell={sell_score.total:.1f} "
            f"candle={candle_score:+.1f} mom={momentum_score:+.1f} "
            f"rsi15m={rsi_15m_bonus:+.1f} atr={atr_bonus:+.1f} "
            f"net={net_score:+.2f}"
        )

        return Prediction(direction, confidence, net_score, details)

    def _score_candle_pattern(self, signals: SignalBundle) -> float:
        """
        Score based on recent 5m candle patterns.
        Positive = bullish, Negative = bearish.
        """
        candles = signals.candles_5m
        if len(candles) < 4:
            return 0.0

        recent = candles[-3:]
        score = 0.0

        # Count bullish vs bearish candles
        bullish = sum(1 for c in recent if c["close"] > c["open"])
        bearish = 3 - bullish

        if bullish >= 3:
            score += 1.0   # strong bullish trend
        elif bullish >= 2:
            score += 0.5
        elif bearish >= 3:
            score -= 1.0   # strong bearish trend
        elif bearish >= 2:
            score -= 0.5

        # Check for momentum (increasing body size)
        bodies = [abs(c["close"] - c["open"]) for c in recent]
        if len(bodies) >= 2 and bodies[-1] > bodies[-2] * 1.2:
            # Momentum increasing
            if recent[-1]["close"] > recent[-1]["open"]:
                score += 0.3
            else:
                score -= 0.3

        return score

    def _score_momentum(self, signals: SignalBundle) -> float:
        """
        Multi-timeframe momentum score.
        Positive = bullish momentum, Negative = bearish.
        """
        score = 0.0

        # 1m momentum
        if abs(signals.change_1m_pct) > 0.001:
            if signals.change_1m_pct > 0:
                score += 0.3
            else:
                score -= 0.3

        # 5m momentum (stronger signal)
        if abs(signals.change_5m_pct) > 0.002:
            if signals.change_5m_pct > 0:
                score += 0.5
            else:
                score -= 0.5

        # 15m momentum (trend confirmation)
        if abs(signals.change_15m_pct) > 0.003:
            if signals.change_15m_pct > 0:
                score += 0.4
            else:
                score -= 0.4

        # All timeframes aligned = bonus
        if (signals.change_1m_pct > 0
                and signals.change_5m_pct > 0
                and signals.change_15m_pct > 0):
            score += 0.5
        elif (signals.change_1m_pct < 0
              and signals.change_5m_pct < 0
              and signals.change_15m_pct < 0):
            score -= 0.5

        return score
