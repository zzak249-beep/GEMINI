#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  TRADING BOT V16 — DIAGNÓSTICO Y FIX DE V15                        ║
║                                                                      ║
║  5 BUGS CORREGIDOS:                                                  ║
║  BUG 1: EMA_TREND duro (close<EMA50→skip) mataba 70% de longs      ║
║         FIX: pasa a confluencia suave (-5pts si en contra)          ║
║  BUG 2: H1 OR condition → ambas vars True → NEUTRAL → None          ║
║         FIX: H1 solo descarta si EMA Y SuperTrend ambos opuestos    ║
║  BUG 3: SuperTrend C3 como confluencia obligatoria (ST 5m=ruido)    ║
║         FIX: ST → bonus +8pts, no confluencia dura                  ║
║  BUG 4: SLOPE_LOOK=5 hace ángulos 67% más pequeños que look=3       ║
║         FIX: look=3, SLOPE_LIMIT=10° (calibrado para 5m real)       ║
║  BUG 5: MIN_CONFLUENCES=4/6 con filtros correlacionados → imposible ║
║         FIX: 3/5 filtros independientes y reales                    ║
║                                                                      ║
║  + LOG DIAGNÓSTICO: cada símbolo muestra por qué fue rechazado      ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

import requests
import pandas as pd
import numpy as np

try:
    from telegram import Bot
    from telegram.constants import ParseMode
    TELEGRAM_OK = True
except ImportError:
    TELEGRAM_OK = False

# ══════════════════════════════════════════════════════════════════════
#  CONFIG — CALIBRADO PARA 5m REAL
# ══════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", os.environ.get("BINGX_API_SECRET", ""))
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TIMEFRAME       = os.environ.get("TIMEFRAME",      "5m")
RISK_PERCENT    = float(os.environ.get("RISK_PERCENT",  "1.0"))
LEVERAGE        = int(os.environ.get("LEVERAGE",        "5"))
LOOP_SECONDS    = int(os.environ.get("LOOP_SECONDS",    "60"))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES", "6"))
SCAN_WORKERS    = int(os.environ.get("SCAN_WORKERS",    "20"))
MAX_SYMBOLS     = int(os.environ.get("MAX_SYMBOLS",     "0"))

# Filtros de calidad — calibrados
MIN_SCORE       = float(os.environ.get("MIN_SCORE",      "40.0"))
MIN_CONFLUENCES = int(os.environ.get("MIN_CONFLUENCES",  "3"))    # FIX: 4→3, de 5 filtros reales
MIN_DIST_PCT    = float(os.environ.get("MIN_DIST_PCT",   "0.15"))
ATR_MAX_PCT     = float(os.environ.get("ATR_MAX_PCT",    "5.0"))  # FIX: más permisivo

# EMAs
EMA_FAST        = int(os.environ.get("EMA_FAST",   "7"))
EMA_SLOW        = int(os.environ.get("EMA_SLOW",   "21"))
EMA_TREND       = int(os.environ.get("EMA_TREND",  "50"))
SLOPE_LIMIT     = float(os.environ.get("SLOPE_LIMIT", "10.0"))   # FIX: 12→10 (realista 5m)
SLOPE_LOOK      = int(os.environ.get("SLOPE_LOOK",   "3"))       # FIX: 5→3 (más reactivo)

# ADX / RSI / Vol
ADX_LEN         = int(os.environ.get("ADX_LEN",  "14"))
ADX_MIN         = float(os.environ.get("ADX_MIN", "20.0"))
RSI_LEN         = int(os.environ.get("RSI_LEN",  "14"))
RSI_OB          = float(os.environ.get("RSI_OB",  "72.0"))
RSI_OS          = float(os.environ.get("RSI_OS",  "28.0"))
VOL_MULT        = float(os.environ.get("VOL_MULT", "0.8"))

# SuperTrend — ahora bonus, no filtro duro
ST_PERIOD       = int(os.environ.get("ST_PERIOD",  "10"))
ST_MULT         = float(os.environ.get("ST_MULT",  "3.0"))

# TP / SL
TP_MULT         = float(os.environ.get("TP_MULT",     "2.5"))
SL_ATR_MULT     = float(os.environ.get("SL_ATR_MULT", "1.5"))
MIN_RR          = float(os.environ.get("MIN_RR",      "1.8"))
BE_ATR_MULT     = float(os.environ.get("BE_ATR_MULT", "1.0"))
TRAILING_STOP   = os.environ.get("TRAILING_STOP", "true").lower() == "true"

# Position sizing
MIN_ORDER_USDT  = float(os.environ.get("MIN_ORDER_USDT", "7.0"))
MAX_ORDER_USDT  = float(os.environ.get("MAX_ORDER_USDT", "50.0"))
MAX_MARGIN_PCT  = float(os.environ.get("MAX_MARGIN_PCT", "30.0"))

# Session filter — OFF por defecto
SESSION_FILTER  = os.environ.get("SESSION_FILTER", "false").lower() == "true"
SESSION_START   = int(os.environ.get("SESSION_START", "6"))
SESSION_END     = int(os.environ.get("SESSION_END",  "22"))

