import os

def _int(key, default):
    v = os.getenv(key, "").strip()
    try: return int(v) if v else default
    except ValueError: return default

def _float(key, default):
    v = os.getenv(key, "").strip()
    try: return float(v) if v else default
    except ValueError: return default

def _bool(key, default):
    v = os.getenv(key, "").strip().lower()
    return v == "true" if v else default

# ── BingX ─────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "").strip()
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
BASE_URL         = "https://open-api.bingx.com"

# ── Telegram ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ── ZigZag ────────────────────────────────────────────────────────────
PIVOT_LEN = _int("PIVOT_LEN", 5)
ATR_LEN   = _int("ATR_LEN", 14)
TIMEFRAME = os.getenv("TIMEFRAME", "3m").strip() or "3m"

# ── Channel Fade ──────────────────────────────────────────────────────
SHORT_PIPS  = _float("SHORT_PIPS", 35.0)
LONG_PIPS   = _float("LONG_PIPS",  22.0)
SL_ATR_MULT = _float("SL_ATR_MULT", 1.5)
MIN_RR      = _float("MIN_RR", 1.3)

# ── ADX — CORREGIDO para estrategia FADE ──────────────────────────────
# ADX bajo = lateral = IDEAL para mean reversion
# Solo filtramos ADX MUY ALTO (tendencia fuerte rompe el canal)
ADX_LEN = _int("ADX_LEN", 14)
ADX_MAX = _float("ADX_MAX", 35.0)   # ← nuevo: máximo permitido
# ADX_MIN eliminado — ya no filtramos por ADX bajo

# ── EMAs ──────────────────────────────────────────────────────────────
EMA_FAST       = _int("EMA_FAST", 7)
EMA_MED        = _int("EMA_MED", 17)
EMA_SLOW       = _int("EMA_SLOW", 21)
EMA_CROSS_BARS = _int("EMA_CROSS_BARS", 3)

# ── Volumen ───────────────────────────────────────────────────────────
VOL_FILTER = _bool("VOL_FILTER", True)
VOL_MULT   = _float("VOL_MULT", 1.1)   # relajado: 1.1x de la media

# ── Time-stop ─────────────────────────────────────────────────────────
TIME_STOP_MINUTES = _int("TIME_STOP_MINUTES", 45)

# ── Trailing Stop + Breakeven ─────────────────────────────────────────
BREAKEVEN_ATR = _float("BREAKEVEN_ATR", 0.8)
TRAIL_ATR     = _float("TRAIL_ATR", 1.5)
TRAIL_DIST    = _float("TRAIL_DIST", 0.7)

# ── Riesgo ────────────────────────────────────────────────────────────
LEVERAGE       = _int("LEVERAGE", 10)
RISK_PCT       = _float("RISK_PCT", 1.5)
MAX_POSITIONS  = _int("MAX_POSITIONS", 3)
MAX_DAILY_LOSS = _float("MAX_DAILY_LOSS", 5.0)

# ── Scanner ───────────────────────────────────────────────────────────
# Whitelist para balance < 100 USDT:
#   SOL excluido (qty min 0.1 SOL, necesitas >9 USDT efectivos)
#   Priorizamos pares con precio < $30 → qty mínima muy baja
WHITELIST_ONLY = _bool("WHITELIST_ONLY", True)
TOP_PAIRS      = _int("TOP_PAIRS", 15)
MIN_QUOTE_VOL  = _float("MIN_QUOTE_VOL", 10_000_000)
MIN_PRICE_USDT = _float("MIN_PRICE_USDT", 0.001)

PAIR_WHITELIST = [
    # precio bajo → qty ejecutable con 32 USDT @ 10x
    "XRP-USDT",   "DOGE-USDT",  "ADA-USDT",   "MATIC-USDT",
    "DOT-USDT",   "LINK-USDT",  "ATOM-USDT",  "UNI-USDT",
    "OP-USDT",    "ARB-USDT",   "SUI-USDT",   "INJ-USDT",
    "FIL-USDT",   "AVAX-USDT",
    # añadir SOL/ETH/BNB cuando balance > 150 USDT
]

# ── Timing ────────────────────────────────────────────────────────────
CANDLE_SLEEP = _int("CANDLE_SLEEP", 60)
KLINE_LIMIT  = _int("KLINE_LIMIT", 150)
PORT         = _int("PORT", 8080)
