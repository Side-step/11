"""
Polymarket v8.0 — Root configuration.
Re-exports all settings from src.config and adds v8.0-specific parameters.
"""
from src.config import *  # noqa: F401,F403 — re-export everything

# ── v8.0 Specific ───────────────────────────────────────────────

LOG_FILE = "bot.log"

# Coins to trade: key → Binance pair
COINS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "doge": "DOGEUSDT",
}

# ── Position / Sizing ───────────────────────────────────────────
MAX_OPEN_POSITIONS = 4
KELLY_FRACTION = 0.25           # Kelly criterion fraction
MIN_BET_USD = 1.0
MAX_BET_USD = 50.0              # max single bet

# ── Timing ──────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 15          # main loop interval
STAGGER_POLL_SEC = 5            # faster polling when hedges pending
DATA_STALE_SEC = 120            # data considered stale after this
MIN_TIME_TO_EXPIRY = 60         # min seconds before market expires

# ── Hedge / Stagger ────────────────────────────────────────────
HEDGE_ENABLED = True
STAGGER_ENABLED = True
MIN_HEDGE_SPREAD = 0.02         # min spread for hedge to be worthwhile
STAGGER_MIN_TIME_BEFORE_EXPIRY = 120  # min time to attempt staggered hedge
STAGGER_MAX_DELAY_SEC = 60      # max delay waiting for better hedge odds
STAGGER_MIN_IMPROVEMENT = 0.005  # min price improvement to place hedge

# ── Loss Management ─────────────────────────────────────────────
LOSS_COOLDOWN_SEC = 1800        # 30 min cooldown
MAX_DAILY_LOSS_PCT = 0.15       # 15% max daily loss

# ── Prediction Thresholds ───────────────────────────────────────
MIN_CONFIDENCE = 0.55           # min prediction confidence to trade
STRONG_CONFIDENCE = 0.70        # strong confidence threshold
SKIP_CONFIDENCE = 0.50          # below this → skip

# ── Order Execution ─────────────────────────────────────────────
FILL_TIMEOUT_SEC = 30           # max wait for fill
FILL_POLL_SEC = 2               # poll interval for fill status
MAKER_FEE_PCT = 0.0             # maker fee (GTC limit)
TAKER_FEE_PCT = 0.01            # taker fee (FAK)
