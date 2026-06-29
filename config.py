import os


def _bool(k, d="false"):
    return os.getenv(k, d).strip().split("#")[0].strip().lower() in ("1", "true", "yes")

def _float(k, d):
    try:
        return float(os.getenv(k, str(d)).strip().split("#")[0].strip())
    except:
        return d

def _int(k, d):
    try:
        return int(os.getenv(k, str(d)).strip().split("#")[0].strip())
    except:
        return d

def _str(k, d=""):
    return os.getenv(k, d).strip().split("#")[0].strip()

def _list(k, d=""):
    v = _str(k, d)
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


# ── Identity
BOT_NAME = _str("BOT_NAME", "joyful-art")
MODE     = _str("MODE", "LIVE")

# ── BingX
API_KEY    = _str("BINGX_API_KEY")
SECRET_KEY = _str("BINGX_SECRET_KEY")
BASE_URL   = "https://open-api.bingx.com"

# ── Strategy direction
SHORT_ONLY = _bool("SHORT_ONLY", "true")

# ── Trading universe
TOP_N_SYMBOLS   = _int("TOP_N_SYMBOLS", 100)
MIN_VOLUME_USDT = _float("MIN_VOLUME_USDT", 5_000_000)
BLACKLIST       = _list("BLACKLIST",
    "ESPORTS,STABLEUSDT,EURUSD,SILVER,SILVERXAG,OILWTI,OILBRENT,PAXG,CUSDT,"
    "SYN,ANIME,FLOCK,GOLD,GOLDXAU,XAU,GASOLINE,PALLADIUM,NCCOPALLADIUM")

# ── Capital & risk  ← CORRECTED
CAPITAL          = _float("CAPITAL", 289.0)
LEVERAGE         = _int("LEVERAGE", 8)
RISK_PCT         = _float("RISK_PCT", 0.8)
MAX_OPEN_TRADES  = _int("MAX_OPEN_TRADES", 9)
MAX_DAILY_TRADES = _int("MAX_DAILY_TRADES", 1800)
DAILY_LOSS_PCT   = _float("DAILY_LOSS_PCT", 5.0)        # era 50% → FIX: 5%
MAX_NOTIONAL_USDT = _float("MAX_NOTIONAL_USDT", 30.0)   # era 800 → FIX: 30
MIN_NOTIONAL_USDT = _float("MIN_NOTIONAL_USDT", 12.0)
MIN_MARGIN_USDT  = _float("MIN_MARGIN_USDT", 8.0)

# ── TP / SL / Trail
SL_ATR_MULT         = _float("SL_ATR_MULT", 1.8)
BREAKEVEN_ATR_MULT  = _float("BREAKEVEN_ATR_MULT", 1.0)
TP1_ATR_MULT        = _float("TP1_ATR_MULT", 1.5)
TP2_ATR_MULT        = _float("TP2_ATR_MULT", 3.5)
TRAIL_DISTANCE_ATR          = _float("TRAIL_DISTANCE_ATR", 1.5)
TRAIL_DISTANCE_ATR_POST_TP1 = _float("TRAIL_DISTANCE_ATR_POST_TP1", 0.8)  # NEW

# ── Time controls
MAX_HOLD_MINUTES         = _int("MAX_HOLD_MINUTES", 60)
TRADE_START_UTC          = _int("TRADE_START_UTC", 7)
TRADE_END_UTC            = _int("TRADE_END_UTC", 21)
SCAN_INTERVAL            = _int("SCAN_INTERVAL", 90)
POSITION_CHECK_INTERVAL  = _int("POSITION_CHECK_INTERVAL", 30)

# ── Timeframes
TIMEFRAME      = _str("TIMEFRAME", "3m")
HTF_TIMEFRAME  = _str("HTF_TIMEFRAME", "15m")
HTF2_TIMEFRAME = _str("HTF2_TIMEFRAME", "1h")
HTF5_TIMEFRAME = _str("HTF5_TIMEFRAME", "4h")

# ── Indicator periods
ATR_LEN    = _int("ATR_LEN", 10)
ADX_LEN    = _int("ADX_LEN", 14)
ADX_TREND  = _float("ADX_TREND", 25.0)
ADX_LATERAL = _float("ADX_LATERAL", 20.0)
LATERAL_ADX_MAX = _float("LATERAL_ADX_MAX", 25.0)

# ── Scoring thresholds
MIN_SCORE  = _int("MIN_SCORE", 55)
FUEL_SCORE = _int("FUEL_SCORE", 62)
SUP_SCORE  = _int("SUP_SCORE", 78)
COUNTER_TREND_PENALTY = _float("COUNTER_TREND_PENALTY", 12.0)