# Circuit breaker
MAX_CONSEC_LOSSES = int(os.environ.get("MAX_CONSEC_LOSSES", "4"))
CB_PAUSE_MINS     = int(os.environ.get("CB_PAUSE_MINS",    "30"))

# Cache
H1_CACHE_TTL  = int(os.environ.get("H1_CACHE_TTL",  "300"))
COOLDOWN_MINS = int(os.environ.get("COOLDOWN_MINS", "15"))

_raw = os.environ.get("CUSTOM_SYMBOLS", "")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE   = "https://open-api.bingx.com"
INTERVAL_MAP = {"1m":"1m","3m":"3m","5m":"5m","15m":"15m","30m":"30m","1h":"1H","4h":"4H"}

EXCLUDED_PREFIXES = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG")
EXCLUDED_KEYWORDS = ("Gasoline","GasOil","Brent","WTI","Copper","Wheat",
                     "Cotton","Soybean","Silver","EURUSD","GBPUSD","JPYUSD")

FALLBACK_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "INJ-USDT","SUI-USDT","ARB-USDT","OP-USDT","WIF-USDT",
    "PEPE-USDT","NEAR-USDT","APT-USDT","HBAR-USDT","AAVE-USDT",
    "LDO-USDT","RUNE-USDT","GRT-USDT","CRV-USDT","DYDX-USDT",
    "TIA-USDT","SEI-USDT","WLD-USDT","FIL-USDT","ICP-USDT",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════
sl_cooldown    = {}
h1_cache       = {}
consec_losses  = 0
cb_pause_until = None

# Diagnóstico: razones de rechazo por ciclo
reject_counter = Counter()

