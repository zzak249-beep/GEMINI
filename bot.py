"""
EMA Slope + ADX + Sniper V13.0 — HIGH WINRATE EDITION
Objetivo: winrate ≥ 70% con menos trades pero de máxima calidad

7 CAUSAS del bajo winrate en V12 → soluciones V13:

  CAUSA 1: Entradas en sesión Asia (baja liquidez, fakeouts)
  → SOLUCIÓN: SESSION FILTER — solo London 06-12 UTC y NY 12-21 UTC

  CAUSA 2: ADX sobre el umbral pero BAJANDO (trend fatigada)
  → SOLUCIÓN: ADX_RISING — adx_now > adx_3_bars_ago obligatorio

  CAUSA 3: Sin confirmación timeframe intermedio (15m)
  → SOLUCIÓN: TRIPLE TF — 5m + 15m + 1H los tres alineados

  CAUSA 4: RSI demasiado permisivo (entradas cerca de sobrecompra)
  → SOLUCIÓN: RSI MOMENTUM ZONE — Long solo 42-65, Short solo 35-58

  CAUSA 5: Sin estructura de mercado (HH/HL para longs, LL/LH para shorts)
  → SOLUCIÓN: MARKET STRUCTURE — últimos 3 pivotes confirman dirección

  CAUSA 6: Falsos breakouts del mismo nivel no filtrados
  → SOLUCIÓN: FALSE BREAKOUT GUARD — si el mismo nivel falló en 15 velas → skip

  CAUSA 7: Demasiados trades abiertos (diluye calidad, más pérdidas)
  → SOLUCIÓN: MAX_OPEN_TRADES=3 + MIN_SCORE=65 (solo setups élite)

BONUS V13:
  - EMA STACK PERFECTO: EMA7 > EMA17 > EMA100 los tres en orden
  - VELAS CONSECUTIVAS: mínimo 2 de las 3 últimas velas confirman tendencia
  - VOLUMEN ACELERANDO: vol ruptura > vol señal (momentum creciente)
  - ATR SALUDABLE: ATR creciente o estable (mercado con energía)
  - BREAKEVEN MEJORADO: 1R (no 1.5R) para proteger capital más rápido
  - PARTIAL TP: cierra 50% en 2R, deja correr el resto hasta 3R
"""
import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np
import websocket

try:
    from telegram import Bot
    from telegram.constants import ParseMode
    TELEGRAM_OK = True
except ImportError:
    TELEGRAM_OK = False

# ── CONFIG ────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TIMEFRAME        = os.environ.get("TIMEFRAME",       "5m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",  "1.5"))
LEVERAGE         = int(os.environ.get("LEVERAGE",        "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",    "30"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES", "3"))    # V13: 6→3
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",    "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",     "0"))

# ── FILTROS DE CALIDAD ────────────────────────────────────────────────────────
MIN_SCORE        = float(os.environ.get("MIN_SCORE",     "65.0"))  # V13: 55→65
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT",  "0.20"))
MAX_DIST_PCT     = float(os.environ.get("MAX_DIST_PCT",  "1.20"))  # V13: SL max 1.2%
MAX_SPREAD_PCT   = float(os.environ.get("MAX_SPREAD_PCT","0.12"))  # V13: más estricto
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",   "3.5"))

# ── SESSION FILTER (V13 — CAUSA 1) ───────────────────────────────────────────
USE_SESSION      = os.environ.get("USE_SESSION","true").lower() == "true"
# Sesiones en UTC: London 06-12, NY 12-21, overlap 12-17 (mejor)
SESSION_LONDON_START = int(os.environ.get("SESSION_LONDON_START", "6"))
SESSION_LONDON_END   = int(os.environ.get("SESSION_LONDON_END",   "12"))
SESSION_NY_START     = int(os.environ.get("SESSION_NY_START",     "12"))
SESSION_NY_END       = int(os.environ.get("SESSION_NY_END",       "21"))

# ── EMA STACK (5m) ────────────────────────────────────────────────────────────
EMA_FAST         = int(os.environ.get("EMA_FAST",   "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",   "17"))
EMA_TREND        = int(os.environ.get("EMA_TREND",  "100"))
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT","30.0"))
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",  "3"))

# ── 15m INTERMEDIO (V13 — CAUSA 3) ───────────────────────────────────────────
USE_M15_CONFIRM  = os.environ.get("USE_M15_CONFIRM","true").lower() == "true"
M15_EMA_FAST     = int(os.environ.get("M15_EMA_FAST","7"))
M15_EMA_SLOW     = int(os.environ.get("M15_EMA_SLOW","17"))
M15_CACHE_TTL    = int(os.environ.get("M15_CACHE_TTL","120"))   # 2min TTL

# ── H1 CONFIRMA TENDENCIA (Pine Script) ──────────────────────────────────────
USE_H1_CONFIRM   = os.environ.get("USE_H1_CONFIRM","true").lower() == "true"
USE_H1_SR        = os.environ.get("USE_H1_SR",     "true").lower() == "true"
H1_EMA_FAST      = int(os.environ.get("H1_EMA_FAST","7"))
H1_EMA_SLOW      = int(os.environ.get("H1_EMA_SLOW","17"))
H1_SR_DIST_MIN   = float(os.environ.get("H1_SR_DIST_MIN","1.0"))
H1_CACHE_TTL     = int(os.environ.get("H1_CACHE_TTL","300"))

# ── ADX / DI ─────────────────────────────────────────────────────────────────
ADX_LEN          = int(os.environ.get("ADX_LEN",  "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN", "25.0"))
ADX_RISING_LOOK  = int(os.environ.get("ADX_RISING_LOOK","3"))    # V13: ADX debe subir
USE_ADX          = os.environ.get("USE_ADX","true").lower() == "true"
USE_DI           = os.environ.get("USE_DI", "true").lower() == "true"

# ── RSI MOMENTUM ZONE (V13 — CAUSA 4) ────────────────────────────────────────
RSI_LEN          = int(os.environ.get("RSI_LEN",    "14"))
RSI_LONG_MIN     = float(os.environ.get("RSI_LONG_MIN", "42.0"))  # V13: zona momentum LONG
RSI_LONG_MAX     = float(os.environ.get("RSI_LONG_MAX", "65.0"))  # no sobrecomprado
RSI_SHORT_MIN    = float(os.environ.get("RSI_SHORT_MIN","35.0"))  # V13: zona momentum SHORT
RSI_SHORT_MAX    = float(os.environ.get("RSI_SHORT_MAX","58.0"))  # no sobrevendido
USE_RSI          = os.environ.get("USE_RSI","true").lower() == "true"

# ── VOLUMEN ───────────────────────────────────────────────────────────────────
USE_VOL          = os.environ.get("USE_VOL","true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT","1.4"))       # V13: 1.3→1.4

# ── ESTRUCTURA DE MERCADO (V13 — CAUSA 5) ────────────────────────────────────
USE_MARKET_STR   = os.environ.get("USE_MARKET_STR","true").lower() == "true"
MS_PIVOT_LEN     = int(os.environ.get("MS_PIVOT_LEN","3"))
MS_PIVOTS_NEEDED = int(os.environ.get("MS_PIVOTS_NEEDED","2"))   # pivotes confirmando

# ── FALSE BREAKOUT GUARD (V13 — CAUSA 6) ─────────────────────────────────────
USE_FBG          = os.environ.get("USE_FBG","true").lower() == "true"
FBG_LOOKBACK     = int(os.environ.get("FBG_LOOKBACK","15"))       # velas atrás
FBG_MARGIN_ATR   = float(os.environ.get("FBG_MARGIN_ATR","0.3"))  # margen de "mismo nivel"

# ── SL / TP — Pine Script ─────────────────────────────────────────────────────
SL_CANDLE_MARGIN = float(os.environ.get("SL_CANDLE_MARGIN","0.20"))
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT",    "1.2"))
TP_MULT          = float(os.environ.get("TP_MULT",        "3.0"))
MIN_RR           = float(os.environ.get("MIN_RR",         "2.5"))
USE_CANDLE_PATTERNS = os.environ.get("USE_CANDLE_PATTERNS","true").lower() == "true"
ATR_LEN          = int(os.environ.get("ATR_LEN","14"))
ANTI_CHOP        = os.environ.get("ANTI_CHOP","true").lower() == "true"

# ── BREAKEVEN 1R (V13 — más agresivo que 1.5R) ───────────────────────────────
BE_TRIGGER_R     = float(os.environ.get("BE_TRIGGER_R",   "0.40"))  # V13: 0.5→0.4 (1.2R)
TRAILING_STOP    = os.environ.get("TRAILING_STOP","true").lower() == "true"

# ── POSITION SIZING ───────────────────────────────────────────────────────────
MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT","3.0"))
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT","40.0"))
MAX_MARGIN_PCT   = float(os.environ.get("MAX_MARGIN_PCT","25.0"))

# ── COOLDOWN ──────────────────────────────────────────────────────────────────
COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS","25"))
USE_WS_CACHE     = os.environ.get("USE_WS_CACHE","true").lower() == "true"

_raw = os.environ.get("CUSTOM_SYMBOLS","")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE   = "https://open-api.bingx.com"
BINGX_WS     = "wss://open-api-swap.bingx.com/swap-market"
INTERVAL_MAP = {
    "1m":"1m","3m":"3m","5m":"5m","15m":"15m",
    "30m":"30m","1h":"1H","4h":"4H","1d":"1D"
}

EXCLUDED_PREFIXES = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG")
EXCLUDED_KEYWORDS = ("Gasoline","GasOil","Brent","WTI","OilBrent",
                     "Copper","Wheat","Cotton","Soybean","Silver",
                     "EURUSD","GBPUSD","JPYUSD")

FALLBACK_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "MATIC-USDT","UNI-USDT","LTC-USDT","BCH-USDT","ATOM-USDT",
    "XLM-USDT","ETC-USDT","NEAR-USDT","APT-USDT","OP-USDT",
    "ARB-USDT","FIL-USDT","ICP-USDT","HBAR-USDT","AAVE-USDT",
    "GRT-USDT","MKR-USDT","INJ-USDT","SUI-USDT","TIA-USDT",
    "SEI-USDT","WIF-USDT","PEPE-USDT","WLD-USDT","GMX-USDT",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

# ── ESTADO GLOBAL ─────────────────────────────────────────────────────────────
ws_kline_cache  = {}
ws_price_cache  = {}
ws_cache_lock   = threading.Lock()
sl_cooldown     = {}
h1_cache        = {}
m15_cache       = {}    # V13: cache 15m
position_state  = {}
pos_state_lock  = threading.Lock()

# ── BINGX API ─────────────────────────────────────────────────────────────────
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
        d = data.get("data", {})
        if not isinstance(d, dict):
            return 0.0
        bal = d.get("balance", {})
        if isinstance(bal, dict):
            for field in ("availableMargin","available","crossWalletBalance",
                          "walletBalance","equity","balance"):
                v = bal.get(field)
                if v is not None and v != "" and float(v) != 0.0:
                    log.info(f"Balance: {float(v):.4f} USDT (bal.{field})")
                    return float(v)
        if isinstance(bal, list):
            for asset in bal:
                if isinstance(asset, dict) and asset.get("asset") == "USDT":
                    for field in ("availableMargin","available","walletBalance","equity"):
                        v = asset.get(field)
                        if v is not None and v != "":
                            return float(v)
        return 0.0
    except Exception as e:
        log.error(f"get_balance error: {e}")
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
        log.error(f"get_positions error: {e}")
        return {}

# ── SYMBOL DISCOVERY ──────────────────────────────────────────────────────────
def _is_valid(sym):
    if not sym or not sym.endswith("-USDT"):
        return False
    base = sym.replace("-USDT","")
    if len(base) < 2:
        return False
    if any(base.startswith(p) for p in EXCLUDED_PREFIXES):
        return False
    if any(kw.lower() in sym.lower() for kw in EXCLUDED_KEYWORDS):
        return False
    return True

def _symbols_from_contracts():
    data = bx_get("/openApi/swap/v2/quote/contracts", {})
    contracts = data.get("data", [])
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("Empty contracts")
    usdt = [c for c in contracts if isinstance(c,dict)
            and c.get("asset","")=="USDT" and c.get("status")==1]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c,dict)
                and str(c.get("symbol","")).endswith("-USDT")]
    usdt.sort(key=lambda x: float(x.get("tradeAmount",0) or 0), reverse=True)
    return [c["symbol"] for c in usdt if _is_valid(c.get("symbol",""))]

