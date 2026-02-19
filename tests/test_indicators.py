"""지표 계산 유닛 테스트."""
import pytest
from src.indicators.technical import (
    compute_rsi, compute_ema_cross, compute_vwap,
    compute_bollinger, compute_macd, compute_volume_ratio, compute_atr,
)
from src.indicators.orderbook import compute_ofi, OrderbookLevel


def _make_candles(closes, volume=100.0):
    """테스트용 캔들 데이터 생성."""
    candles = []
    for i, c in enumerate(closes):
        candles.append({
            "timestamp": 1000 + i * 60,
            "open": c - 0.5,
            "high": c + 1,
            "low": c - 1,
            "close": c,
            "volume": volume,
        })
    return candles


class TestRSI:
    def test_basic_rsi(self):
        # 상승 추세 -> RSI > 50
        closes = list(range(100, 130))
        candles = _make_candles(closes)
        result = compute_rsi(candles, period=14)
        assert result is not None
        assert result.value > 50

    def test_oversold(self):
        # 하락 추세 -> RSI < 30
        closes = list(range(130, 100, -1))
        candles = _make_candles(closes)
        result = compute_rsi(candles, period=14)
        assert result is not None
        assert result.value < 50

    def test_insufficient_data(self):
        candles = _make_candles([100, 101, 102])
        result = compute_rsi(candles, period=14)
        assert result is None


class TestEMA:
    def test_golden_cross(self):
        # 하락 후 상승 -> golden cross
        closes = list(range(120, 100, -1)) + list(range(100, 130))
        candles = _make_candles(closes)
        result = compute_ema_cross(candles, fast=9, slow=21)
        assert result is not None
        assert result.trend == "up"

    def test_dead_cross(self):
        # 상승 후 하락 -> dead cross
        closes = list(range(100, 130)) + list(range(130, 100, -1))
        candles = _make_candles(closes)
        result = compute_ema_cross(candles, fast=9, slow=21)
        assert result is not None
        assert result.trend == "down"


class TestVWAP:
    def test_basic_vwap(self):
        candles = _make_candles([100, 101, 102, 103, 104], volume=1000)
        result = compute_vwap(candles)
        assert result is not None
        assert result.vwap > 0

    def test_empty(self):
        result = compute_vwap([])
        assert result is None


class TestBollinger:
    def test_basic(self):
        closes = [100 + i * 0.1 for i in range(25)]
        candles = _make_candles(closes)
        result = compute_bollinger(candles, period=20)
        assert result is not None
        assert result.upper > result.middle > result.lower

    def test_insufficient(self):
        candles = _make_candles([100, 101])
        result = compute_bollinger(candles, period=20)
        assert result is None


class TestMACD:
    def test_basic(self):
        closes = [100 + i * 0.5 for i in range(40)]
        candles = _make_candles(closes)
        result = compute_macd(candles)
        assert result is not None
        assert result.histogram != 0


class TestVolumeRatio:
    def test_spike(self):
        candles = _make_candles([100] * 25, volume=100)
        # 마지막 캔들에 거래량 급증
        candles[-1]["volume"] = 500
        result = compute_volume_ratio(candles, lookback=20)
        assert result is not None
        assert result.ratio > 3.0
        assert result.strength == "strong"


class TestATR:
    def test_basic(self):
        candles = _make_candles([100 + i for i in range(20)])
        result = compute_atr(candles, period=14)
        assert result is not None
        assert result.atr > 0


class TestOFI:
    def test_buy_pressure(self):
        bids = [OrderbookLevel(0.45, 1000), OrderbookLevel(0.44, 800)]
        asks = [OrderbookLevel(0.46, 200), OrderbookLevel(0.47, 100)]
        result = compute_ofi(bids, asks)
        assert result is not None
        assert result.imbalance > 0.3
        assert result.signal == "buy"

    def test_sell_pressure(self):
        bids = [OrderbookLevel(0.45, 100), OrderbookLevel(0.44, 100)]
        asks = [OrderbookLevel(0.46, 1000), OrderbookLevel(0.47, 800)]
        result = compute_ofi(bids, asks)
        assert result is not None
        assert result.imbalance < -0.3
        assert result.signal == "sell"

    def test_balanced(self):
        bids = [OrderbookLevel(0.45, 500)]
        asks = [OrderbookLevel(0.46, 500)]
        result = compute_ofi(bids, asks)
        assert result is not None
        assert abs(result.imbalance) < 0.1
        assert result.signal == "neutral"

    def test_empty(self):
        result = compute_ofi([], [])
        assert result is None
