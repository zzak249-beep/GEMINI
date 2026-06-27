"""
QF×JP Bot v7.7 — Config JOYFUL-ART (SHORT-only mode)
═══════════════════════════════════════════════════════════════════════════════
MODIFICACIONES SHORT-ONLY:
  ✅ SHORT_ONLY: bloquea todos los LONG
  ✅ LATERAL_ADX_MAX: solo opera en mercado lateral (ADX < umbral)
  ✅ IBS_PULLBACK_*: filtro IBS Pullback SHORT
  ✅ BB_SHORT_*: filtro BB Short (close > upper_BB × 1.01)
═══════════════════════════════════════════════════════════════════════════════
"""
import os
from dotenv import load_dotenv
load_dotenv()

def _bool(k, d): return os.getenv(k, str(d)).strip().lower() in ("true","1","yes")
def _float(k, d):
    try: return float(os.getenv(k, str(d)).strip().split()[0])
    except: return d
def _int(k, d):
    try: return int(os.getenv(k, str(d)).strip().split()[0])
    except: return d
def _list(k, d):
    r = os.getenv(k, d).strip()
    return [x.strip() for x in r.split(",") if x.strip()] if r else []

BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "").strip()
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
BINGX_BASE_URL   = "https://open-api.bingx.com"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MODE = os.getenv("MODE", "SIGNAL").upper()

CAPITAL          = _float("CAPITAL", 500.0)
RISK_PCT         = _float("RISK_PCT", 0.5)
LEVERAGE         = _int("LEVERAGE", 5)
MAX_OPEN_TRADES  = _int("MAX_OPEN_TRADES", 3)
MAX_DAILY_TRADES = _int("MAX_DAILY_TRADES", 10)

MIN_SCORE  = _float("MIN_SCORE",  58.0)
FUEL_SCORE = _float("FUEL_SCORE", 65.0)
SUP_SCORE  = _float("SUP_SCORE",  80.0)
MIN_TIER   = os.getenv("MIN_TIER", "FUEL").upper()

REQUIRE_TL_BREAK = _bool("REQUIRE_TL_BREAK", True)
HTF_MIN_ALIGNED  = _int("HTF_MIN_ALIGNED", 2)

SCAN_INTERVAL   = _int("SCAN_INTERVAL", 60)
TOP_N_SYMBOLS   = _int("TOP_N_SYMBOLS", 0)
BLACKLIST       = set(_list("BLACKLIST", "ESPORTS,STABLE,EURUSD,SILVER,PAXG,CUSDT,SYN,FONK,FOLK,NCS"))
MIN_VOLUME_USDT = _float("MIN_VOLUME_USDT", 5_000_000.0)

TIMEFRAME      = os.getenv("TIMEFRAME",      "3m")
HTF_TIMEFRAME  = os.getenv("HTF_TIMEFRAME",  "15m")
HTF2_TIMEFRAME = os.getenv("HTF2_TIMEFRAME", "1h")
HTF5_TIMEFRAME = os.getenv("HTF5_TIMEFRAME", "4h")

ATR_LEN      = _int("ATR_LEN",       10)
SL_ATR_MULT  = _float("SL_ATR_MULT",  2.0)
TP1_ATR_MULT = _float("TP1_ATR_MULT", 2.0)
TP2_ATR_MULT = _float("TP2_ATR_MULT", 4.0)

ADX_LEN     = _int("ADX_LEN", 14)
ADX_TREND   = _float("ADX_TREND",   25.0)
ADX_LATERAL = _float("ADX_LATERAL", 20.0)

KELLY_WIN_RATE = _float("KELLY_WIN_RATE", 0.55)
KELLY_RR       = _float("KELLY_RR",       1.5)
KELLY_FRACTION = _float("KELLY_FRACTION", 0.15)

CB_ENABLED  = _bool("CB_ENABLED",   False)
CB_ATR_MULT = _float("CB_ATR_MULT", 3.0)
CB_BARS     = _int("CB_BARS",       10)

POSITION_CHECK_INTERVAL = _int("POSITION_CHECK_INTERVAL", 30)