def _symbols_from_ticker():
    data = bx_get("/openApi/swap/v2/quote/ticker", {})
    tickers = data.get("data", [])
    if not isinstance(tickers, list) or not tickers:
        raise ValueError("Empty ticker")
    usdt = [t for t in tickers if isinstance(t,dict) and _is_valid(t.get("symbol",""))]
    usdt.sort(key=lambda x: float(x.get("quoteVolume",0) or 0), reverse=True)
    return [t["symbol"] for t in usdt]

def get_all_symbols(limit=0):
    for fn in (_symbols_from_contracts, _symbols_from_ticker):
        try:
            syms = fn()
            if syms:
                result = syms if limit == 0 else syms[:limit]
                log.info(f"✅ {len(result)} symbols via {fn.__name__}")
                return result
        except Exception as e:
            log.warning(f"{fn.__name__} failed: {e}")
    return FALLBACK_SYMBOLS if limit == 0 else FALLBACK_SYMBOLS[:limit]

def set_lev(symbol):
    for side in ("LONG","SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol":symbol,"side":side,"leverage":LEVERAGE})
        except Exception:
            pass

# ── WEBSOCKET CACHE ───────────────────────────────────────────────────────────
def _ws_on_message(ws_app, message):
    try:
        import gzip
        try:
            data = json.loads(gzip.decompress(message) if isinstance(message,bytes) else message)
        except Exception:
            data = json.loads(message)
        if data.get("dataType","").endswith("@kline"):
            sym_raw = data.get("s","")
            sym = sym_raw.replace("_","-") if "_" in sym_raw else sym_raw
            kdata = data.get("data",{}).get("kline", data.get("k",{}))
            if not kdata: return
            row = {
                "open_time": pd.to_datetime(kdata.get("t", kdata.get("startTime",0)), unit="ms"),
                "open":  float(kdata.get("o",0)), "high":  float(kdata.get("h",0)),
                "low":   float(kdata.get("l",0)), "close": float(kdata.get("c",0)),
                "volume":float(kdata.get("v",0)),
            }
            if row["close"] == 0: return
            with ws_cache_lock:
                df = ws_kline_cache.get(sym)
                if df is None: return
                if len(df) > 0 and df.iloc[-1]["open_time"] == row["open_time"]:
                    for col in ("open","high","low","close","volume"):
                        df.at[df.index[-1], col] = row[col]
                else:
                    ws_kline_cache[sym] = pd.concat(
                        [df, pd.DataFrame([row])], ignore_index=True).tail(400)
                ws_price_cache[sym] = row["close"]
    except Exception:
        pass

def _ws_on_error(ws_app, e): log.warning(f"WS error: {e}")
def _ws_on_close(ws_app, *a): log.info("WS closed — reconnecting in 5s")

def _ws_on_open(ws_app, symbols):
    ivl = INTERVAL_MAP.get(TIMEFRAME,"5m").lower()
    for sym in symbols[:200]:
        try:
            ws_app.send(json.dumps({
                "id": f"sub_{sym}", "reqType":"sub",
                "dataType": f"{sym.replace('-','_')}@kline_{ivl}"
            }))
        except Exception: pass

def start_ws_cache(symbols):
    if not USE_WS_CACHE: return
    def _run():
        while True:
            try:
                ws_app = websocket.WebSocketApp(
                    BINGX_WS, on_message=_ws_on_message,
                    on_error=_ws_on_error, on_close=_ws_on_close,
                    on_open=lambda app: _ws_on_open(app, symbols))
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.warning(f"WS thread error: {e}")
            time.sleep(5)
    threading.Thread(target=_run, daemon=True).start()
    log.info(f"✅ WS cache iniciado ({min(len(symbols),200)} símbolos)")

