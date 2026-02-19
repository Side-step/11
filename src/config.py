"""
Configuration constants for the Polymarket Compound Scalping Bot.
All magic numbers and thresholds are defined here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Wallet & API ──────────────────────────────────────────────
MAGIC_PRIVATE_KEY = os.getenv("MAGIC_PRIVATE_KEY", "")
PROXY_WALLET_ADDRESS = os.getenv("PROXY_WALLET_ADDRESS", "")
BUILDER_KEY = os.getenv("BUILDER_KEY", "")
BUILDER_SECRET = os.getenv("BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASSPHRASE", "")
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Operation Mode ────────────────────────────────────────────
# observe | small_live | five_min_entry | full
OPERATION_MODE = os.getenv("OPERATION_MODE", "full")

# ── Chain & Contracts (Polygon Mainnet) ───────────────────────
CHAIN_ID = 137
SIGNATURE_TYPE = 1  # Proxy wallet (Magic/email login)

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEGRISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEGRISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
ZERO_BYTES32 = "0x" + "0" * 64
INDEX_SETS = [1, 2]

# ── CLOB / Gamma / Data API ──────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
WS_CLOB = "wss://ws-subscriptions-clob.polymarket.com/ws/"

# ── Binance ───────────────────────────────────────────────────
BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443"

# Assets to monitor (XRP explicitly excluded)
MONITORED_ASSETS = ["BTC", "ETH", "SOL", "DOGE"]
EXCLUDED_KEYWORDS = ["XRP", "xrp", "Ripple", "ripple", "\ub9ac\ud50c"]

# ── Compound Betting ──────────────────────────────────────────
BASE_BET_PCT = 0.10          # 10% of balance

STREAK_BET_TABLE = {
    # consecutive_wins -> bet_pct
    0: 0.10,   # default
    3: 0.15,   # momentum
    5: 0.20,   # strong acceleration
    7: 0.25,   # max cap
}
LOSS_STREAK_BET_TABLE = {
    # consecutive_losses -> bet_pct
    0: 0.10,
    2: 0.07,
    4: 0.05,
    6: 0.03,
    8: None,   # cooldown
}
MAX_BET_PCT = 0.25
COOLDOWN_MINUTES = 15
COOLDOWN_RESTART_PCT = 0.05

# ── Profit / Loss Targets ────────────────────────────────────
TAKE_PROFIT_PCT = 0.10       # +10%
STOP_LOSS_PCT = 0.05         # -5%
MAX_HOLD_SECONDS = 900       # 15 minutes
TRAILING_ACTIVATION = 0.05   # activate trailing at +5%
TRAILING_DROP = 0.03         # close if drops 3% from peak

# Timeframe management thresholds (seconds)
PHASE_EARLY_END = 420        # 0:00-7:00
PHASE_MID_END = 720          # 7:00-12:00
PHASE_LATE_END = 870         # 12:00-14:30
REDUCED_TP_PCT = 0.07        # reduced TP in mid-phase
NO_NEW_ENTRY_SEC = 840       # no new entry after 14:00
FORCE_CLOSE_SEC = 870        # force close at 14:30

# ── Market Selection (Type B) ────────────────────────────────
PRICE_MIN = 0.20
PRICE_MAX = 0.55
MIN_DAILY_VOLUME = 30_000
ORDERBOOK_DEPTH_MULTIPLIER = 3
MAX_SPREAD_RATIO = 0.50      # spread <= target_move * 50%
MIN_EXPIRY_HOURS = 48
MIN_RECENT_TRADES = 5        # in last 30 min

# Market grades
GRADE_A_VOLUME = 100_000
GRADE_A_PRICE_MIN = 0.25
GRADE_A_PRICE_MAX = 0.45

# Scan intervals (seconds)
SCAN_A_INTERVAL = 10
SCAN_B_INTERVAL = 30
FULL_RESCAN_INTERVAL = 300

# ── Strategy A: Crypto Price Lag ──────────────────────────────
PRICE_CHANGE_THRESHOLD = 0.01   # 1% in 1 minute
POLYMARKET_LAG_SEC = 30

# ── Strategy C: Orderbook Imbalance ──────────────────────────
IMB_THRESHOLD = 0.4
STRATEGY_C_BET_MULTIPLIER = 0.7
STRATEGY_C_MAX_HOLD = 600       # 10 minutes

# ── 5-Minute Candle Markets ──────────────────────────────────
CANDLE_5M_OBSERVE_SEC = 120     # observe first 2 min
CANDLE_5M_ENTRY_START = 120     # entry window 2:00
CANDLE_5M_ENTRY_END = 210       # entry window ends 3:30
CANDLE_5M_CONSERVATIVE_END = 270  # conservative entry 4:30
CANDLE_5M_NO_ENTRY = 270        # no entry after 4:30

# ── Confluence Scoring ────────────────────────────────────────
CONFLUENCE_MIN_ENTRY = 5
CONFLUENCE_HIGH = 7
CONFLUENCE_HIGHEST = 9
CONFLUENCE_BOOST_HIGH = 1.2
CONFLUENCE_BOOST_HIGHEST = 1.5

# Base weights: (strong_signal, normal_signal, counter_signal)
INDICATOR_BASE_WEIGHTS = {
    "rsi":      (2.0, 1.0, -2.0),
    "ema":      (2.0, 1.0, -2.0),
    "vwap":     (1.0, 0.5, -1.0),
    "bb":       (2.0, 1.0, -1.0),
    "macd":     (1.0, 0.5, -1.0),
    "volume":   (2.0, 1.0, -1.0),
    "ofi":      (1.0, 0.5, -1.0),
}

# ── HWM Drawdown Protection ──────────────────────────────────
HWM_LEVELS = [
    (-0.15, 0.10),   # -15% -> max 10%
    (-0.25, 0.07),   # -25% -> max 7%
    (-0.35, 0.05),   # -35% -> max 5%, strategy A only
    (-0.50, None),    # -50% -> halt
]
HWM_RECOVERY_RATIO = 0.90

# ── Balance Tiers ─────────────────────────────────────────────
TIER_SPLIT_ENTRY = 2000        # split entry above $2000
TIER_FIX_RATIO = 5000          # fix ratio at 10% above $5000

# ── Emergency Rules ───────────────────────────────────────────
API_TIMEOUT_SEC = 5
ORDERBOOK_FAIL_LIMIT = 2
BLACK_SWAN_THRESHOLD = 0.05    # +-5% in 5 min
SLIPPAGE_LIMIT_RATIO = 0.50
SLIPPAGE_CONSECUTIVE = 2
EMERGENCY_COOLDOWN_SEC = 900   # 15 min
RESTART_CHECK_INTERVAL = 900
MAX_RESTART_ATTEMPTS = 5
RESTART_BTC_STABILITY = 0.03   # 5-min change < 3%
RESTART_INITIAL_PCT = 0.05
RESTART_WINS_TO_NORMAL = 2

# ── Auto-Redeem ──────────────────────────────────────────────
REDEEM_SCAN_INTERVAL = 60      # seconds
REDEEM_MAX_RETRY = 3
REDEEM_RETRY_DELAY = 5
REDEEM_MAX_DAILY_ATTEMPTS = 1440

# ── Adaptive Learning ────────────────────────────────────────
LEARNING_RATE = 0.05
WEIGHT_MIN_STRONG = 0.5
WEIGHT_MAX_STRONG = 4.0
WEIGHT_MIN_NORMAL = 0.2
WEIGHT_MAX_NORMAL = 2.0
WEIGHT_MIN_COUNTER = -4.0
WEIGHT_MAX_COUNTER = -0.3
DECAY_FACTOR = 0.995
ROLLING_WINDOW = 200
WARMUP_TRADES = 30
BLEND_FULL_TRADES = 250
PATTERN_MIN_TRADES = 8
PATTERN_LOSS_THRESHOLD = 0.40
PATTERN_WIN_THRESHOLD = 0.65
PATTERN_REEVAL_DAYS = 7
WIN_RATE_BOOST_HIGH = 1.3
WIN_RATE_NEUTRAL = 1.0
WIN_RATE_PENALTY = 0.7
WIN_RATE_STRONG_PENALTY = 0.4
WIN_RATE_MIN_SAMPLES = 10

# ── Logging ───────────────────────────────────────────────────
TRADE_HISTORY_FILE = "data/trade_history.jsonl"
BACKUP_DIR = "data/backups"
LEARNING_STATE_FILE = "data/learning_state.json"
ADAPTIVE_WEIGHTS_FILE = "data/adaptive_weights.json"
PATTERNS_FILE = "data/patterns.json"

# ── News keywords (Strategy B) ───────────────────────────────
HIGH_IMPACT_KEYWORDS = [
    "ETF approved", "ETF denied", "ETF rejection",
    "exchange hack", "exchange hacked",
    "SEC", "CFTC", "MiCA",
    "all-time high", "ATH",
    "Fed rate", "FOMC",
    "hard fork", "upgrade",
    "depeg", "de-peg",
]

NEWS_TIER_1_KEYWORDS = [
    "ETF", "hack", "ATH", "all-time high", "record high",
]
NEWS_TIER_2_KEYWORDS = [
    "SEC", "regulation", "ban", "lawsuit",
]
