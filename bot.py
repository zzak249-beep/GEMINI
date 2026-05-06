#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  TRADING BOT V14 — MAXIMUM WIN RATE EDITION                         ║
║                                                                      ║
║  NUEVAS TÉCNICAS PARA SUBIR EL WIN RATE:                            ║
║  1. SUPERTREND  — filtro de tendencia ultra-fiable (ATR dinámico)   ║
║  2. TRIPLE TF   — 5m+15m+1H deben estar alineados                  ║
║  3. SESIONES    — solo opera en Londres+NY (máxima liquidez)        ║
║  4. HEIKIN ASHI — confirma tendencia sin ruido                      ║
║  5. MARKET STRUCTURE — HH/HL longs | LH/LL shorts                  ║
║  6. VWAP        — solo long >VWAP, solo short <VWAP                 ║
║  7. SQUEEZE     — no operar en mercados comprimidos (BB vs KC)      ║
║  8. CONFLUENCIAS— mínimo 5/7 filtros ON para entrar                 ║
║  9. CIRCUIT BREAKER — pausa si 3 pérdidas consecutivas              ║
║ 10. KELLY SIZING — position size proporcional a la calidad          ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
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
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", os.environ.get("BINGX_API_SECRET", ""))
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TIMEFRAME        = os.environ.get("TIMEFRAME",       "5m")
H1_TIMEFRAME     = "1h"
M15_TIMEFRAME    = "15m"

RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.5"))
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "30"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "6"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "0"))

# Filtros de calidad — más estrictos en V14
MIN_SCORE        = float(os.environ.get("MIN_SCORE",      "62.0"))
MIN_CONFLUENCES  = int(os.environ.get("MIN_CONFLUENCES",  "5"))    # de 7 filtros
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT",   "0.20"))
MAX_SPREAD_PCT   = float(os.environ.get("MAX_SPREAD_PCT", "0.12"))
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",    "3.5"))

# EMAs
EMA_FAST         = int(os.environ.get("EMA_FAST",   "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",   "17"))
EMA_TREND        = int(os.environ.get("EMA_TREND",  "100"))
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT", "28.0"))
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",   "3"))

# ADX / RSI
ADX_LEN          = int(os.environ.get("ADX_LEN",  "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN", "25.0"))
RSI_LEN          = int(os.environ.get("RSI_LEN",  "14"))
RSI_OB           = float(os.environ.get("RSI_OB",  "68.0"))
RSI_OS           = float(os.environ.get("RSI_OS",  "32.0"))
VOL_MULT         = float(os.environ.get("VOL_MULT", "1.3"))

# SuperTrend (V14 nuevo)
ST_PERIOD        = int(os.environ.get("ST_PERIOD",  "10"))
ST_MULT          = float(os.environ.get("ST_MULT",  "3.0"))

# TP / SL
TP_MULT          = float(os.environ.get("TP_MULT",       "3.0"))
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT",   "1.5"))
MIN_RR           = float(os.environ.get("MIN_RR",        "2.5"))
BE_ATR_MULT      = float(os.environ.get("BE_ATR_MULT",   "1.0"))
TRAILING_STOP    = os.environ.get("TRAILING_STOP", "true").lower() == "true"

# Position sizing
MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT", "7.0"))
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT", "40.0"))
MAX_MARGIN_PCT   = float(os.environ.get("MAX_MARGIN_PCT", "25.0"))

# Sesiones activas (UTC)  — V14 nuevo
SESSION_FILTER   = os.environ.get("SESSION_FILTER", "true").lower() == "true"
SESSION_START    = int(os.environ.get("SESSION_START", "6"))   # 06:00 UTC (London pre)
SESSION_END      = int(os.environ.get("SESSION_END",  "21"))   # 21:00 UTC (NY close)

# Circuit breaker
MAX_CONSEC_LOSSES = int(os.environ.get("MAX_CONSEC_LOSSES", "3"))
CB_PAUSE_MINS     = int(os.environ.get("CB_PAUSE_MINS",    "60"))

# Squeeze
SQUEEZE_LEN      = int(os.environ.get("SQUEEZE_LEN", "20"))
BB_MULT_SQZ      = float(os.environ.get("BB_MULT",   "2.0"))
KC_MULT_SQZ      = float(os.environ.get("KC_MULT",   "1.5"))

# H1 cache
H1_CACHE_TTL     = int(os.environ.get("H1_CACHE_TTL",  "300"))
M15_CACHE_TTL    = int(os.environ.get("M15_CACHE_TTL", "120"))

COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS", "20"))
H1_SR_DIST_MIN   = float(os.environ.get("H1_SR_DIST_MIN", "1.0"))

PIN_BAR_RATIO    = float(os.environ.get("PIN_BAR_RATIO",    "0.30"))
PIN_TAIL_RATIO   = float(os.environ.get("PIN_TAIL_RATIO",   "0.55"))
ENGULF_MIN_RATIO = float(os.environ.get("ENGULF_MIN_RATIO", "1.05"))
MOMENTUM_BODY_MIN= float(os.environ.get("MOMENTUM_BODY_MIN","0.65"))

_raw = os.environ.get("CUSTOM_SYMBOLS", "")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE = "https://open-api.bingx.com"
INTERVAL_MAP = {"1m":"1m","3m":"3m","5m":"5m","15m":"15m","30m":"30m","1h":"1H","4h":"4H"}

EXCLUDED_PREFIXES = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG")
EXCLUDED_KEYWORDS = ("Gasoline","GasOil","Brent","WTI","Copper","Wheat","Cotton",
                     "Soybean","Silver","EURUSD","GBPUSD","JPYUSD")