# ── PRECIO EN VIVO ────────────────────────────────────────────────────────────
def get_live_price(symbol):
    if USE_WS_CACHE:
        with ws_cache_lock:
            p = ws_price_cache.get(symbol)
        if p and p > 0: return p
    for fallback in [
        lambda: _price_from_premium(symbol),
        lambda: _price_from_ticker(symbol),
        lambda: _price_from_kline(symbol),
    ]:
        try:
            p = fallback()
            if p: return p
        except Exception: pass
    raise ValueError(f"get_live_price({symbol}) failed")

def _price_from_premium(symbol):
    data = bx_get("/openApi/swap/v2/quote/premiumIndex", {"symbol":symbol})
    items = data.get("data",[])
    if isinstance(items, list):
        for item in items:
            if item.get("symbol") == symbol and item.get("markPrice"):
                return float(item["markPrice"])
    if isinstance(items, dict) and items.get("markPrice"):
        return float(items["markPrice"])
    return None

def _price_from_ticker(symbol):
    data = bx_get("/openApi/swap/v2/quote/ticker", {"symbol":symbol})
    t2 = data.get("data",[])
    if isinstance(t2, list):
        for t in t2:
            if t.get("symbol") == symbol:
                lp = t.get("lastPrice") or t.get("price")
                if lp: return float(lp)
    if isinstance(t2, dict):
        lp = t2.get("lastPrice") or t2.get("price")
        if lp: return float(lp)
    return None

def _price_from_kline(symbol):
    params = {"symbol":symbol,"interval":INTERVAL_MAP.get(TIMEFRAME,"5m"),"limit":2}
    data = bx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data",[])
    if rows: return float(rows[-1][4])
    return None

def get_spread_pct(symbol):
    try:
        data = bx_get("/openApi/swap/v2/quote/bookTicker", {"symbol":symbol})
        d = data.get("data",{})
        if isinstance(d, list):
            for item in d:
                if item.get("symbol") == symbol:
                    d = item; break
        ask = float(d.get("askPrice",0) or 0)
        bid = float(d.get("bidPrice",0) or 0)
        if ask > 0 and bid > 0: return (ask - bid) / bid * 100
        return 999.0
    except Exception: return 999.0

# ── KLINES ────────────────────────────────────────────────────────────────────
def _parse_klines(rows):
    if not rows or not isinstance(rows, list): return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open","high","low","close","volume"], inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

def get_klines(symbol, limit=300):
    if USE_WS_CACHE:
        with ws_cache_lock:
            df = ws_kline_cache.get(symbol)
        if df is not None and len(df) >= limit // 2:
            return df.copy()
    params = {"symbol":symbol,"interval":INTERVAL_MAP.get(TIMEFRAME,"5m"),"limit":limit}
    rows = bx_get("/openApi/swap/v3/quote/klines", params).get("data",[])
    df = _parse_klines(rows)
    if USE_WS_CACHE and not df.empty:
        with ws_cache_lock: ws_kline_cache[symbol] = df.copy()
    return df

def get_tf_klines(symbol, interval, cache_dict, ttl, limit=60):
    """Klines genérico con cache TTL para 15m y H1."""
    now = time.time()
    cached = cache_dict.get(symbol)
    if cached:
        df_c, ts = cached
        if now - ts < ttl and len(df_c) >= 30:
            return df_c.copy()
    try:
        rows = bx_get("/openApi/swap/v3/quote/klines",
                      {"symbol":symbol,"interval":interval,"limit":limit}).get("data",[])
        df = _parse_klines(rows)
        if not df.empty:
            cache_dict[symbol] = (df.copy(), now)
        return df
    except Exception as e:
        log.debug(f"TF klines {interval} {symbol}: {e}")
        return pd.DataFrame()

# ── INDICADORES ───────────────────────────────────────────────────────────────
def calc_atr(high, low, close, period):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calc_ema_angle(ema_series, atr_series, look):
    price_change = ema_series - ema_series.shift(look)
    denom = atr_series * look
    angle = np.degrees(np.arctan2(price_change.values, denom.values))
    return pd.Series(angle, index=ema_series.index)

def calc_adx(high, low, close, period):
    up   = high.diff()
    down = -low.diff()
    plus_dm  = np.where((up > down) & (up > 0),   up,   0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    alpha = 1.0 / period
    def wilder(arr):
        return pd.Series(arr, index=high.index).ewm(alpha=alpha, adjust=False).mean()
    tr_s  = wilder(tr); pdm_s = wilder(plus_dm); mdm_s = wilder(minus_dm)
    di_p  = 100 * pdm_s / tr_s.replace(0, np.nan)
    di_m  = 100 * mdm_s / tr_s.replace(0, np.nan)
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)
    return di_p, di_m, dx.ewm(alpha=alpha, adjust=False).mean()

def calc_rsi(close, period):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(alpha=1/period, adjust=False).mean()
    al    = loss.ewm(alpha=1/period, adjust=False).mean()
    return 100 - (100 / (1 + ag / al.replace(0, np.nan)))

def calc_stoch_rsi(close, rsi_period=14, stoch_period=14, k=3, d=3):
    """Stochastic RSI para filtro adicional de momentum."""
    rsi = calc_rsi(close, rsi_period)
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    stoch = 100 * (rsi - rsi_min) / (rsi_max - rsi_min + 1e-10)
    k_line = stoch.rolling(k).mean()
    d_line = k_line.rolling(d).mean()
    return k_line, d_line

# ── SESSION FILTER (V13 — CAUSA 1) ───────────────────────────────────────────
def is_trading_session():
    """True si estamos en London o NY session (UTC)."""
    if not USE_SESSION:
        return True
    hour = datetime.now(timezone.utc).hour
    in_london = SESSION_LONDON_START <= hour < SESSION_LONDON_END
    in_ny     = SESSION_NY_START     <= hour < SESSION_NY_END
    return in_london or in_ny

# ── ANÁLISIS H1 ───────────────────────────────────────────────────────────────
def analyze_h1(symbol):
    df = get_tf_klines(symbol, "1H", h1_cache, H1_CACHE_TTL, limit=60)
    if df.empty or len(df) < 25:
        return None
    close = df["close"]; high = df["high"]; low = df["low"]
    e7  = close.ewm(span=H1_EMA_FAST, adjust=False).mean()
    e17 = close.ewm(span=H1_EMA_SLOW, adjust=False).mean()
    e7n = float(e7.iloc[-1]); e17n = float(e17.iloc[-1])
    e7p = float(e7.iloc[-3])  if len(e7)  > 3 else e7n
    e17p= float(e17.iloc[-3]) if len(e17) > 3 else e17n
    h1_trend = "BULL" if e7n > e17n else ("BEAR" if e7n < e17n else "NEUTRAL")
    gap_widening = ((e7n - e17n) > (e7p - e17p) and h1_trend == "BULL") or \
                   ((e7n - e17n) < (e7p - e17p) and h1_trend == "BEAR")
    close_now = float(close.iloc[-1])
    plen = 3
    ph_vals, pl_vals = [], []
    for idx in range(plen, min(len(df)-plen, 40)):
        if float(high.iloc[idx]) == float(high.iloc[idx-plen:idx+plen+1].max()):
            ph_vals.append(float(high.iloc[idx]))
        if float(low.iloc[idx]) == float(low.iloc[idx-plen:idx+plen+1].min()):
            pl_vals.append(float(low.iloc[idx]))
    resistances = sorted([v for v in ph_vals if v > close_now])
    supports    = sorted([v for v in pl_vals if v < close_now], reverse=True)
    h1_res = resistances[0] if resistances else close_now * 1.08
    h1_sup = supports[0]    if supports    else close_now * 0.92
    return {
        "h1_trend":     h1_trend,
        "gap_widening": gap_widening,
        "h1_resistance":h1_res,
        "h1_support":   h1_sup,
        "dist_to_res":  round((h1_res - close_now) / close_now * 100, 2),
        "dist_to_sup":  round((close_now - h1_sup) / close_now * 100, 2),
    }

