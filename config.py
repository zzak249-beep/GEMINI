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

BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "").strip()
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
BASE_URL         = "https://open-api.bingx.com"
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PIVOT_LEN = _int("PIVOT_LEN", 5)
ATR_LEN   = _int("ATR_LEN", 14)
TIMEFRAME = os.getenv("TIMEFRAME", "3m").strip() or "3m"

SHORT_PIPS  = _float("SHORT_PIPS", 30.0)
LONG_PIPS   = _float("LONG_PIPS",  20.0)
SL_ATR_MULT = _float("SL_ATR_MULT", 1.5)
MIN_RR      = _float("MIN_RR", 1.3)

# ADX: solo bloquea tendencias fuertes (fade funciona con ADX bajo)
ADX_LEN = _int("ADX_LEN", 14)
ADX_MAX = _float("ADX_MAX", 35.0)

# RSI: confirma overbought/oversold para el fade
RSI_PERIOD = _int("RSI_PERIOD", 14)
RSI_OB     = _float("RSI_OB", 60.0)   # SHORT cuando RSI > 60
RSI_OS     = _float("RSI_OS", 40.0)   # LONG  cuando RSI < 40

VOL_FILTER        = _bool("VOL_FILTER", True)
VOL_MULT          = _float("VOL_MULT", 1.0)
TIME_STOP_MINUTES = _int("TIME_STOP_MINUTES", 45)

# Trailing stop + breakeven
BREAKEVEN_ATR = _float("BREAKEVEN_ATR", 0.8)
TRAIL_ATR     = _float("TRAIL_ATR", 1.5)
TRAIL_DIST    = _float("TRAIL_DIST", 0.7)

LEVERAGE       = _int("LEVERAGE", 10)
RISK_PCT       = _float("RISK_PCT", 1.5)
MAX_POSITIONS  = _int("MAX_POSITIONS", 3)
MAX_DAILY_LOSS = _float("MAX_DAILY_LOSS", 5.0)
MIN_NOTIONAL   = _float("MIN_NOTIONAL", 5.0)   # BingX min 5 USDT por orden

WHITELIST_ONLY = _bool("WHITELIST_ONLY", True)
TOP_PAIRS      = _int("TOP_PAIRS", 15)
MIN_QUOTE_VOL  = _float("MIN_QUOTE_VOL", 10_000_000)
MIN_PRICE_USDT = _float("MIN_PRICE_USDT", 0.001)
SCAN_INTERVAL_H = _int("SCAN_INTERVAL_H", 6)   # re-scan cada 6 horas

PAIR_WHITELIST = [
    "XRP-USDT", "DOGE-USDT", "ADA-USDT", "DOT-USDT",
    "LINK-USDT", "ATOM-USDT", "UNI-USDT", "OP-USDT",
    "ARB-USDT", "SUI-USDT", "INJ-USDT", "FIL-USDT",
    "AVAX-USDT", "MATIC-USDT",
]

CANDLE_SLEEP = _int("CANDLE_SLEEP", 60)
KLINE_LIMIT  = _int("KLINE_LIMIT", 150)
PORT         = _int("PORT", 8080)
