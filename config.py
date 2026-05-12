import os

def _int(key, default):
    v = os.getenv(key, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default

def _float(key, default):
    v = os.getenv(key, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default

def _bool(key, default):
    v = os.getenv(key, "").strip().lower()
    return v == "true" if v else default

# ==========================================
# BINGX API
# ==========================================
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "").strip()
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
BASE_URL         = "https://open-api.bingx.com"

# ==========================================
# TELEGRAM
# ==========================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ==========================================
# ZIGZAG CORE
# ==========================================
PIVOT_LEN = _int("PIVOT_LEN", 5)
ATR_LEN   = _int("ATR_LEN", 14)
TIMEFRAME = os.getenv("TIMEFRAME", "3m").strip() or "3m"

# ==========================================
# CHANNEL FADE
# ==========================================
SHORT_PIPS  = _float("SHORT_PIPS", 45.0)
LONG_PIPS   = _float("LONG_PIPS",  30.0)
SL_ATR_MULT = _float("SL_ATR_MULT", 1.5)
MIN_RR      = _float("MIN_RR", 1.0)

# ==========================================
# FILTROS
# ==========================================
ADX_MIN           = _int("ADX_MIN", 15)
ADX_LEN           = _int("ADX_LEN", 14)
EMA_FAST          = _int("EMA_FAST", 7)
EMA_MED           = _int("EMA_MED", 17)
EMA_SLOW          = _int("EMA_SLOW", 21)
EMA_CROSS_BARS    = _int("EMA_CROSS_BARS", 3)   # legacy, ya no se usa como filtro duro
VOL_FILTER        = _bool("VOL_FILTER", True)
VOL_MULT          = _float("VOL_MULT", 1.0)      # 1.0 = volumen normal o mayor
TIME_STOP_MINUTES = _int("TIME_STOP_MINUTES", 45)

# ==========================================
# GESTIÓN DE RIESGO
# ==========================================
LEVERAGE       = _int("LEVERAGE", 10)
RISK_PCT       = _float("RISK_PCT", 1.5)
MAX_POSITIONS  = _int("MAX_POSITIONS", 3)
MAX_DAILY_LOSS = _float("MAX_DAILY_LOSS", 5.0)

# ==========================================
# SCANNER
# ==========================================
WHITELIST_ONLY = _bool("WHITELIST_ONLY", True)
TOP_PAIRS      = _int("TOP_PAIRS", 15)
MIN_QUOTE_VOL  = _float("MIN_QUOTE_VOL", 10_000_000)  # 10M USDT/día
MIN_PRICE_USDT = _float("MIN_PRICE_USDT", 0.001)

PAIR_WHITELIST = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "SUI-USDT",
    "DOT-USDT", "LTC-USDT", "BCH-USDT", "OP-USDT", "ARB-USDT",
    "MATIC-USDT", "UNI-USDT", "ATOM-USDT", "FIL-USDT", "INJ-USDT",
]

# ==========================================
# TIMING
# ==========================================
CANDLE_SLEEP = _int("CANDLE_SLEEP", 60)
KLINE_LIMIT  = _int("KLINE_LIMIT", 150)
PORT         = _int("PORT", 8080)