# ── Entry filters
EMA9_RALLY_ENABLED   = _bool("EMA9_RALLY_ENABLED", "true")
EMA9_NEAR_PCT        = _float("EMA9_NEAR_PCT", 1.0)
EMA9_VOL_HIGH_MULT   = _float("EMA9_VOL_HIGH_MULT", 1.3)
IBS_PULLBACK_ENABLED = _bool("IBS_PULLBACK_ENABLED", "true")
BB_SHORT_ENABLED     = _bool("BB_SHORT_ENABLED", "true")
BB_SHORT_VETO_LONG   = _bool("BB_SHORT_VETO_LONG", "true")
EMA9_VWAP_ENABLED    = _bool("EMA9_VWAP_ENABLED", "true")
EMA9_VWAP_VETO_LONG  = _bool("EMA9_VWAP_VETO_LONG", "true")
EMA9_VWAP_BOOST      = _float("EMA9_VWAP_BOOST", 9.0)
EMA55_BOOST_ENABLED  = _bool("EMA55_BOOST_ENABLED", "true")
RSI15M_FILTER_ENABLED = _bool("RSI15M_FILTER_ENABLED", "true")
RSI15M_SHORT_MAX      = _float("RSI15M_SHORT_MAX", 60.0)
RSI15M_REQUIRED       = _bool("RSI15M_REQUIRED", "false")
SLOPE_FILTER_ENABLED  = _bool("SLOPE_FILTER_ENABLED", "true")
HTF_MIN_ALIGNED       = _int("HTF_MIN_ALIGNED", 1)

# ── Exit rules
EMA_EXIT_ENABLED     = _bool("EMA_EXIT_ENABLED", "true")
EMA_EXIT_PERIOD      = _int("EMA_EXIT_PERIOD", 9)
EMA_EXIT_MIN_HOLD_MIN = _int("EMA_EXIT_MIN_HOLD_MIN", 3)
CANDLE_TURN_ENABLED  = _bool("CANDLE_TURN_ENABLED", "true")

# ── Optional modules
BTC_REGIME_ENABLED  = _bool("BTC_REGIME_ENABLED", "true")
MS_ENABLED          = _bool("MS_ENABLED", "true")
MS_LEN              = _int("MS_LEN", 10)
BTC_CORR_ENABLED    = _bool("BTC_CORR_ENABLED", "true")
BTC_CORR_THRESHOLD  = _float("BTC_CORR_THRESHOLD", 0.5)
BTC_CORR_MAX_SAME   = _int("BTC_CORR_MAX_SAME", 3)
BTC_CORR_WINDOW_SEC = _int("BTC_CORR_WINDOW_SEC", 1800)
OI_FILTER_ENABLED   = _bool("OI_FILTER_ENABLED", "true")
OI_CASCADE_ENABLED  = _bool("OI_CASCADE_ENABLED", "true")
FR_REGIME_ENABLED   = _bool("FR_REGIME_ENABLED", "true")
HARVEST_ENABLED     = _bool("HARVEST_ENABLED", "true")
HARVEST_FR_THR      = _float("HARVEST_FR_THR", 0.0010)
FR_EXTREME_THR      = _float("FR_EXTREME_THR", 0.0005)
VOL_REGIME_ENABLED  = _bool("VOL_REGIME_ENABLED", "true")
CB_ENABLED          = _bool("CB_ENABLED", "false")
CB_ATR_MULT         = _float("CB_ATR_MULT", 4.0)
CB_BARS             = _int("CB_BARS", 5)
CB_COOLDOWN_SECS    = _int("CB_COOLDOWN_SECS", 1800)
COMPLEMENT_MODE     = _str("COMPLEMENT_MODE", "DISABLED")
RECONCILE_ON_STARTUP = _bool("RECONCILE_ON_STARTUP", "false")

# ── Kelly / sizing
KELLY_WIN_RATE  = _float("KELLY_WIN_RATE", 0.55)
KELLY_RR        = _float("KELLY_RR", 1.5)
KELLY_FRACTION  = _float("KELLY_FRACTION", 0.15)

# ── Copy / multi-bot
COPY_MIN_SCORE     = _int("COPY_MIN_SCORE", 62)
COPY_SIZE_MULT     = _float("COPY_SIZE_MULT", 0.4)
EXCLUSIVE_TOP_N    = _int("EXCLUSIVE_TOP_N", 30)
COPY_MAX_ADVERSE_PCT = _float("COPY_MAX_ADVERSE_PCT", -0.3)
HEDGE_LOSS_COUNT   = _int("HEDGE_LOSS_COUNT", 3)
MASTER_URL         = _str("MASTER_URL", "")

# ── Misc
LIMIT_ORDERS_ENABLED = _bool("LIMIT_ORDERS_ENABLED", "true")
MIN_TIER             = _str("MIN_TIER", "STD")
REQUIRE_TL_BREAK     = _bool("REQUIRE_TL_BREAK", "false")
MAX_SAME_DIRECTION   = _int("MAX_SAME_DIRECTION", 2)
MAX_ADAPTIVE_OFFSET  = _int("MAX_ADAPTIVE_OFFSET", 6)
CORRELATION_WINDOW_SEC = _int("CORRELATION_WINDOW_SEC", 900)
STATE_FILE = _str("STATE_FILE", "/tmp/bot_state.json")
PORT       = _int("PORT", 8080)