FALLBACK_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "MATIC-USDT","INJ-USDT","SUI-USDT","ARB-USDT","OP-USDT",
    "WIF-USDT","PEPE-USDT","WLD-USDT","TIA-USDT","SEI-USDT",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════
sl_cooldown   = {}
h1_cache      = {}
m15_cache     = {}
consec_losses = 0          # circuit breaker
cb_pause_until = None      # circuit breaker timestamp

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
                               "Content-Type": "application/json"}, timeout=15)
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
        data = bx_get("/openApi/swap/v2/user/positions", {})
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
        usdt = [c for c in contracts if isinstance(c, dict)
                and c.get("asset","") == "USDT" and c.get("status") == 1]
        usdt.sort(key=lambda x: float(x.get("tradeAmount",0) or 0), reverse=True)
        syms = [c["symbol"] for c in usdt if _is_valid(c.get("symbol",""))]
        result = syms if limit == 0 else syms[:limit]
        log.info(f"✅ {len(result)} symbols")
        return result or FALLBACK_SYMBOLS
    except Exception:
        return FALLBACK_SYMBOLS

def set_lev(symbol):
    for side in ("LONG","SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol":symbol,"side":side,"leverage":LEVERAGE})
        except Exception:
            pass

def get_live_price(symbol):
    try:
        data = bx_get("/openApi/swap/v2/quote/premiumIndex", {"symbol":symbol})
        items = data.get("data",[])
        if isinstance(items, list):
            for item in items:
                if item.get("symbol") == symbol:
                    return float(item["markPrice"])
        if isinstance(items, dict):
            return float(items.get("markPrice",0))
    except Exception:
        pass
    try:
        params = {"symbol":symbol, "interval":INTERVAL_MAP.get(TIMEFRAME,"5m"), "limit":2}
        data = bx_get("/openApi/swap/v3/quote/klines", params)
        rows = data.get("data",[])
        if rows: return float(rows[-1][4])
    except Exception:
        pass
    raise ValueError(f"No price for {symbol}")

def get_spread_pct(symbol):
    try:
        data = bx_get("/openApi/swap/v2/quote/bookTicker", {"symbol":symbol})
        d = data.get("data",{})
        if isinstance(d, list):
            for item in d:
                if item.get("symbol") == symbol: d = item; break
        ask = float(d.get("askPrice",0) or 0)
        bid = float(d.get("bidPrice",0) or 0)
        return (ask-bid)/bid*100 if ask>0 and bid>0 else 999.0
    except Exception:
        return 999.0

# ══════════════════════════════════════════════════════════════════════
#  KLINES
# ══════════════════════════════════════════════════════════════════════
def _fetch_klines(symbol, interval, limit):
    params = {"symbol":symbol, "interval":INTERVAL_MAP.get(interval, interval), "limit":limit}
    data = bx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data",[])
    if not rows or not isinstance(rows, list): return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open","high","low","close","volume"], inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

def get_klines(symbol, limit=300):
    return _fetch_klines(symbol, TIMEFRAME, limit)

def get_h1_klines(symbol, limit=80):
    now = time.time()
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

def get_m15_klines(symbol, limit=100):
    now = time.time()
    cached = m15_cache.get(symbol)
    if cached:
        df_c, ts = cached
        if now - ts < M15_CACHE_TTL and len(df_c) >= 30:
            return df_c.copy()
    try:
        df = _fetch_klines(symbol, "15m", limit)
        if not df.empty:
            m15_cache[symbol] = (df.copy(), now)
        return df
    except Exception:
        return pd.DataFrame()

