"""봇 상태 + 복리 베팅 유닛 테스트."""
import pytest
from src.bot_state import BotState, BotPhase


class TestCompoundBetting:
    def test_default_bet(self):
        bot = BotState(initial_balance=200.0)
        pct = bot.compute_bet_pct()
        assert pct == 0.10
        assert bot.compute_bet_amount() == 20.0

    def test_win_streak_acceleration(self):
        bot = BotState(initial_balance=200.0)
        bot.phase = BotPhase.RUNNING

        # 3연승 -> 15%
        for _ in range(3):
            bot.record_win(2.0)
        pct = bot.compute_bet_pct()
        assert pct == 0.15

        # 5연승 -> 20%
        for _ in range(2):
            bot.record_win(3.0)
        pct = bot.compute_bet_pct()
        assert pct == 0.20

        # 7연승 -> 25% (cap)
        for _ in range(2):
            bot.record_win(4.0)
        pct = bot.compute_bet_pct()
        assert pct == 0.25

    def test_loss_streak_defense(self):
        bot = BotState(initial_balance=200.0)
        bot.phase = BotPhase.RUNNING

        # 2연패 -> 7%
        bot.record_loss(-2.0)
        bot.record_loss(-2.0)
        assert bot.compute_bet_pct() == 0.07

        # 4연패 -> 5%
        bot.record_loss(-1.5)
        bot.record_loss(-1.5)
        assert bot.compute_bet_pct() == 0.05

        # 6연패 -> 3%
        bot.record_loss(-1.0)
        bot.record_loss(-1.0)
        assert bot.compute_bet_pct() == 0.03

    def test_streak_reset_on_win(self):
        bot = BotState(initial_balance=200.0)
        bot.phase = BotPhase.RUNNING

        bot.record_loss(-2.0)
        bot.record_loss(-2.0)
        assert bot.current_bet_pct == 0.07

        bot.record_win(3.0)
        assert bot.consecutive_losses == 0
        assert bot.consecutive_wins == 1
        assert bot.compute_bet_pct() == 0.10

    def test_streak_reset_on_loss(self):
        bot = BotState(initial_balance=200.0)
        bot.phase = BotPhase.RUNNING

        for _ in range(5):
            bot.record_win(2.0)
        assert bot.compute_bet_pct() == 0.20

        bot.record_loss(-3.0)
        assert bot.consecutive_wins == 0
        assert bot.compute_bet_pct() == 0.10

    def test_8_loss_cooldown(self):
        bot = BotState(initial_balance=200.0)
        bot.phase = BotPhase.RUNNING

        for _ in range(8):
            bot.record_loss(-1.0)

        assert bot.phase == BotPhase.COOLDOWN


class TestHWMProtection:
    def test_hwm_tracking(self):
        bot = BotState(initial_balance=200.0)
        bot.record_win(100.0)  # balance 300
        assert bot.hwm == 300.0

        bot.record_loss(-50.0)  # balance 250
        assert bot.hwm == 300.0  # HWM unchanged

    def test_hwm_drawdown_cap(self):
        bot = BotState(initial_balance=400.0)
        bot.hwm = 400.0

        # -15% drawdown -> cap at 10%
        bot.balance = 340.0
        pct = bot.compute_bet_pct()
        assert pct <= 0.10

    def test_hwm_severe_drawdown(self):
        bot = BotState(initial_balance=400.0)
        bot.hwm = 400.0

        # -35% drawdown -> cap at 5%, strategy A only
        bot.balance = 260.0
        assert bot.hwm_restricts_strategy()

    def test_hwm_halt(self):
        bot = BotState(initial_balance=400.0)
        bot.hwm = 400.0
        bot.balance = 200.0  # -50%
        assert bot.should_halt_hwm()

    def test_hwm_recovery(self):
        bot = BotState(initial_balance=400.0)
        bot.hwm = 400.0
        bot.balance = 360.0  # 90% of HWM
        assert bot.hwm_recovered()


class TestBalanceTiers:
    def test_large_balance_cap(self):
        bot = BotState(initial_balance=6000.0)
        for _ in range(7):
            bot.record_win(100.0)
        pct = bot.compute_bet_pct()
        assert pct <= 0.10  # capped at 10% for $5000+


class TestDailyReset:
    def test_reset(self):
        bot = BotState(initial_balance=200.0)
        bot.daily_trades = 10
        bot.daily_wins = 7
        bot.daily_pnl = 50.0
        bot.reset_daily()
        assert bot.daily_trades == 0
        assert bot.daily_wins == 0
        assert bot.daily_pnl == 0.0
