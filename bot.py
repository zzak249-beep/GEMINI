"""
EMA Slope + ADX + Sniper Multi-TF V12.0 — SNIPER EDITION
Fusión de V11 (Dual-TF, Candle Patterns) + Sniper Bot Pine Script V6.2

MEJORAS V12 sobre V11:
  1. ENTRADA EN RUPTURA: entra solo si la vela actual rompe el máximo/mínimo
     de la vela señal (como Pine Script: stop=high / stop=low)
  2. SL QUIRÚRGICO: exactamente bajo el MÍNIMO de la vela señal (- 0.2×ATR)
     En lugar de SL por ATR fijo → stops hasta 40% más ajustados
  3. H1 DUAL EMA 7/17: confirma tendencia H1 con EMA7 > EMA17 (Pine Script)
  4. BREAKEVEN AL 1.5R: cuando el precio llega al 50% del recorrido al TP,
     mueve el SL a precio de entrada (sistema de estado por posición)
  5. ENGULFING ESTRICTO: close > prev_high (long) / close < prev_low (short)
     Condición de ruptura real, no solo cuerpo
  6. PIN BAR EXACTO Pine Script: cola > 2×cuerpo + mecha contraria < cuerpo
  7. SCORE SNIPER: bonus +15 si la vela rompe el nivel con volumen
  8. POSICIÓN STATE: tracking de entry/tp/sl por símbolo para BE dinámico
  9. ANTI-CHOP MEJORADO: oscilación de ADX + rango relativo
 10. SCAN DE VELA SEÑAL: busca en i-1 (señal) + confirma ruptura en i (entry)
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

TIMEFRAME        = os.environ.get("TIMEFRAME",        "5m")   # marco de entrada
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.5"))
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "30"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "6"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "0"))

# ── FILTROS ───────────────────────────────────────────────────────────────────
MIN_SCORE        = float(os.environ.get("MIN_SCORE",      "55.0"))
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT",   "0.20"))   # V12: más ajustado
MAX_SPREAD_PCT   = float(os.environ.get("MAX_SPREAD_PCT", "0.15"))
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",    "4.0"))

# ── EMA ENTRADA (5m) — Tren Bala ─────────────────────────────────────────────
EMA_FAST         = int(os.environ.get("EMA_FAST",    "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",    "17"))
EMA_TREND        = int(os.environ.get("EMA_TREND",   "100"))  # filtro adicional 5m
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT","30.0"))
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",  "3"))

# ── H1 DUAL EMA (V12 Pine Script) ────────────────────────────────────────────
H1_EMA_FAST      = int(os.environ.get("H1_EMA_FAST", "7"))   # V12: EMA7 H1
H1_EMA_SLOW      = int(os.environ.get("H1_EMA_SLOW", "17"))  # V12: EMA17 H1
USE_H1_CONFIRM   = os.environ.get("USE_H1_CONFIRM","true").lower() == "true"
USE_H1_SR        = os.environ.get("USE_H1_SR",     "true").lower() == "true"
H1_SR_DIST_MIN   = float(os.environ.get("H1_SR_DIST_MIN","1.0"))
H1_CACHE_TTL     = int(os.environ.get("H1_CACHE_TTL","300"))

# ── ADX / RSI / VOLUME ───────────────────────────────────────────────────────
ADX_LEN          = int(os.environ.get("ADX_LEN",  "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN", "25.0"))
USE_ADX          = os.environ.get("USE_ADX","true").lower() == "true"
USE_DI           = os.environ.get("USE_DI", "true").lower() == "true"
RSI_LEN          = int(os.environ.get("RSI_LEN",  "14"))
RSI_OB           = float(os.environ.get("RSI_OB",  "72.0"))
RSI_OS           = float(os.environ.get("RSI_OS",  "28.0"))
USE_RSI          = os.environ.get("USE_RSI","true").lower() == "true"
USE_VOL          = os.environ.get("USE_VOL","true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT","1.3"))
ATR_LEN          = int(os.environ.get("ATR_LEN",  "14"))
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN", "3"))
ANTI_CHOP        = os.environ.get("ANTI_CHOP","true").lower() == "true"

# ── SL / TP (V12 Pine Script) ────────────────────────────────────────────────
# SL = bajo/encima del mínimo/máximo de la vela señal ± SL_CANDLE_ATR_MARGIN
SL_CANDLE_MARGIN = float(os.environ.get("SL_CANDLE_MARGIN","0.20"))  # 0.2×ATR (Pine Script)
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT",    "1.2"))    # fallback si no hay vela señal
TP_MULT          = float(os.environ.get("TP_MULT",        "3.0"))    # R:R 1:3
MIN_RR           = float(os.environ.get("MIN_RR",         "2.5"))
USE_CANDLE_PATTERNS = os.environ.get("USE_CANDLE_PATTERNS","true").lower() == "true"

# ── BREAKEVEN 1.5R (V12 Pine Script) ─────────────────────────────────────────
BE_TRIGGER_R     = float(os.environ.get("BE_TRIGGER_R",   "0.50"))   # al 50% del TP → BE
TRAILING_STOP    = os.environ.get("TRAILING_STOP","true").lower() == "true"

# ── POSITION SIZING ───────────────────────────────────────────────────────────
MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT","3.0"))
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT","40.0"))
MAX_MARGIN_PCT   = float(os.environ.get("MAX_MARGIN_PCT","25.0"))

# ── COOLDOWN ──────────────────────────────────────────────────────────────────
COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS","20"))
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
sl_cooldown     = {}   # {symbol: datetime}
h1_cache        = {}   # {symbol: (df, timestamp)}

# V12: estado de posición para gestión de breakeven dinámico
# {symbol: {"entry": float, "sl": float, "tp": float, "be_hit": bool, "side": str}}
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
    usdt = [c for c in contracts if isinstance(c,dict) and
            c.get("asset","")=="USDT" and c.get("status")==1]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c,dict) and
                str(c.get("symbol","")).endswith("-USDT")]
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
    log.warning(f"⚠️ Using fallback ({len(FALLBACK_SYMBOLS)} syms)")
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
            if not kdata:
                return
            row = {
                "open_time": pd.to_datetime(kdata.get("t", kdata.get("startTime",0)), unit="ms"),
                "open":  float(kdata.get("o",0)),
                "high":  float(kdata.get("h",0)),
                "low":   float(kdata.get("l",0)),
                "close": float(kdata.get("c",0)),
                "volume":float(kdata.get("v",0)),
            }
            if row["close"] == 0:
                return
            with ws_cache_lock:
                df = ws_kline_cache.get(sym)
                if df is None:
                    return
                if len(df) > 0 and df.iloc[-1]["open_time"] == row["open_time"]:
                    for col in ("open","high","low","close","volume"):
                        df.at[df.index[-1], col] = row[col]
                else:
                    ws_kline_cache[sym] = pd.concat(
                        [df, pd.DataFrame([row])], ignore_index=True).tail(400)
                ws_price_cache[sym] = row["close"]
    except Exception:
        pass

def _ws_on_error(ws_app, error):
    log.warning(f"WS error: {error}")
def _ws_on_close(ws_app, *args):
    log.info("WS closed — reconnecting in 5s")

def _ws_on_open(ws_app, symbols):
    ivl = INTERVAL_MAP.get(TIMEFRAME,"5m").lower()
    for sym in symbols[:200]:
        bx_sym = sym.replace("-","_")
        try:
            ws_app.send(json.dumps({
                "id": f"sub_{sym}", "reqType": "sub",
                "dataType": f"{bx_sym}@kline_{ivl}"
            }))
        except Exception:
            pass

def start_ws_cache(symbols):
    if not USE_WS_CACHE:
        return
    def _run():
        while True:
            try:
                ws_app = websocket.WebSocketApp(
                    BINGX_WS,
                    on_message=_ws_on_message,
                    on_error=_ws_on_error,
                    on_close=_ws_on_close,
                    on_open=lambda app: _ws_on_open(app, symbols)
                )
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.warning(f"WS thread error: {e}")
            time.sleep(5)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    log.info(f"✅ WebSocket cache iniciado ({min(len(symbols),200)} símbolos)")

# ── PRECIO EN VIVO ────────────────────────────────────────────────────────────
def get_live_price(symbol):
    if USE_WS_CACHE:
        with ws_cache_lock:
            p = ws_price_cache.get(symbol)
        if p and p > 0:
            return p
    errors = []
    try:
        data = bx_get("/openApi/swap/v2/quote/premiumIndex", {"symbol":symbol})
        items = data.get("data",[])
        if isinstance(items, list):
            for item in items:
                if item.get("symbol") == symbol:
                    mp = item.get("markPrice")
                    if mp: return float(mp)
        elif isinstance(items, dict) and items.get("symbol") == symbol:
            mp = items.get("markPrice")
            if mp: return float(mp)
    except Exception as e:
        errors.append(str(e))
    try:
        data2 = bx_get("/openApi/swap/v2/quote/ticker", {"symbol":symbol})
        t2 = data2.get("data",[])
        if isinstance(t2, list):
            for t in t2:
                if t.get("symbol") == symbol:
                    lp = t.get("lastPrice") or t.get("price")
                    if lp: return float(lp)
        elif isinstance(t2, dict):
            lp = t2.get("lastPrice") or t2.get("price")
            if lp: return float(lp)
    except Exception as e:
        errors.append(str(e))
    try:
        params = {"symbol":symbol,"interval":INTERVAL_MAP.get(TIMEFRAME,"5m"),"limit":2}
        data3 = bx_get("/openApi/swap/v3/quote/klines", params)
        rows = data3.get("data",[])
        if rows: return float(rows[-1][4])
    except Exception as e:
        errors.append(str(e))
    raise ValueError(f"get_live_price({symbol}) failed: {errors}")

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
        if ask > 0 and bid > 0:
            return (ask - bid) / bid * 100
        return 999.0
    except Exception:
        return 999.0

# ── KLINES ────────────────────────────────────────────────────────────────────
def get_klines(symbol, limit=300):
    if USE_WS_CACHE:
        with ws_cache_lock:
            df = ws_kline_cache.get(symbol)
        if df is not None and len(df) >= limit // 2:
            return df.copy()
    params = {"symbol":symbol,"interval":INTERVAL_MAP.get(TIMEFRAME,"5m"),"limit":limit}
    data = bx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data",[])
    if not rows or not isinstance(rows, list):
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open","high","low","close","volume"], inplace=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    if USE_WS_CACHE:
        with ws_cache_lock:
            ws_kline_cache[symbol] = df.copy()
    return df

def get_h1_klines(symbol, limit=60):
    now = time.time()
    cached = h1_cache.get(symbol)
    if cached:
        df_c, ts = cached
        if now - ts < H1_CACHE_TTL and len(df_c) >= 30:
            return df_c.copy()
    try:
        params = {"symbol":symbol,"interval":"1H","limit":limit}
        data = bx_get("/openApi/swap/v3/quote/klines", params)
        rows = data.get("data",[])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
        for col in ("open","high","low","close","volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.dropna(subset=["open","high","low","close"], inplace=True)
        df = df.sort_values("open_time").reset_index(drop=True)
        h1_cache[symbol] = (df.copy(), now)
        return df
    except Exception as e:
        log.debug(f"H1 klines {symbol}: {e}")
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
    """Ángulo normalizado por ATR: mismo cálculo que Pine Script."""
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
    tr_s  = wilder(tr)
    pdm_s = wilder(plus_dm)
    mdm_s = wilder(minus_dm)
    di_p  = 100 * pdm_s / tr_s.replace(0, np.nan)
    di_m  = 100 * mdm_s / tr_s.replace(0, np.nan)
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)
    adx   = dx.ewm(alpha=alpha, adjust=False).mean()
    return di_p, di_m, adx

def calc_rsi(close, period):
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

# ── ANÁLISIS H1 — EMA7/EMA17 (Pine Script V6.2) ───────────────────────────────
def analyze_h1(symbol):
    """
    V12: Igual que el Pine Script.
    htf_trend_long  = ema7_h1 > ema17_h1
    htf_trend_short = ema7_h1 < ema17_h1
    Añade S/R por pivotes y distancia para filtro.
    """
    df = get_h1_klines(symbol, limit=60)
    if df.empty or len(df) < 25:
        return None

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    ema7_h1  = close.ewm(span=H1_EMA_FAST, adjust=False).mean()
    ema17_h1 = close.ewm(span=H1_EMA_SLOW, adjust=False).mean()

    e7  = float(ema7_h1.iloc[-1])
    e17 = float(ema17_h1.iloc[-1])
    e7p = float(ema7_h1.iloc[-3]) if len(ema7_h1) > 3 else e7
    e17p= float(ema17_h1.iloc[-3]) if len(ema17_h1) > 3 else e17

    # Pine Script exact: bull = ema7 > ema17, bear = ema7 < ema17
    if e7 > e17:
        h1_trend = "BULL"
    elif e7 < e17:
        h1_trend = "BEAR"
    else:
        h1_trend = "NEUTRAL"

    # Momentum: la separación entre EMA7 y EMA17 se está ampliando?
    gap_now  = e7 - e17
    gap_prev = e7p - e17p
    gap_widening = (gap_now > gap_prev and h1_trend == "BULL") or \
                   (gap_now < gap_prev and h1_trend == "BEAR")

    close_now = float(close.iloc[-1])
    atr_h1    = calc_atr(high, low, close, 14)
    atr_h1_v  = float(atr_h1.iloc[-1])

    # Pivotes H1 para S/R
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
        "h1_trend":      h1_trend,
        "gap_widening":  gap_widening,
        "ema7_h1":       e7,
        "ema17_h1":      e17,
        "h1_resistance": h1_res,
        "h1_support":    h1_sup,
        "h1_atr":        atr_h1_v,
        "dist_to_res":   round((h1_res - close_now) / close_now * 100, 2),
        "dist_to_sup":   round((close_now - h1_sup) / close_now * 100, 2),
    }

# ── PATRONES DE VELA — PINE SCRIPT EXACT (V12) ───────────────────────────────

def detect_engulfing_pine(df, i, direction):
    """
    Pine Script exact:
    LONG:  close > open  AND prev_close < prev_open  AND close > prev_high
    SHORT: close < open  AND prev_close > prev_open  AND close < prev_low
    Retorna (bool, strength 0-100).
    """
    if i < 1:
        return False, 0.0

    c  = float(df["close"].iloc[i]);   o  = float(df["open"].iloc[i])
    h  = float(df["high"].iloc[i]);    l  = float(df["low"].iloc[i])
    cp = float(df["close"].iloc[i-1]); op = float(df["open"].iloc[i-1])
    hp = float(df["high"].iloc[i-1]);  lp = float(df["low"].iloc[i-1])

    body_cur = abs(c - o)
    body_pre = abs(cp - op)
    if body_pre < 1e-10:
        return False, 0.0

    if direction == "LONG":
        # Vela actual alcista, previa bajista, close actual rompe encima del high previo
        if c > o and cp < op and c > hp:
            strength = min((body_cur / body_pre) * 50, 100.0)
            return True, round(strength, 1)
    else:
        # Vela actual bajista, previa alcista, close actual rompe debajo del low previo
        if c < o and cp > op and c < lp:
            strength = min((body_cur / body_pre) * 50, 100.0)
            return True, round(strength, 1)

    return False, 0.0


def detect_pin_bar_pine(df, i, direction):
    """
    Pine Script exact:
    LONG:  close > open AND (open - low) > body_size * 2 AND (high - close) < body_size
    SHORT: close < open AND (high - open) > body_size * 2 AND (close - low) < body_size
    Retorna (bool, strength 0-100).
    """
    c  = float(df["close"].iloc[i])
    o  = float(df["open"].iloc[i])
    h  = float(df["high"].iloc[i])
    l  = float(df["low"].iloc[i])

    body_size = abs(c - o)
    if body_size < 1e-10:
        return False, 0.0

    total_range = h - l
    if total_range < 1e-10:
        return False, 0.0

    if direction == "LONG":
        lower_wick = o - l if c > o else c - l
        upper_wick = h - c if c > o else h - o
        if c > o and lower_wick > body_size * 2 and upper_wick < body_size:
            tail_ratio = lower_wick / total_range
            strength = min(tail_ratio * 130, 100.0)
            return True, round(strength, 1)
    else:
        upper_wick = h - o if c < o else h - c
        lower_wick = c - l if c < o else o - l
        if c < o and upper_wick > body_size * 2 and lower_wick < body_size:
            tail_ratio = upper_wick / total_range
            strength = min(tail_ratio * 130, 100.0)
            return True, round(strength, 1)

    return False, 0.0


def detect_momentum_candle(df, i, direction, atr):
    """Vela de impulso: cuerpo ≥65% del rango, ≥0.5×ATR, mecha contraria mínima."""
    o = float(df["open"].iloc[i])
    h = float(df["high"].iloc[i])
    l = float(df["low"].iloc[i])
    c = float(df["close"].iloc[i])

    total_range = h - l
    if total_range < 1e-10 or atr < 1e-10:
        return False, 0.0

    body = abs(c - o)
    if body / total_range < 0.65 or body < atr * 0.5:
        return False, 0.0

    if direction == "LONG" and c > o:
        if (h - c) < body * 0.35:
            return True, round(min(body/total_range * 90, 100.0), 1)
    elif direction == "SHORT" and c < o:
        if (c - l) < body * 0.35:
            return True, round(min(body/total_range * 90, 100.0), 1)

    return False, 0.0


def detect_inside_bar_breakout(df, i, direction):
    """Compresión (inside bar en i-1) seguida de breakout en i."""
    if i < 2:
        return False, 0.0
    h_m2 = float(df["high"].iloc[i-2]); l_m2 = float(df["low"].iloc[i-2])
    h_m1 = float(df["high"].iloc[i-1]); l_m1 = float(df["low"].iloc[i-1])
    c     = float(df["close"].iloc[i])
    if not (h_m1 <= h_m2 and l_m1 >= l_m2):
        return False, 0.0
    if direction == "LONG"  and c > h_m2: return True, 60.0
    if direction == "SHORT" and c < l_m2: return True, 60.0
    return False, 0.0


def is_choppy_market(df, adx_val):
    if not ANTI_CHOP:
        return False
    if adx_val < ADX_MIN:
        return True
    if len(df) < 15:
        return False
    recent    = df.iloc[-12:]
    avg_range = float((recent["high"] - recent["low"]).mean())
    atr_s     = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
    if float(atr_s.iloc[-1]) > 0 and avg_range < float(atr_s.iloc[-1]) * 0.75:
        return True
    return False

# ── ESTRATEGIA PRINCIPAL V12 — SNIPER DUAL-TF ────────────────────────────────
def scan_symbol(symbol):
    """
    V12 SNIPER:
    1. Busca vela SEÑAL en i-1 (pin bar / engulfing / momentum) con slope ≥30°
    2. Confirma RUPTURA en i: precio actual rompe high[i-1] (long) / low[i-1] (short)
    3. SL = low[i-1] - 0.2×ATR (long) / high[i-1] + 0.2×ATR (short) — Pine Script
    4. TP = entry + (entry - SL) × 3.0
    5. H1: solo long si ema7_h1 > ema17_h1, solo short si ema7_h1 < ema17_h1
    """
    if symbol in sl_cooldown:
        elapsed = (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds() / 60
        if elapsed < COOLDOWN_MINS:
            return None

    try:
        df = get_klines(symbol, limit=300)
        min_bars = max(EMA_TREND + 10, ADX_LEN * 2 + 5, RSI_LEN + 5, 60)
        if df.empty or len(df) < min_bars:
            return None

        # ── INDICADORES ───────────────────────────────────────────────────────
        atr_s     = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
        ema_f     = df["close"].ewm(span=EMA_FAST,  adjust=False).mean()
        ema_s     = df["close"].ewm(span=EMA_SLOW,  adjust=False).mean()
        ema_trend = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
        angle     = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        di_p, di_m, adx_s = calc_adx(df["high"], df["low"], df["close"], ADX_LEN)
        rsi_s     = calc_rsi(df["close"], RSI_LEN)
        vol_ma    = df["volume"].rolling(20).mean()

        # Índices: sig = vela señal (i-1), conf = vela de confirmación/ruptura (i)
        sig  = len(df) - 2   # vela señal (última cerrada)
        conf = len(df) - 2   # usamos la misma vela como señal+confirmación en 5m
        # En 5m el ciclo de 30s captura la vela recién cerrada, que YA rompió
        # el nivel de la ante-anterior. Así somos más estrictos:
        signal_candle_idx = len(df) - 3   # vela que forma el patrón
        break_candle_idx  = len(df) - 2   # vela que confirma la ruptura

        if signal_candle_idx < max(EMA_TREND + 2, ADX_LEN * 2, 50):
            return None

        sc = signal_candle_idx   # índice vela señal
        bc = break_candle_idx    # índice vela ruptura

        # Datos de la vela SEÑAL
        sc_close  = float(df["close"].iloc[sc])
        sc_open   = float(df["open"].iloc[sc])
        sc_high   = float(df["high"].iloc[sc])
        sc_low    = float(df["low"].iloc[sc])

        # Datos de la vela RUPTURA (confirma el movimiento)
        bc_close  = float(df["close"].iloc[bc])
        bc_high   = float(df["high"].iloc[bc])
        bc_low    = float(df["low"].iloc[bc])
        bc_vol    = float(df["volume"].iloc[bc])

        # Indicadores en la vela señal
        catr      = float(atr_s.iloc[sc])
        angle_now = float(angle.iloc[sc])
        adx_now   = float(adx_s.iloc[sc])
        di_p_now  = float(di_p.iloc[sc])
        di_m_now  = float(di_m.iloc[sc])
        rsi_now   = float(rsi_s.iloc[sc])
        ema_f_sc  = float(ema_f.iloc[sc])
        ema_s_sc  = float(ema_s.iloc[sc])
        ema_f_pr  = float(ema_f.iloc[sc-1])
        ema_trend_sc = float(ema_trend.iloc[sc])
        vma       = float(vol_ma.iloc[sc])
        bc_vma    = float(vol_ma.iloc[bc])

        if any(np.isnan(x) for x in [angle_now, adx_now, catr, ema_f_sc,
                                      ema_s_sc, ema_trend_sc, rsi_now]):
            return None
        if vma <= 0 or catr <= 0:
            return None

        atr_pct = (catr / sc_close) * 100
        if atr_pct > ATR_MAX_PCT:
            return None

        # Anti-chop en ventana de velas
        if is_choppy_market(df.iloc[:sc+1], adx_now):
            return None

        vratio_sig  = round(float(df["volume"].iloc[sc]) / vma, 2) if vma > 0 else 0.0
        vratio_conf = round(bc_vol / bc_vma, 2) if bc_vma > 0 else 0.0

        # ── CONDICIONES EMA/ADX EN VELA SEÑAL ────────────────────────────────
        trend_long  = sc_close > ema_trend_sc
        trend_short = sc_close < ema_trend_sc

        cross_long  = ema_f_sc > ema_s_sc
        cross_short = ema_f_sc < ema_s_sc

        angle_long  = angle_now >= SLOPE_LIMIT    # ≥30°
        angle_short = angle_now <= -SLOPE_LIMIT

        accel_long  = ema_f_sc > ema_f_pr
        accel_short = ema_f_sc < ema_f_pr

        gap_now  = ema_f_sc - ema_s_sc
        gap_prev = ema_f_pr - float(ema_s.iloc[sc-1])
        widening_long  = gap_now > gap_prev
        widening_short = gap_now < gap_prev

        vol_ok       = (not USE_VOL) or (vratio_sig >= VOL_MULT)
        adx_ok       = (not USE_ADX) or (adx_now > ADX_MIN)
        rsi_long_ok  = (not USE_RSI) or (rsi_now < RSI_OB)
        rsi_short_ok = (not USE_RSI) or (rsi_now > RSI_OS)
        di_long_ok   = (not USE_DI)  or (di_p_now > di_m_now)
        di_short_ok  = (not USE_DI)  or (di_m_now > di_p_now)

        base_long_sc = (cross_long and angle_long and adx_ok and vol_ok and
                        trend_long and rsi_long_ok and di_long_ok and
                        accel_long and widening_long)
        base_short_sc = (cross_short and angle_short and adx_ok and vol_ok and
                         trend_short and rsi_short_ok and di_short_ok and
                         accel_short and widening_short)

        if not base_long_sc and not base_short_sc:
            return None

        # ── DETECCIÓN PATRÓN EN VELA SEÑAL ───────────────────────────────────
        if not USE_CANDLE_PATTERNS:
            # Sin patrones: exige que la vela de confirmación supere el high/low señal
            if base_long_sc and bc_close > sc_high:
                pattern_name  = "BREAKOUT"
                pattern_score = 50.0
            elif base_short_sc and bc_close < sc_low:
                pattern_name  = "BREAKOUT"
                pattern_score = 50.0
            else:
                return None
        else:
            # Detectar patrón en la vela señal
            direction_probe = "LONG" if base_long_sc else "SHORT"

            is_eng, eng_str = detect_engulfing_pine(df, sc, direction_probe)
            is_pin, pin_str = detect_pin_bar_pine(df, sc, direction_probe)
            is_mom, mom_str = detect_momentum_candle(df, sc, direction_probe, catr)
            is_ib,  ib_str  = detect_inside_bar_breakout(df, sc, direction_probe)

            has_pattern = is_eng or is_pin or is_mom or is_ib
            if not has_pattern:
                return None

            if is_pin:
                pattern_name  = "PIN_BAR"
                pattern_score = pin_str
            elif is_eng:
                pattern_name  = "ENGULF"
                pattern_score = eng_str
            elif is_mom:
                pattern_name  = "MOMENTUM"
                pattern_score = mom_str
            else:
                pattern_name  = "INSIDE_BR"
                pattern_score = ib_str

            # V12 CLAVE: confirmar que vela de ruptura (bc) ha superado el nivel
            if base_long_sc and bc_close <= sc_high:
                return None   # precio no rompió el high de la vela señal → esperar
            if base_short_sc and bc_close >= sc_low:
                return None   # precio no rompió el low de la vela señal → esperar

        direction = "LONG" if base_long_sc else "SHORT"

        # ── H1 CONFIRMACIÓN (Pine Script: solo Long si H1 bull) ──────────────
        h1_ctx   = None
        h1_bonus = 0
        h1_trend = "UNKNOWN"

        if USE_H1_CONFIRM:
            h1_ctx = analyze_h1(symbol)
            if h1_ctx:
                h1_trend = h1_ctx["h1_trend"]
                if direction == "LONG"  and h1_trend == "BULL":
                    h1_bonus = 20
                    if h1_ctx.get("gap_widening"): h1_bonus += 5
                elif direction == "SHORT" and h1_trend == "BEAR":
                    h1_bonus = 20
                    if h1_ctx.get("gap_widening"): h1_bonus += 5
                elif h1_trend == "NEUTRAL":
                    h1_bonus = 3
                else:
                    return None   # contra-tendencia H1 → descartar

                if USE_H1_SR and h1_ctx:
                    if direction == "LONG"  and h1_ctx["dist_to_res"] < H1_SR_DIST_MIN:
                        return None
                    if direction == "SHORT" and h1_ctx["dist_to_sup"] < H1_SR_DIST_MIN:
                        return None

        # ── SL QUIRÚRGICO — Pine Script ───────────────────────────────────────
        # SL = bajo/encima del mínimo/máximo de la VELA SEÑAL ± 0.2×ATR
        sl_margin = catr * SL_CANDLE_MARGIN

        if direction == "LONG":
            sl_price = sc_low  - sl_margin
            # Garantizar distancia mínima
            entry_ref = bc_close   # precio de cierre de confirmación (proxy de entrada)
            if (entry_ref - sl_price) / entry_ref * 100 < MIN_DIST_PCT:
                sl_price = entry_ref * (1 - MIN_DIST_PCT / 100)
            if sl_price >= entry_ref:
                return None
            tp_price = entry_ref + (entry_ref - sl_price) * TP_MULT
        else:
            sl_price = sc_high + sl_margin
            entry_ref = bc_close
            if (sl_price - entry_ref) / entry_ref * 100 < MIN_DIST_PCT:
                sl_price = entry_ref * (1 + MIN_DIST_PCT / 100)
            if sl_price <= entry_ref:
                return None
            tp_price = entry_ref - (sl_price - entry_ref) * TP_MULT

        dist     = abs(entry_ref - sl_price)
        dist_pct = (dist / entry_ref) * 100

        if dist_pct < MIN_DIST_PCT:
            return None

        rr = abs(tp_price - entry_ref) / dist
        if rr < MIN_RR:
            return None

        # ── BONUS: ruptura de confirmación con volumen ────────────────────────
        vol_breakout_bonus = min(vratio_conf * 4, 15) if vratio_conf >= VOL_MULT else 0

        # ── SCORING V12 ───────────────────────────────────────────────────────
        score  = min(abs(angle_now) / SLOPE_LIMIT * 28, 28)     # ángulo: max 28
        score += min((adx_now - ADX_MIN) / ADX_MIN * 18, 18)    # ADX: max 18
        score += h1_bonus                                         # H1: max 25
        score += min(pattern_score / 8, 14)                      # patrón: max 14
        score += vol_breakout_bonus                               # vol ruptura: max 15
        score += min(vratio_sig * 4, 8)                          # vol señal: max 8
        score += min((rr - MIN_RR) * 2, 5)                      # R:R extra: max 5
        score += min(abs(di_p_now - di_m_now) / 10, 4)          # DI spread: max 4

        if score < MIN_SCORE:
            return None

        method_str = f"SNIPER|{pattern_name}|H1:{h1_trend}|{TIMEFRAME}"

        return {
            "symbol":        symbol,
            "signal":        direction,
            "method":        method_str,
            "pattern":       pattern_name,
            "close":         entry_ref,       # precio de la vela de ruptura
            "sc_high":       sc_high,         # high vela señal
            "sc_low":        sc_low,          # low vela señal
            "sl":            round(sl_price, 6),
            "tp":            round(tp_price, 6),
            "atr":           catr,
            "atr_pct":       round(atr_pct, 2),
            "vol_ratio":     vratio_sig,
            "vol_conf":      vratio_conf,
            "angle":         round(angle_now, 1),
            "adx":           round(adx_now, 1),
            "rsi":           round(rsi_now, 1),
            "score":         round(score, 1),
            "rr":            round(rr, 2),
            "dist_pct":      round(dist_pct, 3),
            "di_spread":     round(abs(di_p_now - di_m_now), 1),
            "h1_trend":      h1_trend,
            "h1_ctx":        h1_ctx,
            "pat_score":     round(pattern_score, 1),
        }

    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

# ── RECALC SL/TP CON PRECIO EN VIVO ──────────────────────────────────────────
def recalc_sl_tp(sig, live_price):
    """
    V12: mantiene el SL basado en la vela señal (sc_low / sc_high),
    solo ajusta si el live_price cambió significativamente.
    """
    catr      = sig["atr"]
    direction = sig["signal"]
    sl_margin = catr * SL_CANDLE_MARGIN

    if direction == "LONG":
        sl_price = sig["sc_low"] - sl_margin
        if (live_price - sl_price) / live_price * 100 < MIN_DIST_PCT:
            sl_price = live_price * (1 - MIN_DIST_PCT / 100)
        if sl_price >= live_price:
            return None, None
        tp_price = live_price + (live_price - sl_price) * TP_MULT
    else:
        sl_price = sig["sc_high"] + sl_margin
        if (sl_price - live_price) / live_price * 100 < MIN_DIST_PCT:
            sl_price = live_price * (1 + MIN_DIST_PCT / 100)
        if sl_price <= live_price:
            return None, None
        tp_price = live_price - (sl_price - live_price) * TP_MULT

    rr = abs(tp_price - live_price) / abs(live_price - sl_price)
    if rr < MIN_RR:
        return None, None

    return round(sl_price, 6), round(tp_price, 6)

# ── POSITION SIZING (FÓRMULA CORRECTA) ───────────────────────────────────────
def calc_qty(balance, entry, sl):
    """
    notional = risk_usdt / dist_pct
    Con stops ajustados (0.3-0.8%) el notional será mayor → trades más grandes.
    """
    dist_pct = abs(entry - sl) / entry
    if dist_pct < 1e-8:
        return 0, 0

    risk_usdt    = balance * (RISK_PERCENT / 100)
    notional     = risk_usdt / dist_pct
    max_margin   = balance * (MAX_MARGIN_PCT / 100)
    max_notional = min(MAX_ORDER_USDT, max_margin * LEVERAGE)
    notional     = max(MIN_ORDER_USDT, min(notional, max_notional))
    qty          = notional / entry

    return round(max(qty, 0.001), 4), round(notional, 2)

# ── ORDEN ─────────────────────────────────────────────────────────────────────
def open_order(symbol, side, qty, sl, tp):
    payload = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type":         "MARKET",
        "quantity":     round(qty, 4),
        "stopLoss": json.dumps({
            "type": "STOP_MARKET", "stopPrice": round(sl, 6),
            "workingType": "MARK_PRICE"
        }),
        "takeProfit": json.dumps({
            "type": "TAKE_PROFIT_MARKET", "stopPrice": round(tp, 6),
            "workingType": "MARK_PRICE"
        }),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    code = resp.get("code", -1)
    if code != 0:
        raise ValueError(f"BingX code={code}: {resp.get('msg','unknown')}")
    return resp

def open_order_with_retry(symbol, side, qty, sl, tp, retries=1):
    for attempt in range(retries + 1):
        try:
            return open_order(symbol, side, qty, sl, tp)
        except ValueError as e:
            if "101400" in str(e) and attempt < retries:
                log.warning(f"Error 101400 en {symbol}, reintentando...")
                time.sleep(1)
                try:
                    fp = get_live_price(symbol)
                    if side == "BUY":
                        sl = round(fp * (1 - MIN_DIST_PCT / 100), 6)
                        tp = round(fp + (fp - sl) * TP_MULT, 6)
                    else:
                        sl = round(fp * (1 + MIN_DIST_PCT / 100), 6)
                        tp = round(fp - (sl - fp) * TP_MULT, 6)
                except Exception as ep:
                    raise
            else:
                raise

# ── GESTIÓN DINÁMICA — BREAKEVEN AL 1.5R (Pine Script) ───────────────────────
def update_breakeven_stops(positions):
    """
    V12: Pine Script breakeven logic.
    Cuando precio alcanza el 50% del camino al TP (1.5R), mueve SL a entry.
    Usa position_state para recordar si ya se activó el BE por símbolo.
    """
    if not TRAILING_STOP or not positions:
        return

    with pos_state_lock:
        state_copy = dict(position_state)

    for sym, pos in positions.items():
        try:
            ps = state_copy.get(sym)
            if not ps:
                continue

            if ps.get("be_hit"):
                continue   # ya en breakeven, no modificar

            side  = ps["side"]
            entry = ps["entry"]
            tp    = ps["tp"]
            sl    = ps["sl"]

            live = get_live_price(sym)
            total_dist = abs(tp - entry)
            be_trigger = entry + total_dist * BE_TRIGGER_R if side == "LONG" \
                         else entry - total_dist * BE_TRIGGER_R

            # ¿Precio alcanzó el punto de BE (50% hacia TP = 1.5R)?
            triggered = (side == "LONG"  and live >= be_trigger) or \
                        (side == "SHORT" and live <= be_trigger)

            if not triggered:
                continue

            # Mover SL a entry + pequeño buffer (0.05%)
            new_sl = round(entry * 1.0005, 6) if side == "LONG" \
                     else round(entry * 0.9995, 6)

            # Colocar nuevo SL en BingX
            bx_post("/openApi/swap/v2/trade/order", {
                "symbol":        sym,
                "type":          "STOP_MARKET",
                "side":          "SELL" if side == "LONG" else "BUY",
                "positionSide":  side,
                "stopPrice":     new_sl,
                "closePosition": "true",
                "workingType":   "MARK_PRICE"
            })

            with pos_state_lock:
                if sym in position_state:
                    position_state[sym]["be_hit"] = True
                    position_state[sym]["sl"] = new_sl

            log.info(f"✅ BE 1.5R activado: {sym} {side} entry={entry:.6g} → SL={new_sl:.6g} "
                     f"(price={live:.6g}, trigger={be_trigger:.6g})")
            tg(f"🔄 <b>Breakeven activado: {sym}</b>\n"
               f"<b>Side:</b> {side} | <b>Entry:</b> {entry:.6g}\n"
               f"<b>Nuevo SL:</b> {new_sl:.6g} (price: {live:.6g})\n"
               f"<b>TP objetivo:</b> {tp:.6g}")

        except Exception as e:
            log.debug(f"BE update {sym}: {e}")

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def _send(msg):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN: return
    try:
        asyncio.run(_send(msg))
    except Exception as e:
        log.warning(f"Telegram error: {e}")

def tg_startup(balance, symbols):
    tg(
        f"🎯 <b>EMA+ADX+Sniper Elite V12.0 — SNIPER EDITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔀 <b>TF:</b> {TIMEFRAME} entradas + <b>H1 filtro</b> EMA7/17\n"
        f"<b>EMA:</b> {EMA_FAST}/{EMA_SLOW} (entrada) | "
        f"<b>H1:</b> EMA{H1_EMA_FAST}/{H1_EMA_SLOW}\n"
        f"<b>Slope≥:</b> {SLOPE_LIMIT}° 🚄 | <b>ADX≥:</b> {ADX_MIN}\n"
        f"<b>SL:</b> Vela señal ±{SL_CANDLE_MARGIN}×ATR 🎯\n"
        f"<b>TP mult:</b> {TP_MULT}x | <b>Min R:R:</b> {MIN_RR} | "
        f"<b>Score≥:</b> {MIN_SCORE}\n"
        f"<b>BE @ 1.5R:</b> ✅ (50% hacia TP)\n"
        f"<b>Patrones:</b> PIN+ENGULF+MOM+INSIDE_BR (Pine Script exact)\n"
        f"<b>Ruptura confirmada:</b> ✅ break de high/low señal\n"
        f"<b>Pos size:</b> {MIN_ORDER_USDT}–{MAX_ORDER_USDT} USDT | "
        f"<b>Max trades:</b> {MAX_OPEN_TRADES}\n"
        f"<b>Balance:</b> {balance:.2f} USDT | <b>Símbolos:</b> {len(symbols)}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_entry(sig, qty, notional, balance, spread_pct=None):
    d  = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    pi = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥",
          "INSIDE_BR":"📦","BREAKOUT":"🚀"}.get(sig.get("pattern",""),"⚡")
    h1 = sig.get("h1_trend","?")
    sp = f" | Spread: {spread_pct:.3f}%" if spread_pct is not None else ""
    h1c = sig.get("h1_ctx")
    sr_str = ""
    if h1c:
        sr_str = (f"\n<b>H1 Res:</b> {h1c['h1_resistance']:.6g} "
                  f"(+{h1c['dist_to_res']:.1f}%) | "
                  f"<b>H1 Sup:</b> {h1c['h1_support']:.6g} "
                  f"(-{h1c['dist_to_sup']:.1f}%)")
    tg(
        f"<b>✅ SNIPER DISPARADO — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"{pi} <b>Patrón:</b> {sig.get('pattern','?')} | "
        f"<b>H1:</b> {h1} | <b>R:R:</b> 1:{sig['rr']}\n"
        f"<b>Ang:</b> {sig['angle']}° | <b>ADX:</b> {sig['adx']} | "
        f"<b>RSI:</b> {sig['rsi']} | <b>DI±:</b> {sig.get('di_spread','?')}\n"
        f"<b>Vol señal:</b> {sig['vol_ratio']}x | "
        f"<b>Vol ruptura:</b> {sig.get('vol_conf',0)}x | "
        f"<b>ATR:</b> {sig['atr_pct']}%{sp}"
        f"{sr_str}\n"
        f"<b>Vela señal:</b> H={sig.get('sc_high',0):.6g} / L={sig.get('sc_low',0):.6g}\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.6g}</code>\n"
        f"<b>BE activa @:</b> ~{round(sig['close'] + (sig['tp']-sig['close'])*BE_TRIGGER_R, 6) if sig['signal']=='LONG' else round(sig['close'] - (sig['close']-sig['tp'])*BE_TRIGGER_R, 6):.6g}\n"
        f"<b>Qty:</b> {qty:.4f} | <b>Notional:</b> {notional:.2f} USDT | "
        f"<b>Riesgo:</b> {balance * RISK_PERCENT / 100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_count):
    if not signals: return
    lines = [
        f"🎯 <b>{len(signals)} disparo(s) / {total}</b> | Trades: {open_count}/{MAX_OPEN_TRADES}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    icons = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","INSIDE_BR":"📦","BREAKOUT":"🚀"}
    for s in signals[:6]:
        e = "🟢" if s["signal"] == "LONG" else "🔴"
        pi = icons.get(s.get("pattern",""),"⚡")
        lines.append(
            f"{e}{pi} <b>{s['symbol']}</b> H1:{s.get('h1_trend','?')} "
            f"Score:{s['score']} Ang:{s['angle']}° ADX:{s['adx']} "
            f"RSI:{s['rsi']} RR:1:{s['rr']} VC:{s.get('vol_conf',0)}x"
        )
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_diag(signals, skip_reasons):
    lines = [
        f"⚠️ <b>DIAGNÓSTICO: {len(signals)} señales, 0 órdenes</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for sym, reason in list(skip_reasons.items())[:8]:
        lines.append(f"  • <b>{sym}</b>: {reason}")
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== EMA+ADX+Sniper V12.0 — SNIPER EDITION ===")
    log.info(f"  TF: {TIMEFRAME} | H1 EMA{H1_EMA_FAST}/{H1_EMA_SLOW}")
    log.info(f"  Slope≥{SLOPE_LIMIT}° | ADX≥{ADX_MIN} | SL±{SL_CANDLE_MARGIN}×ATR")
    log.info(f"  TP×{TP_MULT} | Min R:R {MIN_RR} | BE@{BE_TRIGGER_R*100:.0f}% TP | Score≥{MIN_SCORE}")
    log.info(f"  Pos: {MIN_ORDER_USDT}–{MAX_ORDER_USDT} USDT | Cooldown: {COOLDOWN_MINS}min")

    symbols   = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols:
        symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance: {balance:.4f} | Symbols: {len(symbols)} | Open: {len(positions)}")

    # Pre-cargar klines REST
    log.info("Pre-cargando klines 5m...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s, 300), symbols[:100]))

    # Pre-cargar H1 en background
    def _prefetch_h1():
        log.info("Pre-cargando klines H1...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(lambda s: get_h1_klines(s, 60), symbols[:80]))
        log.info("H1 cache listo.")
    threading.Thread(target=_prefetch_h1, daemon=True).start()

    start_ws_cache(symbols)
    time.sleep(2)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))

    tg_startup(balance, symbols)
    log.info("✅ Bot V12 iniciado.")

    errors = 0

    while True:
        t0 = time.time()
        try:
            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)

            log.info(
                f"── V12 [SNIPER] {balance:.4f} USDT | "
                f"{open_count}/{MAX_OPEN_TRADES} trades | {len(symbols)} sym ──"
            )

            # Limpiar state de posiciones cerradas
            with pos_state_lock:
                closed = [s for s in position_state if s not in positions]
                for s in closed:
                    del position_state[s]

            # Breakeven dinámico 1.5R
            if TRAILING_STOP and positions:
                update_breakeven_stops(positions)

            # Scan paralelo
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan_symbol, s): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r:
                        signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Señales: {len(signals)}/{len(symbols)}")

            if signals:
                tg_scan(signals, len(symbols), open_count)
                for s in signals[:5]:
                    log.info(
                        f"  🎯 {s['symbol']} {s['signal']} [{s['pattern']}] "
                        f"H1:{s.get('h1_trend','?')} score={s['score']} "
                        f"ang={s['angle']}° adx={s['adx']} rr=1:{s['rr']} "
                        f"vc={s.get('vol_conf',0)}x"
                    )

            entered      : set  = set()
            skip_reasons : dict = {}
            orders_opened = 0

            for sig in signals:
                sym = sig["symbol"]

                if sym in positions:
                    skip_reasons[sym] = "ya en posición"; continue
                if sym in entered:
                    skip_reasons[sym] = "ya intentado"; continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info(f"Max trades ({MAX_OPEN_TRADES}) alcanzado."); break
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
                        f"SNIPER {sym} {side} qty={qty:.4f} "
                        f"notional={notional:.2f}U live={live_price:.6g} "
                        f"sl={sl_live:.6g} tp={tp_live:.6g} spread={spread:.3f}%"
                    )

                    res = open_order_with_retry(sym, side, qty, sl_live, tp_live, retries=1)
                    log.info(f"✅ {sym} {side} qty={qty:.4f} {notional:.2f}U | {res}")

                    # Actualizar señal con precios reales
                    sig["close"]    = live_price
                    sig["sl"]       = sl_live
                    sig["tp"]       = tp_live
                    sig["dist_pct"] = round(abs(live_price - sl_live) / live_price * 100, 3)
                    sig["rr"]       = round(abs(tp_live - live_price) / abs(live_price - sl_live), 2)

                    # Registrar estado de posición para BE management
                    with pos_state_lock:
                        position_state[sym] = {
                            "side":    sig["signal"],
                            "entry":   live_price,
                            "sl":      sl_live,
                            "tp":      tp_live,
                            "be_hit":  False,
                        }

                    tg_entry(sig, qty, notional, balance, spread_pct=spread)
                    entered.add(sym)
                    open_count    += 1
                    orders_opened += 1
                    time.sleep(0.5)

                except Exception as e:
                    reason = str(e)[:100]
                    log.error(f"Order FAILED {sym}: {e}")
                    skip_reasons[sym] = f"error: {reason}"
                    if "stop" in reason.lower() or "liquidat" in reason.lower():
                        sl_cooldown[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            if signals and orders_opened == 0 and skip_reasons:
                log.warning(f"Señales={len(signals)} 0 órdenes. Razones: {skip_reasons}")
                tg_diag(signals, skip_reasons)

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot V12 detenido</b>")
            break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors <= 3:
                tg(f"⚠️ <b>Error ciclo #{errors}</b>: <code>{str(e)[:200]}</code>")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: 10 errores. Detenido.</b>"); break

        time.sleep(max(0, LOOP_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