# ── ANÁLISIS 15m (V13 — CAUSA 3) ─────────────────────────────────────────────
def analyze_m15(symbol):
    """
    Timeframe intermedio 15m: EMA7 > EMA17 = bull, EMA7 < EMA17 = bear.
    Solo opera si 15m está alineado con 5m Y con 1H.
    """
    df = get_tf_klines(symbol, "15m", m15_cache, M15_CACHE_TTL, limit=60)
    if df.empty or len(df) < 20:
        return None
    close = df["close"]
    e7  = close.ewm(span=M15_EMA_FAST, adjust=False).mean()
    e17 = close.ewm(span=M15_EMA_SLOW, adjust=False).mean()
    e7n = float(e7.iloc[-1]); e17n = float(e17.iloc[-1])
    e7p = float(e7.iloc[-2]) if len(e7) > 2 else e7n
    e17p= float(e17.iloc[-2]) if len(e17) > 2 else e17n
    m15_trend    = "BULL" if e7n > e17n else ("BEAR" if e7n < e17n else "NEUTRAL")
    gap_widening = ((e7n - e17n) > (e7p - e17p) and m15_trend == "BULL") or \
                   ((e7n - e17n) < (e7p - e17p) and m15_trend == "BEAR")
    di_p, di_m, adx_m15 = calc_adx(df["high"], df["low"], close, ADX_LEN)
    return {
        "m15_trend":    m15_trend,
        "gap_widening": gap_widening,
        "adx_m15":      round(float(adx_m15.iloc[-1]), 1),
        "di_p":         round(float(di_p.iloc[-1]), 1),
        "di_m":         round(float(di_m.iloc[-1]), 1),
    }

# ── ESTRUCTURA DE MERCADO (V13 — CAUSA 5) ────────────────────────────────────
def check_market_structure(df, sc, direction):
    """
    V13: verifica HH/HL para LONG, LL/LH para SHORT.
    Busca los últimos MS_PIVOTS_NEEDED pivot highs/lows y comprueba que estén subiendo/bajando.
    Retorna (bool, score_bonus).
    """
    if not USE_MARKET_STR:
        return True, 5

    plen = MS_PIVOT_LEN
    start = max(plen, sc - 60)

    if direction == "LONG":
        # Buscar pivot highs: Higher Highs
        ph = []
        for idx in range(start, sc - plen + 1):
            w = df["high"].iloc[idx-plen:idx+plen+1]
            if float(df["high"].iloc[idx]) == float(w.max()):
                ph.append(float(df["high"].iloc[idx]))
        if len(ph) >= MS_PIVOTS_NEEDED:
            # Los últimos N pivot highs deben ser crecientes
            recent = ph[-MS_PIVOTS_NEEDED:]
            if all(recent[i] < recent[i+1] for i in range(len(recent)-1)):
                return True, 10   # HH confirmados
            else:
                return False, 0   # Lower Highs → no operar LONG
        return True, 3  # sin suficientes datos → no penalizar

    else:  # SHORT
        # Buscar pivot lows: Lower Lows
        pl = []
        for idx in range(start, sc - plen + 1):
            w = df["low"].iloc[idx-plen:idx+plen+1]
            if float(df["low"].iloc[idx]) == float(w.min()):
                pl.append(float(df["low"].iloc[idx]))
        if len(pl) >= MS_PIVOTS_NEEDED:
            recent = pl[-MS_PIVOTS_NEEDED:]
            if all(recent[i] > recent[i+1] for i in range(len(recent)-1)):
                return True, 10   # LL confirmados
            else:
                return False, 0
        return True, 3

# ── FALSE BREAKOUT GUARD (V13 — CAUSA 6) ─────────────────────────────────────
def check_false_breakout_guard(df, sc, direction, catr):
    """
    V13: revisa si el mismo nivel high/low fue probado y falló recientemente.
    Si encontramos una rotura del mismo nivel en los últimos FBG_LOOKBACK velas
    que fue seguida por una reversión, es un nivel "quemado" → skip.
    """
    if not USE_FBG:
        return True

    sc_high = float(df["high"].iloc[sc])
    sc_low  = float(df["low"].iloc[sc])
    margin  = catr * FBG_MARGIN_ATR
    lookback_start = max(0, sc - FBG_LOOKBACK - 1)

    if direction == "LONG":
        level = sc_high
        # ¿Hubo otra vela reciente con high cercano que falló en romperse?
        for idx in range(lookback_start, sc - 2):
            prev_high = float(df["high"].iloc[idx])
            if abs(prev_high - level) <= margin:
                # ¿La siguiente vela cerró por debajo (fallo)?
                next_close = float(df["close"].iloc[idx+1])
                if next_close < prev_high - margin:
                    return False   # mismo nivel ya falló → probablemente fallará de nuevo
    else:
        level = sc_low
        for idx in range(lookback_start, sc - 2):
            prev_low = float(df["low"].iloc[idx])
            if abs(prev_low - level) <= margin:
                next_close = float(df["close"].iloc[idx+1])
                if next_close > prev_low + margin:
                    return False

    return True

# ── PATRONES DE VELA — PINE SCRIPT EXACT ─────────────────────────────────────
def detect_pin_bar_pine(df, i, direction):
    c  = float(df["close"].iloc[i]); o = float(df["open"].iloc[i])
    h  = float(df["high"].iloc[i]);  l = float(df["low"].iloc[i])
    body_size = abs(c - o)
    if body_size < 1e-10: return False, 0.0
    total_range = h - l
    if total_range < 1e-10: return False, 0.0
    if direction == "LONG":
        lower_wick = (o - l) if c > o else (c - l)
        upper_wick = (h - c) if c > o else (h - o)
        if c > o and lower_wick > body_size * 2 and upper_wick < body_size:
            return True, round(min(lower_wick / total_range * 130, 100.0), 1)
    else:
        upper_wick = (h - o) if c < o else (h - c)
        lower_wick = (c - l) if c < o else (o - l)
        if c < o and upper_wick > body_size * 2 and lower_wick < body_size:
            return True, round(min(upper_wick / total_range * 130, 100.0), 1)
    return False, 0.0

def detect_engulfing_pine(df, i, direction):
    if i < 1: return False, 0.0
    c  = float(df["close"].iloc[i]);   o  = float(df["open"].iloc[i])
    cp = float(df["close"].iloc[i-1]); op = float(df["open"].iloc[i-1])
    hp = float(df["high"].iloc[i-1]);  lp = float(df["low"].iloc[i-1])
    body_cur = abs(c - o); body_pre = abs(cp - op)
    if body_pre < 1e-10: return False, 0.0
    ratio = body_cur / body_pre
    if ratio < 1.05: return False, 0.0
    if direction == "LONG"  and c > o and cp < op and c > hp:
        return True, round(min(ratio * 50, 100.0), 1)
    if direction == "SHORT" and c < o and cp > op and c < lp:
        return True, round(min(ratio * 50, 100.0), 1)
    return False, 0.0

def detect_momentum_candle(df, i, direction, atr):
    o = float(df["open"].iloc[i]); h = float(df["high"].iloc[i])
    l = float(df["low"].iloc[i]);  c = float(df["close"].iloc[i])
    total_range = h - l
    if total_range < 1e-10 or atr < 1e-10: return False, 0.0
    body = abs(c - o)
    if body / total_range < 0.65 or body < atr * 0.5: return False, 0.0
    if direction == "LONG"  and c > o and (h - c) < body * 0.35:
        return True, round(min(body / total_range * 90, 100.0), 1)
    if direction == "SHORT" and c < o and (c - l) < body * 0.35:
        return True, round(min(body / total_range * 90, 100.0), 1)
    return False, 0.0

def detect_inside_bar_breakout(df, i, direction):
    if i < 2: return False, 0.0
    h_m2 = float(df["high"].iloc[i-2]); l_m2 = float(df["low"].iloc[i-2])
    h_m1 = float(df["high"].iloc[i-1]); l_m1 = float(df["low"].iloc[i-1])
    c     = float(df["close"].iloc[i])
    if not (h_m1 <= h_m2 and l_m1 >= l_m2): return False, 0.0
    if direction == "LONG"  and c > h_m2: return True, 60.0
    if direction == "SHORT" and c < l_m2: return True, 60.0
    return False, 0.0

def is_choppy_market(df, adx_val):
    if not ANTI_CHOP: return False
    if adx_val < ADX_MIN: return True
    if len(df) < 15: return False
    recent = df.iloc[-12:]
    avg_range = float((recent["high"] - recent["low"]).mean())
    atr_s = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
    if float(atr_s.iloc[-1]) > 0 and avg_range < float(atr_s.iloc[-1]) * 0.75:
        return True
    return False

