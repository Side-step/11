"""
Polymarket v8.0 — Risk Manager.
Kelly-criterion sizing with hedge pair evaluation.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import config as cfg

log = logging.getLogger("risk_manager")


@dataclass
class HedgeDecision:
    approved: bool
    primary_amount: float = 0.0
    hedge_amount: float = 0.0
    net_profit: float = 0.0
    guaranteed: float = 0.0
    reason: str = ""


@dataclass
class SingleDecision:
    approved: bool
    bet_amount: float = 0.0
    reason: str = ""


@dataclass
class StaggerImprovement:
    price_improvement: float = 0.0
    odds_profit: float = 0.0


class RiskManager:
    """
    Position sizing using Kelly criterion and hedge evaluation.
    """

    def __init__(self):
        self.open_positions: int = 0

    def evaluate_hedge(
        self,
        confidence: float,
        primary_price: float,
        hedge_price: float,
        bankroll: float,
        stagger: bool = False,
    ) -> HedgeDecision:
        """
        Evaluate a hedge bet pair.
        Returns HedgeDecision with amounts for both legs.

        For a binary market:
          - Buy YES at primary_price, BUY NO at hedge_price
          - If YES wins: payout = 1/primary_price - cost
          - If NO wins: payout = 1/hedge_price - cost
          - Guaranteed profit if primary_price + hedge_price < 1.0
        """
        # Position limit
        if self.open_positions >= cfg.MAX_OPEN_POSITIONS:
            return HedgeDecision(False, reason="max positions reached")

        # Price validation
        if primary_price <= 0.01 or primary_price >= 0.99:
            return HedgeDecision(False, reason=f"primary price {primary_price} out of range")
        if hedge_price <= 0.01 or hedge_price >= 0.99:
            return HedgeDecision(False, reason=f"hedge price {hedge_price} out of range")

        # Bankroll check
        if bankroll < cfg.MIN_BET_USD * 2:
            return HedgeDecision(False, reason=f"bankroll ${bankroll:.2f} too low")

        # Spread check (skip in stagger mode — enforced at hedge placement)
        total_cost = primary_price + hedge_price
        spread = 1.0 - total_cost
        if not stagger and spread < cfg.MIN_HEDGE_SPREAD:
            return HedgeDecision(
                False,
                reason=f"spread {spread:.3f} < min {cfg.MIN_HEDGE_SPREAD}",
            )

        # Kelly sizing
        kelly_frac = self._kelly_fraction(confidence, primary_price)
        base_amount = bankroll * kelly_frac

        # Cap to configured limits
        base_amount = max(cfg.MIN_BET_USD, min(base_amount, cfg.MAX_BET_USD))

        # Split: primary gets more weight based on confidence
        primary_ratio = 0.5 + (confidence - 0.5) * 0.4  # 0.5 ~ 0.7
        primary_amount = round(base_amount * primary_ratio, 2)
        hedge_amount = round(base_amount * (1 - primary_ratio), 2)

        # Ensure minimums
        primary_amount = max(primary_amount, cfg.MIN_BET_USD)
        hedge_amount = max(hedge_amount, cfg.MIN_BET_USD)

        # Check total doesn't exceed available bankroll
        total_bet = primary_amount + hedge_amount
        available = bankroll * 0.8  # keep 20% reserve
        if total_bet > available:
            ratio = available / total_bet
            primary_amount = round(primary_amount * ratio, 2)
            hedge_amount = round(hedge_amount * ratio, 2)

        # Calculate guaranteed profit (if spread > 0)
        # Guaranteed = min(primary_payout, hedge_payout) - total_cost
        if total_cost < 1.0:
            primary_shares = primary_amount / primary_price
            hedge_shares = hedge_amount / hedge_price
            win_primary = primary_shares - primary_amount - hedge_amount
            win_hedge = hedge_shares - primary_amount - hedge_amount
            guaranteed = min(win_primary, win_hedge)
            net_profit = max(win_primary, win_hedge)
        else:
            guaranteed = 0.0
            net_profit = (primary_amount / primary_price - primary_amount - hedge_amount) * confidence

        return HedgeDecision(
            approved=True,
            primary_amount=primary_amount,
            hedge_amount=hedge_amount,
            net_profit=round(net_profit, 4),
            guaranteed=round(guaranteed, 4),
        )

    def evaluate(
        self,
        confidence: float,
        market_price: float,
        bankroll: float,
    ) -> SingleDecision:
        """
        Evaluate a single-direction bet (no hedge).
        """
        if self.open_positions >= cfg.MAX_OPEN_POSITIONS:
            return SingleDecision(False, reason="max positions reached")

        if market_price <= 0.01 or market_price >= 0.99:
            return SingleDecision(False, reason=f"price {market_price} out of range")

        if bankroll < cfg.MIN_BET_USD:
            return SingleDecision(False, reason=f"bankroll ${bankroll:.2f} too low")

        kelly_frac = self._kelly_fraction(confidence, market_price)
        bet_amount = bankroll * kelly_frac

        bet_amount = max(cfg.MIN_BET_USD, min(bet_amount, cfg.MAX_BET_USD))

        # Don't bet more than 20% of bankroll on a single position
        bet_amount = min(bet_amount, bankroll * 0.20)

        return SingleDecision(approved=True, bet_amount=round(bet_amount, 2))

    def calc_staggered_improvement(
        self,
        primary_amount: float,
        primary_price: float,
        hedge_amount: float,
        initial_hedge_price: float,
        current_hedge_price: float,
    ) -> StaggerImprovement:
        """Calculate improvement from staggered hedge placement."""
        price_improvement = initial_hedge_price - current_hedge_price
        if initial_hedge_price > 0 and current_hedge_price > 0:
            initial_shares = hedge_amount / initial_hedge_price
            current_shares = hedge_amount / current_hedge_price
            odds_profit = (current_shares - initial_shares) * current_hedge_price
        else:
            odds_profit = 0.0

        return StaggerImprovement(
            price_improvement=round(price_improvement, 4),
            odds_profit=round(odds_profit, 4),
        )

    def _kelly_fraction(self, confidence: float, market_price: float) -> float:
        """
        Kelly criterion: f* = (p*b - q) / b
        where p = probability of winning, q = 1-p, b = net odds

        For Polymarket binary:
          b = (1/price) - 1 = (1 - price) / price
          p = confidence (our estimated probability)
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0

        p = confidence
        q = 1 - p
        b = (1 - market_price) / market_price

        if b <= 0:
            return 0.0

        kelly = (p * b - q) / b
        kelly = max(0, kelly)

        # Apply fraction and cap
        return min(kelly * cfg.KELLY_FRACTION, 0.20)