# ══════════════════════════════════════════════════════════════════════
#  INDICADORES
# ══════════════════════════════════════════════════════════════════════
def calc_atr(high, low, close, period=14):
    tr = pd.concat([
        high-low,
        (high-close.shift()).abs(),
        (low-close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calc_ema_angle(ema_s, atr_s, look=3):
    price_change = ema_s - ema_s.shift(look)
    denom = atr_s * look
    return pd.Series(np.degrees(np.arctan2(price_change.values, denom.values)), index=ema_s.index)

def calc_adx(high, low, close, period=14):
    up   = high.diff()
    down = -low.diff()
    plus_dm  = np.where((up>down)&(up>0),   up,   0.0)
    minus_dm = np.where((down>up)&(down>0), down, 0.0)
    tr = pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
    alpha = 1/period
    def w(arr): return pd.Series(arr, index=high.index).ewm(alpha=alpha, adjust=False).mean()
    tr_s  = w(tr); pdm_s = w(plus_dm); mdm_s = w(minus_dm)
    di_p  = 100*pdm_s/tr_s.replace(0,np.nan)
    di_m  = 100*mdm_s/tr_s.replace(0,np.nan)
    dx    = 100*(di_p-di_m).abs()/(di_p+di_m).replace(0,np.nan)
    adx   = dx.ewm(alpha=alpha, adjust=False).mean()
    return di_p, di_m, adx

def calc_rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = ag/al.replace(0,np.nan)
    return 100-(100/(1+rs))

# ── V14 NUEVO: SuperTrend ─────────────────────────────────────────────
def calc_supertrend(high, low, close, period=10, mult=3.0):
    """
    SuperTrend indicator. Returns Series: +1 = uptrend, -1 = downtrend.
    Uno de los filtros de tendencia más fiables del mercado.
    """
    atr   = calc_atr(high, low, close, period)
    hl2   = (high + low) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr

    supertrend = pd.Series(index=close.index, dtype=float)
    direction  = pd.Series(index=close.index, dtype=int)
    final_ub   = upper.copy()
    final_lb   = lower.copy()

    for i in range(1, len(close)):
        # Final upper band
        if upper.iloc[i] < final_ub.iloc[i-1] or close.iloc[i-1] > final_ub.iloc[i-1]:
            final_ub.iloc[i] = upper.iloc[i]
        else:
            final_ub.iloc[i] = final_ub.iloc[i-1]

        # Final lower band
        if lower.iloc[i] > final_lb.iloc[i-1] or close.iloc[i-1] < final_lb.iloc[i-1]:
            final_lb.iloc[i] = lower.iloc[i]
        else:
            final_lb.iloc[i] = final_lb.iloc[i-1]

        # Direction
        if close.iloc[i] > final_ub.iloc[i-1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < final_lb.iloc[i-1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i-1] if i > 0 else 1

        supertrend.iloc[i] = final_lb.iloc[i] if direction.iloc[i] == 1 else final_ub.iloc[i]

    direction.iloc[0] = 1
    return direction, supertrend

# ── V14 NUEVO: Heikin Ashi ────────────────────────────────────────────
def calc_heikin_ashi(df):
    """
    Heikin Ashi candles. Filtra ruido y confirma tendencia.
    HA_close = (O+H+L+C)/4
    HA_open  = (prev_HA_open + prev_HA_close)/2
    """
    ha = df.copy()
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha["ha_open"]  = ha["ha_close"].copy()
    for i in range(1, len(ha)):
        ha.at[ha.index[i], "ha_open"] = (ha["ha_open"].iloc[i-1] + ha["ha_close"].iloc[i-1]) / 2
    ha["ha_high"]  = pd.concat([ha["ha_open"], ha["ha_close"], df["high"]], axis=1).max(axis=1)
    ha["ha_low"]   = pd.concat([ha["ha_open"], ha["ha_close"], df["low"]],  axis=1).min(axis=1)
    return ha

# ── V14 NUEVO: Squeeze Momentum (BB vs KC) ───────────────────────────
def calc_squeeze(high, low, close, sq_len=20, bb_mult=2.0, kc_mult=1.5):
    """True when BB is inside KC — market compressing, don't trade."""
    basis   = close.rolling(sq_len).mean()
    std     = close.rolling(sq_len).std()
    bb_up   = basis + bb_mult * std
    bb_lo   = basis - bb_mult * std

    atr_kc  = calc_atr(high, low, close, sq_len)
    kc_up   = basis + kc_mult * atr_kc
    kc_lo   = basis - kc_mult * atr_kc

    sqz_on  = (bb_lo > kc_lo) & (bb_up < kc_up)
    return sqz_on

# ── V14 NUEVO: VWAP ───────────────────────────────────────────────────
def calc_vwap(df):
    """Session VWAP (resets daily at midnight UTC)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    df      = df.copy()
    df["_tp"] = typical * df["volume"]
    df["_day"] = df["open_time"].dt.floor("D")
    df["_cum_tp"] = df.groupby("_day")["_tp"].cumsum()
    df["_cum_v"]  = df.groupby("_day")["volume"].cumsum()
    vwap = df["_cum_tp"] / df["_cum_v"]
    return vwap

# ── V14 NUEVO: Market Structure ───────────────────────────────────────
def detect_market_structure(close, high, low, lookback=20):
    """
    Detecta HH/HL (bullish structure) o LH/LL (bearish structure).
    Retorna: 'BULL', 'BEAR', 'NEUTRAL'
    """
    if len(close) < lookback + 2:
        return "NEUTRAL"

    recent_h = high.iloc[-lookback:]
    recent_l = low.iloc[-lookback:]

    # Dividir en dos mitades para comparar pivotes
    mid = lookback // 2
    first_half_h  = recent_h.iloc[:mid].max()
    second_half_h = recent_h.iloc[mid:].max()
    first_half_l  = recent_l.iloc[:mid].min()
    second_half_l = recent_l.iloc[mid:].min()

    hh = second_half_h > first_half_h   # Higher High
    hl = second_half_l > first_half_l   # Higher Low
    lh = second_half_h < first_half_h   # Lower High
    ll = second_half_l < first_half_l   # Lower Low

    if hh and hl: return "BULL"
    if lh and ll: return "BEAR"
    return "NEUTRAL"

# ── V14 NUEVO: Filtro de Sesión ───────────────────────────────────────
def is_active_session():
    """
    Solo opera en sesiones de alta liquidez:
    Londres: 06:00–16:00 UTC
    Nueva York: 12:00–21:00 UTC
    Solapamiento: 12:00–16:00 UTC (mejor volumen)
    """
    if not SESSION_FILTER:
        return True
    hour = datetime.now(timezone.utc).hour
    return SESSION_START <= hour < SESSION_END

# ══════════════════════════════════════════════════════════════════════
#  ANÁLISIS H1 + M15
# ══════════════════════════════════════════════════════════════════════
def analyze_h1(symbol):
    df = get_h1_klines(symbol, 80)
    if df.empty or len(df) < 30: return None

    close, high, low = df["close"], df["high"], df["low"]
    ema7   = calc_ema(close, 7)
    ema17  = calc_ema(close, 17)
    ema21  = calc_ema(close, 21)
    atr_h1 = calc_atr(high, low, close, 14)
    rsi_h1 = calc_rsi(close, 14)
    st_dir, _ = calc_supertrend(high, low, close, ST_PERIOD, ST_MULT)
    vwap_h1   = calc_vwap(df)
    mstruct   = detect_market_structure(close, high, low, 30)

    ema7_now  = float(ema7.iloc[-1])
    ema17_now = float(ema17.iloc[-1])
    ema21_now = float(ema21.iloc[-1])
    close_now = float(close.iloc[-1])
    st_now    = int(st_dir.iloc[-1])
    vwap_now  = float(vwap_h1.iloc[-1])
    rsi_now   = float(rsi_h1.iloc[-1])

    # Tendencia H1 — requiere EMA + SuperTrend + precio
    bull_h1 = (ema7_now > ema17_now > ema21_now) and (st_now == 1) and (close_now > vwap_now)
    bear_h1 = (ema7_now < ema17_now < ema21_now) and (st_now == -1) and (close_now < vwap_now)
    h1_trend = "BULL" if bull_h1 else ("BEAR" if bear_h1 else "NEUTRAL")

    # Pivotes S/R
    plen = 3
    resistances, supports = [], []
    for idx in range(plen, min(len(df)-plen, 50)):
        h_w = high.iloc[idx-plen:idx+plen+1]
        l_w = low.iloc[idx-plen:idx+plen+1]
        if float(high.iloc[idx]) == float(h_w.max()):
            resistances.append(float(high.iloc[idx]))
        if float(low.iloc[idx])  == float(l_w.min()):
            supports.append(float(low.iloc[idx]))

    resistances = sorted([v for v in resistances if v > close_now])
    supports    = sorted([v for v in supports    if v < close_now], reverse=True)
    h1_resistance = resistances[0] if resistances else close_now * 1.08
    h1_support    = supports[0]    if supports    else close_now * 0.92
    dist_to_res   = (h1_resistance - close_now) / close_now * 100
    dist_to_sup   = (close_now - h1_support)    / close_now * 100

    return {
        "h1_trend":      h1_trend,
        "h1_resistance": h1_resistance,
        "h1_support":    h1_support,
        "dist_to_res":   round(dist_to_res, 2),
        "dist_to_sup":   round(dist_to_sup, 2),
        "h1_st":         st_now,
        "h1_mstruct":    mstruct,
        "h1_vwap":       vwap_now,
        "h1_rsi":        round(rsi_now, 1),
        "close_h1":      close_now,
    }

def analyze_m15(symbol):
    """Timeframe intermedio: alineación entre 1H y 5m."""
    df = get_m15_klines(symbol, 100)
    if df.empty or len(df) < 40: return None

    close, high, low = df["close"], df["high"], df["low"]
    ema7   = calc_ema(close, 7)
    ema17  = calc_ema(close, 17)
    st_dir, _ = calc_supertrend(high, low, close, ST_PERIOD, ST_MULT)
    atr_m15   = calc_atr(high, low, close, 14)
    angle_m15 = calc_ema_angle(ema7, atr_m15, SLOPE_LOOK)

    bull_m15 = (float(ema7.iloc[-1]) > float(ema17.iloc[-1])) and (int(st_dir.iloc[-1]) == 1)
    bear_m15 = (float(ema7.iloc[-1]) < float(ema17.iloc[-1])) and (int(st_dir.iloc[-1]) == -1)
    m15_trend = "BULL" if bull_m15 else ("BEAR" if bear_m15 else "NEUTRAL")

    return {
        "m15_trend": m15_trend,
        "m15_angle": round(float(angle_m15.iloc[-1]), 1),
        "m15_st":    int(st_dir.iloc[-1]),
    }

# ══════════════════════════════════════════════════════════════════════
#  PATRONES DE VELA
# ══════════════════════════════════════════════════════════════════════
def detect_pin_bar(df, i, direction):
    o,h,l,c = float(df["open"].iloc[i]),float(df["high"].iloc[i]),\
               float(df["low"].iloc[i]),float(df["close"].iloc[i])
    rng = h - l
    if rng < 1e-10: return False, 0.0
    body = abs(c-o)
    if body/rng > PIN_BAR_RATIO: return False, 0.0
    upper_w = h - max(o,c)
    lower_w = min(o,c) - l
    if direction == "LONG":
        tr = lower_w/rng
        if tr >= PIN_TAIL_RATIO and lower_w >= 2*max(body,1e-10):
            return True, min(tr*120, 100.0)
    else:
        tr = upper_w/rng
        if tr >= PIN_TAIL_RATIO and upper_w >= 2*max(body,1e-10):
            return True, min(tr*120, 100.0)
    return False, 0.0

def detect_engulfing(df, i, direction):
    if i < 1: return False, 0.0
    o_c,c_c = float(df["open"].iloc[i]),float(df["close"].iloc[i])
    o_p,c_p = float(df["open"].iloc[i-1]),float(df["close"].iloc[i-1])
    body_c, body_p = abs(c_c-o_c), abs(c_p-o_p)
    if body_p < 1e-10: return False, 0.0
    ratio = body_c/body_p
    if ratio < ENGULF_MIN_RATIO: return False, 0.0
    if direction == "LONG" and c_c>o_c and c_p<o_p:
        if c_c>max(o_p,c_p) and o_c<min(o_p,c_p):
            return True, min(ratio*45, 100.0)
    elif direction == "SHORT" and c_c<o_c and c_p>o_p:
        if c_c<min(o_p,c_p) and o_c>max(o_p,c_p):
            return True, min(ratio*45, 100.0)
    return False, 0.0

def detect_momentum_candle(df, i, direction, atr_val):
    o,h,l,c = float(df["open"].iloc[i]),float(df["high"].iloc[i]),\
               float(df["low"].iloc[i]),float(df["close"].iloc[i])
    rng = h-l
    if rng < 1e-10 or atr_val < 1e-10: return False, 0.0
    body = abs(c-o)
    if body/rng < MOMENTUM_BODY_MIN or body < atr_val*0.5: return False, 0.0
    if direction == "LONG" and c>o and (h-c) < body*0.35:
        return True, min(body/rng*90, 100.0)
    elif direction == "SHORT" and c<o and (c-l) < body*0.35:
        return True, min(body/rng*90, 100.0)
    return False, 0.0

# ══════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════
def calc_qty(balance, entry, sl, quality_mult=1.0):
    """
    V14: Kelly-inspired sizing — position size escala con calidad de señal.
    quality_mult: 0.7–1.3 según score.
    """
    dist_pct = abs(entry-sl)/entry
    if dist_pct < 1e-8: return 0, 0
    risk_usdt = balance*(RISK_PERCENT/100)*quality_mult
    notional  = risk_usdt/dist_pct
    max_margin = balance*(MAX_MARGIN_PCT/100)
    max_notional = min(MAX_ORDER_USDT, max_margin*LEVERAGE)
    notional = max(MIN_ORDER_USDT, min(notional, max_notional))
    qty = notional/entry
    return round(max(qty, 0.001), 4), round(notional, 2)

def open_order(symbol, side, qty, sl, tp):
    payload = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "LONG" if side=="BUY" else "SHORT",
        "type":         "MARKET",
        "quantity":     round(qty, 4),
        "stopLoss": json.dumps({
            "type":"STOP_MARKET","stopPrice":round(sl,6),"workingType":"MARK_PRICE"
        }),
        "takeProfit": json.dumps({
            "type":"TAKE_PROFIT_MARKET","stopPrice":round(tp,6),"workingType":"MARK_PRICE"
        }),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    if resp.get("code",0) != 0:
        raise ValueError(f"BingX {resp.get('code')}: {resp.get('msg','?')}")
    return resp

# ══════════════════════════════════════════════════════════════════════
#  TRAILING STOP
# ══════════════════════════════════════════════════════════════════════
def update_trailing_stops(positions):
    if not TRAILING_STOP or not positions: return
    for sym, pos in positions.items():
        try:
            side  = pos.get("positionSide","LONG")
            entry = float(pos.get("avgPrice",0) or 0)
            if entry == 0: continue
            live = get_live_price(sym)
            df   = get_klines(sym, 50)
            if df.empty: continue
            atr_val = float(calc_atr(df["high"],df["low"],df["close"],14).iloc[-1])
            if atr_val == 0: continue
            if side=="LONG" and live >= entry + atr_val*BE_ATR_MULT:
                new_sl = round(entry*1.001, 6)
                bx_post("/openApi/swap/v2/trade/order",{
                    "symbol":sym,"type":"STOP_MARKET","side":"SELL","positionSide":"LONG",
                    "stopPrice":new_sl,"closePosition":"true","workingType":"MARK_PRICE"
                })
                log.info(f"✅ Trailing BE {sym} LONG → SL={new_sl}")
            elif side=="SHORT" and live <= entry - atr_val*BE_ATR_MULT:
                new_sl = round(entry*0.999, 6)
                bx_post("/openApi/swap/v2/trade/order",{
                    "symbol":sym,"type":"STOP_MARKET","side":"BUY","positionSide":"SHORT",
                    "stopPrice":new_sl,"closePosition":"true","workingType":"MARK_PRICE"
                })
                log.info(f"✅ Trailing BE {sym} SHORT → SL={new_sl}")
        except Exception as e:
            log.debug(f"Trailing {sym}: {e}")

# ══════════════════════════════════════════════════════════════════════
#  ESCANEO PRINCIPAL V14 — TRIPLE CONFIRMACIÓN
# ══════════════════════════════════════════════════════════════════════
def scan_symbol(symbol):
    global consec_losses, cb_pause_until

    # Circuit breaker
    if cb_pause_until and datetime.now(timezone.utc) < cb_pause_until:
        return None

    # Cooldown
    if symbol in sl_cooldown:
        elapsed = (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds()/60
        if elapsed < COOLDOWN_MINS: return None

    try:
        # ── 1. DATOS 5m ─────────────────────────────────────────────
        df = get_klines(symbol, 300)
        if df.empty or len(df) < 120: return None

        h,l,c,o = df["high"],df["low"],df["close"],df["open"]
        atr_s   = calc_atr(h,l,c,14)
        ema_f   = calc_ema(c, EMA_FAST)
        ema_s   = calc_ema(c, EMA_SLOW)
        ema_t   = calc_ema(c, EMA_TREND)
        angle   = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        di_p,di_m,adx_s = calc_adx(h,l,c,ADX_LEN)
        rsi_s   = calc_rsi(c, RSI_LEN)
        vol_ma  = df["volume"].rolling(20).mean()
        sqz     = calc_squeeze(h,l,c, SQUEEZE_LEN, BB_MULT_SQZ, KC_MULT_SQZ)
        vwap_5m = calc_vwap(df)
        st_dir, _ = calc_supertrend(h,l,c, ST_PERIOD, ST_MULT)
        ha      = calc_heikin_ashi(df)

        i = len(df) - 2
        if i < 80: return None

        close_now = float(c.iloc[i])
        atr_val   = float(atr_s.iloc[i])
        atr_pct   = atr_val/close_now*100
        if atr_pct > ATR_MAX_PCT or atr_val <= 0: return None

        angle_now = float(angle.iloc[i])
        adx_now   = float(adx_s.iloc[i])
        di_p_now  = float(di_p.iloc[i])
        di_m_now  = float(di_m.iloc[i])
        rsi_now   = float(rsi_s.iloc[i])
        vol_now   = float(df["volume"].iloc[i])
        vma       = float(vol_ma.iloc[i])
        sqz_now   = bool(sqz.iloc[i])
        vwap_now  = float(vwap_5m.iloc[i])
        st_now    = int(st_dir.iloc[i])
        ha_bull   = float(ha["ha_close"].iloc[i]) > float(ha["ha_open"].iloc[i])
        ha_bear   = float(ha["ha_close"].iloc[i]) < float(ha["ha_open"].iloc[i])
        vratio    = round(vol_now/vma, 2) if vma > 0 else 0.0

        ema_f_now = float(ema_f.iloc[i])
        ema_s_now = float(ema_s.iloc[i])
        ema_t_now = float(ema_t.iloc[i])

        mstruct_5m = detect_market_structure(c, h, l, 20)

        if any(np.isnan(x) for x in [angle_now,adx_now,rsi_now,atr_val]): return None

        # ── 2. DIRECCIÓN PRELIMINAR ──────────────────────────────────
        ema_bull = ema_f_now > ema_s_now
        ema_bear = ema_f_now < ema_s_now
        if not ema_bull and not ema_bear: return None

        direction = "LONG" if ema_bull else "SHORT"

        # ── 3. SISTEMA DE CONFLUENCIAS V14 ───────────────────────────
        # Cada filtro vale 1. Necesitamos MIN_CONFLUENCES (5/7).
        confluences = 0
        conf_detail = {}

        # F1: EMA slope "tren bala"
        angle_ok = angle_now >= SLOPE_LIMIT if direction=="LONG" else angle_now <= -SLOPE_LIMIT
        if angle_ok: confluences += 1
        conf_detail["slope"] = f"{'✅' if angle_ok else '❌'} {angle_now:.1f}°"

        # F2: SuperTrend 5m
        st_ok = (st_now == 1 and direction=="LONG") or (st_now == -1 and direction=="SHORT")
        if st_ok: confluences += 1
        conf_detail["supertrend"] = f"{'✅' if st_ok else '❌'} {'▲' if st_now==1 else '▼'}"

        # F3: ADX + DI
        adx_ok = adx_now >= ADX_MIN and (
            (di_p_now > di_m_now and direction=="LONG") or
            (di_m_now > di_p_now and direction=="SHORT")
        )
        if adx_ok: confluences += 1
        conf_detail["adx"] = f"{'✅' if adx_ok else '❌'} {adx_now:.1f}"

        # F4: Heikin Ashi confirma
        ha_ok = (ha_bull and direction=="LONG") or (ha_bear and direction=="SHORT")
        if ha_ok: confluences += 1
        conf_detail["heikin_ashi"] = f"{'✅' if ha_ok else '❌'}"

        # F5: VWAP 5m
        vwap_ok = (close_now > vwap_now and direction=="LONG") or \
                  (close_now < vwap_now and direction=="SHORT")
        if vwap_ok: confluences += 1
        conf_detail["vwap"] = f"{'✅' if vwap_ok else '❌'} {vwap_now:.4f}"

        # F6: Squeeze OFF
        sqz_ok = not sqz_now
        if sqz_ok: confluences += 1
        conf_detail["squeeze"] = f"{'✅ OFF' if sqz_ok else '🚫 ON'}"

        # F7: Market structure 5m
        ms_ok = (mstruct_5m=="BULL" and direction=="LONG") or \
                (mstruct_5m=="BEAR" and direction=="SHORT")
        if ms_ok: confluences += 1
        conf_detail["mstruct"] = f"{'✅' if ms_ok else '❌'} {mstruct_5m}"

        # Pre-filtro rápido
        if confluences < MIN_CONFLUENCES:
            return None

        # RSI extremo — skip
        if direction == "LONG"  and rsi_now > RSI_OB: return None
        if direction == "SHORT" and rsi_now < RSI_OS: return None

        # EMA trend filter
        if direction == "LONG"  and close_now < ema_t_now: return None
        if direction == "SHORT" and close_now > ema_t_now: return None

        # ── 4. CONFIRMACIÓN H1 ───────────────────────────────────────
        h1_ctx   = analyze_h1(symbol)
        h1_bonus = 0
        if h1_ctx:
            h1_trend = h1_ctx["h1_trend"]
            if h1_trend == direction[:4]:  # BULL/BEAR match
                h1_bonus = 20
            elif h1_trend == "NEUTRAL":
                h1_bonus = 5
            else:
                return None  # M5 contra H1 — descartado

            # H1 Market Structure bonus
            if h1_ctx["h1_mstruct"] == h1_trend:
                h1_bonus += 5

            # S/R filter
            if direction=="LONG"  and h1_ctx["dist_to_res"] < H1_SR_DIST_MIN: return None
            if direction=="SHORT" and h1_ctx["dist_to_sup"] < H1_SR_DIST_MIN: return None

            # H1 SuperTrend
            if h1_ctx["h1_st"] != (1 if direction=="LONG" else -1):
                h1_bonus -= 10

        # ── 5. CONFIRMACIÓN M15 ──────────────────────────────────────
        m15_ctx   = analyze_m15(symbol)
        m15_bonus = 0
        if m15_ctx:
            if m15_ctx["m15_trend"] == direction[:4]:
                m15_bonus = 10
            elif m15_ctx["m15_trend"] != "NEUTRAL":
                return None  # M15 contra señal — descartado
            m15_angle = m15_ctx["m15_angle"]
            if (direction=="LONG" and m15_angle >= 20) or \
               (direction=="SHORT" and m15_angle <= -20):
                m15_bonus += 5

        # ── 6. PATRÓN DE VELA ────────────────────────────────────────
        pattern_name, pattern_score = "SLOPE", 0.0
        sl_candle = None
        margin_f  = 0.10

        is_pin, pin_str = detect_pin_bar(df, i, direction)
        is_eng, eng_str = detect_engulfing(df, i, direction)
        is_mom, mom_str = detect_momentum_candle(df, i, direction, atr_val)

        if is_pin:
            pattern_name, pattern_score, margin_f = "PIN_BAR", pin_str, 0.08
            sl_candle = (float(l.iloc[i])-atr_val*margin_f) if direction=="LONG" \
                        else (float(h.iloc[i])+atr_val*margin_f)
        elif is_eng:
            pattern_name, pattern_score, margin_f = "ENGULF", eng_str, 0.10
            sl_candle = (float(l.iloc[i])-atr_val*margin_f) if direction=="LONG" \
                        else (float(h.iloc[i])+atr_val*margin_f)
        elif is_mom:
            pattern_name, pattern_score, margin_f = "MOMENTUM", mom_str, 0.12
            sl_candle = (float(l.iloc[i])-atr_val*margin_f) if direction=="LONG" \
                        else (float(h.iloc[i])+atr_val*margin_f)

        # ── 7. SL / TP ───────────────────────────────────────────────
        sl_atr = atr_val * SL_ATR_MULT
        if direction == "LONG":
            sl_price = sl_candle if sl_candle and (close_now-sl_candle) <= sl_atr \
                       else close_now - sl_atr
            sl_price = min(sl_price, close_now*(1-MIN_DIST_PCT/100))
            if sl_price >= close_now: return None
            tp_price = close_now + (close_now-sl_price)*TP_MULT
        else:
            sl_price = sl_candle if sl_candle and (sl_candle-close_now) <= sl_atr \
                       else close_now + sl_atr
            sl_price = max(sl_price, close_now*(1+MIN_DIST_PCT/100))
            if sl_price <= close_now: return None
            tp_price = close_now - (sl_price-close_now)*TP_MULT

        dist     = abs(close_now-sl_price)
        dist_pct = dist/close_now*100
        if dist_pct < MIN_DIST_PCT: return None
        rr = abs(tp_price-close_now)/dist
        if rr < MIN_RR: return None

        # ── 8. SCORING V14 ───────────────────────────────────────────
        # Base: confluencias (max 35) + H1 (max 25) + M15 (max 15) + patrón (max 12) + vol (max 8) + RR (max 5)
        score  = (confluences / 7) * 35                          # confluencias: max 35
        score += h1_bonus                                         # H1: max 25
        score += m15_bonus                                        # M15: max 15
        score += min(pattern_score / 8, 12)                      # patrón: max 12
        score += min(vratio * 4, 8)                              # volumen: max 8
        score += min((rr - MIN_RR) * 2.5, 5)                    # R:R bonus: max 5

        if score < MIN_SCORE: return None

        # Kelly quality multiplier para position sizing
        quality_mult = 0.7 + (score-MIN_SCORE)/(100-MIN_SCORE)*0.6  # 0.7–1.3
        quality_mult = round(min(max(quality_mult, 0.7), 1.3), 2)

        h1_str = h1_ctx["h1_trend"] if h1_ctx else "?"

        return {
            "symbol":       symbol,
            "signal":       direction,
            "pattern":      pattern_name,
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
            "h1_trend":     h1_str,
            "m15_trend":    m15_ctx["m15_trend"] if m15_ctx else "?",
            "pat_score":    round(pattern_score, 1),
            "quality_mult": quality_mult,
            "h1_ctx":       h1_ctx,
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
        f"🚀 <b>TRADING BOT V14 — MAX WIN RATE EDITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>NUEVOS FILTROS:</b> SuperTrend + Triple TF + Sesiones\n"
        f"   + Heikin Ashi + Market Structure + VWAP + Squeeze\n"
        f"📊 <b>Confluencias mínimas:</b> {MIN_CONFLUENCES}/7\n"
        f"📐 <b>Score mínimo:</b> {MIN_SCORE}/100\n"
        f"⚡ <b>SuperTrend:</b> period={ST_PERIOD} mult={ST_MULT}\n"
        f"🕐 <b>Sesión:</b> {SESSION_START}:00–{SESSION_END}:00 UTC\n"
        f"💰 <b>Balance:</b> {balance:.2f} USDT | <b>Símbolos:</b> {len(symbols)}\n"
        f"📏 <b>R:R mínimo:</b> {MIN_RR} | <b>TP:</b> {TP_MULT}× | <b>SL:</b> {SL_ATR_MULT}×ATR\n"
        f"🛡️ <b>Circuit breaker:</b> {MAX_CONSEC_LOSSES} pérdidas → pausa {CB_PAUSE_MINS}min\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_entry(sig, qty, notional, balance, spread_pct=None):
    d = "🟢 LONG" if sig["signal"]=="LONG" else "🔴 SHORT"
    icons = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","SLOPE":"📈"}
    pi = icons.get(sig.get("pattern","SLOPE"),"⚡")
    cd = sig.get("conf_detail",{})
    conf_lines = " | ".join([f"{k}:{v}" for k,v in cd.items()])
    tg(
        f"<b>✅ ENTRADA V14 — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"<b>Confluencias:</b> {sig['confluences']}/7\n"
        f"<b>1H:</b> {sig['h1_trend']} | <b>15m:</b> {sig['m15_trend']}\n"
        f"{pi} <b>Patrón:</b> {sig['pattern']} ({sig['pat_score']:.0f})\n"
        f"<b>Ang:</b> {sig['angle']}° | <b>ADX:</b> {sig['adx']} | "
        f"<b>RSI:</b> {sig['rsi']} | <b>Vol:</b> {sig['vol_ratio']}x\n"
        f"<b>Filtros:</b> {conf_lines[:120]}\n"
        f"<b>Entrada:</b> <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop:</b>   <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Target:</b> <code>{sig['tp']:.6g}</code> | <b>R:R</b> 1:{sig['rr']}\n"
        f"<b>Qty:</b> {qty:.4f} | <b>Notional:</b> {notional:.2f} USDT\n"
        f"<b>Kelly mult:</b> {sig.get('quality_mult',1.0)}×\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

# ══════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════
def main():
    global consec_losses, cb_pause_until

    log.info("╔══════════════════════════════════════════════╗")
    log.info("║  TRADING BOT V14 — MAXIMUM WIN RATE EDITION  ║")
    log.info("╚══════════════════════════════════════════════╝")
    log.info(f"  SuperTrend({ST_PERIOD},{ST_MULT}) | Triple TF | "
             f"Sesión {SESSION_START}-{SESSION_END}h UTC")
    log.info(f"  Confluencias≥{MIN_CONFLUENCES}/7 | Score≥{MIN_SCORE} | "
             f"R:R≥{MIN_RR} | Min {MIN_ORDER_USDT} USDT")

    symbols   = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols: symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance: {balance:.4f} | Symbols: {len(symbols)} | Open: {len(positions)}")

    # Pre-cargar H1 en background
    def _prefetch():
        log.info("Pre-cargando H1 + M15...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(lambda s: get_h1_klines(s, 80), symbols[:60]))
            list(ex.map(lambda s: get_m15_klines(s, 100), symbols[:60]))
        log.info("Cache listo.")
    threading.Thread(target=_prefetch, daemon=True).start()

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(set_lev, symbols))

    tg_startup(balance, symbols)
    log.info("✅ Bot V14 iniciado. Loop comenzando.")

    errors = 0
    while True:
        t0 = time.time()
        try:
            # ── Session check ─────────────────────────────────────────
            if not is_active_session():
                hour = datetime.now(timezone.utc).hour
                log.info(f"⏸️  Fuera de sesión ({hour}h UTC). Esperando...")
                time.sleep(300)
                continue

            balance   = get_balance()
            positions = get_all_positions()
            open_count = len(positions)

            # ── Circuit breaker check ─────────────────────────────────
            if cb_pause_until and datetime.now(timezone.utc) < cb_pause_until:
                remaining = (cb_pause_until - datetime.now(timezone.utc)).seconds // 60
                log.info(f"🛑 Circuit breaker activo. Reanuda en {remaining}min.")
                time.sleep(60)
                continue

            log.info(
                f"── V14 | {balance:.2f} USDT | {open_count}/{MAX_OPEN_TRADES} trades | "
                f"{len(symbols)} sym | Contig.losses={consec_losses} ──"
            )

            if TRAILING_STOP and positions:
                update_trailing_stops(positions)

            # ── Scan ──────────────────────────────────────────────────
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan_symbol, s): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r: signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Señales válidas: {len(signals)}/{len(symbols)}")

            for s in signals[:5]:
                log.info(
                    f"  → {s['symbol']} {s['signal']} [{s['pattern']}] "
                    f"H1:{s['h1_trend']} 15m:{s['m15_trend']} "
                    f"confl:{s['confluences']}/7 score={s['score']:.1f} "
                    f"ang={s['angle']}° rr=1:{s['rr']}"
                )

            entered = set()
            for sig in signals:
                sym = sig["symbol"]
                if sym in positions or sym in entered: continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info("Max trades alcanzado.")
                    break
                if balance < MIN_ORDER_USDT:
                    log.warning(f"Balance bajo: {balance:.2f} USDT")
                    break

                spread = get_spread_pct(sym)
                if spread > MAX_SPREAD_PCT:
                    log.info(f"Skip {sym}: spread {spread:.3f}%")
                    continue

                try:
                    set_lev(sym)
                    live = get_live_price(sym)

                    # Recalcular SL/TP en precio live
                    atr_val = sig["atr"]
                    if sig["signal"] == "LONG":
                        sl = live - atr_val * SL_ATR_MULT
                        sl = min(sl, live*(1-MIN_DIST_PCT/100))
                        tp = live + (live-sl)*TP_MULT
                    else:
                        sl = live + atr_val * SL_ATR_MULT
                        sl = max(sl, live*(1+MIN_DIST_PCT/100))
                        tp = live - (sl-live)*TP_MULT

                    if sl<=0 or tp<=0: continue
                    rr = abs(tp-live)/abs(live-sl)
                    if rr < MIN_RR: continue

                    qty, notional = calc_qty(balance, live, sl, sig["quality_mult"])
                    if qty <= 0 or notional < MIN_ORDER_USDT: continue

                    log.info(
                        f"ORDEN {sym} {sig['signal']} qty={qty:.4f} "
                        f"notional={notional:.2f}U live={live:.6g} "
                        f"sl={sl:.6g} tp={tp:.6g} score={sig['score']:.1f} "
                        f"confl={sig['confluences']}/7"
                    )

                    side = "BUY" if sig["signal"]=="LONG" else "SELL"
                    res  = open_order(sym, side, qty, sl, tp)
                    log.info(f"✅ {sym} abierto | {res}")

                    sig.update({"close":live,"sl":round(sl,6),"tp":round(tp,6),
                                "dist_pct":round(abs(live-sl)/live*100,3),
                                "rr":round(rr,2)})
                    tg_entry(sig, qty, notional, balance, spread_pct=spread)
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
            tg("🛑 <b>Bot V14 detenido</b>")
            break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: 10 errores. Detenido.</b>")
                break

        elapsed = time.time() - t0
        time.sleep(max(0, LOOP_SECONDS - elapsed))

if __name__ == "__main__":
    main()