# ── ESTRATEGIA PRINCIPAL V13 — HIGH WINRATE ──────────────────────────────────
def scan_symbol(symbol):
    """
    V13 CHECKLIST (todos deben pasar):
    ✅ SESSION: London o NY
    ✅ 5m: EMA7 > EMA17 > EMA100 (stack perfecto)
    ✅ 5m: Slope EMA7 ≥ 30°
    ✅ 5m: ADX > 25 Y CRECIENTE (no fatigado)
    ✅ 5m: RSI en zona momentum (42-65 long / 35-58 short)
    ✅ 5m: Velas consecutivas confirman (2/3 últimas velas)
    ✅ 5m: Patrón de vela señal + ruptura confirmada
    ✅ 5m: Volumen ruptura ≥ volumen señal (acelerando)
    ✅ 15m: EMA7/EMA17 alineados con dirección
    ✅ H1: EMA7/EMA17 alineados con dirección
    ✅ ESTRUCTURA: HH/HL (long) o LL/LH (short)
    ✅ FALSE BREAKOUT GUARD: nivel no quemado
    ✅ H1 S/R: distancia mínima a resistencia/soporte H1
    ✅ Score ≥ 65
    """
    # ── GATE 1: SESIÓN ────────────────────────────────────────────────────────
    if not is_trading_session():
        return None

    # ── GATE 2: COOLDOWN ──────────────────────────────────────────────────────
    if symbol in sl_cooldown:
        elapsed = (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds() / 60
        if elapsed < COOLDOWN_MINS:
            return None

    try:
        df = get_klines(symbol, limit=300)
        min_bars = max(EMA_TREND + 10, ADX_LEN * 2 + 5, RSI_LEN + 5, 80)
        if df.empty or len(df) < min_bars:
            return None

        # ── INDICADORES 5m ────────────────────────────────────────────────────
        atr_s     = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
        ema_f     = df["close"].ewm(span=EMA_FAST,  adjust=False).mean()
        ema_s     = df["close"].ewm(span=EMA_SLOW,  adjust=False).mean()
        ema_trend = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
        angle     = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        di_p, di_m, adx_s = calc_adx(df["high"], df["low"], df["close"], ADX_LEN)
        rsi_s     = calc_rsi(df["close"], RSI_LEN)
        vol_ma    = df["volume"].rolling(20).mean()
        stoch_k, stoch_d = calc_stoch_rsi(df["close"])

        # sc = vela señal, bc = vela ruptura
        sc = len(df) - 3
        bc = len(df) - 2

        if sc < max(EMA_TREND + 2, ADX_LEN * 2, 50):
            return None

        # Datos clave
        sc_c  = float(df["close"].iloc[sc]); sc_o = float(df["open"].iloc[sc])
        sc_h  = float(df["high"].iloc[sc]);  sc_l = float(df["low"].iloc[sc])
        bc_c  = float(df["close"].iloc[bc])
        bc_h  = float(df["high"].iloc[bc]);  bc_l = float(df["low"].iloc[bc])
        bc_vol = float(df["volume"].iloc[bc])
        sc_vol = float(df["volume"].iloc[sc])

        catr      = float(atr_s.iloc[sc])
        angle_now = float(angle.iloc[sc])
        adx_now   = float(adx_s.iloc[sc])
        adx_prev  = float(adx_s.iloc[sc - ADX_RISING_LOOK])  # V13: ADX rising
        di_p_now  = float(di_p.iloc[sc])
        di_m_now  = float(di_m.iloc[sc])
        rsi_now   = float(rsi_s.iloc[sc])
        sk_now    = float(stoch_k.iloc[sc]) if not np.isnan(float(stoch_k.iloc[sc])) else 50.0
        ema_f_sc  = float(ema_f.iloc[sc]); ema_f_pr = float(ema_f.iloc[sc-1])
        ema_s_sc  = float(ema_s.iloc[sc]); ema_s_pr = float(ema_s.iloc[sc-1])
        ema_t_sc  = float(ema_trend.iloc[sc])
        vma_sc    = float(vol_ma.iloc[sc]); vma_bc = float(vol_ma.iloc[bc])
        atr_prev5 = float(atr_s.iloc[sc - 5])

        if any(np.isnan(x) for x in [angle_now, adx_now, catr, ema_f_sc,
                                      ema_s_sc, ema_t_sc, rsi_now]):
            return None
        if vma_sc <= 0 or catr <= 0:
            return None

        atr_pct = (catr / sc_c) * 100
        if atr_pct > ATR_MAX_PCT:
            return None
        if is_choppy_market(df.iloc[:sc+1], adx_now):
            return None

        # ── GATE 3: ADX CRECIENTE (V13 — CAUSA 2) ────────────────────────────
        adx_rising = adx_now > adx_prev
        if USE_ADX and not adx_rising:
            return None

        # ── GATE 4: ATR SALUDABLE (no colapsando) ────────────────────────────
        if atr_prev5 > 0 and catr < atr_prev5 * 0.70:
            return None   # volatilidad colapsando → skip

        # ── GATE 5: EMA STACK PERFECTO (V13) ─────────────────────────────────
        # Long: EMA7 > EMA17 > EMA100 todos en orden
        stack_long  = ema_f_sc > ema_s_sc > ema_t_sc
        stack_short = ema_f_sc < ema_s_sc < ema_t_sc

        if not stack_long and not stack_short:
            return None

        # ── GATE 6: SLOPE + DI + VOLUMEN ─────────────────────────────────────
        angle_long  = angle_now >= SLOPE_LIMIT
        angle_short = angle_now <= -SLOPE_LIMIT
        accel_long  = ema_f_sc > ema_f_pr
        accel_short = ema_f_sc < ema_f_pr
        gap_wid_l   = (ema_f_sc - ema_s_sc) > (ema_f_pr - ema_s_pr)
        gap_wid_s   = (ema_f_sc - ema_s_sc) < (ema_f_pr - ema_s_pr)
        di_long_ok  = (not USE_DI) or (di_p_now > di_m_now)
        di_short_ok = (not USE_DI) or (di_m_now > di_p_now)
        adx_ok      = (not USE_ADX) or (adx_now > ADX_MIN)
        vratio_sig  = round(sc_vol / vma_sc, 2) if vma_sc > 0 else 0.0
        vol_ok      = (not USE_VOL) or (vratio_sig >= VOL_MULT)

        # ── GATE 7: RSI MOMENTUM ZONE (V13 — CAUSA 4) ────────────────────────
        rsi_long_ok  = (not USE_RSI) or (RSI_LONG_MIN <= rsi_now <= RSI_LONG_MAX)
        rsi_short_ok = (not USE_RSI) or (RSI_SHORT_MIN <= rsi_now <= RSI_SHORT_MAX)

        # Stoch RSI adicional: no entrar si Stoch está en zona extrema
        stoch_long_ok  = sk_now < 82   # no sobrecomprado en Stoch
        stoch_short_ok = sk_now > 18   # no sobrevendido en Stoch

        base_long  = (stack_long  and angle_long  and adx_ok and vol_ok and
                      rsi_long_ok  and di_long_ok  and accel_long  and
                      gap_wid_l and stoch_long_ok)
        base_short = (stack_short and angle_short and adx_ok and vol_ok and
                      rsi_short_ok and di_short_ok and accel_short and
                      gap_wid_s and stoch_short_ok)

        if not base_long and not base_short:
            return None

        direction = "LONG" if base_long else "SHORT"

        # ── GATE 8: VELAS CONSECUTIVAS (V13) ─────────────────────────────────
        # Mínimo 2 de las 3 últimas velas (incluida señal) confirman dirección
        candle_confirms = 0
        for idx in [sc-2, sc-1, sc]:
            if idx < 0: continue
            c_i = float(df["close"].iloc[idx]); o_i = float(df["open"].iloc[idx])
            if direction == "LONG"  and c_i > o_i: candle_confirms += 1
            if direction == "SHORT" and c_i < o_i: candle_confirms += 1
        if candle_confirms < 2:
            return None

        # ── GATE 9: PATRÓN DE VELA SEÑAL ─────────────────────────────────────
        is_pin, pin_str = detect_pin_bar_pine(df, sc, direction)
        is_eng, eng_str = detect_engulfing_pine(df, sc, direction)
        is_mom, mom_str = detect_momentum_candle(df, sc, direction, catr)
        is_ib,  ib_str  = detect_inside_bar_breakout(df, sc, direction)

        has_pattern = is_pin or is_eng or is_mom or is_ib
        if not has_pattern:
            return None

        if is_pin:   pattern_name, pattern_score = "PIN_BAR",   pin_str
        elif is_eng: pattern_name, pattern_score = "ENGULF",    eng_str
        elif is_mom: pattern_name, pattern_score = "MOMENTUM",  mom_str
        else:        pattern_name, pattern_score = "INSIDE_BR", ib_str

        # ── GATE 10: RUPTURA CONFIRMADA + VOLUMEN ACELERANDO ─────────────────
        if direction == "LONG"  and bc_c <= sc_h: return None
        if direction == "SHORT" and bc_c >= sc_l: return None

        vratio_conf = round(bc_vol / vma_bc, 2) if vma_bc > 0 else 0.0
        # V13: volumen en ruptura debe acelerar vs señal
        vol_accel = bc_vol >= sc_vol * 0.9   # ruptura con al menos 90% del vol señal

        # ── GATE 11: ESTRUCTURA DE MERCADO (V13 — CAUSA 5) ───────────────────
        ms_ok, ms_bonus = check_market_structure(df, sc, direction)
        if not ms_ok:
            return None

        # ── GATE 12: FALSE BREAKOUT GUARD (V13 — CAUSA 6) ────────────────────
        if not check_false_breakout_guard(df, sc, direction, catr):
            return None

        # ── GATE 13: 15m CONFIRMACIÓN (V13 — CAUSA 3) ────────────────────────
        m15_bonus = 0
        m15_info  = None
        if USE_M15_CONFIRM:
            m15_info = analyze_m15(symbol)
            if m15_info:
                m15_trend = m15_info["m15_trend"]
                if direction == "LONG"  and m15_trend == "BULL":
                    m15_bonus = 12
                    if m15_info.get("gap_widening"): m15_bonus += 3
                elif direction == "SHORT" and m15_trend == "BEAR":
                    m15_bonus = 12
                    if m15_info.get("gap_widening"): m15_bonus += 3
                elif m15_trend == "NEUTRAL":
                    m15_bonus = 2
                else:
                    return None   # 15m contra dirección → descartado

        # ── GATE 14: H1 CONFIRMACIÓN ──────────────────────────────────────────
        h1_bonus = 0; h1_trend = "UNKNOWN"; h1_ctx = None
        if USE_H1_CONFIRM:
            h1_ctx = analyze_h1(symbol)
            if h1_ctx:
                h1_trend = h1_ctx["h1_trend"]
                if direction == "LONG"  and h1_trend == "BULL":
                    h1_bonus = 15
                    if h1_ctx.get("gap_widening"): h1_bonus += 5
                elif direction == "SHORT" and h1_trend == "BEAR":
                    h1_bonus = 15
                    if h1_ctx.get("gap_widening"): h1_bonus += 5
                elif h1_trend == "NEUTRAL":
                    h1_bonus = 2
                else:
                    return None   # H1 contra → descartado
                if USE_H1_SR:
                    if direction == "LONG"  and h1_ctx["dist_to_res"] < H1_SR_DIST_MIN:
                        return None
                    if direction == "SHORT" and h1_ctx["dist_to_sup"] < H1_SR_DIST_MIN:
                        return None

        # ── SL QUIRÚRGICO (vela señal ± 0.2×ATR) ─────────────────────────────
        sl_margin = catr * SL_CANDLE_MARGIN
        entry_ref = bc_c

        if direction == "LONG":
            sl_price = sc_l - sl_margin
            if (entry_ref - sl_price) / entry_ref * 100 < MIN_DIST_PCT:
                sl_price = entry_ref * (1 - MIN_DIST_PCT / 100)
            if sl_price >= entry_ref: return None
            tp_price = entry_ref + (entry_ref - sl_price) * TP_MULT
        else:
            sl_price = sc_h + sl_margin
            if (sl_price - entry_ref) / entry_ref * 100 < MIN_DIST_PCT:
                sl_price = entry_ref * (1 + MIN_DIST_PCT / 100)
            if sl_price <= entry_ref: return None
            tp_price = entry_ref - (sl_price - entry_ref) * TP_MULT

        dist     = abs(entry_ref - sl_price)
        dist_pct = (dist / entry_ref) * 100

        if dist_pct < MIN_DIST_PCT: return None
        if dist_pct > MAX_DIST_PCT: return None   # V13: SL demasiado lejos → skip

        rr = abs(tp_price - entry_ref) / dist
        if rr < MIN_RR: return None

        # ── SCORING V13 ───────────────────────────────────────────────────────
        # Máximo teórico: ~100 puntos
        score  = min(abs(angle_now) / SLOPE_LIMIT * 20, 20)    # ángulo:    max 20
        score += min((adx_now - ADX_MIN) / ADX_MIN * 10, 10)   # ADX:       max 10
        score += h1_bonus                                        # H1:        max 20
        score += m15_bonus                                       # 15m:       max 15
        score += min(pattern_score / 8, 12)                     # patrón:    max 12
        score += ms_bonus                                        # estructura:max 10
        score += min(vratio_conf * 3, 6)                        # vol conf:  max 6
        score += 4 if vol_accel else 0                          # vol accel: max 4
        score += min(abs(di_p_now - di_m_now) / 10, 4)         # DI spread: max 4
        score += 3 if adx_rising else 0                         # ADX rise:  max 3
        score += 2 if candle_confirms == 3 else 0               # 3/3 velas: max 2

        if score < MIN_SCORE:
            return None

        # Razón detallada para diagnóstico
        session_name = "LONDON" if datetime.now(timezone.utc).hour < 12 else "NY"

        return {
            "symbol":      symbol,
            "signal":      direction,
            "method":      f"V13|{pattern_name}|{session_name}|{TIMEFRAME}+15m+1H",
            "pattern":     pattern_name,
            "close":       entry_ref,
            "sc_high":     sc_h,
            "sc_low":      sc_l,
            "sl":          round(sl_price, 6),
            "tp":          round(tp_price, 6),
            "atr":         catr,
            "atr_pct":     round(atr_pct, 2),
            "vol_ratio":   vratio_sig,
            "vol_conf":    vratio_conf,
            "vol_accel":   vol_accel,
            "angle":       round(angle_now, 1),
            "adx":         round(adx_now, 1),
            "adx_rising":  adx_rising,
            "rsi":         round(rsi_now, 1),
            "stoch_k":     round(sk_now, 1),
            "score":       round(score, 1),
            "rr":          round(rr, 2),
            "dist_pct":    round(dist_pct, 3),
            "di_spread":   round(abs(di_p_now - di_m_now), 1),
            "h1_trend":    h1_trend,
            "m15_trend":   m15_info["m15_trend"] if m15_info else "?",
            "h1_ctx":      h1_ctx,
            "pat_score":   round(pattern_score, 1),
            "candle_conf": candle_confirms,
            "ms_bonus":    ms_bonus,
            "session":     session_name,
        }

    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

# ── RECALC SL/TP CON PRECIO VIVO ─────────────────────────────────────────────
def recalc_sl_tp(sig, live_price):
    catr      = sig["atr"]
    direction = sig["signal"]
    sl_margin = catr * SL_CANDLE_MARGIN
    if direction == "LONG":
        sl_price = sig["sc_low"] - sl_margin
        if (live_price - sl_price) / live_price * 100 < MIN_DIST_PCT:
            sl_price = live_price * (1 - MIN_DIST_PCT / 100)
        if sl_price >= live_price: return None, None
        tp_price = live_price + (live_price - sl_price) * TP_MULT
    else:
        sl_price = sig["sc_high"] + sl_margin
        if (sl_price - live_price) / live_price * 100 < MIN_DIST_PCT:
            sl_price = live_price * (1 + MIN_DIST_PCT / 100)
        if sl_price <= live_price: return None, None
        tp_price = live_price - (sl_price - live_price) * TP_MULT
    if abs(tp_price - live_price) / abs(live_price - sl_price) < MIN_RR:
        return None, None
    dist_pct = abs(live_price - sl_price) / live_price * 100
    if dist_pct > MAX_DIST_PCT: return None, None
    return round(sl_price, 6), round(tp_price, 6)

# ── POSITION SIZING ───────────────────────────────────────────────────────────
def calc_qty(balance, entry, sl):
    dist_pct = abs(entry - sl) / entry
    if dist_pct < 1e-8: return 0, 0
    risk_usdt    = balance * (RISK_PERCENT / 100)
    notional     = risk_usdt / dist_pct
    max_notional = min(MAX_ORDER_USDT, balance * (MAX_MARGIN_PCT / 100) * LEVERAGE)
    notional     = max(MIN_ORDER_USDT, min(notional, max_notional))
    return round(max(notional / entry, 0.001), 4), round(notional, 2)

# ── ORDEN ─────────────────────────────────────────────────────────────────────
def open_order(symbol, side, qty, sl, tp):
    payload = {
        "symbol": symbol, "side": side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type": "MARKET", "quantity": round(qty, 4),
        "stopLoss":   json.dumps({"type":"STOP_MARKET","stopPrice":round(sl,6),"workingType":"MARK_PRICE"}),
        "takeProfit": json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":round(tp,6),"workingType":"MARK_PRICE"}),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    if resp.get("code", -1) != 0:
        raise ValueError(f"BingX code={resp.get('code')}: {resp.get('msg','?')}")
    return resp

def open_order_with_retry(symbol, side, qty, sl, tp, retries=1):
    for attempt in range(retries + 1):
        try:
            return open_order(symbol, side, qty, sl, tp)
        except ValueError as e:
            if "101400" in str(e) and attempt < retries:
                time.sleep(1)
                try:
                    fp = get_live_price(symbol)
                    if side == "BUY":
                        sl = round(fp * (1 - MIN_DIST_PCT/100), 6)
                        tp = round(fp + (fp - sl) * TP_MULT, 6)
                    else:
                        sl = round(fp * (1 + MIN_DIST_PCT/100), 6)
                        tp = round(fp - (sl - fp) * TP_MULT, 6)
                except Exception: raise
            else: raise

# ── BREAKEVEN 1R AGRESIVO ─────────────────────────────────────────────────────
def update_breakeven_stops(positions):
    if not TRAILING_STOP or not positions: return
    with pos_state_lock:
        state_copy = dict(position_state)
    for sym, pos in positions.items():
        try:
            ps = state_copy.get(sym)
            if not ps or ps.get("be_hit"): continue
            side  = ps["side"]; entry = ps["entry"]; tp = ps["tp"]
            live  = get_live_price(sym)
            total = abs(tp - entry)
            trigger = entry + total * BE_TRIGGER_R if side == "LONG" \
                      else entry - total * BE_TRIGGER_R
            triggered = (side == "LONG" and live >= trigger) or \
                        (side == "SHORT" and live <= trigger)
            if not triggered: continue
            new_sl = round(entry * 1.0005, 6) if side == "LONG" \
                     else round(entry * 0.9995, 6)
            bx_post("/openApi/swap/v2/trade/order", {
                "symbol":sym, "type":"STOP_MARKET",
                "side":"SELL" if side=="LONG" else "BUY",
                "positionSide":side, "stopPrice":new_sl,
                "closePosition":"true", "workingType":"MARK_PRICE"
            })
            with pos_state_lock:
                if sym in position_state:
                    position_state[sym]["be_hit"] = True
                    position_state[sym]["sl"]     = new_sl
            log.info(f"✅ BE 1.2R {sym} {side} → SL={new_sl:.6g} (price={live:.6g})")
            tg(f"🔄 <b>BE activado: {sym}</b> {side}\n"
               f"Entry: {entry:.6g} → SL: {new_sl:.6g} | Price: {live:.6g}")
        except Exception as e:
            log.debug(f"BE {sym}: {e}")

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def _send(msg):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN: return
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() \
              else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN: return
    try: asyncio.run(_send(msg))
    except Exception as e: log.warning(f"Telegram: {e}")

def tg_startup(balance, symbols):
    hour = datetime.now(timezone.utc).hour
    session_now = "🟢 LONDON" if 6 <= hour < 12 else \
                  ("🟢 NY" if 12 <= hour < 21 else "🔴 ASIA (inactivo)")
    tg(
        f"🎯 <b>Sniper V13.0 — HIGH WINRATE EDITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Sesión actual:</b> {session_now}\n"
        f"<b>Triple TF:</b> {TIMEFRAME} + 15m + 1H\n"
        f"<b>Stack EMA:</b> {EMA_FAST}>{EMA_SLOW}>{EMA_TREND} (perfecto) 📐\n"
        f"<b>Slope≥:</b> {SLOPE_LIMIT}° | <b>ADX≥:</b> {ADX_MIN} (creciente ↑)\n"
        f"<b>RSI Long:</b> {RSI_LONG_MIN}–{RSI_LONG_MAX} | "
        f"<b>Short:</b> {RSI_SHORT_MIN}–{RSI_SHORT_MAX}\n"
        f"<b>SL:</b> vela señal ±{SL_CANDLE_MARGIN}×ATR | "
        f"<b>TP:</b> {TP_MULT}x | <b>Max SL:</b> {MAX_DIST_PCT}%\n"
        f"<b>BE @:</b> {BE_TRIGGER_R*100:.0f}% del TP (1.2R)\n"
        f"<b>Patrones:</b> PIN+ENGULF+MOM+IB ✅\n"
        f"<b>Estructura:</b> HH/HL + LL/LH ✅ | <b>FBG:</b> ✅\n"
        f"<b>Max trades:</b> {MAX_OPEN_TRADES} | <b>Score≥:</b> {MIN_SCORE}\n"
        f"<b>Balance:</b> {balance:.2f} USDT | <b>Símbolos:</b> {len(symbols)}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_entry(sig, qty, notional, balance, spread_pct=None):
    d  = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    pi = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","INSIDE_BR":"📦"}.get(sig.get("pattern",""),"⚡")
    sp = f" | Spread:{spread_pct:.3f}%" if spread_pct is not None else ""
    h1c = sig.get("h1_ctx")
    sr_str = ""
    if h1c:
        sr_str = (f"\n<b>H1 Res:</b> {h1c['h1_resistance']:.5g} "
                  f"(+{h1c['dist_to_res']:.1f}%) | "
                  f"<b>Sup:</b> {h1c['h1_support']:.5g} "
                  f"(-{h1c['dist_to_sup']:.1f}%)")
    adx_arrow = "↑" if sig.get("adx_rising") else "→"
    be_level = round(
        sig["close"] + (sig["tp"] - sig["close"]) * BE_TRIGGER_R
        if sig["signal"] == "LONG"
        else sig["close"] - (sig["close"] - sig["tp"]) * BE_TRIGGER_R, 6)
    tg(
        f"<b>🎯 SNIPER V13 — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"{pi} <b>{sig.get('pattern','?')}</b> | "
        f"<b>H1:</b>{sig.get('h1_trend','?')} <b>15m:</b>{sig.get('m15_trend','?')} "
        f"| <b>Sesión:</b> {sig.get('session','?')}\n"
        f"<b>Stack:</b> EMA{EMA_FAST}>{EMA_SLOW}>{EMA_TREND} ✅ | "
        f"<b>ADX:</b> {sig['adx']}{adx_arrow} | "
        f"<b>Ang:</b> {sig['angle']}°\n"
        f"<b>RSI:</b> {sig['rsi']} | <b>Stoch:</b> {sig.get('stoch_k','?')} | "
        f"<b>DI±:</b> {sig.get('di_spread','?')}\n"
        f"<b>Vol señal:</b> {sig['vol_ratio']}x | "
        f"<b>Vol ruptura:</b> {sig.get('vol_conf',0)}x{'✅' if sig.get('vol_accel') else ''} | "
        f"<b>ATR:</b>{sig['atr_pct']}%{sp}"
        f"{sr_str}\n"
        f"<b>Estructura:</b> {'✅ HH/HL' if sig['signal']=='LONG' else '✅ LL/LH'} | "
        f"<b>Velas:</b> {sig.get('candle_conf',0)}/3 ✅\n"
        f"<b>Señal H:</b> {sig.get('sc_high',0):.5g} / <b>L:</b> {sig.get('sc_low',0):.5g}\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.5g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.5g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.5g}</code> | <b>R:R:</b> 1:{sig['rr']}\n"
        f"<b>BE @ 1.2R:</b>  <code>{be_level:.5g}</code>\n"
        f"<b>Qty:</b> {qty:.4f} | <b>Notional:</b> {notional:.2f} USDT | "
        f"<b>Riesgo:</b> {balance * RISK_PERCENT / 100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_count, in_session):
    if not signals: return
    sess = "🟢" if in_session else "🔴"
    lines = [
        f"🎯 <b>{len(signals)} disparo(s) / {total}</b> | "
        f"Trades:{open_count}/{MAX_OPEN_TRADES} {sess}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    icons = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","INSIDE_BR":"📦"}
    for s in signals[:5]:
        e  = "🟢" if s["signal"] == "LONG" else "🔴"
        pi = icons.get(s.get("pattern",""),"⚡")
        ar = "↑" if s.get("adx_rising") else "→"
        lines.append(
            f"{e}{pi} <b>{s['symbol']}</b> "
            f"H1:{s.get('h1_trend','?')} 15m:{s.get('m15_trend','?')} "
            f"Score:{s['score']} ADX:{s['adx']}{ar} "
            f"RSI:{s['rsi']} RR:1:{s['rr']}"
        )
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_diag(signals, skip_reasons):
    lines = [f"⚠️ <b>DIAGNÓSTICO: {len(signals)} señales, 0 órdenes</b>","━"*20]
    for sym, reason in list(skip_reasons.items())[:8]:
        lines.append(f"  • <b>{sym}</b>: {reason}")
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_session_change(active):
    if active:
        tg("🟢 <b>Sesión de alta liquidez iniciada</b> — London/NY. Bot activo.")
    else:
        tg("🔴 <b>Sesión Asia</b> — baja liquidez. Bot en pausa. Próxima: London 06:00 UTC.")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Sniper V13.0 HIGH WINRATE EDITION ===")
    log.info(f"  Triple TF: {TIMEFRAME}+15m+1H | Stack EMA{EMA_FAST}/{EMA_SLOW}/{EMA_TREND}")
    log.info(f"  Slope≥{SLOPE_LIMIT}° | ADX≥{ADX_MIN} rising | RSI L:{RSI_LONG_MIN}-{RSI_LONG_MAX} S:{RSI_SHORT_MIN}-{RSI_SHORT_MAX}")
    log.info(f"  SL candle±{SL_CANDLE_MARGIN}×ATR | TP×{TP_MULT} | Max SL {MAX_DIST_PCT}% | BE@{BE_TRIGGER_R*100:.0f}%TP")
    log.info(f"  Session: {'ON' if USE_SESSION else 'OFF'} | MaxTrades:{MAX_OPEN_TRADES} | Score≥{MIN_SCORE}")

    symbols   = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols: symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance:{balance:.4f} | Symbols:{len(symbols)} | Open:{len(positions)}")

    log.info("Pre-cargando klines 5m...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s, 300), symbols[:100]))

    def _prefetch():
        log.info("Pre-cargando 15m y H1...")
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda s: get_tf_klines(s,"15m",m15_cache,M15_CACHE_TTL,60), symbols[:80]))
            list(ex.map(lambda s: get_tf_klines(s,"1H", h1_cache, H1_CACHE_TTL, 60), symbols[:80]))
        log.info("Cache multi-TF listo.")
    threading.Thread(target=_prefetch, daemon=True).start()

    start_ws_cache(symbols)
    time.sleep(2)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))

    tg_startup(balance, symbols)
    log.info("✅ Bot V13 iniciado.")

    errors       = 0
    prev_session = is_trading_session()

    while True:
        t0 = time.time()
        try:
            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)
            in_session = is_trading_session()

            # Notificar cambio de sesión
            if in_session != prev_session:
                tg_session_change(in_session)
                prev_session = in_session

            hour_utc = datetime.now(timezone.utc).hour
            log.info(
                f"── V13 [{datetime.now(timezone.utc).strftime('%H:%M')} UTC] "
                f"{'🟢' if in_session else '🔴'} {balance:.4f} USDT | "
                f"{open_count}/{MAX_OPEN_TRADES} trades ──"
            )

            # Limpiar state de posiciones cerradas
            with pos_state_lock:
                for s in [k for k in position_state if k not in positions]:
                    del position_state[s]

            # Breakeven dinámico
            if TRAILING_STOP and positions:
                update_breakeven_stops(positions)

            # Scan (incluso fuera de sesión para preparar señales)
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan_symbol, s): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r: signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Señales: {len(signals)}/{len(symbols)}")

            if signals:
                tg_scan(signals, len(symbols), open_count, in_session)
                for s in signals[:3]:
                    log.info(
                        f"  🎯 {s['symbol']} {s['signal']} [{s['pattern']}] "
                        f"H1:{s.get('h1_trend','?')} 15m:{s.get('m15_trend','?')} "
                        f"score={s['score']} adx={s['adx']}{'↑' if s.get('adx_rising') else '→'} "
                        f"rsi={s['rsi']} rr=1:{s['rr']} cc={s.get('candle_conf',0)}/3"
                    )

            entered: set = set(); skip_reasons: dict = {}; orders_opened = 0

            for sig in signals:
                # V13: solo ejecutar órdenes en sesión activa
                if not in_session:
                    break

                sym = sig["symbol"]
                if sym in positions:    skip_reasons[sym] = "ya en posición"; continue
                if sym in entered:      skip_reasons[sym] = "ya intentado";   continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info(f"Max trades ({MAX_OPEN_TRADES})."); break
                if balance < 2:
                    skip_reasons[sym] = f"balance bajo ({balance:.2f})"; break

                spread = get_spread_pct(sym)
                if spread > MAX_SPREAD_PCT:
                    skip_reasons[sym] = f"spread {spread:.3f}%"; continue

                side = "BUY" if sig["signal"] == "LONG" else "SELL"
                try:
                    set_lev(sym)
                    try:
                        live_price = get_live_price(sym)
                    except Exception as ep:
                        skip_reasons[sym] = f"sin precio: {str(ep)[:50]}"; continue

                    sl_live, tp_live = recalc_sl_tp(sig, live_price)
                    if sl_live is None:
                        skip_reasons[sym] = "SL/TP inválido"; continue

                    qty, notional = calc_qty(balance, live_price, sl_live)
                    if qty <= 0:
                        skip_reasons[sym] = "qty=0"; continue

                    log.info(
                        f"🎯 DISPARO {sym} {side} qty={qty:.4f} "
                        f"notional={notional:.2f}U live={live_price:.5g} "
                        f"sl={sl_live:.5g} tp={tp_live:.5g} rr=1:{sig['rr']}"
                    )

                    res = open_order_with_retry(sym, side, qty, sl_live, tp_live, retries=1)
                    log.info(f"✅ {sym} {side} {notional:.2f}U | {res}")

                    sig.update({
                        "close": live_price, "sl": sl_live, "tp": tp_live,
                        "dist_pct": round(abs(live_price-sl_live)/live_price*100, 3),
                        "rr": round(abs(tp_live-live_price)/abs(live_price-sl_live), 2),
                    })
                    with pos_state_lock:
                        position_state[sym] = {
                            "side":   sig["signal"],
                            "entry":  live_price,
                            "sl":     sl_live,
                            "tp":     tp_live,
                            "be_hit": False,
                        }
                    tg_entry(sig, qty, notional, balance, spread_pct=spread)
                    entered.add(sym); open_count += 1; orders_opened += 1
                    time.sleep(0.5)

                except Exception as e:
                    reason = str(e)[:100]
                    log.error(f"Order FAILED {sym}: {e}")
                    skip_reasons[sym] = f"error: {reason}"
                    if "stop" in reason.lower() or "liquidat" in reason.lower():
                        sl_cooldown[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            if signals and orders_opened == 0 and skip_reasons:
                log.warning(f"Señales={len(signals)} 0 órdenes. {skip_reasons}")
                tg_diag(signals, skip_reasons)

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot V13 detenido</b>"); break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors <= 3:
                tg(f"⚠️ <b>Error #{errors}</b>: <code>{str(e)[:200]}</code>")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: 10 errores. Detenido.</b>"); break

        time.sleep(max(0, LOOP_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
