import os

def _int(key, default):
    return int(os.getenv(key, str(default)).strip())

def _float(key, default):
    return float(os.getenv(key, str(default)).strip())

def _bool(key, default):
    return os.getenv(key, str(default)).strip().lower() == "true"

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
TIMEFRAME = os.getenv("TIMEFRAME", "3m").strip()

# ==========================================
# ESTRATEGIA: CHANNEL FADE V32
# ==========================================
SHORT_PIPS  = _float("SHORT_PIPS", 45.0)
LONG_PIPS   = _float("LONG_PIPS",  30.0)
PIP_SIZE    = _float("PIP_SIZE",   0.001)
SL_ATR_MULT = _float("SL_ATR_MULT", 1.5)

# ==========================================
# FILTROS V32 — APEX QUANTUM SHIELD
# ==========================================
ADX_MIN    = _int("ADX_MIN", 25)
ADX_LEN    = _int("ADX_LEN", 14)
EMA_FAST   = _int("EMA_FAST", 7)
EMA_MED    = _int("EMA_MED",  17)
EMA_SLOW   = _int("EMA_SLOW", 21)
VOL_FILTER = _bool("VOL_FILTER", True)
VOL_MULT   = _float("VOL_MULT", 1.5)
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
TOP_PAIRS      = _int("TOP_PAIRS", 15)
MIN_PRICE_USDT = _float("MIN_PRICE_USDT", 0.001)

# ==========================================
# TIMING
# ==========================================
CANDLE_SLEEP = _int("CANDLE_SLEEP", 60)
KLINE_LIMIT  = _int("KLINE_LIMIT",  120)
PORT         = _int("PORT", 8080)