BREAKEVEN_ATR_MULT = _float("BREAKEVEN_ATR_MULT", 1.0)
TRAIL_DISTANCE_ATR = _float("TRAIL_DISTANCE_ATR", 1.5)

MAX_HOLD_MINUTES           = _int("MAX_HOLD_MINUTES", 60)
TIME_STOP_MIN_PROGRESS_ATR = _float("TIME_STOP_MIN_PROGRESS_ATR", 0.5)

CORRELATION_WINDOW_SEC = _int("CORRELATION_WINDOW_SEC", 900)
MAX_SAME_DIRECTION     = _int("MAX_SAME_DIRECTION", 2)

DAILY_LOSS_PCT = _float("DAILY_LOSS_PCT", 1.5)

MAX_NOTIONAL_USDT   = _float("MAX_NOTIONAL_USDT",   200.0)
MIN_NOTIONAL_USDT   = _float("MIN_NOTIONAL_USDT",     3.0)
FIXED_NOTIONAL_USDT = _float("FIXED_NOTIONAL_USDT",   0.0)
MIN_MARGIN_USDT     = _float("MIN_MARGIN_USDT",        1.0)

RECONCILE_ON_STARTUP = _bool("RECONCILE_ON_STARTUP", False)
EMA_EXIT_ENABLED     = _bool("EMA_EXIT_ENABLED", False)

TRADE_START_UTC = _int("TRADE_START_UTC", 0)
TRADE_END_UTC   = _int("TRADE_END_UTC",   24)

FR_EXTREME_THR = _float("FR_EXTREME_THR", 0.0005)

OI_FILTER_ENABLED = _bool("OI_FILTER_ENABLED", True)

LIMIT_ORDERS_ENABLED = _bool("LIMIT_ORDERS_ENABLED", True)
LIMIT_TIMEOUT_SECS   = _int("LIMIT_TIMEOUT_SECS", 25)

FR_REGIME_ENABLED = _bool("FR_REGIME_ENABLED", True)
HARVEST_ENABLED   = _bool("HARVEST_ENABLED",   True)
HARVEST_FR_THR    = _float("HARVEST_FR_THR",   0.0010)

VOL_REGIME_ENABLED = _bool("VOL_REGIME_ENABLED", True)

CANDLE_TURN_ENABLED       = _bool("CANDLE_TURN_ENABLED", False)
CANDLE_TURN_BOOST         = _float("CANDLE_TURN_BOOST", 3.0)
CANDLE_TURN_TOLERANCE_MIN = _int("CANDLE_TURN_TOLERANCE_MIN", 1)

SLOPE_FILTER_ENABLED = _bool("SLOPE_FILTER_ENABLED", True)

BTC_CORR_ENABLED    = _bool("BTC_CORR_ENABLED",    True)
BTC_CORR_THRESHOLD  = _float("BTC_CORR_THRESHOLD", 0.5)
BTC_CORR_MAX_SAME   = _int("BTC_CORR_MAX_SAME",    3)
BTC_CORR_WINDOW_SEC = _int("BTC_CORR_WINDOW_SEC",  1800)

WS_ENABLED        = _bool("WS_ENABLED", False)
EXPLOSION_ENABLED = _bool("EXPLOSION_ENABLED", True)

PORT = _int("PORT", 8080)

CVD_ROLL_WINDOW = _int("CVD_ROLL_WINDOW", 60)
EQL_LEN         = _int("EQL_LEN", 20)
EQL_TOL         = _float("EQL_TOL", 0.15)
OBP2_DIST       = _float("OBP2_DIST", 1.5)
PRE_SCORE       = _float("PRE_SCORE", 45.0)

