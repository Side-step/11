"""컨플루언스 스코어링 유닛 테스트."""
import pytest
from src.confluence import compute_confluence, build_snapshot
from src.indicators.technical import RSIResult, EMAResult, VWAPResult, MACDResult, VolumeResult
from src.indicators.orderbook import OFIResult


def _rsi(value, signal, strength):
    return RSIResult(value=value, is_oversold=value < 30, is_overbought=value > 70,
                     signal=signal, strength=strength)


def _ema(signal, strength):
    return EMAResult(ema_fast=100, ema_slow=99, cross="golden" if signal == "buy" else "dead",
                     cross_bars_ago=1, trend="up" if signal == "buy" else "down",
                     signal=signal, strength=strength)


def _volume(signal, strength, ratio=3.0):
    return VolumeResult(current_volume=300, avg_volume=100, ratio=ratio,
                        signal=signal, strength=strength)


class TestConfluenceScoring:
    def test_strong_buy_signals(self):
        score = compute_confluence(
            direction="buy",
            rsi_1m=_rsi(25, "buy", "strong"),
            ema=_ema("buy", "strong"),
            volume=_volume("buy", "strong"),
        )
        assert score.total >= 5
        assert score.entry_allowed

    def test_weak_signals_blocked(self):
        score = compute_confluence(
            direction="buy",
            rsi_1m=_rsi(45, "neutral", "none"),
        )
        assert score.total < 5
        assert not score.entry_allowed

    def test_counter_signals(self):
        score = compute_confluence(
            direction="buy",
            rsi_1m=_rsi(75, "sell", "strong"),
            ema=_ema("sell", "strong"),
        )
        assert score.total < 0

    def test_high_confluence_boost(self):
        score = compute_confluence(
            direction="buy",
            rsi_1m=_rsi(20, "buy", "strong"),
            ema=_ema("buy", "strong"),
            volume=_volume("buy", "strong"),
            macd=MACDResult(macd_line=1, signal_line=0.5, histogram=0.5,
                            cross="bullish", signal="buy", strength="strong"),
        )
        assert score.total >= 7
        assert score.boost_factor >= 1.2

    def test_adaptive_weights(self):
        adaptive = {
            "rsi": (3.0, 1.5, -2.5),
            "ema": (1.5, 0.8, -1.5),
        }
        score = compute_confluence(
            direction="buy",
            rsi_1m=_rsi(25, "buy", "strong"),
            ema=_ema("buy", "strong"),
            adaptive_weights=adaptive,
            blend_ratio=0.5,
        )
        # 적응형 가중치가 적용되면 점수가 변화함
        assert score.total > 0


class TestSnapshot:
    def test_build_snapshot(self):
        snap = build_snapshot(
            rsi_1m=_rsi(28, "buy", "strong"),
            ema=_ema("buy", "strong"),
            current_price=0.45,
        )
        assert snap.rsi_1m == 28
        assert snap.ema_cross == "golden"


class TestXRPExclusion:
    def test_xrp_keywords(self):
        from src.polymarket_api import MarketInfo
        m = MarketInfo(
            condition_id="test",
            question="Will XRP reach $1?",
        )
        assert m.is_xrp

    def test_ripple_keyword(self):
        from src.polymarket_api import MarketInfo
        m = MarketInfo(
            condition_id="test",
            question="Will Ripple win the SEC case?",
        )
        assert m.is_xrp

    def test_btc_not_xrp(self):
        from src.polymarket_api import MarketInfo
        m = MarketInfo(
            condition_id="test",
            question="Will BTC reach $100K?",
        )
        assert not m.is_xrp
