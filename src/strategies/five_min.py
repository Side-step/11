"""
5분봉 단기 추세 추종 전략.
Binance 5분봉 캔들 패턴 + 지표를 활용하여 Polymarket 크립토 시장에 진입합니다.
빠른 복리 회전이 핵심 장점입니다.
"""
from __future__ import annotations

import logging
import time
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
from src.indicators.orderbook import compute_ofi

logger = logging.getLogger(__name__)


@dataclass
class FiveMinSignal:
    market: MarketInfo
    direction: str              # "Yes" or "No"
    asset_symbol: str
    candle_elapsed_sec: float
    price_vs_open: float        # 현재가 vs 캔들 시작가 차이(%)
    confluence: ConfluenceScore
    snapshot: IndicatorSnapshot
    hold_to_expiry: bool        # True면 만기까지 보유


def is_five_min_market(market: MarketInfo) -> bool:
    """크립토 관련 시장인지 판별합니다 (모든 크립토 마켓 대상)."""
    if not market.accepting_orders or not market.enable_order_book:
        return False
    if market.is_xrp or market.closed:
        return False
    if market.seconds_delay > 0:
        return False
    return config.detect_asset(market.question) is not None


def get_candle_elapsed(market: MarketInfo) -> float:
    """현재 5분 캔들의 경과 시간(초)을 추정합니다."""
    now = time.time()
    return now % 300


def evaluate_five_min(
    bot: BotState,
    feed: BinanceFeed,
    poly: PolymarketClient,
    markets: list[MarketInfo],
    adaptive_weights: dict = None,
    blend_ratio: float = 0.0,
    win_rate_factors: dict = None,
) -> Optional[FiveMinSignal]:
    """
    5분봉 추세 추종 전략을 평가합니다.

    진입 조건:
    1. 캔들 시작 후 2분 경과 (방향 확인)
    2. BTC/ETH 실시간 가격이 캔들 시작가 대비 방향 확인
    3. 컨플루언스 점수 ≥ 임계값
    4. 1분봉 RSI가 극단(30 이하 또는 70 이상) 아님
    5. 직전 3개 5분봉 추세 방향과 일치
    """
    if not bot.can_trade():
        return None

    # 크립토 시장 필터링
    five_min_markets = [m for m in markets if is_five_min_market(m)]
    if not five_min_markets:
        logger.debug("5min: No eligible crypto markets found")
        return None

    for market in five_min_markets:
        # 동일 시장에 이미 포지션 있으면 스킵
        if bot.has_market_position(market.condition_id):
            continue

        # 관련 자산 식별
        asset = _detect_asset_5min(market.question)
        if not asset:
            continue

        state = feed.get_state(asset)
        if not state:
            continue

        # 캔들 경과 시간 확인
        elapsed = get_candle_elapsed(market)

        # 진입 타이밍 매트릭스
        if elapsed < config.CANDLE_5M_ENTRY_START:
            continue  # 아직 관찰 단계
        if elapsed > config.CANDLE_5M_NO_ENTRY:
            continue  # 너무 늦음

        conservative = elapsed > config.CANDLE_5M_ENTRY_END

        # RSI 극단값 체크 (반전 위험)
        if state.rsi_1m:
            if state.rsi_1m.value <= 30 or state.rsi_1m.value >= 70:
                if conservative:
                    continue  # 극단 + 시간 부족 = 스킵

        # 5분봉 추세 방향 확인 (직전 3개)
        if len(state.candles_5m) >= 4:
            recent_3 = state.candles_5m[-4:-1]
            up_count = sum(1 for c in recent_3 if c["close"] > c["open"])
            trend_dir = "up" if up_count >= 2 else "down"
        else:
            trend_dir = "up"  # 데이터 부족 시 기본값

        # 현재가 vs 현재 5분 캔들 시작가
        if state.candles_5m:
            candle_open = state.candles_5m[-1]["open"]
            if candle_open > 0:
                price_vs_open = (state.price - candle_open) / candle_open
            else:
                price_vs_open = 0.0
        else:
            continue

        # 방향 결정: 추세 방향과 현재 캔들 방향이 일치해야 함
        if price_vs_open > 0 and trend_dir == "up":
            direction = "Yes"
            eval_dir = "buy"
        elif price_vs_open < 0 and trend_dir == "down":
            direction = "No"
            eval_dir = "buy"
        else:
            continue  # 방향 불일치

        token_id = (
            market.yes_token_id if direction == "Yes"
            else market.no_token_id
        )
        if not token_id:
            continue

        # 오더북 확인
        bids, asks = poly.get_orderbook(token_id)
        if not bids or not asks:
            continue

        ofi = compute_ofi(bids, asks)

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

        # 보수적 진입 구간에서는 기준 상향
        min_score = config.CONFLUENCE_HIGH if conservative else config.CONFLUENCE_MIN_ENTRY
        if confluence.total < min_score:
            logger.debug("5min: %s %s confluence %.1f < %.1f, skip",
                         asset, market.question[:30], confluence.total, min_score)
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
            current_price=state.price,
        )

        # 만기까지 보유 여부: 컨플루언스가 높으면 만기 대기
        hold_to_expiry = confluence.total >= config.CONFLUENCE_HIGH

        return FiveMinSignal(
            market=market,
            direction=direction,
            asset_symbol=asset,
            candle_elapsed_sec=elapsed,
            price_vs_open=price_vs_open,
            confluence=confluence,
            snapshot=snapshot,
            hold_to_expiry=hold_to_expiry,
        )

    return None


def _detect_asset_5min(question: str) -> Optional[str]:
    """시장 질문에서 자산을 추출합니다."""
    return config.detect_asset(question)
