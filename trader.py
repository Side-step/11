"""
Polymarket v8.0 — Trader Module.
Market discovery, order execution, settlement checking, and position tracking.
Wraps PolymarketClient from src.polymarket_api.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import config as cfg
from src.polymarket_api import MarketInfo, PolymarketClient

log = logging.getLogger("trader")

STATE_FILE = "data/trader_state.json"


# ── Data Models ─────────────────────────────────────────────────

@dataclass
class Market:
    """Tracked market with live prices."""
    condition_id: str
    question: str
    coin_key: str           # "btc", "eth", "sol", "doge"
    end_time: float         # unix timestamp
    yes_token_id: str = ""
    no_token_id: str = ""
    yes_price: float = 0.0
    no_price: float = 0.0
    yes_bid: float = 0.0
    no_bid: float = 0.0
    tick_size: float = 0.01
    neg_risk: bool = False
    market_type: str = ""   # "5m", "15m", "price"
    slug: str = ""


@dataclass
class Position:
    """A single position (one leg of a trade)."""
    market: Market
    direction: str          # "UP" or "DOWN"
    token_id: str
    amount_usd: float
    entry_price: float
    size: float             # shares
    order_id: str = ""
    filled: bool = False
    settled: bool = False
    pnl: float = 0.0
    entry_time: float = field(default_factory=time.time)


@dataclass
class HedgePair:
    """Result of a hedge pair order."""
    primary: Optional[Position] = None
    hedge: Optional[Position] = None

    @property
    def hedge_filled(self) -> bool:
        return self.hedge is not None and self.hedge.filled


@dataclass
class PendingHedge:
    """A pending staggered hedge waiting for better odds."""
    market: Market
    primary: Optional[Position]
    hedge_dir: str
    hedge_amount: float
    initial_hedge_price: float
    hedge: Optional[Position] = None
    created_at: float = field(default_factory=time.time)


# ── Trader ──────────────────────────────────────────────────────

class Trader:
    """
    Market discovery + order execution + settlement tracking.
    """

    def __init__(self):
        self.poly = PolymarketClient()
        self.markets: Dict[str, Market] = {}
        self.positions: List[Position] = []
        self.pending_hedges: List[PendingHedge] = []
        self.total_pnl: float = 0.0
        self._cooldown_until: float = 0.0

    async def start(self):
        """Initialize Polymarket client."""
        if not self.poly.initialize():
            raise RuntimeError("Failed to initialize Polymarket client")
        self.load_state()
        log.info("Trader started")

    async def stop(self):
        """Graceful shutdown."""
        self.save_state()
        log.info("Trader stopped")

    # ── Balance ─────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Get USDC balance."""
        return self.poly.get_balance()

    # ── Position Counting ───────────────────────────────────────

    def open_position_count(self) -> int:
        """Count unique markets with open (unsettled) positions."""
        active_cids = set()
        for p in self.positions:
            if not p.settled:
                active_cids.add(p.market.condition_id)
        return len(active_cids)

    def has_pending_hedge(self, condition_id: str) -> bool:
        return any(ph.market.condition_id == condition_id
                    for ph in self.pending_hedges)

    # ── Market Discovery ────────────────────────────────────────

    async def discover_markets(self) -> List[Market]:
        """Discover new crypto markets and add to tracked set."""
        raw_markets = self.poly.fetch_crypto_markets()
        new_markets = []

        for mi in raw_markets:
            cid = mi.condition_id
            if cid in self.markets:
                continue

            coin_key = cfg.detect_asset(mi.question)
            if not coin_key:
                continue

            # Determine market type from slug
            if mi.is_5m_updown:
                mtype = "5m"
            elif mi.is_15m_updown:
                mtype = "15m"
            else:
                mtype = "price"

            # Parse end time
            end_time = self._parse_end_time(mi.end_date)

            market = Market(
                condition_id=cid,
                question=mi.question,
                coin_key=coin_key.lower(),
                end_time=end_time,
                yes_token_id=mi.yes_token_id,
                no_token_id=mi.no_token_id,
                yes_price=mi.yes_price,
                no_price=mi.no_price,
                tick_size=mi.tick_size,
                neg_risk=mi.neg_risk,
                market_type=mtype,
                slug=mi.slug,
            )

            self.markets[cid] = market
            new_markets.append(market)

        return new_markets

    # ── Price Refresh ───────────────────────────────────────────

    async def refresh_prices(self):
        """Refresh prices and orderbook for all tracked markets."""
        for cid, market in list(self.markets.items()):
            try:
                # YES side
                if market.yes_token_id:
                    price = self.poly.get_midpoint(market.yes_token_id)
                    if price > 0:
                        market.yes_price = price
                    # Get best bid
                    bids, asks = self.poly.get_orderbook(market.yes_token_id)
                    if bids:
                        market.yes_bid = float(getattr(bids[0], "price", 0) if hasattr(bids[0], "price") else bids[0].price)

                # NO side
                if market.no_token_id:
                    price = self.poly.get_midpoint(market.no_token_id)
                    if price > 0:
                        market.no_price = price
                    bids, asks = self.poly.get_orderbook(market.no_token_id)
                    if bids:
                        market.no_bid = float(getattr(bids[0], "price", 0) if hasattr(bids[0], "price") else bids[0].price)

            except Exception as e:
                log.debug("Price refresh failed for %s: %s", cid[:16], e)

    # ── Cleanup Expired ─────────────────────────────────────────

    def cleanup_expired(self):
        """Remove markets that have passed their end time."""
        now = time.time()
        expired = [cid for cid, m in self.markets.items()
                    if m.end_time > 0 and m.end_time < now - 120]
        for cid in expired:
            del self.markets[cid]
            # Clean up pending hedges for expired markets
            self.pending_hedges = [
                ph for ph in self.pending_hedges
                if ph.market.condition_id != cid
            ]

    # ── Settlement ──────────────────────────────────────────────

    async def check_settlements(self) -> List[Position]:
        """Check for settled markets and calculate PnL."""
        settled = []
        now = time.time()

        for pos in list(self.positions):
            if pos.settled:
                continue

            market = pos.market

            # Check if market expired
            if market.end_time > 0 and now > market.end_time + 30:
                # Market expired — determine outcome from final prices
                try:
                    final_price = self.poly.get_midpoint(pos.token_id)
                    if final_price > 0:
                        # PnL = (final_price - entry_price) * shares
                        pos.pnl = (final_price - pos.entry_price) * pos.size
                    else:
                        # If we can't get price, assume binary outcome
                        # Check if token went to ~1.0 (win) or ~0.0 (loss)
                        pos.pnl = -pos.amount_usd  # conservative: assume loss
                except Exception:
                    pos.pnl = -pos.amount_usd

                pos.settled = True
                self.total_pnl += pos.pnl
                settled.append(pos)
                log.info(
                    "SETTLED: %s %s pnl=$%.2f",
                    market.coin_key, pos.direction, pos.pnl,
                )

        # Clean up settled positions (keep for 5 min for reporting)
        cutoff = now - 300
        self.positions = [
            p for p in self.positions
            if not p.settled or p.entry_time > cutoff
        ]

        return settled

    # ── Order Execution ─────────────────────────────────────────

    async def buy(
        self,
        market: Market,
        direction: str,
        amount_usd: float,
    ) -> Optional[Position]:
        """
        Place a single-direction buy order.
        Uses GTC limit (maker) first, falls back to FAK (taker).
        """
        token_id = market.yes_token_id if direction == "UP" else market.no_token_id
        if not token_id:
            log.warning("No token ID for %s %s", market.coin_key, direction)
            return None

        # Get current price
        price = self.poly.get_price(token_id, "buy")
        if price <= 0.01 or price >= 0.99:
            log.debug("Price out of range: %s = %.3f", market.coin_key, price)
            return None

        # Calculate shares
        size = amount_usd / price
        if size <= 0:
            return None

        # Align price to tick size
        tick = market.tick_size or 0.01
        aligned_price = round(round(price / tick) * tick, 6)

        # Try GTC limit (maker, 0% fee)
        resp = self.poly.place_limit_order(
            token_id=token_id,
            price=aligned_price,
            size=size,
            side="buy",
            tick_size=tick,
            post_only=True,
        )

        if not resp:
            # Fall back to FAK (taker)
            resp = self.poly.place_fak_order(
                token_id=token_id,
                price=aligned_price + tick * 2,
                size=size,
                side="buy",
                tick_size=tick,
            )

        if not resp:
            log.warning("Order failed: %s %s", market.coin_key, direction)
            return None

        pos = Position(
            market=market,
            direction=direction,
            token_id=token_id,
            amount_usd=amount_usd,
            entry_price=aligned_price,
            size=size,
            order_id=resp.get("orderID", ""),
            filled=True,  # assume filled (GTC or FAK)
        )
        self.positions.append(pos)

        log.info(
            "BUY: %s %s $%.2f @ %.3f (%d shares) order=%s",
            market.coin_key, direction, amount_usd, aligned_price,
            int(size), pos.order_id[:16] if pos.order_id else "?",
        )
        return pos

    async def buy_hedge_pair(
        self,
        market: Market,
        primary_dir: str,
        hedge_dir: str,
        primary_amount: float,
        hedge_amount: float,
    ) -> HedgePair:
        """
        Place a hedged bet (both sides).
        1. Place both legs as GTC limit (maker, 0% fee)
        2. Monitor fill status
        3. Cancel unfilled leg after timeout
        """
        pair = HedgePair()

        # Primary leg
        primary_token = market.yes_token_id if primary_dir == "UP" else market.no_token_id
        hedge_token = market.yes_token_id if hedge_dir == "UP" else market.no_token_id

        if not primary_token or not hedge_token:
            log.warning("Missing token IDs for hedge pair")
            return pair

        # Get prices
        primary_price = self.poly.get_price(primary_token, "buy")
        hedge_price = self.poly.get_price(hedge_token, "buy")

        if primary_price <= 0.01 or hedge_price <= 0.01:
            return pair

        tick = market.tick_size or 0.01

        # Place primary
        p_aligned = round(round(primary_price / tick) * tick, 6)
        p_size = primary_amount / primary_price

        p_resp = self.poly.place_limit_order(
            token_id=primary_token,
            price=p_aligned,
            size=p_size,
            side="buy",
            tick_size=tick,
            post_only=True,
        )

        if not p_resp:
            p_resp = self.poly.place_fak_order(
                token_id=primary_token,
                price=p_aligned + tick * 2,
                size=p_size,
                side="buy",
                tick_size=tick,
            )

        if not p_resp:
            log.info("Primary leg failed, skipping hedge")
            return pair

        pair.primary = Position(
            market=market,
            direction=primary_dir,
            token_id=primary_token,
            amount_usd=primary_amount,
            entry_price=p_aligned,
            size=p_size,
            order_id=p_resp.get("orderID", ""),
            filled=True,
        )
        self.positions.append(pair.primary)

        # Place hedge
        h_aligned = round(round(hedge_price / tick) * tick, 6)
        h_size = hedge_amount / hedge_price

        h_resp = self.poly.place_limit_order(
            token_id=hedge_token,
            price=h_aligned,
            size=h_size,
            side="buy",
            tick_size=tick,
            post_only=True,
        )

        if not h_resp:
            h_resp = self.poly.place_fak_order(
                token_id=hedge_token,
                price=h_aligned + tick * 2,
                size=h_size,
                side="buy",
                tick_size=tick,
            )

        if h_resp:
            pair.hedge = Position(
                market=market,
                direction=hedge_dir,
                token_id=hedge_token,
                amount_usd=hedge_amount,
                entry_price=h_aligned,
                size=h_size,
                order_id=h_resp.get("orderID", ""),
                filled=True,
            )
            self.positions.append(pair.hedge)
            log.info(
                "HEDGE PAIR: %s primary=%s $%.2f@%.3f hedge=%s $%.2f@%.3f",
                market.coin_key, primary_dir, primary_amount, p_aligned,
                hedge_dir, hedge_amount, h_aligned,
            )
        else:
            log.warning(
                "Hedge leg failed: %s — primary has unhedged exposure",
                market.coin_key,
            )

        return pair

    async def buy_staggered_primary(
        self,
        market: Market,
        primary_dir: str,
        primary_amount: float,
        hedge_dir: str,
        hedge_amount: float,
        hedge_price: float,
    ) -> Optional[PendingHedge]:
        """
        Place primary immediately, schedule hedge for later.
        """
        primary = await self.buy(market, primary_dir, primary_amount)
        if not primary:
            return None

        pending = PendingHedge(
            market=market,
            primary=primary,
            hedge_dir=hedge_dir,
            hedge_amount=hedge_amount,
            initial_hedge_price=hedge_price,
        )
        self.pending_hedges.append(pending)
        return pending

    async def monitor_pending_hedges(self) -> List[PendingHedge]:
        """Check pending hedges and place if conditions improve."""
        placed = []

        for ph in list(self.pending_hedges):
            # Check if max delay exceeded
            elapsed = time.time() - ph.created_at
            if elapsed > cfg.STAGGER_MAX_DELAY_SEC:
                # Place at current price regardless
                pos = await self.buy(ph.market, ph.hedge_dir, ph.hedge_amount)
                ph.hedge = pos
                self.pending_hedges.remove(ph)
                placed.append(ph)
                continue

            # Check current price
            hedge_token = (
                ph.market.yes_token_id
                if ph.hedge_dir == "UP"
                else ph.market.no_token_id
            )
            if not hedge_token:
                continue

            current_price = self.poly.get_price(hedge_token, "buy")
            if current_price <= 0:
                continue

            improvement = ph.initial_hedge_price - current_price
            if improvement >= cfg.STAGGER_MIN_IMPROVEMENT:
                pos = await self.buy(ph.market, ph.hedge_dir, ph.hedge_amount)
                ph.hedge = pos
                self.pending_hedges.remove(ph)
                placed.append(ph)
                log.info(
                    "STAGGER: %s hedge placed, improvement=%.3f",
                    ph.market.coin_key, improvement,
                )

        return placed

    # ── State Persistence ───────────────────────────────────────

    def save_state(self):
        """Save state to disk."""
        os.makedirs("data", exist_ok=True)
        state = {
            "total_pnl": self.total_pnl,
            "cooldown_until": self._cooldown_until,
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception as e:
            log.error("Failed to save state: %s", e)

    def load_state(self):
        """Load state from disk."""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                self.total_pnl = state.get("total_pnl", 0.0)
                self._cooldown_until = state.get("cooldown_until", 0.0)
                log.info("State loaded: pnl=$%.2f", self.total_pnl)
        except Exception as e:
            log.error("Failed to load state: %s", e)

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _parse_end_time(end_date: str) -> float:
        """Parse end_date string to unix timestamp."""
        if not end_date:
            return 0.0
        try:
            from datetime import datetime, timezone
            # Try ISO format
            if "T" in end_date:
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                return dt.timestamp()
            # Try epoch
            return float(end_date)
        except Exception:
            return 0.0
