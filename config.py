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
SHORT_PIPS  = _float("SHORT_PIPS", 35.0)    # subido de 20 → menos señales, más calidad
LONG_PIPS   = _float("LONG_PIPS",  22.0)
SL_ATR_MULT = _float("SL_ATR_MULT", 1.5)
MIN_RR      = _float("MIN_RR", 1.5)         # subido de 1.0 → ventaja matemática real

# ==========================================
# FILTROS
# ==========================================
ADX_MIN           = _int("ADX_MIN", 20)
ADX_LEN           = _int("ADX_LEN", 14)
EMA_FAST          = _int("EMA_FAST", 7)
EMA_MED           = _int("EMA_MED", 17)
EMA_SLOW          = _int("EMA_SLOW", 21)
EMA_CROSS_BARS    = _int("EMA_CROSS_BARS", 3)
VOL_FILTER        = _bool("VOL_FILTER", True)  # activado
VOL_MULT          = _float("VOL_MULT", 1.2)
TIME_STOP_MINUTES = _int("TIME_STOP_MINUTES", 45)

# ==========================================
# TRAILING STOP + BREAKEVEN
# ==========================================
# Breakeven: activa cuando precio se mueve BREAKEVEN_ATR × ATR a favor
# → mueve el SL software al precio de entrada (no pierdas nada)
BREAKEVEN_ATR = _float("BREAKEVEN_ATR", 0.8)

# Trailing: activa cuando precio se mueve TRAIL_ATR × ATR a favor
# → SL sigue al precio máximo favorable dejando TRAIL_DIST × ATR de margen
TRAIL_ATR  = _float("TRAIL_ATR", 1.5)
TRAIL_DIST = _float("TRAIL_DIST", 0.7)

# ==========================================
# GESTIÓN DE RIESGO
# ==========================================
LEVERAGE       = _int("LEVERAGE", 10)
RISK_PCT       = _float("RISK_PCT", 1.5)
MAX_POSITIONS  = _int("MAX_POSITIONS", 3)
MAX_DAILY_LOSS = _float("MAX_DAILY_LOSS", 5.0)

# ==========================================
# SCANNER
# Whitelist optimizada para balance < 100 USDT:
#   - Excluye BTC (qty mínima ~$80), BCH, LTC (qty mínima alta)
#   - Prioriza altcoins con precio < $200 y qty mínima baja
# ==========================================
WHITELIST_ONLY = _bool("WHITELIST_ONLY", True)
TOP_PAIRS      = _int("TOP_PAIRS", 15)
MIN_QUOTE_VOL  = _float("MIN_QUOTE_VOL", 15_000_000)
MIN_PRICE_USDT = _float("MIN_PRICE_USDT", 0.001)

PAIR_WHITELIST = [
    # Tier A — alta liquidez + qty mínima viable con 32 USDT
    "SOL-USDT", "XRP-USDT", "DOGE-USDT", "ADA-USDT",
    "LINK-USDT", "SUI-USDT", "AVAX-USDT", "DOT-USDT",
    # Tier B — buena liquidez en BingX
    "OP-USDT", "ARB-USDT", "MATIC-USDT", "UNI-USDT",
    "ATOM-USDT", "INJ-USDT", "FIL-USDT",
    # BTC/ETH solo cuando balance > 150 USDT — comenta si no
    # "ETH-USDT", "BNB-USDT",
]

# ==========================================
# TIMING
# ==========================================
CANDLE_SLEEP = _int("CANDLE_SLEEP", 60)
KLINE_LIMIT  = _int("KLINE_LIMIT", 150)
PORT         = _int("PORT", 8080)
