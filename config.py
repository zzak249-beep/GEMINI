import os
def _f(k,d):
    try: return float(os.getenv(k,str(d)).split("#")[0].strip())
    except: return d
def _i(k,d):
    try: return int(float(os.getenv(k,str(d)).split("#")[0].strip()))
    except: return d
def _b(k,d):
    return os.getenv(k,"").strip().lower().split("#")[0].strip() in ("1","true","yes") if os.getenv(k,"") else d
def _s(k,d=""): return os.getenv(k,d).strip().split("#")[0].strip() or d

BOT_NAME    = _s("BOT_NAME","mean-reversion")
API_KEY     = _s("BINGX_API_KEY")
SECRET_KEY  = _s("BINGX_SECRET_KEY")
BASE_URL    = "https://open-api.bingx.com"
TELEGRAM_TOKEN = _s("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = _s("TELEGRAM_CHAT_ID")

SYMBOL      = _s("SYMBOL","")        # vacío = multi-symbol scan
TIMEFRAME   = _s("TIMEFRAME","15m")
LEVERAGE    = _i("LEVERAGE",5)
DIRECTION   = _s("DIRECTION","BOTH") # LONG | SHORT | BOTH

# Mean Reversion params
MR_ADX_MAX          = _f("MR_ADX_MAX",30.0)      # lateral si ADX < 25
MR_RSI_SHORT        = _f("MR_RSI_SHORT",65.0)    # sobrecomprado
MR_RSI_LONG         = _f("MR_RSI_LONG",35.0)     # sobrevendido
MR_FUNDING_MIN_SHORT= _f("MR_FUNDING_MIN_SHORT",0.0) # funding >= 0 para short

# Risk
FIXED_NOTIONAL_USDT = _f("FIXED_NOTIONAL_USDT",15.0)
MIN_NOTIONAL_USDT   = _f("MIN_NOTIONAL_USDT",10.0)
MAX_NOTIONAL_USDT   = _f("MAX_NOTIONAL_USDT",60.0)
SL_ATR_MULT         = _f("SL_ATR_MULT",1.5)
TP1_ATR_MULT        = _f("TP1_ATR_MULT",2.0)
TRAIL_DISTANCE_ATR  = _f("TRAIL_DISTANCE_ATR",1.5)
MAX_HOLD_MINUTES    = _i("MAX_HOLD_MINUTES",240)
MAX_OPEN_TRADES     = _i("MAX_OPEN_TRADES",4)
RISK_PCT            = _f("RISK_PCT",1.0)
MAX_DAILY_LOSS_PCT  = _f("MAX_DAILY_LOSS_PCT",3.0)

# Scanner
TOP_N_SYMBOLS       = _i("TOP_N_SYMBOLS",150)
MIN_VOLUME_USDT     = _f("MIN_VOLUME_USDT",500_000.0)
SCAN_INTERVAL       = _i("SCAN_INTERVAL",60)
TRAILING_CHECK_SEC  = _i("TRAILING_CHECK_SEC",30)

# Session (24/7 by default)
SESSION_START = _i("SESSION_START",0)
SESSION_END   = _i("SESSION_END",24)
PORT          = _i("PORT",8080)

BLACKLIST = set(s.strip().upper() for s in _s("BLACKLIST",
    "EURUSD,GOLD,SILVER,OILWTI,OILBRENT,CUSDT,PAXG,STABLEUSDT").split(",") if s.strip())