KOTE_DIP_PCT        = _float("KOTE_DIP_PCT", 20.0)
KOTE_RSI_OVERSOLD   = _float("KOTE_RSI_OVERSOLD", 24.0)
KOTE_USE_BB_FILTER  = _bool("KOTE_USE_BB_FILTER", True)
KOTE_USE_RSI_FILTER = _bool("KOTE_USE_RSI_FILTER", True)
KOTE_RSI_LEN        = _int("KOTE_RSI_LEN", 14)
KOTE_BB_LEN         = _int("KOTE_BB_LEN", 20)
KOTE_BB_MULT        = _float("KOTE_BB_MULT", 2.0)
KOTE_DIP_USES_LOW   = _bool("KOTE_DIP_USES_LOW", True)
KOTE_LIQ_LOOKBACK   = _int("KOTE_LIQ_LOOKBACK", 50)
KOTE_LIQ_MARGIN_PCT = _float("KOTE_LIQ_MARGIN_PCT", 0.1)
KOTE_SL_ATR_BUFFER  = _float("KOTE_SL_ATR_BUFFER", 0.5)
KOTE_FIB_LOOKBACK   = _int("KOTE_FIB_LOOKBACK", 20)
KOTE_REQUIRE_FIB    = _bool("KOTE_REQUIRE_FIB", False)
KOTE_SCAN_INTERVAL  = _int("KOTE_SCAN_INTERVAL", 900)
KOTE_SYMBOLS_LIST   = _list("KOTE_SYMBOLS_LIST", "")

# ══════════════════════════════════════════════════════════════════════════════
# NUEVAS VARIABLES — SHORT-ONLY MODE (joyful-art)
# ══════════════════════════════════════════════════════════════════════════════

# ── SHORT-only mode ───────────────────────────────────────────────────────────
SHORT_ONLY = _bool("SHORT_ONLY", False)

# ── Lateral market filter (ADX) ───────────────────────────────────────────────
# 0 = desactivado. Si ADX > valor → mercado en tendencia → skip.
# Recomendado para SHORT-only: 28-32
LATERAL_ADX_MAX = _float("LATERAL_ADX_MAX", 0.0)

# ── Counter-trend penalty ─────────────────────────────────────────────────────
COUNTER_TREND_PENALTY = _float("COUNTER_TREND_PENALTY", 8.0)

# ── Conviction mínima ─────────────────────────────────────────────────────────
MIN_CONVICTION = _int("MIN_CONVICTION", 5)

# ── IBS Pullback SHORT filter ─────────────────────────────────────────────────
# Pine: "10 Bar Low Pullback" (Botnet101). Nuevo mínimo N barras + IBS > 0.85.
# Solo SHORT. Veta si precio encima EMA (contexto alcista = no shortear).
IBS_PULLBACK_ENABLED = _bool("IBS_PULLBACK_ENABLED", False)
IBS_LOOKBACK         = _int("IBS_LOOKBACK",   10)
IBS_THRESHOLD        = _float("IBS_THRESHOLD", 0.85)
IBS_EMA_PERIOD       = _int("IBS_EMA_PERIOD",  50)   # EMA50 en 3m ≈ 2.5h
IBS_USE_EMA          = _bool("IBS_USE_EMA",    True)
IBS_BOOST            = _float("IBS_BOOST",     8.0)

# ── BB Short filter ───────────────────────────────────────────────────────────
# Pine: "BB Short DCA Strategy". close > upper_BB × (1 + pct/100).
# SHORT: boost. LONG: veto si BB_SHORT_VETO_LONG=true.
# DCA del Pine original NO implementado (requiere reescribir position_manager).
BB_SHORT_ENABLED   = _bool("BB_SHORT_ENABLED",   False)
BB_SHORT_LENGTH    = _int("BB_SHORT_LENGTH",      20)
BB_SHORT_STD       = _float("BB_SHORT_STD",       2.0)
BB_SHORT_ABOVE_PCT = _float("BB_SHORT_ABOVE_PCT", 1.0)
BB_SHORT_BOOST     = _float("BB_SHORT_BOOST",     10.0)
BB_SHORT_VETO_LONG = _bool("BB_SHORT_VETO_LONG",  True)

# ── Momentum exit ─────────────────────────────────────────────────────────────
MOMENTUM_EXIT_ENABLED = _bool("MOMENTUM_EXIT_ENABLED", False)
MOMENTUM_EXIT_OB      = _float("MOMENTUM_EXIT_OB", 62.0)
MOMENTUM_EXIT_OS      = _float("MOMENTUM_EXIT_OS", 38.0)
