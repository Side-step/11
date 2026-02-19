"""
Orderbook-based indicators: Order Flow Imbalance (OFI) and Microprice.
Operates on Polymarket orderbook data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class OrderbookLevel:
    price: float
    size: float


@dataclass
class OFIResult:
    imbalance: float           # -1 to +1
    microprice: float
    best_bid: float
    best_ask: float
    spread: float
    bid_depth: float
    ask_depth: float
    signal: str                # "buy" | "sell" | "neutral"
    strength: str


def compute_ofi(
    bids: List[OrderbookLevel],
    asks: List[OrderbookLevel],
    levels: int = 5,
) -> Optional[OFIResult]:
    """
    Compute Order Flow Imbalance and Microprice from orderbook.

    IMB = (V_bid - V_ask) / (V_bid + V_ask)
    Microprice = (Best_Bid * V_ask + Best_Ask * V_bid) / (V_bid + V_ask)
    """
    if not bids or not asks:
        return None

    top_bids = bids[:levels]
    top_asks = asks[:levels]

    v_bid = sum(b.size for b in top_bids)
    v_ask = sum(a.size for a in top_asks)

    total = v_bid + v_ask
    if total == 0:
        return None

    imb = (v_bid - v_ask) / total

    best_bid = top_bids[0].price
    best_ask = top_asks[0].price
    spread = best_ask - best_bid

    microprice = (best_bid * v_ask + best_ask * v_bid) / total

    if imb > 0.4:
        signal = "buy"
        strength = "strong"
    elif imb > 0.3:
        signal = "buy"
        strength = "normal"
    elif imb < -0.4:
        signal = "sell"
        strength = "strong"
    elif imb < -0.3:
        signal = "sell"
        strength = "normal"
    else:
        signal = "neutral"
        strength = "none"

    return OFIResult(
        imbalance=round(imb, 4),
        microprice=round(microprice, 6),
        best_bid=best_bid,
        best_ask=best_ask,
        spread=round(spread, 6),
        bid_depth=round(v_bid, 2),
        ask_depth=round(v_ask, 2),
        signal=signal,
        strength=strength,
    )


def check_orderbook_sufficient(
    bids: List[OrderbookLevel],
    asks: List[OrderbookLevel],
    required_depth: float,
) -> bool:
    """Check if orderbook has sufficient depth for the bet size."""
    bid_total = sum(b.size * b.price for b in bids)
    ask_total = sum(a.size * a.price for a in asks)
    return bid_total >= required_depth and ask_total >= required_depth
