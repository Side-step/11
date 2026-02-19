"""
전략 B: 뉴스/이벤트 선점 + 지표 확인 스캘핑.
크립토 뉴스 → Polymarket 반영 시간차를 이용한 선점.
뉴스 방향과 보조 지표 방향이 일치할 때만 진입.
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
from src.indicators.orderbook import compute_ofi

logger = logging.getLogger(__name__)


@dataclass
class NewsEvent:
    headline: str
    sentiment: str          # "positive" | "negative"
    tier: int               # 1, 2, or 3
    related_asset: str      # "BTC", "ETH", etc.
    timestamp: float


@dataclass
class StrategyBSignal:
    market: MarketInfo
    direction: str
    news: NewsEvent
    confluence: ConfluenceScore
    snapshot: IndicatorSnapshot
    bet_multiplier: float   # 뉴스 등급에 따른 배수


def classify_news(headline: str) -> Optional[NewsEvent]:
    """
    뉴스 헤드라인을 분류합니다.
    실제 구현에서는 외부 뉴스 피드 API를 사용합니다.
    """
    import time

    headline_lower = headline.lower()

    # XRP 관련 뉴스 무시
    for kw in config.EXCLUDED_KEYWORDS:
        if kw.lower() in headline_lower:
            return None

    # 자산 식별
    asset = "BTC"
    for symbol in config.MONITORED_ASSETS:
        if symbol.lower() in headline_lower:
            asset = symbol
            break

    # 감성 분류
    positive_words = [
        "approved", "approval", "bullish", "surge", "rally",
        "ath", "all-time high", "record", "upgrade", "adoption",
    ]
    negative_words = [
        "denied", "rejection", "hack", "hacked", "crash",
        "ban", "lawsuit", "depeg", "exploit", "breach",
    ]

    sentiment = "positive"
    for w in negative_words:
        if w in headline_lower:
            sentiment = "negative"
            break

    # 등급 분류
    tier = 3
    for kw in config.NEWS_TIER_1_KEYWORDS:
        if kw.lower() in headline_lower:
            tier = 1
            break
    if tier == 3:
        for kw in config.NEWS_TIER_2_KEYWORDS:
            if kw.lower() in headline_lower:
                tier = 2
                break

    return NewsEvent(
        headline=headline,
        sentiment=sentiment,
        tier=tier,
        related_asset=asset,
        timestamp=time.time(),
    )


def evaluate_strategy_b(
    bot: BotState,
    feed: BinanceFeed,
    poly: PolymarketClient,
    markets: list[MarketInfo],
    news_event: NewsEvent,
    adaptive_weights: dict = None,
    blend_ratio: float = 0.0,
    win_rate_factors: dict = None,
) -> Optional[StrategyBSignal]:
    """
    전략 B를 평가합니다.

    1. 뉴스 감지 → 관련 시장 식별
    2. 뉴스 방향과 지표 방향 일치 확인
    3. Polymarket 미반영 확인
    """
    if not bot.can_trade():
        return None

    state = feed.get_state(news_event.related_asset)
    if not state:
        return None

    # 뉴스 방향 → 매매 방향
    if news_event.sentiment == "positive":
        direction = "Yes"
        eval_dir = "buy"
    else:
        direction = "No"
        eval_dir = "buy"

    # 지표가 이미 반영 완료 여부 확인
    # RSI가 이미 과매수(긍정 뉴스) 또는 과매도(부정 뉴스)면 보류
    if state.rsi_1m:
        if news_event.sentiment == "positive" and state.rsi_1m.value > 70:
            logger.debug("Strategy B: RSI already overbought, news likely priced in")
            return None
        if news_event.sentiment == "negative" and state.rsi_1m.value < 30:
            logger.debug("Strategy B: RSI already oversold, news likely priced in")
            return None

    # 관련 시장 찾기
    related = _find_news_markets(markets, news_event)
    if not related:
        return None

    for market in related:
        token_id = market.yes_token_id if direction == "Yes" else market.no_token_id

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

        # 뉴스 방향과 지표 방향이 일치해야 진입
        if not confluence.entry_allowed:
            logger.debug("Strategy B: confluence %.1f < 5", confluence.total)
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

        # 뉴스 등급별 베팅 배수
        if news_event.tier == 1:
            bet_multiplier = 1.0
        elif news_event.tier == 2:
            bet_multiplier = 0.7
        else:
            bet_multiplier = 0.5

        return StrategyBSignal(
            market=market,
            direction=direction,
            news=news_event,
            confluence=confluence,
            snapshot=snapshot,
            bet_multiplier=bet_multiplier,
        )

    return None


def _find_news_markets(
    markets: list[MarketInfo],
    news: NewsEvent,
) -> list[MarketInfo]:
    """뉴스와 관련된 시장을 찾습니다."""
    related = []
    asset_lower = news.related_asset.lower()

    for m in markets:
        if m.is_xrp or m.grade in ("X", "C"):
            continue
        if not m.accepting_orders or m.seconds_delay > 0:
            continue

        q_lower = m.question.lower()
        if asset_lower in q_lower or news.related_asset in m.question:
            price = m.yes_price if news.sentiment == "positive" else m.no_price
            if config.PRICE_MIN <= price <= config.PRICE_MAX:
                related.append(m)

    related.sort(key=lambda x: (0 if x.grade == "A" else 1, -x.volume_24h))
    return related[:3]
