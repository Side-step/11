"""
Core bot state: balance tracking, compound betting, streak management,
HWM protection, and position lifecycle.
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src import config

logger = logging.getLogger(__name__)


class BotPhase(Enum):
    BOOTING = "booting"
    RUNNING = "running"
    COOLDOWN = "cooldown"
    HALTED = "halted"


class ExitReason(Enum):
    TAKE_PROFIT = "tp"
    STOP_LOSS = "sl"
    TIME_LIMIT = "time"
    TRAILING = "trailing"
    INDICATOR_REVERSAL = "indicator_reversal"
    EXPIRY = "expiry"
    EMERGENCY = "emergency"
    MANUAL = "manual"


@dataclass
class Position:
    market_id: str
    market_question: str
    market_type: str            # "Type A" or "Type B"
    strategy: str               # "A", "B", "C", "5min"
    direction: str              # "Yes" or "No"
    token_id: str
    entry_price: float
    size: float                 # number of shares
    bet_amount: float           # USD spent
    entry_time: float = field(default_factory=time.time)
    confluence_score: float = 0.0
    boost_factor: float = 1.0

    # Live tracking
    current_price: float = 0.0
    peak_price: float = 0.0
    trailing_active: bool = False

    # Orders
    tp_order_id: Optional[str] = None
    sl_pending: bool = False

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price

    @property
    def pnl_usd(self) -> float:
        return self.pnl_pct * self.bet_amount

    @property
    def hold_time_sec(self) -> float:
        return time.time() - self.entry_time

    @property
    def tp_price(self) -> float:
        return self.entry_price * (1 + config.TAKE_PROFIT_PCT)

    @property
    def sl_price(self) -> float:
        return self.entry_price * (1 - config.STOP_LOSS_PCT)

    def get_phase(self) -> str:
        elapsed = self.hold_time_sec
        if elapsed < config.PHASE_EARLY_END:
            return "early"
        elif elapsed < config.PHASE_MID_END:
            return "mid"
        elif elapsed < config.PHASE_LATE_END:
            return "late"
        else:
            return "closing"

    def should_reduce_tp(self) -> bool:
        return self.hold_time_sec >= config.PHASE_EARLY_END

    def should_activate_trailing(self) -> bool:
        return (
            self.hold_time_sec >= config.PHASE_EARLY_END
            and self.pnl_pct >= config.TRAILING_ACTIVATION
        )

    def trailing_stop_hit(self) -> bool:
        if not self.trailing_active:
            return False
        return self.current_price <= self.peak_price * (1 - config.TRAILING_DROP)

    def should_force_close(self) -> bool:
        return self.hold_time_sec >= config.FORCE_CLOSE_SEC

    def should_block_new_entry(self) -> bool:
        return self.hold_time_sec >= config.NO_NEW_ENTRY_SEC


class BotState:
    """Central mutable state for the scalping bot."""

    def __init__(self, initial_balance: float = 0.0):
        # Balance
        self.balance: float = initial_balance
        self.initial_balance: float = initial_balance
        self.hwm: float = initial_balance

        # Streaks
        self.consecutive_wins: int = 0
        self.consecutive_losses: int = 0

        # Compound betting
        self.current_bet_pct: float = config.BASE_BET_PCT

        # Phase
        self.phase: BotPhase = BotPhase.BOOTING
        self.cooldown_until: float = 0.0
        self.cooldown_reason: str = ""

        # Positions (최대 MAX_CONCURRENT_POSITIONS개 동시)
        self.positions: list[Position] = []

        # Daily stats
        self.daily_trades: int = 0
        self.daily_wins: int = 0
        self.daily_losses: int = 0
        self.daily_pnl: float = 0.0
        self.daily_start_balance: float = initial_balance

        # All-time
        self.total_trades: int = 0
        self.max_drawdown_pct: float = 0.0

        # Emergency
        self.restart_attempts: int = 0
        self.slippage_consecutive: int = 0
        self.orderbook_fail_count: int = 0

    # ── Compound Betting ──────────────────────────────────────

    def compute_bet_pct(self) -> float:
        """Determine betting percentage based on streaks and HWM."""
        # Check HWM drawdown cap first
        hwm_cap = self._hwm_bet_cap()

        # Streak-based ratio
        if self.consecutive_losses >= 8:
            return None  # cooldown signal
        if self.consecutive_losses > 0:
            pct = config.BASE_BET_PCT
            for threshold, ratio in sorted(config.LOSS_STREAK_BET_TABLE.items()):
                if self.consecutive_losses >= threshold and ratio is not None:
                    pct = ratio
            streak_pct = pct
        elif self.consecutive_wins > 0:
            pct = config.BASE_BET_PCT
            for threshold, ratio in sorted(config.STREAK_BET_TABLE.items()):
                if self.consecutive_wins >= threshold:
                    pct = ratio
            streak_pct = pct
        else:
            streak_pct = config.BASE_BET_PCT

        # Balance tier cap
        if self.balance >= config.TIER_FIX_RATIO:
            streak_pct = min(streak_pct, 0.10)

        # HWM cap overrides everything
        if hwm_cap is not None:
            final = min(streak_pct, hwm_cap)
        else:
            final = streak_pct

        final = min(final, config.MAX_BET_PCT)
        self.current_bet_pct = final
        return final

    def available_balance(self) -> float:
        """주문 가능한 잔고 (열린 포지션 노출 차감)."""
        return max(0.0, self.balance - self.total_exposure())

    def compute_bet_amount(self, boost: float = 1.0) -> float:
        """Return dollar amount to bet, applying compound logic."""
        pct = self.compute_bet_pct()
        if pct is None:
            return 0.0  # cooldown needed
        avail = self.available_balance()
        amount = avail * pct * boost
        # Cap boost to MAX_BET_PCT
        max_amount = avail * config.MAX_BET_PCT
        return min(amount, max_amount)

    def _hwm_bet_cap(self) -> Optional[float]:
        """Return max bet percentage based on HWM drawdown, or None."""
        if self.hwm <= 0:
            return None
        dd = (self.balance - self.hwm) / self.hwm
        for threshold, cap in config.HWM_LEVELS:
            if dd <= threshold:
                return cap  # may be None for halt
        return None

    def hwm_drawdown_pct(self) -> float:
        if self.hwm <= 0:
            return 0.0
        return (self.balance - self.hwm) / self.hwm

    def should_halt_hwm(self) -> bool:
        dd = self.hwm_drawdown_pct()
        return dd <= -0.50

    def hwm_restricts_strategy(self) -> bool:
        """True if drawdown is severe enough to restrict to strategy A only."""
        dd = self.hwm_drawdown_pct()
        return dd <= -0.35

    def is_hwm_limited(self) -> bool:
        dd = self.hwm_drawdown_pct()
        return dd <= -0.15

    def hwm_recovered(self) -> bool:
        if self.hwm <= 0:
            return True
        return self.balance >= self.hwm * config.HWM_RECOVERY_RATIO

    # ── Trade Result Processing ───────────────────────────────

    def record_win(self, pnl_usd: float):
        self.balance += pnl_usd
        self.consecutive_wins += 1
        self.consecutive_losses = 0
        self.daily_wins += 1
        self.daily_trades += 1
        self.daily_pnl += pnl_usd
        self.total_trades += 1
        if self.balance > self.hwm:
            self.hwm = self.balance
        self._update_drawdown()
        self.compute_bet_pct()
        logger.info(
            "WIN: +$%.2f | Balance: $%.2f | Streak: %d wins",
            pnl_usd, self.balance, self.consecutive_wins,
        )

    def record_loss(self, pnl_usd: float):
        self.balance += pnl_usd  # pnl_usd is negative
        self.consecutive_losses += 1
        self.consecutive_wins = 0
        self.daily_losses += 1
        self.daily_trades += 1
        self.daily_pnl += pnl_usd
        self.total_trades += 1
        self._update_drawdown()
        pct = self.compute_bet_pct()
        if pct is None:
            self.enter_cooldown("8 consecutive losses")
        logger.info(
            "LOSS: -$%.2f | Balance: $%.2f | Streak: %d losses",
            abs(pnl_usd), self.balance, self.consecutive_losses,
        )

    def _update_drawdown(self):
        dd = self.hwm_drawdown_pct()
        if dd < self.max_drawdown_pct:
            self.max_drawdown_pct = dd

    # ── Cooldown / Emergency ──────────────────────────────────

    def enter_cooldown(self, reason: str):
        self.phase = BotPhase.COOLDOWN
        self.cooldown_until = time.time() + config.EMERGENCY_COOLDOWN_SEC
        self.cooldown_reason = reason
        self.restart_attempts = 0
        logger.warning("COOLDOWN: %s until %.0f", reason, self.cooldown_until)

    def is_in_cooldown(self) -> bool:
        if self.phase != BotPhase.COOLDOWN:
            return False
        if time.time() >= self.cooldown_until:
            return False
        return True

    def try_restart(self) -> bool:
        """Attempt restart after cooldown. Returns True if successful."""
        self.restart_attempts += 1
        if self.restart_attempts > config.MAX_RESTART_ATTEMPTS:
            logger.warning("Max restart attempts exceeded, continuing retries")
        # Reset to safe state
        self.current_bet_pct = config.RESTART_INITIAL_PCT
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.phase = BotPhase.RUNNING
        self.slippage_consecutive = 0
        self.orderbook_fail_count = 0
        logger.info("RESTART: attempt %d, bet_pct=%.0f%%",
                     self.restart_attempts, self.current_bet_pct * 100)
        return True

    # ── Daily Reset ───────────────────────────────────────────

    def reset_daily(self):
        self.daily_trades = 0
        self.daily_wins = 0
        self.daily_losses = 0
        self.daily_pnl = 0.0
        self.daily_start_balance = self.balance

    # ── Helpers ───────────────────────────────────────────────

    def has_position(self) -> bool:
        """하나 이상의 포지션이 열려 있는지 확인합니다."""
        return len(self.positions) > 0

    def position_count(self) -> int:
        """현재 열린 포지션 수를 반환합니다."""
        return len(self.positions)

    def can_open_more(self) -> bool:
        """추가 포지션을 열 수 있는지 확인합니다."""
        return len(self.positions) < config.MAX_CONCURRENT_POSITIONS

    def has_market_position(self, market_id: str) -> bool:
        """해당 시장에 이미 포지션이 있는지 확인합니다."""
        return any(p.market_id == market_id for p in self.positions)

    def add_position(self, pos: Position):
        """포지션을 추가합니다."""
        self.positions.append(pos)

    def remove_position(self, pos: Position):
        """포지션을 제거합니다."""
        self.positions = [p for p in self.positions if p is not pos]

    def total_exposure(self) -> float:
        """전체 포지션의 합계 베팅 금액을 반환합니다."""
        return sum(p.bet_amount for p in self.positions)

    def can_trade(self) -> bool:
        """새로운 포지션 진입이 가능한지 확인합니다."""
        if self.phase not in (BotPhase.RUNNING,):
            return False
        if not self.can_open_more():
            return False
        if self.balance < 1.0:
            return False
        # 가용 잔고가 $1 미만이면 추가 진입 불가
        if self.available_balance() < 1.0:
            return False
        # 총 노출이 잔고의 80%를 넘으면 추가 진입 불가
        if self.total_exposure() >= self.balance * 0.80:
            return False
        return True

    @property
    def position(self) -> Optional[Position]:
        """하위 호환: 첫 번째 포지션 반환."""
        return self.positions[0] if self.positions else None

    def summary(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "hwm": round(self.hwm, 2),
            "drawdown_pct": round(self.hwm_drawdown_pct() * 100, 1),
            "bet_pct": round(self.current_bet_pct * 100, 1),
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "daily_trades": self.daily_trades,
            "daily_wins": self.daily_wins,
            "daily_losses": self.daily_losses,
            "daily_pnl": round(self.daily_pnl, 2),
            "phase": self.phase.value,
            "total_trades": self.total_trades,
            "open_positions": self.position_count(),
        }
