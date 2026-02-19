"""
컨플루언스 스코어링 시스템.
7개 보조 지표의 신호를 합산하여 진입/청산 점수를 계산합니다.
적응형 가중치와 승률 부스트를 통합합니다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from src import config
from src.indicators.technical import (
    RSIResult, EMAResult, VWAPResult, BollingerResult,
    MACDResult, VolumeResult,
)
from src.indicators.orderbook import OFIResult

logger = logging.getLogger(__name__)


@dataclass
class ConfluenceScore:
    total: float
    breakdown: Dict[str, float]
    direction: str              # "buy" | "sell"
    entry_allowed: bool
    boost_factor: float         # 1.0, 1.2, or 1.5


@dataclass
class IndicatorSnapshot:
    """진입 시점의 모든 지표 상태를 기록."""
    rsi_1m: Optional[float] = None
    rsi_5m: Optional[float] = None
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    ema_cross: str = "none"
    ema_cross_bars_ago: int = 0
    vwap_position: float = 0.0
    bb_position: float = 0.0
    bb_bandwidth: float = 0.0
    macd_histogram: float = 0.0
    macd_cross: str = "none"
    volume_ratio: float = 0.0
    orderbook_imb: float = 0.0
    microprice_gap: float = 0.0

    def to_dict(self) -> dict:
        return {
            "rsi_1m": self.rsi_1m,
            "rsi_5m": self.rsi_5m,
            "ema9": self.ema9,
            "ema21": self.ema21,
            "ema_cross": self.ema_cross,
            "ema_cross_bars_ago": self.ema_cross_bars_ago,
            "vwap_position": self.vwap_position,
            "bb_position": self.bb_position,
            "bb_bandwidth": self.bb_bandwidth,
            "macd_histogram": self.macd_histogram,
            "macd_cross": self.macd_cross,
            "volume_ratio": self.volume_ratio,
            "orderbook_imb": self.orderbook_imb,
            "microprice_gap": self.microprice_gap,
        }


def _score_indicator(
    signal: str,
    strength: str,
    direction: str,
    weights: tuple,
    adaptive_weight: Optional[tuple] = None,
    blend_ratio: float = 0.0,
    win_rate_factor: float = 1.0,
) -> float:
    """
    단일 지표의 점수를 계산합니다.

    Args:
        signal: 지표의 신호 ("buy" | "sell" | "neutral")
        strength: 신호 강도 ("strong" | "normal" | "none")
        direction: 평가 방향 ("buy" | "sell")
        weights: (강한_신호, 보통_신호, 역신호) 기본 가중치
        adaptive_weight: 적응형 가중치 (학습 후)
        blend_ratio: 적응형 블렌딩 비율 (0~0.8)
        win_rate_factor: 승률 기반 부스트/감점 계수
    """
    strong_w, normal_w, counter_w = weights

    if adaptive_weight and blend_ratio > 0:
        a_strong, a_normal, a_counter = adaptive_weight
        strong_w = (1 - blend_ratio) * strong_w + blend_ratio * a_strong
        normal_w = (1 - blend_ratio) * normal_w + blend_ratio * a_normal
        counter_w = (1 - blend_ratio) * counter_w + blend_ratio * a_counter

    if signal == direction:
        if strength == "strong":
            base = strong_w
        elif strength == "normal":
            base = normal_w
        else:
            base = 0.0
    elif signal == "neutral" or strength == "none":
        base = 0.0
    else:
        # 역신호
        if strength == "strong":
            base = counter_w
        elif strength == "normal":
            base = counter_w * 0.5
        else:
            base = 0.0

    return base * win_rate_factor


def compute_confluence(
    direction: str,
    rsi_1m: Optional[RSIResult] = None,
    rsi_5m: Optional[RSIResult] = None,
    ema: Optional[EMAResult] = None,
    vwap: Optional[VWAPResult] = None,
    bb: Optional[BollingerResult] = None,
    macd: Optional[MACDResult] = None,
    volume: Optional[VolumeResult] = None,
    ofi: Optional[OFIResult] = None,
    adaptive_weights: Optional[Dict[str, tuple]] = None,
    blend_ratio: float = 0.0,
    win_rate_factors: Optional[Dict[str, float]] = None,
) -> ConfluenceScore:
    """
    7개 지표의 컨플루언스 점수를 계산합니다.

    Args:
        direction: 평가할 방향 ("buy" | "sell")
        rsi_1m~ofi: 각 지표의 계산 결과
        adaptive_weights: 학습된 적응형 가중치
        blend_ratio: 적응형 블렌딩 비율
        win_rate_factors: 지표별 승률 부스트 계수

    Returns:
        ConfluenceScore
    """
    aw = adaptive_weights or {}
    wrf = win_rate_factors or {}
    base = config.INDICATOR_BASE_WEIGHTS
    breakdown = {}
    total = 0.0

    # RSI: 1분 + 5분 합산
    rsi_score = 0.0
    if rsi_1m:
        # 1분봉 + 5분봉 조합으로 강도 판단
        combined_strength = rsi_1m.strength
        if rsi_5m:
            if rsi_1m.signal == direction:
                if rsi_1m.is_oversold and rsi_5m.value < 40:
                    combined_strength = "strong"
                elif rsi_1m.is_overbought and rsi_5m.value > 60:
                    combined_strength = "strong"

        rsi_score = _score_indicator(
            rsi_1m.signal, combined_strength, direction,
            base["rsi"], aw.get("rsi"), blend_ratio,
            wrf.get("rsi", 1.0),
        )
    breakdown["rsi"] = round(rsi_score, 3)
    total += rsi_score

    # EMA Cross
    ema_score = 0.0
    if ema:
        ema_score = _score_indicator(
            ema.signal, ema.strength, direction,
            base["ema"], aw.get("ema"), blend_ratio,
            wrf.get("ema", 1.0),
        )
    breakdown["ema"] = round(ema_score, 3)
    total += ema_score

    # VWAP
    vwap_score = 0.0
    if vwap:
        vwap_score = _score_indicator(
            vwap.signal, vwap.strength, direction,
            base["vwap"], aw.get("vwap"), blend_ratio,
            wrf.get("vwap", 1.0),
        )
    breakdown["vwap"] = round(vwap_score, 3)
    total += vwap_score

    # Bollinger Bands
    bb_score = 0.0
    if bb:
        bb_score = _score_indicator(
            bb.signal, bb.strength, direction,
            base["bb"], aw.get("bb"), blend_ratio,
            wrf.get("bb", 1.0),
        )
    breakdown["bb"] = round(bb_score, 3)
    total += bb_score

    # MACD
    macd_score = 0.0
    if macd:
        macd_score = _score_indicator(
            macd.signal, macd.strength, direction,
            base["macd"], aw.get("macd"), blend_ratio,
            wrf.get("macd", 1.0),
        )
    breakdown["macd"] = round(macd_score, 3)
    total += macd_score

    # Volume
    vol_score = 0.0
    if volume:
        vol_score = _score_indicator(
            volume.signal, volume.strength, direction,
            base["volume"], aw.get("volume"), blend_ratio,
            wrf.get("volume", 1.0),
        )
    breakdown["volume"] = round(vol_score, 3)
    total += vol_score

    # OFI
    ofi_score = 0.0
    if ofi:
        ofi_score = _score_indicator(
            ofi.signal, ofi.strength, direction,
            base["ofi"], aw.get("ofi"), blend_ratio,
            wrf.get("ofi", 1.0),
        )
    breakdown["ofi"] = round(ofi_score, 3)
    total += ofi_score

    total = round(total, 3)

    # 진입 허용 및 부스트 결정
    entry_allowed = total >= config.CONFLUENCE_MIN_ENTRY
    if total >= config.CONFLUENCE_HIGHEST:
        boost = config.CONFLUENCE_BOOST_HIGHEST
    elif total >= config.CONFLUENCE_HIGH:
        boost = config.CONFLUENCE_BOOST_HIGH
    else:
        boost = 1.0

    return ConfluenceScore(
        total=total,
        breakdown=breakdown,
        direction=direction,
        entry_allowed=entry_allowed,
        boost_factor=boost,
    )


def build_snapshot(
    rsi_1m: Optional[RSIResult] = None,
    rsi_5m: Optional[RSIResult] = None,
    ema: Optional[EMAResult] = None,
    vwap: Optional[VWAPResult] = None,
    bb: Optional[BollingerResult] = None,
    macd: Optional[MACDResult] = None,
    volume: Optional[VolumeResult] = None,
    ofi: Optional[OFIResult] = None,
    current_price: float = 0.0,
) -> IndicatorSnapshot:
    """모든 지표의 현재 상태를 스냅샷으로 캡처."""
    snap = IndicatorSnapshot()
    if rsi_1m:
        snap.rsi_1m = rsi_1m.value
    if rsi_5m:
        snap.rsi_5m = rsi_5m.value
    if ema:
        snap.ema9 = ema.ema_fast
        snap.ema21 = ema.ema_slow
        snap.ema_cross = ema.cross
        snap.ema_cross_bars_ago = ema.cross_bars_ago
    if vwap:
        snap.vwap_position = vwap.position_pct
    if bb:
        snap.bb_position = bb.position
        snap.bb_bandwidth = bb.bandwidth
    if macd:
        snap.macd_histogram = macd.histogram
        snap.macd_cross = macd.cross
    if volume:
        snap.volume_ratio = volume.ratio
    if ofi:
        snap.orderbook_imb = ofi.imbalance
        if current_price > 0:
            snap.microprice_gap = round(current_price - ofi.microprice, 6)
    return snap


def evaluate_exit_signal(
    position_direction: str,
    score: ConfluenceScore,
) -> tuple[bool, str]:
    """
    포지션 보유 중 역방향 점수가 높으면 청산 신호.
    Returns: (should_exit, reason)
    """
    # 역방향 점수 체크
    reverse_dir = "sell" if position_direction == "buy" else "buy"
    if score.direction == reverse_dir:
        if score.total >= 7:
            return True, "indicator_reversal_strong"
        elif score.total >= 5:
            return True, "indicator_reversal"
    return False, ""
