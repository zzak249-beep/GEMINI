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
# CHANNEL FADE V32
# PIP_SIZE se calcula dinámicamente en strategy.py
# según el precio del par (no usar variable fija)
# ==========================================
SHORT_PIPS  = _float("SHORT_PIPS", 45.0)
LONG_PIPS   = _float("LONG_PIPS",  30.0)
SL_ATR_MULT = _float("SL_ATR_MULT", 1.5)
MIN_RR      = _float("MIN_RR", 1.5)   # ← NUEVO: descarta trades con RR < 1.5

# ==========================================
# FILTROS V32
# ==========================================
ADX_MIN           = _int("ADX_MIN", 25)
ADX_LEN           = _int("ADX_LEN", 14)
EMA_FAST          = _int("EMA_FAST", 7)
EMA_MED           = _int("EMA_MED",  17)
EMA_SLOW          = _int("EMA_SLOW", 21)
VOL_FILTER        = _bool("VOL_FILTER", True)
VOL_MULT          = _float("VOL_MULT", 1.5)
TIME_STOP_MINUTES = _int("TIME_STOP_MINUTES", 45)

# ==========================================
# GESTIÓN DE RIESGO — DINERO REAL
# Empieza en LEVERAGE=5, RISK_PCT=1.0
# Sube solo cuando tengas 20+ trades positivos
# ==========================================
LEVERAGE       = _int("LEVERAGE", 5)
RISK_PCT       = _float("RISK_PCT", 1.0)
MAX_POSITIONS  = _int("MAX_POSITIONS", 3)
MAX_DAILY_LOSS = _float("MAX_DAILY_LOSS", 4.0)

# ==========================================
# SCANNER
# WHITELIST_ONLY=true → usa solo pares de PAIR_WHITELIST
# WHITELIST_ONLY=false → scan dinámico con MIN_QUOTE_VOL alto
# ==========================================
WHITELIST_ONLY  = _bool("WHITELIST_ONLY", True)
TOP_PAIRS       = _int("TOP_PAIRS", 12)
MIN_QUOTE_VOL   = _float("MIN_QUOTE_VOL", 50_000_000)  # 50M mínimo
MIN_PRICE_USDT  = _float("MIN_PRICE_USDT", 0.01)

# Pares de alta liquidez con mean-reversion fiable en 3m
# (volumen real > 50M USDT/día en BingX, spreads bajos)
PAIR_WHITELIST = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT",
    "XRP-USDT", "DOGE-USDT", "ADA-USDT", "AVAX-USDT",
    "LINK-USDT", "DOT-USDT", "LTC-USDT", "BCH-USDT",
]

# ==========================================
# TIMING
# ==========================================
CANDLE_SLEEP = _int("CANDLE_SLEEP", 60)
KLINE_LIMIT  = _int("KLINE_LIMIT",  120)
PORT         = _int("PORT", 8080)