# ══════════════════════════════════════════════════════════════════════
#  BINGX API
# ══════════════════════════════════════════════════════════════════════
def _sign(params):
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(BINGX_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()

def bx_get(path, params=None):
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.get(BINGX_BASE + path, params=p,
                     headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()

def bx_post(path, payload):
    p = dict(payload)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.post(BINGX_BASE + path, json=p,
                      headers={"X-BX-APIKEY": BINGX_API_KEY,
                               "Content-Type":"application/json"}, timeout=15)
    r.raise_for_status()
    return r.json()

def get_balance():
    try:
        data = bx_get("/openApi/swap/v2/user/balance")
        bal  = data.get("data", {}).get("balance", {})
        for f in ("availableMargin","available","crossWalletBalance","walletBalance","equity"):
            v = bal.get(f)
            if v is not None and v != "" and float(v) > 0:
                log.info(f"Balance: {float(v):.4f} USDT ({f})")
                return float(v)
        return 0.0
    except Exception as e:
        log.error(f"get_balance: {e}")
        return 0.0

def get_all_positions():
    try:
        data   = bx_get("/openApi/swap/v2/user/positions", {})
        result = {}
        for p in data.get("data", []):
            if isinstance(p, dict) and float(p.get("positionAmt", 0)) != 0:
                result[p["symbol"]] = p
        log.info(f"Open positions ({len(result)}): {list(result.keys())[:8]}")
        return result
    except Exception as e:
        log.error(f"get_positions: {e}")
        return {}

def _is_valid(sym):
    if not sym or not sym.endswith("-USDT"): return False
    base = sym.replace("-USDT","")
    if len(base) < 2: return False
    if any(base.startswith(p) for p in EXCLUDED_PREFIXES): return False
    if any(kw.lower() in sym.lower() for kw in EXCLUDED_KEYWORDS): return False
    return True

def get_all_symbols(limit=0):
    try:
        data = bx_get("/openApi/swap/v2/quote/contracts", {})
        contracts = data.get("data", [])
        usdt = [c for c in contracts
                if isinstance(c, dict) and c.get("asset","") == "USDT" and c.get("status") == 1]
        if not usdt:
            usdt = [c for c in contracts
                    if isinstance(c, dict) and str(c.get("symbol","")).endswith("-USDT")]
        usdt.sort(key=lambda x: float(x.get("tradeAmount",0) or 0), reverse=True)
        syms   = [c["symbol"] for c in usdt if _is_valid(c.get("symbol",""))]
        result = syms if limit == 0 else syms[:limit]
        log.info(f"✅ {len(result)} symbols")
        return result or FALLBACK_SYMBOLS
    except Exception as e:
        log.warning(f"get_all_symbols: {e}")
        return FALLBACK_SYMBOLS

def set_lev(symbol):
    for side in ("LONG","SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol":symbol,"side":side,"leverage":LEVERAGE})
        except Exception:
            pass

def get_live_price(symbol):
    for endpoint, parser in [
        ("/openApi/swap/v2/quote/premiumIndex", lambda d: float(
            next((i["markPrice"] for i in (d.get("data",[]) if isinstance(d.get("data",[]),list) else [d.get("data",{})]) if i.get("symbol")==symbol and i.get("markPrice"), 0)) or 0
        )),
        ("/openApi/swap/v2/quote/ticker", lambda d: float(
            next((i.get("lastPrice") or i.get("price",0) for i in (d.get("data",[]) if isinstance(d.get("data",[]),list) else [d.get("data",{})]) if i.get("symbol")==symbol), 0) or 0
        )),
    ]:
        try:
            v = parser(bx_get(endpoint, {"symbol":symbol}))
            if v > 0: return v
        except Exception:
            pass
    try:
        rows = bx_get("/openApi/swap/v3/quote/klines",
                      {"symbol":symbol,"interval":INTERVAL_MAP.get(TIMEFRAME,"5m"),"limit":2}).get("data",[])
        if rows: return float(rows[-1][4])
    except Exception:
        pass
    raise ValueError(f"No price for {symbol}")

# ══════════════════════════════════════════════════════════════════════
#  KLINES
# ══════════════════════════════════════════════════════════════════════
def _fetch_klines(symbol, interval, limit):
    params = {"symbol":symbol, "interval":INTERVAL_MAP.get(interval,interval), "limit":limit}
    data   = bx_get("/openApi/swap/v3/quote/klines", params)
    rows   = data.get("data",[])
    if not rows or not isinstance(rows, list): return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open","high","low","close","volume"], inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

def get_klines(symbol, limit=200):
    return _fetch_klines(symbol, TIMEFRAME, limit)

def get_h1_klines(symbol, limit=80):
    now    = time.time()
    cached = h1_cache.get(symbol)
    if cached:
        df_c, ts = cached
        if now - ts < H1_CACHE_TTL and len(df_c) >= 30:
            return df_c.copy()
    try:
        df = _fetch_klines(symbol, "1h", limit)
        if not df.empty:
            h1_cache[symbol] = (df.copy(), now)
        return df
    except Exception:
        return pd.DataFrame()

# ══════════════════════════════════════════════════════════════════════
#  INDICADORES
# ══════════════════════════════════════════════════════════════════════
def calc_atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calc_ema_angle(ema_s, atr_s, look=3):
    """Pendiente en grados normalizada por ATR."""
    price_change = ema_s - ema_s.shift(look)
    denom        = (atr_s * look).replace(0, np.nan)
    return pd.Series(
        np.degrees(np.arctan2(price_change.values, denom.values)),
        index=ema_s.index
    )

def calc_adx(high, low, close, period=14):
    up   = high.diff()
    down = -low.diff()
    plus_dm  = np.where((up > down) & (up > 0),   up,   0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    alpha = 1 / period
    def w(arr): return pd.Series(arr, index=high.index).ewm(alpha=alpha, adjust=False).mean()
    tr_s  = w(tr); pdm_s = w(plus_dm); mdm_s = w(minus_dm)
    di_p  = 100 * pdm_s / tr_s.replace(0, np.nan)
    di_m  = 100 * mdm_s / tr_s.replace(0, np.nan)
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)
    return di_p, di_m, dx.ewm(alpha=alpha, adjust=False).mean()

def calc_rsi(close, period=14):
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_supertrend(high, low, close, period=10, mult=3.0):
    """
    SuperTrend. Devuelve Series: +1 uptrend, -1 downtrend.
    Implementación eficiente con arrays numpy.
    """
    n      = len(close)
    atr    = calc_atr(high, low, close, period).values
    hl2    = ((high + low) / 2).values
    close_ = close.values

    upper_raw = hl2 + mult * atr
    lower_raw = hl2 - mult * atr

    final_ub  = upper_raw.copy()
    final_lb  = lower_raw.copy()
    direction = np.ones(n, dtype=int)

    for i in range(1, n):
        # Upper band
        if upper_raw[i] < final_ub[i-1] or close_[i-1] > final_ub[i-1]:
            final_ub[i] = upper_raw[i]
        else:
            final_ub[i] = final_ub[i-1]
        # Lower band
        if lower_raw[i] > final_lb[i-1] or close_[i-1] < final_lb[i-1]:
            final_lb[i] = lower_raw[i]
        else:
            final_lb[i] = final_lb[i-1]
        # Direction
        if close_[i] > final_ub[i-1]:
            direction[i] = 1
        elif close_[i] < final_lb[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]

    return pd.Series(direction, index=close.index)

def calc_heikin_ashi_last(df, i):
    """Calcula solo la última vela HA (O+H+L+C)/4 sin bucle completo."""
    ha_close = (float(df["open"].iloc[i]) + float(df["high"].iloc[i]) +
                float(df["low"].iloc[i])  + float(df["close"].iloc[i])) / 4
    ha_open  = (float(df["open"].iloc[i-1]) + float(df["close"].iloc[i-1])) / 2
    return ha_close > ha_open   # True = bullish HA candle

def calc_squeeze_off(high, low, close, sq_len=20, bb_m=2.0, kc_m=1.5):
    """True = squeeze OFF (mercado expandido, OK para operar)."""
    basis = close.rolling(sq_len).mean()
    std   = close.rolling(sq_len).std()
    bb_lo = basis - bb_m * std
    bb_up = basis + bb_m * std
    atr   = calc_atr(high, low, close, sq_len)
    kc_lo = basis - kc_m * atr
    kc_up = basis + kc_m * atr
    sqz_on = (bb_lo > kc_lo) & (bb_up < kc_up)
    return ~sqz_on

def calc_vwap(df):
    typical    = (df["high"] + df["low"] + df["close"]) / 3
    df2        = df.copy()
    df2["_tp"] = typical * df["volume"]
    df2["_day"]= df2["open_time"].dt.floor("D")
    df2["_ctp"]= df2.groupby("_day")["_tp"].cumsum()
    df2["_cv"] = df2.groupby("_day")["volume"].cumsum()
    return (df2["_ctp"] / df2["_cv"]).fillna(method="ffill")

# ══════════════════════════════════════════════════════════════════════
#  H1 ANALYSIS — FIX BUG 2 (OR condition → AND para descartar)
# ══════════════════════════════════════════════════════════════════════
def analyze_h1(symbol):
    """
    H1 trend usando EMA7 vs EMA21.
    FIX: solo descarta si EMA Y SuperTrend ambos contra señal.
    NEUTRAL = permitido.
    """
    df = get_h1_klines(symbol, 80)
    if df.empty or len(df) < 30:
        return {"h1_trend": "NEUTRAL", "h1_bonus": 5}

    close, high, low = df["close"], df["high"], df["low"]
    ema7  = calc_ema(close, 7)
    ema21 = calc_ema(close, 21)
    st    = calc_supertrend(high, low, close, ST_PERIOD, ST_MULT)

    ema7_now  = float(ema7.iloc[-1])
    ema21_now = float(ema21.iloc[-1])
    st_now    = int(st.iloc[-1])
    close_now = float(close.iloc[-1])

    # FIX: tendencia clara solo si EMA Y ST coinciden
    bull_clear = (ema7_now > ema21_now) and (st_now == 1)
    bear_clear = (ema7_now < ema21_now) and (st_now == -1)
    # Señal parcial (solo uno de los dos)
    bull_weak  = (ema7_now > ema21_now) or (st_now == 1)
    bear_weak  = (ema7_now < ema21_now) or (st_now == -1)

    if bull_clear:
        h1_trend = "BULL"
    elif bear_clear:
        h1_trend = "BEAR"
    elif bull_weak and not bear_weak:
        h1_trend = "BULL_WEAK"
    elif bear_weak and not bull_weak:
        h1_trend = "BEAR_WEAK"
    else:
        h1_trend = "NEUTRAL"

    return {
        "h1_trend": h1_trend,
        "h1_ema":   "BULL" if ema7_now > ema21_now else "BEAR",
        "h1_st":    st_now,
        "h1_close": close_now,
    }

def h1_allows(h1_trend, direction):
    """
    FIX BUG 2: solo descarta si H1 claramente opuesto.
    BULL, BULL_WEAK, NEUTRAL → permitidos para LONG.
    BEAR_WEAK → permitido con penalización.
    BEAR (clara) → descarta LONG.
    """
    if direction == "LONG":
        if h1_trend == "BULL":       return True,  20
        if h1_trend == "BULL_WEAK":  return True,  12
        if h1_trend == "NEUTRAL":    return True,   5
        if h1_trend == "BEAR_WEAK":  return True,  -5  # permitido pero penaliza
        if h1_trend == "BEAR":       return False,  0  # descarta
    else:  # SHORT
        if h1_trend == "BEAR":       return True,  20
        if h1_trend == "BEAR_WEAK":  return True,  12
        if h1_trend == "NEUTRAL":    return True,   5
        if h1_trend == "BULL_WEAK":  return True,  -5
        if h1_trend == "BULL":       return False,  0
    return True, 0

# ══════════════════════════════════════════════════════════════════════
#  PATRONES DE VELA
# ══════════════════════════════════════════════════════════════════════
def detect_candle_pattern(df, i, direction, atr_val):
    if i < 1: return "NONE", 0.0, None

    o, h, l, c = (float(df["open"].iloc[i]), float(df["high"].iloc[i]),
                  float(df["low"].iloc[i]),  float(df["close"].iloc[i]))
    o1, c1 = float(df["open"].iloc[i-1]), float(df["close"].iloc[i-1])

    rng  = h - l
    body = abs(c - o)
    if rng < 1e-10 or atr_val < 1e-10: return "NONE", 0.0, None

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # Pin Bar
    if body / rng < 0.35:
        if direction == "LONG" and lower_wick/rng >= 0.55 and lower_wick >= 2*max(body,1e-10):
            return "PIN_BAR", min(lower_wick/rng*120, 100.0), l - atr_val*0.1
        if direction == "SHORT" and upper_wick/rng >= 0.55 and upper_wick >= 2*max(body,1e-10):
            return "PIN_BAR", min(upper_wick/rng*120, 100.0), h + atr_val*0.1

    # Engulfing
    body1 = abs(c1 - o1)
    if body1 > 1e-10 and body/body1 >= 1.05:
        if direction == "LONG" and c>o and c1<o1 and c>max(o1,c1) and o<min(o1,c1):
            return "ENGULF", min(body/body1*45, 100.0), l - atr_val*0.1
        if direction == "SHORT" and c<o and c1>o1 and c<min(o1,c1) and o>max(o1,c1):
            return "ENGULF", min(body/body1*45, 100.0), h + atr_val*0.1

    # Momentum
    if body/rng >= 0.65 and body >= atr_val*0.5:
        if direction == "LONG" and c>o and upper_wick < body*0.35:
            return "MOMENTUM", min(body/rng*90, 100.0), l - atr_val*0.1
        if direction == "SHORT" and c<o and lower_wick < body*0.35:
            return "MOMENTUM", min(body/rng*90, 100.0), h + atr_val*0.1

    return "NONE", 0.0, None

# ══════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════
def calc_qty(balance, entry, sl, quality_mult=1.0):
    dist_pct = abs(entry - sl) / entry
    if dist_pct < 1e-8: return 0, 0
    risk_usdt    = balance * (RISK_PERCENT / 100) * quality_mult
    notional     = risk_usdt / dist_pct
    max_notional = min(MAX_ORDER_USDT, balance*(MAX_MARGIN_PCT/100)*LEVERAGE)
    notional     = max(MIN_ORDER_USDT, min(notional, max_notional))
    qty          = notional / entry
    return round(max(qty, 0.001), 4), round(notional, 2)

def open_order(symbol, side, qty, sl, tp):
    payload = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "LONG" if side=="BUY" else "SHORT",
        "type":         "MARKET",
        "quantity":     round(qty, 4),
        "stopLoss":     json.dumps({"type":"STOP_MARKET","stopPrice":round(sl,6),"workingType":"MARK_PRICE"}),
        "takeProfit":   json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":round(tp,6),"workingType":"MARK_PRICE"}),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    if resp.get("code", 0) != 0:
        raise ValueError(f"BingX {resp.get('code')}: {resp.get('msg','?')}")
    return resp

def open_order_retry(symbol, side, qty, sl, tp, atr_val, direction, retries=1):
    for attempt in range(retries + 1):
        try:
            return open_order(symbol, side, qty, sl, tp)
        except ValueError as e:
            if "101400" in str(e) and attempt < retries:
                time.sleep(1)
                live = get_live_price(symbol)
                if direction == "LONG":
                    sl = round(live - atr_val*SL_ATR_MULT, 6)
                    tp = round(live + (live-sl)*TP_MULT, 6)
                else:
                    sl = round(live + atr_val*SL_ATR_MULT, 6)
                    tp = round(live - (sl-live)*TP_MULT, 6)
            else:
                raise

def update_trailing_stops(positions):
    if not TRAILING_STOP or not positions: return
    for sym, pos in positions.items():
        try:
            side  = pos.get("positionSide","LONG")
            entry = float(pos.get("avgPrice",0) or 0)
            if entry == 0: continue
            live  = get_live_price(sym)
            df    = get_klines(sym, 50)
            if df.empty: continue
            atr_v = float(calc_atr(df["high"],df["low"],df["close"],14).iloc[-1])
            if side == "LONG" and live >= entry + atr_v*BE_ATR_MULT:
                bx_post("/openApi/swap/v2/trade/order",{
                    "symbol":sym,"type":"STOP_MARKET","side":"SELL","positionSide":"LONG",
                    "stopPrice":round(entry*1.001,6),"closePosition":"true","workingType":"MARK_PRICE"
                })
                log.info(f"✅ Trailing BE {sym} LONG")
            elif side == "SHORT" and live <= entry - atr_v*BE_ATR_MULT:
                bx_post("/openApi/swap/v2/trade/order",{
                    "symbol":sym,"type":"STOP_MARKET","side":"BUY","positionSide":"SHORT",
                    "stopPrice":round(entry*0.999,6),"closePosition":"true","workingType":"MARK_PRICE"
                })
                log.info(f"✅ Trailing BE {sym} SHORT")
        except Exception as e:
            log.debug(f"Trailing {sym}: {e}")

# ══════════════════════════════════════════════════════════════════════
#  ESCANEO PRINCIPAL V16 — CON DIAGNÓSTICO
# ══════════════════════════════════════════════════════════════════════
def scan_symbol(symbol):
    """
    V16: filtros calibrados para 5m real.
    Cada rechazo incrementa reject_counter para diagnóstico.
    """
    def reject(reason):
        reject_counter[reason] += 1
        return None

    if symbol in sl_cooldown:
        if (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds()/60 < COOLDOWN_MINS:
            return None

    try:
        df = get_klines(symbol, 200)
        if df.empty or len(df) < 100:
            return reject("klines_insuf")

        h, l, c, o = df["high"], df["low"], df["close"], df["open"]
        atr_s = calc_atr(h, l, c, 14)
        ema_f = calc_ema(c, EMA_FAST)
        ema_s = calc_ema(c, EMA_SLOW)
        ema_t = calc_ema(c, EMA_TREND)
        angle = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        di_p, di_m, adx_s = calc_adx(h, l, c, ADX_LEN)
        rsi_s  = calc_rsi(c, RSI_LEN)
        vol_ma = df["volume"].rolling(20).mean()
        sqz_ok = calc_squeeze_off(h, l, c, 20, 2.0, 1.5)
        vwap_s = calc_vwap(df)
        st_5m  = calc_supertrend(h, l, c, ST_PERIOD, ST_MULT)

        i = len(df) - 2
        if i < 80: return reject("klines_insuf")

        close_now = float(c.iloc[i])
        atr_val   = float(atr_s.iloc[i])
        if atr_val <= 0: return reject("atr_zero")
        atr_pct   = atr_val / close_now * 100
        if atr_pct > ATR_MAX_PCT: return reject(f"atr_high_{atr_pct:.1f}%")

        angle_now = float(angle.iloc[i])
        adx_now   = float(adx_s.iloc[i])
        di_p_now  = float(di_p.iloc[i])
        di_m_now  = float(di_m.iloc[i])
        rsi_now   = float(rsi_s.iloc[i])
        vol_now   = float(df["volume"].iloc[i])
        vma       = float(vol_ma.iloc[i])
        sqz_now   = bool(sqz_ok.iloc[i])
        vwap_now  = float(vwap_s.iloc[i]) if not np.isnan(float(vwap_s.iloc[i])) else close_now
        st_now    = int(st_5m.iloc[i])
        vratio    = round(vol_now/vma, 2) if vma > 0 else 0.0
        ema_f_now = float(ema_f.iloc[i])
        ema_s_now = float(ema_s.iloc[i])
        ema_t_now = float(ema_t.iloc[i])

        if any(np.isnan(x) for x in [angle_now, adx_now, rsi_now, ema_f_now]):
            return reject("nan_values")

        # ── Dirección por EMA fast/slow ──────────────────────────────
        if   ema_f_now > ema_s_now: direction = "LONG"
        elif ema_f_now < ema_s_now: direction = "SHORT"
        else: return reject("ema_flat")

        # ── RSI extremo (único filtro duro además de EMA) ─────────────
        if direction == "LONG"  and rsi_now > RSI_OB: return reject("rsi_overbought")
        if direction == "SHORT" and rsi_now < RSI_OS: return reject("rsi_oversold")

        # ══ CONFLUENCIAS V16 — 5 filtros, mínimo 3 ═══════════════════
        confluences = 0
        conf_detail = {}

        # C1: Slope del EMA (FIX look=3, limit=10°)
        ang_ok = angle_now >= SLOPE_LIMIT if direction=="LONG" else angle_now <= -SLOPE_LIMIT
        if ang_ok: confluences += 1
        conf_detail["slope"] = f"{'✅' if ang_ok else '❌'}{angle_now:.1f}°"

        # C2: ADX (sin DI obligatorio — FIX: DI como bonus)
        adx_ok = adx_now >= ADX_MIN
        if adx_ok: confluences += 1
        conf_detail["adx"] = f"{'✅' if adx_ok else '❌'}{adx_now:.0f}"
        # DI alignment como bonus
        di_bonus = 5 if ((di_p_now > di_m_now and direction=="LONG") or
                         (di_m_now > di_p_now and direction=="SHORT")) else 0

        # C3: Heikin Ashi
        ha_bull = calc_heikin_ashi_last(df, i)
        ha_ok   = (ha_bull and direction=="LONG") or (not ha_bull and direction=="SHORT")
        if ha_ok: confluences += 1
        conf_detail["HA"] = "✅" if ha_ok else "❌"

        # C4: Volumen
        vol_ok = vratio >= VOL_MULT
        if vol_ok: confluences += 1
        conf_detail["vol"] = f"{'✅' if vol_ok else '❌'}{vratio:.1f}x"

        # C5: Squeeze OFF
        if sqz_now: confluences += 1
        conf_detail["sqz"] = "✅OFF" if sqz_now else "❌ON"

        if confluences < MIN_CONFLUENCES:
            return reject(f"confl_{confluences}/{MIN_CONFLUENCES}")

        # ── H1 alignment (FIX BUG 2) ─────────────────────────────────
        h1_ctx  = analyze_h1(symbol)
        h1_ok, h1_bonus = h1_allows(h1_ctx["h1_trend"], direction)
        if not h1_ok:
            return reject(f"h1_contra_{h1_ctx['h1_trend']}")
        conf_detail["H1"] = f"{h1_ctx['h1_trend']}({h1_bonus:+d})"

        # ── FIX BUG 3: SuperTrend 5m como bonus, no confluencia ──────
        st_bonus = 8 if ((st_now == 1 and direction=="LONG") or
                         (st_now == -1 and direction=="SHORT")) else -3
        conf_detail["ST"] = f"{'✅' if st_bonus>0 else '❌'}({'▲' if st_now==1 else '▼'})"

        # ── FIX BUG 1: EMA_TREND como bonus, no filtro duro ──────────
        trend_bonus = 0
        if direction == "LONG":
            trend_bonus = 8 if close_now > ema_t_now else -3
        else:
            trend_bonus = 8 if close_now < ema_t_now else -3
        conf_detail["EMA50"] = f"{'✅' if trend_bonus>0 else '❌'}"

        # ── VWAP bonus ────────────────────────────────────────────────
        vwap_bonus = 6 if ((close_now > vwap_now and direction=="LONG") or
                           (close_now < vwap_now and direction=="SHORT")) else 0

        # ── Patrón de vela ────────────────────────────────────────────
        pat_name, pat_score, sl_candle = detect_candle_pattern(df, i, direction, atr_val)

        # ── SL / TP ───────────────────────────────────────────────────
        sl_atr = atr_val * SL_ATR_MULT
        if direction == "LONG":
            sl_price = close_now - sl_atr
            if sl_candle and sl_candle > 0:
                sl_price = min(sl_price, sl_candle)
            sl_price = min(sl_price, close_now*(1-MIN_DIST_PCT/100))
            if sl_price >= close_now: return reject("sl_invalid_long")
            tp_price = close_now + (close_now-sl_price)*TP_MULT
        else:
            sl_price = close_now + sl_atr
            if sl_candle and sl_candle > 0:
                sl_price = max(sl_price, sl_candle)
            sl_price = max(sl_price, close_now*(1+MIN_DIST_PCT/100))
            if sl_price <= close_now: return reject("sl_invalid_short")
            tp_price = close_now - (sl_price-close_now)*TP_MULT

        dist     = abs(close_now-sl_price)
        dist_pct = dist/close_now*100
        if dist_pct < MIN_DIST_PCT: return reject("dist_small")
        rr = abs(tp_price-close_now)/dist
        if rr < MIN_RR: return reject(f"rr_low_{rr:.1f}")

        # ── SCORING V16 ───────────────────────────────────────────────
        score  = (confluences/5)*35          # confluencias reales: max 35
        score += h1_bonus                    # H1: máx 20, mín -5
        score += st_bonus                    # SuperTrend: +8/-3
        score += trend_bonus                 # EMA50: +8/-3
        score += di_bonus                    # DI alineado: +5
        score += vwap_bonus                  # VWAP: +6
        score += min(pat_score/7, 15)        # patrón: max 15
        score += min(vratio*4, 8)            # vol extra: max 8
        score += min((rr-MIN_RR)*2, 6)      # RR bonus: max 6

        if score < MIN_SCORE:
            return reject(f"score_{score:.0f}<{MIN_SCORE}")

        quality_mult = round(min(max(0.7+(score-MIN_SCORE)/60*0.6, 0.7), 1.3), 2)

        return {
            "symbol":       symbol,
            "signal":       direction,
            "pattern":      pat_name,
            "close":        close_now,
            "sl":           round(sl_price, 6),
            "tp":           round(tp_price, 6),
            "atr":          atr_val,
            "atr_pct":      round(atr_pct, 2),
            "vol_ratio":    vratio,
            "angle":        round(angle_now, 1),
            "adx":          round(adx_now, 1),
            "rsi":          round(rsi_now, 1),
            "score":        round(score, 1),
            "rr":           round(rr, 2),
            "dist_pct":     round(dist_pct, 3),
            "confluences":  confluences,
            "conf_detail":  conf_detail,
            "h1_trend":     h1_ctx["h1_trend"],
            "pat_score":    round(pat_score, 1),
            "quality_mult": quality_mult,
        }

    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════
async def _send_tg(msg):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN: return
    bot = Bot(token=TELEGRAM_TOKEN)
    cid = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=cid, text=msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN: return
    try: asyncio.run(_send_tg(msg))
    except Exception as e: log.warning(f"Telegram: {e}")

def tg_startup(balance, symbols):
    tg(
        f"🚀 <b>TRADING BOT V16 — DIAGNÓSTICO + FIX</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 <b>5 bugs corregidos de V15:</b>\n"
        f"  • EMA50 → bonus suave (no filtro duro)\n"
        f"  • H1 solo descarta si EMA+ST ambos opuestos\n"
        f"  • SuperTrend → +8pts bonus (no confluencia)\n"
        f"  • SLOPE_LOOK=3, SLOPE_LIMIT=10° (calibrado 5m)\n"
        f"  • Confluencias: {MIN_CONFLUENCES}/5 (era 4/6)\n"
        f"📊 <b>Score≥:</b> {MIN_SCORE} | <b>ADX≥:</b> {ADX_MIN}\n"
        f"💰 <b>Balance:</b> {balance:.2f} USDT | <b>Sym:</b> {len(symbols)}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_entry(sig, qty, notional, balance):
    d    = "🟢 LONG" if sig["signal"]=="LONG" else "🔴 SHORT"
    icon = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","NONE":"📈"}.get(sig["pattern"],"⚡")
    cd   = " | ".join(f"{k}:{v}" for k,v in sig.get("conf_detail",{}).items())
    tg(
        f"<b>✅ ENTRADA V16 — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']:.0f}/100\n"
        f"<b>Confl:</b> {sig['confluences']}/5 | <b>H1:</b> {sig['h1_trend']}\n"
        f"{icon} <b>Patrón:</b> {sig['pattern']} ({sig['pat_score']:.0f})\n"
        f"<b>Filtros:</b> {cd}\n"
        f"<b>Ang:</b> {sig['angle']}° | <b>ADX:</b> {sig['adx']} | "
        f"<b>RSI:</b> {sig['rsi']} | <b>Vol:</b> {sig['vol_ratio']}x\n"
        f"<b>Entrada:</b> <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop:</b>   <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Target:</b> <code>{sig['tp']:.6g}</code> | <b>R:R</b> 1:{sig['rr']}\n"
        f"<b>Qty:</b> {qty:.4f} | <b>Notional:</b> {notional:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def tg_diag(total, cycle):
    """Envía diagnóstico con las razones de rechazo más comunes."""
    top = reject_counter.most_common(8)
    lines = [
        f"📊 <b>DIAGNÓSTICO V16 — ciclo #{cycle}/{total} sym</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for reason, count in top:
        pct = count / max(total, 1) * 100
        lines.append(f"  • <code>{reason}</code>: {count} ({pct:.0f}%)")
    lines.append(f"\n💡 Si 'confl_X/3' es top → bajar MIN_CONFLUENCES")
    lines.append(f"💡 Si 'h1_contra_BULL' alto → mercado alcista, solo longs")
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

# ══════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════
def main():
    global consec_losses, cb_pause_until

    log.info("╔══════════════════════════════════════════════╗")
    log.info("║  TRADING BOT V16 — 5 BUGS CORREGIDOS         ║")
    log.info("╚══════════════════════════════════════════════╝")
    log.info(f"  Slope≥{SLOPE_LIMIT}°(look={SLOPE_LOOK}) | ADX≥{ADX_MIN} | "
             f"Confl≥{MIN_CONFLUENCES}/5 | Score≥{MIN_SCORE}")
    log.info(f"  EMA_TREND=bonus | H1=bonus(no duro) | ST=bonus | VWAP=bonus")

    symbols = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols: symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance: {balance:.4f} | Symbols: {len(symbols)} | Open: {len(positions)}")

    def _prefetch():
        log.info("Pre-cargando H1...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(get_h1_klines, symbols[:80]))
        log.info("H1 cache listo.")
    threading.Thread(target=_prefetch, daemon=True).start()

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(set_lev, symbols))

    tg_startup(balance, symbols)
    log.info("✅ Bot V16 iniciado. Loop comenzando.")

    errors = 0
    cycle  = 0

    while True:
        t0     = time.time()
        cycle += 1
        reject_counter.clear()

        try:
            if SESSION_FILTER:
                hour = datetime.now(timezone.utc).hour
                if not (SESSION_START <= hour < SESSION_END):
                    log.info(f"⏸️  Fuera de sesión ({hour}h UTC).")
                    time.sleep(300)
                    continue

            if cb_pause_until and datetime.now(timezone.utc) < cb_pause_until:
                rem = (cb_pause_until - datetime.now(timezone.utc)).seconds // 60
                log.info(f"🛑 CB activo: {rem}min restantes.")
                time.sleep(60)
                continue

            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)

            log.info(f"── V16 | {balance:.2f}U | {open_count}/{MAX_OPEN_TRADES} | "
                     f"{len(symbols)} sym | ciclo #{cycle} ──")

            if TRAILING_STOP and positions:
                update_trailing_stops(positions)

            # Scan
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan_symbol, s): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r: signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Señales: {len(signals)}/{len(symbols)}")

            # Log top razones de rechazo
            top_rejects = reject_counter.most_common(5)
            if top_rejects:
                log.info("Top rechazos: " + " | ".join(f"{r}={c}" for r,c in top_rejects))

            if not signals:
                tg_diag(len(symbols), cycle)
            else:
                for s in signals[:5]:
                    log.info(
                        f"  → {s['symbol']} {s['signal']} [{s['pattern']}] "
                        f"H1:{s['h1_trend']} confl:{s['confluences']}/5 "
                        f"score={s['score']:.1f} ang={s['angle']}° rr=1:{s['rr']}"
                    )

            # Ejecutar órdenes
            entered = set()
            for sig in signals:
                sym = sig["symbol"]
                if sym in positions or sym in entered: continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info("Max trades alcanzado.")
                    break
                if balance < MIN_ORDER_USDT:
                    log.warning(f"Balance bajo: {balance:.2f}U")
                    break

                try:
                    set_lev(sym)
                    live = get_live_price(sym)
                    atr_val   = sig["atr"]
                    direction = sig["signal"]

                    if direction == "LONG":
                        sl = round(live - atr_val*SL_ATR_MULT, 6)
                        sl = round(min(sl, live*(1-MIN_DIST_PCT/100)), 6)
                        tp = round(live + (live-sl)*TP_MULT, 6)
                    else:
                        sl = round(live + atr_val*SL_ATR_MULT, 6)
                        sl = round(max(sl, live*(1+MIN_DIST_PCT/100)), 6)
                        tp = round(live - (sl-live)*TP_MULT, 6)

                    if sl <= 0 or tp <= 0: continue
                    rr_live = abs(tp-live)/abs(live-sl)
                    if rr_live < MIN_RR: continue

                    qty, notional = calc_qty(balance, live, sl, sig["quality_mult"])
                    if qty <= 0 or notional < MIN_ORDER_USDT: continue

                    log.info(f"ORDEN {sym} {direction} qty={qty:.4f} "
                             f"notional={notional:.1f}U live={live:.6g} "
                             f"sl={sl:.6g} tp={tp:.6g} score={sig['score']:.1f}")

                    side = "BUY" if direction=="LONG" else "SELL"
                    res  = open_order_retry(sym, side, qty, sl, tp, atr_val, direction)
                    log.info(f"✅ {sym} abierto")

                    sig.update({"close":live,"sl":sl,"tp":tp,
                                "dist_pct":round(abs(live-sl)/live*100,3),
                                "rr":round(rr_live,2)})
                    tg_entry(sig, qty, notional, balance)
                    entered.add(sym)
                    open_count += 1
                    time.sleep(0.5)

                except Exception as e:
                    log.error(f"Order FAILED {sym}: {e}")
                    if "stop" in str(e).lower() or "liquidat" in str(e).lower():
                        sl_cooldown[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot V16 detenido</b>")
            break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: 10 errores.</b>")
                break

        time.sleep(max(0, LOOP_SECONDS - (time.time() - t0)))

if __name__ == "__main__":
    main()
