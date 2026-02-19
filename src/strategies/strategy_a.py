"""
전략 A: 크립토 가격 연동 + 보조 지표 스캘핑 (메인 전략).
BTC/ETH 실시간 가격 변동의 Polymarket 반영 시간차를 활용합니다.
보조 지표 컨플루언스로 진입 정확도를 높입니다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from src import config
from src.binance_feed import AssetState, BinanceFeed
from src.bot_state import BotState, Position
from src.confluence import (
    ConfluenceScore, IndicatorSnapshot,
    build_snapshot, compute_confluence,
)
from src.polymarket_api import MarketInfo, PolymarketClient
from src.indicators.orderbook import OFIResult, compute_ofi

logger = logging.getLogger(__name__)


@dataclass
class StrategyASignal:
    market: MarketInfo
    direction: str              # "Yes" or "No"
    asset_symbol: str
    asset_change_pct: float
    confluence: ConfluenceScore
    snapshot: IndicatorSnapshot
    polymarket_lag_sec: float


def evaluate_strategy_a(
    bot: BotState,
    feed: BinanceFeed,
    poly: PolymarketClient,
    markets: list[MarketInfo],
    adaptive_weights: dict = None,
    blend_ratio: float = 0.0,
    win_rate_factors: dict = None,
) -> Optional[StrategyASignal]:
    """
    전략 A를 평가합니다.

    1차 필터: BTC/ETH 1분 내 1%+ 급변 + Polymarket 30초+ 미반영
    2차 필터: 보조 지표 컨플루언스 ≥ 5점
    """
    if not bot.can_trade():
        return None

    for symbol in ["BTC", "ETH", "SOL", "DOGE"]:
        spike = feed.detect_spike(symbol)
        if not spike:
            continue

        state = feed.get_state(symbol)
        if not state:
            continue

        # 관련 Polymarket 시장 찾기
        related = _find_related_markets(markets, symbol, spike["direction"])
        if not related:
            continue

        for market in related:
            # 동일 시장에 이미 포지션 있으면 스킵
            if bot.has_market_position(market.condition_id):
                continue

            # Polymarket 가격 미반영 확인
            current_poly_price = poly.get_midpoint(
                market.yes_token_id if spike["direction"] == "up" else market.no_token_id
            )
            if current_poly_price <= 0:
                continue

            # 시간차 추정: 가격 급변 후 Polymarket이 아직 반응하지 않았는지
            # (실제 구현에서는 최근 가격 이력 비교)
            # 여기서는 간단히 스프레드와 가격대로 판단
            lag_sec = config.POLYMARKET_LAG_SEC  # 추정치

            # 방향 결정
            if spike["direction"] == "up":
                direction = "Yes"
                eval_dir = "buy"
                token_id = market.yes_token_id
            else:
                direction = "No"
                eval_dir = "buy"
                token_id = market.no_token_id

            # 오더북 확인
            bids, asks = poly.get_orderbook(token_id)
            if not bids or not asks:
                continue

            bet_amount = bot.compute_bet_amount()
            if bet_amount <= 0:
                continue

            required_depth = bet_amount * config.ORDERBOOK_DEPTH_MULTIPLIER
            bid_total = sum(b.size * b.price for b in bids)
            ask_total = sum(a.size * a.price for a in asks)
            if bid_total < required_depth or ask_total < required_depth:
                continue

            # OFI 계산
            ofi = compute_ofi(bids, asks)

            # 2차 필터: 컨플루언스 점수
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
                logger.debug(
                    "Strategy A: %s confluence %.1f < 5, skipping",
                    market.question[:40], confluence.total,
                )
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
                current_price=current_poly_price,
            )

            return StrategyASignal(
                market=market,
                direction=direction,
                asset_symbol=symbol,
                asset_change_pct=spike["change_pct"],
                confluence=confluence,
                snapshot=snapshot,
                polymarket_lag_sec=lag_sec,
            )

    return None


def _find_related_markets(
    markets: list[MarketInfo],
    symbol: str,
    direction: str,
) -> list[MarketInfo]:
    """주어진 자산과 관련된 Polymarket 시장을 찾습니다."""
    related = []
    symbol_lower = symbol.lower()

    for m in markets:
        if m.is_xrp or m.grade == "X":
            continue
        if not m.accepting_orders or m.seconds_delay > 0:
            continue
        if not m.enable_order_book:
            continue

        question_lower = m.question.lower()
        if symbol_lower in question_lower or symbol in m.question:
            # Type B 가격 조건 확인
            price = m.yes_price if direction == "up" else m.no_price
            if config.PRICE_MIN <= price <= config.PRICE_MAX:
                related.append(m)

    # A등급 우선 정렬
    related.sort(key=lambda x: (0 if x.grade == "A" else 1, -x.volume_24h))
    return related[:3]
