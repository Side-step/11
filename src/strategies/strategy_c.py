"""
전략 C: 오더북 불균형 + 지표 확인 스캘핑 (보조 전략).
대량 주문 방향으로 단기 가격 이동을 예측합니다.
전략 A, B 신호가 없을 때만 사용합니다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src import config
from src.binance_feed import BinanceFeed
from src.bot_state import BotState
from src.confluence import (
    ConfluenceScore, IndicatorSnapshot,
    build_snapshot, compute_confluence,
)
from src.polymarket_api import MarketInfo, PolymarketClient
from src.indicators.orderbook import OFIResult, compute_ofi

logger = logging.getLogger(__name__)


@dataclass
class StrategyCSignal:
    market: MarketInfo
    direction: str
    imbalance: float
    confluence: ConfluenceScore
    snapshot: IndicatorSnapshot


def evaluate_strategy_c(
    bot: BotState,
    feed: BinanceFeed,
    poly: PolymarketClient,
    markets: list[MarketInfo],
    adaptive_weights: dict = None,
    blend_ratio: float = 0.0,
    win_rate_factors: dict = None,
) -> Optional[StrategyCSignal]:
    """
    전략 C를 평가합니다.

    조건:
    - |IMB| > 0.4
    - EMA 방향 + MACD 방향 일치
    - 베팅 비율 × 0.7 (보조 전략 축소)
    """
    if not bot.can_trade():
        return None

    # HWM -35% 이하면 전략 A만 허용
    if bot.hwm_restricts_strategy():
        return None

    for market in markets:
        if market.is_xrp or market.grade in ("X", "C"):
            continue
        if not market.accepting_orders or market.seconds_delay > 0:
            continue

        # 관련 자산 판별
        asset = _detect_asset(market.question)
        if not asset:
            continue

        state = feed.get_state(asset)
        if not state:
            continue

        # Yes 토큰 오더북 확인
        for side, token_id in [
            ("Yes", market.yes_token_id),
            ("No", market.no_token_id),
        ]:
            bids, asks = poly.get_orderbook(token_id)
            if not bids or not asks:
                continue

            ofi = compute_ofi(bids, asks)
            if not ofi:
                continue

            # IMB 조건
            if abs(ofi.imbalance) <= config.IMB_THRESHOLD:
                continue

            # 방향 결정
            if ofi.imbalance > config.IMB_THRESHOLD:
                direction = "Yes" if side == "Yes" else "No"
                eval_dir = "buy"
            else:
                direction = "No" if side == "Yes" else "Yes"
                eval_dir = "buy"

            # EMA + MACD 방향 확인 (둘 다 같은 방향이어야 함)
            if state.ema and state.macd:
                ema_dir = state.ema.signal  # "buy" or "sell"
                macd_dir = state.macd.signal
                if ema_dir != macd_dir:
                    continue
                if ema_dir != eval_dir:
                    continue
            else:
                continue

            # 컨플루언스 계산
            confluence = compute_confluence(
                direction=eval_dir,
                rsi_1m=state.rsi_1m,
                rsi_5m=state.rsi_5m,
                ema=state.ema,
                vwap=state.vwap,
                bb=state.bollinger,
                macd=state.macd,
                volume=state.volume_ratio,
                ofi=ofi,
                adaptive_weights=adaptive_weights,
                blend_ratio=blend_ratio,
                win_rate_factors=win_rate_factors,
            )

            if not confluence.entry_allowed:
                continue

            snapshot = build_snapshot(
                rsi_1m=state.rsi_1m,
                rsi_5m=state.rsi_5m,
                ema=state.ema,
                vwap=state.vwap,
                bb=state.bollinger,
                macd=state.macd,
                volume=state.volume_ratio,
                ofi=ofi,
            )

            return StrategyCSignal(
                market=market,
                direction=direction,
                imbalance=ofi.imbalance,
                confluence=confluence,
                snapshot=snapshot,
            )

    return None


def _detect_asset(question: str) -> Optional[str]:
    """시장 질문에서 관련 자산을 추출합니다."""
    q = question.upper()
    for asset in config.MONITORED_ASSETS:
        if asset in q:
            return asset
    return None
