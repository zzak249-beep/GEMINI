"""
EMA Slope + ADX + Multi-Timeframe Elite V11.0 — BULLET TRAIN EDITION

MEJORAS V11 sobre V10:
  1. DUAL TIMEFRAME: 5m entradas + 1H tendencia/S&R ("golden setup")
  2. EMA 7/17 en 5m con pendiente ≥30° (estrategia "tren bala")
  3. PATRONES DE VELA: Pin Bar, Engulfing, Vela Momentum (entradas de precisión)
  4. R:R OBJETIVO 3:1: TP = 3× distancia SL, stop ajustado en vela señal
  5. ANTI-CHOP: ADX≥25 + rango normalizado anti-mercado-lateral
  6. H1 S/R ZONES: no comprar en resistencia H1, no vender en soporte H1
  7. GOLDEN SETUP: M5 + H1 alineados = señal de máxima calidad (+20 score)
  8. POSITION SIZING CORRECTO: riesgo_usdt / dist_sl% (sin error de leverage)
  9. STOP EN VELA SEÑAL: SL debajo del mínimo / encima del máximo exacto
 10. H1 CACHE TTL: klines H1 con cache de 5min para no sobrecargar API
"""
import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np
import websocket  # pip install websocket-client

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

# V11: 5m entradas, 1H tendencia
TIMEFRAME        = os.environ.get("TIMEFRAME",        "5m")
H1_TIMEFRAME     = os.environ.get("H1_TIMEFRAME",     "1h")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.5"))   # V11: 1→1.5%
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "30"))    # V11: 45→30
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "6"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "0"))

# ── FILTROS DE CALIDAD ────────────────────────────────────────────────────────
MIN_SCORE        = float(os.environ.get("MIN_SCORE",      "55.0"))   # V11: 45→55
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT",   "0.25"))   # V11: más ajustado
MAX_SPREAD_PCT   = float(os.environ.get("MAX_SPREAD_PCT", "0.15"))
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",    "3.5"))    # V11: más permisivo en 5m

# ── EMA SLOPE — ESTRATEGIA "TREN BALA" ───────────────────────────────────────
EMA_FAST         = int(os.environ.get("EMA_FAST",         "7"))      # V11: 8→7
EMA_SLOW         = int(os.environ.get("EMA_SLOW",         "17"))     # V11: 21→17
EMA_TREND        = int(os.environ.get("EMA_TREND",        "100"))    # filtro tendencia 5m
H1_EMA_TREND     = int(os.environ.get("H1_EMA_TREND",     "21"))     # EMA 21 en H1
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT",    "30.0"))   # V11: 20→30° "tren bala"
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",       "3"))      # V11: 5→3 más reactivo

# ── ADX ───────────────────────────────────────────────────────────────────────
ADX_LEN          = int(os.environ.get("ADX_LEN",          "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN",        "25.0"))   # V11: 22→25 anti-chop
USE_ADX          = os.environ.get("USE_ADX", "true").lower() == "true"
USE_DI           = os.environ.get("USE_DI",  "true").lower() == "true"

# ── RSI ───────────────────────────────────────────────────────────────────────
RSI_LEN          = int(os.environ.get("RSI_LEN",          "14"))
RSI_OB           = float(os.environ.get("RSI_OB",         "70.0"))
RSI_OS           = float(os.environ.get("RSI_OS",         "30.0"))
USE_RSI          = os.environ.get("USE_RSI", "true").lower() == "true"

# ── VOLUME ────────────────────────────────────────────────────────────────────
USE_VOL          = os.environ.get("USE_VOL", "true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT",       "1.3"))    # V11: 1.2→1.3

# ── ATR / SL / TP ─────────────────────────────────────────────────────────────
ATR_LEN          = int(os.environ.get("ATR_LEN",          "14"))
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN",        "3"))
TP_MULT          = float(os.environ.get("TP_MULT",        "3.0"))    # V11: 2.0→3.0 (R:R 1:3)
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT",    "1.5"))    # V11: 2.5→1.5 stop ajustado
MIN_RR           = float(os.environ.get("MIN_RR",         "2.5"))    # V11: nuevo R:R mínimo

# ── PATRONES DE VELA (V11 NUEVO) ─────────────────────────────────────────────
USE_CANDLE_PATTERNS = os.environ.get("USE_CANDLE_PATTERNS", "true").lower() == "true"
PIN_BAR_RATIO       = float(os.environ.get("PIN_BAR_RATIO",   "0.30"))  # cuerpo/rango max
PIN_TAIL_RATIO      = float(os.environ.get("PIN_TAIL_RATIO",  "0.55"))  # cola/rango min
ENGULF_MIN_RATIO    = float(os.environ.get("ENGULF_MIN_RATIO","1.05"))  # cuerpo actual/prev
MOMENTUM_BODY_MIN   = float(os.environ.get("MOMENTUM_BODY_MIN","0.65")) # cuerpo/rango min

# ── H1 CONFIRMACIÓN (V11 NUEVO) ───────────────────────────────────────────────
USE_H1_CONFIRM   = os.environ.get("USE_H1_CONFIRM", "true").lower() == "true"
USE_H1_SR        = os.environ.get("USE_H1_SR",      "true").lower() == "true"
H1_SR_DIST_MIN   = float(os.environ.get("H1_SR_DIST_MIN", "1.2"))   # % dist mínima a S/R H1
H1_CACHE_TTL     = int(os.environ.get("H1_CACHE_TTL",    "300"))     # segundos TTL cache H1
ANTI_CHOP        = os.environ.get("ANTI_CHOP", "true").lower() == "true"

# ── POSITION SIZING V11 ───────────────────────────────────────────────────────
MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT", "3.0"))    # V11: 1.5→3.0
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT", "40.0"))   # V11: 7→40 (fix principal)
MAX_MARGIN_PCT   = float(os.environ.get("MAX_MARGIN_PCT", "25.0"))   # % máx del balance como margen

# ── TRAILING / COOLDOWN ───────────────────────────────────────────────────────
TRAILING_STOP    = os.environ.get("TRAILING_STOP", "true").lower() == "true"
BE_ATR_MULT      = float(os.environ.get("BE_ATR_MULT",    "1.0"))    # V11: 0.8→1.0
COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS",    "20"))     # V11: 30→20
USE_WS_CACHE     = os.environ.get("USE_WS_CACHE", "true").lower() == "true"
STRATEGY_MODE    = os.environ.get("STRATEGY_MODE", "dual_tf")        # V11: nuevo modo

_raw = os.environ.get("CUSTOM_SYMBOLS", "")
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
ws_kline_cache  = {}   # {symbol: pd.DataFrame}
ws_price_cache  = {}   # {symbol: float}
ws_cache_lock   = threading.Lock()
sl_cooldown     = {}   # {symbol: datetime}
h1_cache        = {}   # {symbol: (df, timestamp)} — cache H1 con TTL

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
            for field in ("equity","walletBalance","availableMargin"):
                v = bal.get(field)
                if v is not None and v != "":
                    return float(v)
        if d.get("asset") == "USDT":
            for field in ("availableMargin","available","walletBalance","equity"):
                v = d.get(field)
                if v is not None and v != "":
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
    base = sym.replace("-USDT", "")
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
    usdt = [c for c in contracts if isinstance(c, dict) and c.get("asset", "") == "USDT" and c.get("status") == 1]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c, dict) and str(c.get("symbol", "")).endswith("-USDT")]
    usdt.sort(key=lambda x: float(x.get("tradeAmount", 0) or 0), reverse=True)
    return [c["symbol"] for c in usdt if _is_valid(c.get("symbol", ""))]

def _symbols_from_ticker():
    data = bx_get("/openApi/swap/v2/quote/ticker", {})
    tickers = data.get("data", [])
    if not isinstance(tickers, list) or not tickers:
        raise ValueError("Empty ticker")
    usdt = [t for t in tickers if isinstance(t, dict) and _is_valid(t.get("symbol", ""))]
    usdt.sort(key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
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
    for side in ("LONG", "SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol": symbol, "side": side, "leverage": LEVERAGE})
        except Exception:
            pass

# ── WEBSOCKET KLINE CACHE ─────────────────────────────────────────────────────
def _ws_on_message(ws_app, message):
    try:
        import gzip
        try:
            data = json.loads(gzip.decompress(message) if isinstance(message, bytes) else message)
        except Exception:
            data = json.loads(message)

        if data.get("dataType", "").endswith("@kline"):
            sym_raw = data.get("s", "")
            sym = sym_raw.replace("_", "-") if "_" in sym_raw else sym_raw
            kdata = data.get("data", {}).get("kline", data.get("k", {}))
            if not kdata:
                return
            row = {
                "open_time": pd.to_datetime(kdata.get("t", kdata.get("startTime", 0)), unit="ms"),
                "open":  float(kdata.get("o", 0)),
                "high":  float(kdata.get("h", 0)),
                "low":   float(kdata.get("l", 0)),
                "close": float(kdata.get("c", 0)),
                "volume":float(kdata.get("v", 0)),
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
                    new_row = pd.DataFrame([row])
                    ws_kline_cache[sym] = pd.concat([df, new_row], ignore_index=True).tail(400)
                ws_price_cache[sym] = row["close"]
    except Exception:
        pass

def _ws_on_error(ws_app, error):
    log.warning(f"WS error: {error}")

def _ws_on_close(ws_app, *args):
    log.info("WS closed — reconnecting in 5s")

def _ws_on_open(ws_app, symbols):
    ivl = INTERVAL_MAP.get(TIMEFRAME, "5m").lower()
    for sym in symbols[:200]:
        bx_sym = sym.replace("-", "_")
        sub_msg = json.dumps({
            "id": f"sub_{sym}",
            "reqType": "sub",
            "dataType": f"{bx_sym}@kline_{ivl}"
        })
        try:
            ws_app.send(sub_msg)
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
    log.info(f"✅ WebSocket cache iniciado para {min(len(symbols),200)} símbolos")

# ── PRECIO EN VIVO ─────────────────────────────────────────────────────────────
def get_live_price(symbol):
    if USE_WS_CACHE:
        with ws_cache_lock:
            p = ws_price_cache.get(symbol)
        if p and p > 0:
            return p

    errors = []
    try:
        data = bx_get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        items = data.get("data", [])
        if isinstance(items, list):
            for item in items:
                if item.get("symbol") == symbol:
                    mp = item.get("markPrice")
                    if mp:
                        return float(mp)
        if isinstance(items, dict) and items.get("symbol") == symbol:
            mp = items.get("markPrice")
            if mp:
                return float(mp)
    except Exception as e:
        errors.append(f"premiumIndex: {e}")

    try:
        data2 = bx_get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        tickers = data2.get("data", [])
        if isinstance(tickers, list):
            for t in tickers:
                if t.get("symbol") == symbol:
                    lp = t.get("lastPrice") or t.get("price")
                    if lp:
                        return float(lp)
        if isinstance(tickers, dict):
            lp = tickers.get("lastPrice") or tickers.get("price")
            if lp:
                return float(lp)
    except Exception as e:
        errors.append(f"ticker: {e}")

    try:
        params = {"symbol": symbol, "interval": INTERVAL_MAP.get(TIMEFRAME, "5m"), "limit": 2}
        data3 = bx_get("/openApi/swap/v3/quote/klines", params)
        rows = data3.get("data", [])
        if rows and isinstance(rows, list):
            return float(rows[-1][4])
    except Exception as e:
        errors.append(f"kline: {e}")

    raise ValueError(f"get_live_price({symbol}) failed: {errors}")

# ── SPREAD FILTER ──────────────────────────────────────────────────────────────
def get_spread_pct(symbol):
    try:
        data = bx_get("/openApi/swap/v2/quote/bookTicker", {"symbol": symbol})
        d = data.get("data", {})
        if isinstance(d, list):
            for item in d:
                if item.get("symbol") == symbol:
                    d = item
                    break
        ask = float(d.get("askPrice", 0) or 0)
        bid = float(d.get("bidPrice", 0) or 0)
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

    params = {"symbol": symbol, "interval": INTERVAL_MAP.get(TIMEFRAME, "5m"), "limit": limit}
    data = bx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data", [])
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
    """Fetch H1 klines con cache TTL de H1_CACHE_TTL segundos."""
    now = time.time()
    cached = h1_cache.get(symbol)
    if cached:
        df_c, ts = cached
        if now - ts < H1_CACHE_TTL and len(df_c) >= 30:
            return df_c.copy()

    try:
        params = {"symbol": symbol, "interval": "1H", "limit": limit}
        data = bx_get("/openApi/swap/v3/quote/klines", params)
        rows = data.get("data", [])
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

def calc_ema_angle(ema_s, atr_s, look):
    price_change = ema_s - ema_s.shift(look)
    denom = atr_s * look
    angle = np.degrees(np.arctan2(price_change.values, denom.values))
    return pd.Series(angle, index=ema_s.index)

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
    tr_s   = wilder(tr)
    pdm_s  = wilder(plus_dm)
    mdm_s  = wilder(minus_dm)
    di_p   = 100 * pdm_s / tr_s.replace(0, np.nan)
    di_m   = 100 * mdm_s / tr_s.replace(0, np.nan)
    dx     = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)
    adx    = dx.ewm(alpha=alpha, adjust=False).mean()
    return di_p, di_m, adx

def calc_rsi(close, period):
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ── ANÁLISIS H1 (V11 NUEVO) ───────────────────────────────────────────────────
def analyze_h1(symbol):
    """
    Análisis H1: tendencia EMA21, niveles S/R por pivotes, distancia a S/R.
    Retorna dict o None si no hay datos suficientes.
    """
    df = get_h1_klines(symbol, limit=60)
    if df.empty or len(df) < 30:
        return None

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    ema21     = close.ewm(span=H1_EMA_TREND, adjust=False).mean()
    ema21_now = float(ema21.iloc[-1])
    ema21_prev= float(ema21.iloc[-4]) if len(ema21) > 4 else ema21_now
    close_now = float(close.iloc[-1])

    # Tendencia H1 según precio vs EMA21 + pendiente EMA21
    if close_now > ema21_now and ema21_now > ema21_prev:
        h1_trend = "BULL"
    elif close_now < ema21_now and ema21_now < ema21_prev:
        h1_trend = "BEAR"
    else:
        h1_trend = "NEUTRAL"

    # H1 ATR
    atr_h1 = calc_atr(high, low, close, 14)
    atr_h1_val = float(atr_h1.iloc[-1])

    # Pivotes H1 (últimas 40 velas, look 3)
    ph_vals, pl_vals = [], []
    plen = 3
    for idx in range(plen, min(len(df)-plen, 40)):
        h_window = high.iloc[idx-plen:idx+plen+1]
        l_window = low.iloc[idx-plen:idx+plen+1]
        if float(high.iloc[idx]) == float(h_window.max()):
            ph_vals.append(float(high.iloc[idx]))
        if float(low.iloc[idx]) == float(l_window.min()):
            pl_vals.append(float(low.iloc[idx]))

    resistances = sorted([v for v in ph_vals if v > close_now])
    supports    = sorted([v for v in pl_vals if v < close_now], reverse=True)

    h1_resistance = resistances[0] if resistances else close_now * 1.08
    h1_support    = supports[0]    if supports    else close_now * 0.92

    dist_to_res = (h1_resistance - close_now) / close_now * 100
    dist_to_sup = (close_now    - h1_support)  / close_now * 100

    # Momentum H1: RSI H1
    rsi_h1 = calc_rsi(close, 14)
    rsi_h1_val = float(rsi_h1.iloc[-1])

    return {
        "h1_trend":      h1_trend,
        "h1_ema21":      ema21_now,
        "h1_resistance": h1_resistance,
        "h1_support":    h1_support,
        "h1_atr":        atr_h1_val,
        "h1_rsi":        round(rsi_h1_val, 1),
        "dist_to_res":   round(dist_to_res, 2),
        "dist_to_sup":   round(dist_to_sup, 2),
        "close_h1":      close_now,
    }

# ── PATRONES DE VELA (V11 NUEVO) ─────────────────────────────────────────────

def detect_pin_bar(df, i, direction):
    """
    Pin Bar: cuerpo pequeño, cola larga de rechazo.
    LONG: cola inferior ≥55% del rango → rechazo bajista → entrada alcista.
    SHORT: cola superior ≥55% del rango → rechazo alcista → entrada bajista.
    Retorna (bool, fuerza 0-100).
    """
    o = float(df["open"].iloc[i])
    h = float(df["high"].iloc[i])
    l = float(df["low"].iloc[i])
    c = float(df["close"].iloc[i])

    total_range = h - l
    if total_range < 1e-10:
        return False, 0.0

    body       = abs(c - o)
    body_ratio = body / total_range

    if body_ratio > PIN_BAR_RATIO:   # cuerpo demasiado grande
        return False, 0.0

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if direction == "LONG":
        tail_ratio = lower_wick / total_range
        if tail_ratio >= PIN_TAIL_RATIO and lower_wick >= 2 * max(body, 1e-10):
            strength = min(tail_ratio * 120, 100.0)
            return True, round(strength, 1)
    else:  # SHORT
        tail_ratio = upper_wick / total_range
        if tail_ratio >= PIN_TAIL_RATIO and upper_wick >= 2 * max(body, 1e-10):
            strength = min(tail_ratio * 120, 100.0)
            return True, round(strength, 1)

    return False, 0.0


def detect_engulfing(df, i, direction):
    """
    Vela Envolvente: el cuerpo actual engulfa completamente al cuerpo anterior.
    LONG: vela alcista actual engloba vela bajista anterior.
    SHORT: vela bajista actual engloba vela alcista anterior.
    """
    if i < 1:
        return False, 0.0

    o_cur = float(df["open"].iloc[i]);   c_cur = float(df["close"].iloc[i])
    o_pre = float(df["open"].iloc[i-1]); c_pre = float(df["close"].iloc[i-1])

    body_cur = abs(c_cur - o_cur)
    body_pre = abs(c_pre - o_pre)

    if body_pre < 1e-10:
        return False, 0.0

    ratio = body_cur / body_pre
    if ratio < ENGULF_MIN_RATIO:
        return False, 0.0

    if direction == "LONG":
        # Actual alcista, previa bajista, actual envuelve
        if c_cur > o_cur and c_pre < o_pre:
            if c_cur > max(o_pre, c_pre) and o_cur < min(o_pre, c_pre):
                strength = min(ratio * 45, 100.0)
                return True, round(strength, 1)
    else:  # SHORT
        if c_cur < o_cur and c_pre > o_pre:
            if c_cur < min(o_pre, c_pre) and o_cur > max(o_pre, c_pre):
                strength = min(ratio * 45, 100.0)
                return True, round(strength, 1)

    return False, 0.0


def detect_momentum_candle(df, i, direction, atr):
    """
    Vela de Impulso: cuerpo grande (≥65% rango), mecha mínima contraria,
    tamaño ≥0.5×ATR. Es la vela de "tren bala" sin ambigüedades.
    """
    o = float(df["open"].iloc[i])
    h = float(df["high"].iloc[i])
    l = float(df["low"].iloc[i])
    c = float(df["close"].iloc[i])

    total_range = h - l
    if total_range < 1e-10 or atr < 1e-10:
        return False, 0.0

    body       = abs(c - o)
    body_ratio = body / total_range

    if body_ratio < MOMENTUM_BODY_MIN:
        return False, 0.0
    if body < atr * 0.5:    # vela demasiado pequeña
        return False, 0.0

    if direction == "LONG" and c > o:
        upper_wick = h - c
        if upper_wick < body * 0.35:  # mecha superior contenida
            strength = min(body_ratio * 90, 100.0)
            return True, round(strength, 1)
    elif direction == "SHORT" and c < o:
        lower_wick = c - l
        if lower_wick < body * 0.35:
            strength = min(body_ratio * 90, 100.0)
            return True, round(strength, 1)

    return False, 0.0


def detect_inside_bar_breakout(df, i, direction):
    """
    Inside bar breakout: vela actual rompe el rango de la vela madre.
    Señal de continuación tras compresión.
    """
    if i < 2:
        return False, 0.0

    # Inside bar = i-1 dentro del rango de i-2
    h_m2 = float(df["high"].iloc[i-2])
    l_m2 = float(df["low"].iloc[i-2])
    h_m1 = float(df["high"].iloc[i-1])
    l_m1 = float(df["low"].iloc[i-1])
    h_now = float(df["high"].iloc[i])
    l_now = float(df["low"].iloc[i])
    c_now = float(df["close"].iloc[i])

    is_inside = (h_m1 <= h_m2) and (l_m1 >= l_m2)
    if not is_inside:
        return False, 0.0

    if direction == "LONG" and c_now > h_m2:
        return True, 65.0
    elif direction == "SHORT" and c_now < l_m2:
        return True, 65.0

    return False, 0.0


def is_choppy_market(df, adx_val):
    """
    Anti-chop: mercado lateral si ADX < min O rango promedio < 0.75×ATR.
    """
    if not ANTI_CHOP:
        return False
    if adx_val < ADX_MIN:
        return True
    if len(df) < 15:
        return False
    recent    = df.iloc[-12:]
    avg_range = float((recent["high"] - recent["low"]).mean())
    atr_s     = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
    atr_val   = float(atr_s.iloc[-1])
    if atr_val > 0 and avg_range < atr_val * 0.75:
        return True
    return False

# ── RECALC SL/TP ──────────────────────────────────────────────────────────────
def recalc_sl_tp(sig, live_price):
    catr      = sig["atr"]
    direction = sig["signal"]
    sl_distance = catr * SL_ATR_MULT

    if direction == "LONG":
        sl_price = live_price - sl_distance
        dist_pct = (live_price - sl_price) / live_price * 100
        if dist_pct < MIN_DIST_PCT:
            sl_price = live_price * (1 - MIN_DIST_PCT / 100)
        tp_price = live_price + (live_price - sl_price) * TP_MULT
        if sl_price >= live_price or tp_price <= live_price:
            return None, None
    else:
        sl_price = live_price + sl_distance
        dist_pct = (sl_price - live_price) / live_price * 100
        if dist_pct < MIN_DIST_PCT:
            sl_price = live_price * (1 + MIN_DIST_PCT / 100)
        tp_price = live_price - (sl_price - live_price) * TP_MULT
        if sl_price <= live_price or tp_price >= live_price:
            return None, None

    final_dist_pct = abs(live_price - sl_price) / live_price * 100
    if final_dist_pct < MIN_DIST_PCT * 0.9:
        return None, None

    rr = abs(tp_price - live_price) / abs(live_price - sl_price)
    if rr < MIN_RR:
        return None, None

    return round(sl_price, 6), round(tp_price, 6)

# ── POSITION SIZING V11 (FÓRMULA CORREGIDA) ──────────────────────────────────
def calc_qty(balance, entry, sl):
    """
    V11: Formula correcta de position sizing.
    notional = risk_usdt / dist_pct
    El leverage en BingX futures se aplica sobre el margen (margen = notional/leverage).
    No multiplicar por leverage en notional — ya está implícito en la cuenta de futuros.

    Con balance=180 USDT, risk=1.5%, dist=0.8%:
    notional = 2.7 / 0.008 = 337.5 USDT (margen = 337.5/5 = 67.5 USDT)
    Pero limitamos a MAX_ORDER_USDT y MAX_MARGIN_PCT.
    """
    dist_pct = abs(entry - sl) / entry
    if dist_pct < 1e-8:
        return 0, 0

    risk_usdt = balance * (RISK_PERCENT / 100)

    # Notional correcto: arriesgamos exactamente risk_usdt si SL se activa
    notional = risk_usdt / dist_pct

    # Límite de margen máximo como % del balance
    max_margin   = balance * (MAX_MARGIN_PCT / 100)
    max_notional_by_margin = max_margin * LEVERAGE
    max_notional = min(MAX_ORDER_USDT, max_notional_by_margin)

    notional = max(MIN_ORDER_USDT, min(notional, max_notional))
    qty = notional / entry

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
            "type":        "STOP_MARKET",
            "stopPrice":   round(sl, 6),
            "workingType": "MARK_PRICE"
        }),
        "takeProfit": json.dumps({
            "type":        "TAKE_PROFIT_MARKET",
            "stopPrice":   round(tp, 6),
            "workingType": "MARK_PRICE"
        }),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    code = resp.get("code", -1)
    if code != 0:
        raise ValueError(f"BingX code={code}: {resp.get('msg', 'unknown')}")
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
                    fresh_price = get_live_price(symbol)
                    if side == "BUY":
                        sl = round(fresh_price * (1 - MIN_DIST_PCT / 100), 6)
                        tp = round(fresh_price + (fresh_price - sl) * TP_MULT, 6)
                    else:
                        sl = round(fresh_price * (1 + MIN_DIST_PCT / 100), 6)
                        tp = round(fresh_price - (sl - fresh_price) * TP_MULT, 6)
                except Exception as ep:
                    log.warning(f"Retry get_live_price falló: {ep}")
                    raise
            else:
                raise

# ── TRAILING STOP ─────────────────────────────────────────────────────────────
def update_trailing_stops(positions):
    if not TRAILING_STOP or not positions:
        return
    for sym, pos in positions.items():
        try:
            side  = pos.get("positionSide", "LONG")
            entry = float(pos.get("avgPrice", 0) or 0)
            if entry == 0:
                continue
            live = get_live_price(sym)
            with ws_cache_lock:
                df = ws_kline_cache.get(sym)
            if df is None or len(df) < 20:
                continue
            atr_s = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
            atr   = float(atr_s.iloc[-2]) if len(atr_s) > 1 else 0
            if atr == 0:
                continue
            if side == "LONG" and live >= entry + atr * BE_ATR_MULT:
                new_sl = round(entry * 1.001, 6)
                bx_post("/openApi/swap/v2/trade/order", {
                    "symbol": sym, "type": "STOP_MARKET",
                    "side": "SELL", "positionSide": "LONG",
                    "stopPrice": new_sl, "closePosition": "true",
                    "workingType": "MARK_PRICE"
                })
                log.info(f"✅ Trailing BE {sym} LONG → SL={new_sl}")
            elif side == "SHORT" and live <= entry - atr * BE_ATR_MULT:
                new_sl = round(entry * 0.999, 6)
                bx_post("/openApi/swap/v2/trade/order", {
                    "symbol": sym, "type": "STOP_MARKET",
                    "side": "BUY", "positionSide": "SHORT",
                    "stopPrice": new_sl, "closePosition": "true",
                    "workingType": "MARK_PRICE"
                })
                log.info(f"✅ Trailing BE {sym} SHORT → SL={new_sl}")
        except Exception as e:
            log.debug(f"Trailing stop {sym}: {e}")

# ── ESTRATEGIA PRINCIPAL V11 ──────────────────────────────────────────────────
def scan_symbol(symbol):
    """
    V11 DUAL-TF: escaneo con M5 entradas + H1 confirmación.
    Detecta patrones de vela (Pin Bar, Engulfing, Momentum, Inside Bar).
    SL ajustado en la vela señal. TP a 3×distancia. R:R mínimo 2.5.
    """
    # Cooldown check
    if symbol in sl_cooldown:
        elapsed = (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds() / 60
        if elapsed < COOLDOWN_MINS:
            return None

    try:
        # ── 1. DATOS 5m ───────────────────────────────────────────────────────
        df = get_klines(symbol, limit=300)
        min_bars = max(EMA_TREND + 10, ADX_LEN * 2 + 5, RSI_LEN + 5, 60)
        if df.empty or len(df) < min_bars:
            return None

        # ── 2. INDICADORES 5m ─────────────────────────────────────────────────
        atr_s        = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
        ema_f        = df["close"].ewm(span=EMA_FAST,  adjust=False).mean()
        ema_s        = df["close"].ewm(span=EMA_SLOW,  adjust=False).mean()
        ema_trend    = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
        angle        = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        di_p, di_m, adx_s = calc_adx(df["high"], df["low"], df["close"], ADX_LEN)
        rsi_s        = calc_rsi(df["close"], RSI_LEN)
        vol_ma       = df["volume"].rolling(20).mean()

        i = len(df) - 2   # última vela completamente cerrada
        if i < max(EMA_TREND + 2, ADX_LEN * 2, 50):
            return None

        close_now     = float(df["close"].iloc[i])
        open_now      = float(df["open"].iloc[i])
        high_now      = float(df["high"].iloc[i])
        low_now       = float(df["low"].iloc[i])
        ema_f_now     = float(ema_f.iloc[i])
        ema_s_now     = float(ema_s.iloc[i])
        ema_f_prev    = float(ema_f.iloc[i-1])
        ema_s_prev    = float(ema_s.iloc[i-1])
        ema_trend_now = float(ema_trend.iloc[i])
        angle_now     = float(angle.iloc[i])
        adx_now       = float(adx_s.iloc[i])
        di_p_now      = float(di_p.iloc[i])
        di_m_now      = float(di_m.iloc[i])
        rsi_now       = float(rsi_s.iloc[i])
        vol_now       = float(df["volume"].iloc[i])
        vma           = float(vol_ma.iloc[i])
        catr          = float(atr_s.iloc[i])

        if any(np.isnan(x) for x in [angle_now, adx_now, catr, ema_f_now,
                                      ema_s_now, ema_trend_now, rsi_now,
                                      di_p_now, di_m_now]):
            return None
        if vma <= 0 or catr <= 0:
            return None

        atr_pct = (catr / close_now) * 100
        if atr_pct > ATR_MAX_PCT:
            return None

        # ── 3. ANTI-CHOP ──────────────────────────────────────────────────────
        if is_choppy_market(df, adx_now):
            return None

        vratio = round(vol_now / vma, 2) if vma > 0 else 0.0

        # ── 4. CONDICIONES DE TENDENCIA 5m ────────────────────────────────────
        trend_long  = close_now > ema_trend_now
        trend_short = close_now < ema_trend_now

        ema_cross_long  = ema_f_now > ema_s_now
        ema_cross_short = ema_f_now < ema_s_now

        # V11: pendiente ≥30° — "tren bala"
        angle_long_ok  = angle_now >= SLOPE_LIMIT
        angle_short_ok = angle_now <= -SLOPE_LIMIT

        # V11: EMA fast acelerando (no solo cruzando)
        accel_long  = ema_f_now > ema_f_prev
        accel_short = ema_f_now < ema_f_prev

        # V11: EMA fast/slow también acelerando en la misma dirección
        ema_gap_widening_long  = (ema_f_now - ema_s_now) > (ema_f_prev - ema_s_prev)
        ema_gap_widening_short = (ema_s_now - ema_f_now) > (ema_s_prev - ema_f_prev)

        vol_confirm  = (not USE_VOL) or (vratio >= VOL_MULT)
        adx_confirm  = (not USE_ADX) or (adx_now > ADX_MIN)
        rsi_long_ok  = (not USE_RSI) or (rsi_now < RSI_OB)
        rsi_short_ok = (not USE_RSI) or (rsi_now > RSI_OS)
        di_long_ok   = (not USE_DI)  or (di_p_now > di_m_now)
        di_short_ok  = (not USE_DI)  or (di_m_now > di_p_now)

        base_long = (
            ema_cross_long and angle_long_ok and adx_confirm and
            vol_confirm and trend_long and rsi_long_ok and
            di_long_ok and accel_long and ema_gap_widening_long
        )
        base_short = (
            ema_cross_short and angle_short_ok and adx_confirm and
            vol_confirm and trend_short and rsi_short_ok and
            di_short_ok and accel_short and ema_gap_widening_short
        )

        if not base_long and not base_short:
            return None

        # ── 5. H1 CONFIRMACIÓN (GOLDEN SETUP) ────────────────────────────────
        h1_ctx    = None
        h1_bonus  = 0
        h1_trend  = "UNKNOWN"
        h1_ok     = True

        if USE_H1_CONFIRM:
            h1_ctx = analyze_h1(symbol)
            if h1_ctx:
                h1_trend = h1_ctx["h1_trend"]

                # Golden setup: M5 + H1 alineados
                if base_long and h1_trend == "BULL":
                    h1_bonus = 20
                elif base_short and h1_trend == "BEAR":
                    h1_bonus = 20
                elif h1_trend == "NEUTRAL":
                    h1_bonus = 5   # neutral ok pero sin bonus máximo
                else:
                    # M5 contra H1 = descartado
                    h1_ok = False

                # S/R filter: no comprar en resistencia, no vender en soporte H1
                if USE_H1_SR and h1_ctx and h1_ok:
                    if base_long and h1_ctx["dist_to_res"] < H1_SR_DIST_MIN:
                        h1_ok = False  # resistencia H1 muy cerca arriba
                    elif base_short and h1_ctx["dist_to_sup"] < H1_SR_DIST_MIN:
                        h1_ok = False  # soporte H1 muy cerca abajo

        if not h1_ok:
            return None

        direction = "LONG" if base_long else "SHORT"

        # ── 6. DETECCIÓN DE PATRÓN DE VELA ───────────────────────────────────
        pattern_name  = "SLOPE"
        pattern_score = 0.0
        sl_candle     = None   # SL ajustado a la vela de señal

        if USE_CANDLE_PATTERNS:
            is_pin, pin_str    = detect_pin_bar(df, i, direction)
            is_eng, eng_str    = detect_engulfing(df, i, direction)
            is_mom, mom_str    = detect_momentum_candle(df, i, direction, catr)
            is_ib,  ib_str     = detect_inside_bar_breakout(df, i, direction)

            # Prioridad: Pin > Engulf > Momentum > Inside > solo slope
            if is_pin:
                pattern_name  = "PIN_BAR"
                pattern_score = pin_str
                # SL debajo del mínimo / encima del máximo de la vela pin
                margin = catr * 0.08
                sl_candle = (low_now  - margin) if direction == "LONG" else (high_now + margin)
            elif is_eng:
                pattern_name  = "ENGULF"
                pattern_score = eng_str
                margin = catr * 0.10
                sl_candle = (low_now  - margin) if direction == "LONG" else (high_now + margin)
            elif is_mom:
                pattern_name  = "MOMENTUM"
                pattern_score = mom_str
                margin = catr * 0.12
                sl_candle = (low_now  - margin) if direction == "LONG" else (high_now + margin)
            elif is_ib:
                pattern_name  = "INSIDE_BR"
                pattern_score = ib_str
                # SL debajo de la inside bar
                prev_low  = float(df["low"].iloc[i-1])
                prev_high = float(df["high"].iloc[i-1])
                margin = catr * 0.10
                sl_candle = (prev_low - margin) if direction == "LONG" else (prev_high + margin)

        # ── 7. CALCULAR SL / TP ───────────────────────────────────────────────
        sl_atr_dist = catr * SL_ATR_MULT   # fallback por ATR

        if direction == "LONG":
            # Usa el SL de la vela si es más ajustado que el ATR-based
            if sl_candle is not None:
                candle_dist = close_now - sl_candle
                atr_dist    = sl_atr_dist
                # Elegir el más ajustado (pero respetar MIN_DIST_PCT)
                sl_price = sl_candle if candle_dist <= atr_dist else (close_now - sl_atr_dist)
            else:
                sl_price = close_now - sl_atr_dist

            # Garantizar MIN_DIST_PCT
            if (close_now - sl_price) / close_now * 100 < MIN_DIST_PCT:
                sl_price = close_now * (1 - MIN_DIST_PCT / 100)

            if sl_price >= close_now:
                return None

            tp_price = close_now + (close_now - sl_price) * TP_MULT

        else:  # SHORT
            if sl_candle is not None:
                candle_dist = sl_candle - close_now
                atr_dist    = sl_atr_dist
                sl_price = sl_candle if candle_dist <= atr_dist else (close_now + sl_atr_dist)
            else:
                sl_price = close_now + sl_atr_dist

            if (sl_price - close_now) / close_now * 100 < MIN_DIST_PCT:
                sl_price = close_now * (1 + MIN_DIST_PCT / 100)

            if sl_price <= close_now:
                return None

            tp_price = close_now - (sl_price - close_now) * TP_MULT

        dist     = abs(close_now - sl_price)
        dist_pct = (dist / close_now) * 100

        if dist_pct < MIN_DIST_PCT:
            return None

        rr = abs(tp_price - close_now) / dist
        if rr < MIN_RR:
            return None

        # ── 8. SCORING V11 ────────────────────────────────────────────────────
        # Pesos: ángulo(30) + ADX(20) + H1(20) + patrón(12) + vol(8) + RR(6) + DI(4)
        score  = min(abs(angle_now) / SLOPE_LIMIT * 30, 30)    # ángulo: max 30
        score += min((adx_now - ADX_MIN) / ADX_MIN * 20, 20)   # ADX: max 20
        score += h1_bonus                                        # H1 confirm: max 20
        score += min(pattern_score / 8, 12)                     # patrón: max 12
        score += min(vratio * 5, 8)                             # volumen: max 8
        score += min((rr - MIN_RR) * 3, 6)                     # R:R extra: max 6
        score += min(abs(di_p_now - di_m_now) / 10, 4)         # DI spread: max 4

        if score < MIN_SCORE:
            return None

        method_str = f"{pattern_name}|TF:{TIMEFRAME}+1H|{h1_trend}"

        return {
            "symbol":    symbol,
            "signal":    direction,
            "method":    method_str,
            "pattern":   pattern_name,
            "close":     close_now,
            "sl":        round(sl_price, 6),
            "tp":        round(tp_price, 6),
            "atr":       catr,
            "atr_pct":   round(atr_pct, 2),
            "vol_ratio": vratio,
            "angle":     round(angle_now, 1),
            "adx":       round(adx_now, 1),
            "rsi":       round(rsi_now, 1),
            "score":     round(score, 1),
            "rr":        round(rr, 2),
            "dist_pct":  round(dist_pct, 3),
            "di_spread": round(abs(di_p_now - di_m_now), 1),
            "h1_trend":  h1_trend,
            "h1_ctx":    h1_ctx,
            "pat_score": round(pattern_score, 1),
        }

    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def _send(msg):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        asyncio.run(_send(msg))
    except Exception as e:
        log.warning(f"Telegram error: {e}")

def tg_startup(balance, symbols):
    tg(
        f"🚀 <b>EMA+ADX+ZigZag Elite V11.0 — BULLET TRAIN EDITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔀 <b>Modo:</b> DUAL TF ({TIMEFRAME} entradas + 1H tendencia)\n"
        f"<b>EMA:</b> {EMA_FAST}/{EMA_SLOW}/T{EMA_TREND} | "
        f"<b>Slope≥:</b> {SLOPE_LIMIT}° 🚄 | <b>ADX≥:</b> {ADX_MIN}\n"
        f"<b>TP mult:</b> {TP_MULT}x | <b>SL ATR:</b> {SL_ATR_MULT}x | "
        f"<b>Min R:R:</b> {MIN_RR} | <b>Score≥:</b> {MIN_SCORE}\n"
        f"<b>H1 confirm:</b> {'✅' if USE_H1_CONFIRM else '❌'} | "
        f"<b>H1 S/R:</b> {'✅' if USE_H1_SR else '❌'}\n"
        f"<b>Patrones:</b> {'✅ PIN+ENG+MOM+IB' if USE_CANDLE_PATTERNS else '❌'}\n"
        f"<b>Anti-chop:</b> {'✅' if ANTI_CHOP else '❌'} | "
        f"<b>Trailing:</b> {'✅' if TRAILING_STOP else '❌'} (BE@{BE_ATR_MULT}x ATR)\n"
        f"<b>Vol≥:</b> {VOL_MULT}x | <b>RSI:</b> {RSI_OS}–{RSI_OB}\n"
        f"<b>Pos size:</b> {MIN_ORDER_USDT}–{MAX_ORDER_USDT} USDT | "
        f"<b>Max trades:</b> {MAX_OPEN_TRADES}\n"
        f"<b>Balance:</b> {balance:.2f} USDT | <b>Símbolos:</b> {len(symbols)}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_entry(sig, qty, notional, balance, spread_pct=None):
    d = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    spread_str = f" | <b>Spread:</b> {spread_pct:.3f}%" if spread_pct is not None else ""
    h1_str = f" | <b>H1:</b> {sig.get('h1_trend','?')}"
    pat    = sig.get("pattern", "SLOPE")
    pat_icon = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","INSIDE_BR":"📦","SLOPE":"📈"}.get(pat,"⚡")
    h1_ctx = sig.get("h1_ctx")
    sr_str = ""
    if h1_ctx:
        sr_str = (f"\n<b>H1 Res:</b> {h1_ctx['h1_resistance']:.6g} "
                  f"(+{h1_ctx['dist_to_res']:.1f}%) | "
                  f"<b>H1 Sup:</b> {h1_ctx['h1_support']:.6g} "
                  f"(-{h1_ctx['dist_to_sup']:.1f}%)")
    tg(
        f"<b>✅ ORDEN ABIERTA — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"{pat_icon} <b>Patrón:</b> {pat} ({sig.get('pat_score',0):.0f}){h1_str}\n"
        f"<b>Ang:</b> {sig['angle']}° | <b>ADX:</b> {sig['adx']} | "
        f"<b>RSI:</b> {sig['rsi']} | <b>DI±:</b> {sig.get('di_spread','?')}\n"
        f"<b>Vol:</b> {sig['vol_ratio']}x | <b>ATR:</b> {sig['atr_pct']}%{spread_str}"
        f"{sr_str}\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.6g}</code>\n"
        f"<b>R:R:</b> 1:{sig['rr']} | <b>Qty:</b> {qty:.4f} | "
        f"<b>Notional:</b> {notional:.2f} USDT\n"
        f"<b>Riesgo máx:</b> {balance * RISK_PERCENT / 100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_count):
    if not signals:
        return
    lines = [
        f"🔍 <b>{len(signals)} señal(es) / {total}</b> | Trades: {open_count}/{MAX_OPEN_TRADES}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    pat_icons = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","INSIDE_BR":"📦","SLOPE":"📈"}
    for s in signals[:6]:
        e  = "🟢" if s["signal"] == "LONG" else "🔴"
        pi = pat_icons.get(s.get("pattern","SLOPE"),"⚡")
        lines.append(
            f"{e}{pi} <b>{s['symbol']}</b> H1:{s.get('h1_trend','?')} "
            f"Score:{s['score']} Ang:{s['angle']}° ADX:{s['adx']} "
            f"RSI:{s['rsi']} Vol:{s['vol_ratio']}x RR:1:{s['rr']}"
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
    log.info("=== EMA+ADX+ZigZag Elite V11.0 BULLET TRAIN EDITION ===")
    log.info(f"  TF: {TIMEFRAME} + 1H | EMA {EMA_FAST}/{EMA_SLOW}/T{EMA_TREND}")
    log.info(f"  Slope≥{SLOPE_LIMIT}° | ADX≥{ADX_MIN} | TP×{TP_MULT} | SL×{SL_ATR_MULT} | Min R:R {MIN_RR}")
    log.info(f"  H1={USE_H1_CONFIRM} | Patrones={USE_CANDLE_PATTERNS} | AntiChop={ANTI_CHOP}")
    log.info(f"  Score≥{MIN_SCORE} | Pos: {MIN_ORDER_USDT}–{MAX_ORDER_USDT} USDT | Cooldown: {COOLDOWN_MINS}min")

    symbols   = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols:
        symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance: {balance:.4f} | Symbols: {len(symbols)} | Open: {len(positions)}")

    # Pre-cargar klines en cache REST (5m)
    log.info("Pre-cargando klines 5m en cache REST...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s, 300), symbols[:100]))
    log.info("Cache REST listo.")

    # Pre-cargar H1 para los top símbolos (en background para no bloquear)
    def _prefetch_h1():
        log.info("Pre-cargando klines H1...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(lambda s: get_h1_klines(s, 60), symbols[:80]))
        log.info("Cache H1 listo.")
    threading.Thread(target=_prefetch_h1, daemon=True).start()

    # WebSocket cache en background
    start_ws_cache(symbols)
    time.sleep(2)

    # Configurar leverage
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))

    tg_startup(balance, symbols)
    log.info("✅ Bot V11 iniciado. Loop comenzando.")

    errors = 0

    while True:
        t0 = time.time()
        try:
            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)

            log.info(
                f"── V11 [DUAL-TF] {balance:.4f} USDT | "
                f"{open_count}/{MAX_OPEN_TRADES} trades | {len(symbols)} sym ──"
            )

            # Trailing stops en posiciones abiertas
            if TRAILING_STOP and positions:
                update_trailing_stops(positions)

            # Scan
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan_symbol, s): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r:
                        signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Signals: {len(signals)}/{len(symbols)}")

            if signals:
                tg_scan(signals, len(symbols), open_count)
                for s in signals[:5]:
                    log.info(
                        f"  → {s['symbol']} {s['signal']} [{s['pattern']}] "
                        f"H1:{s.get('h1_trend','?')} score={s['score']} "
                        f"ang={s['angle']}° adx={s['adx']} rsi={s['rsi']} "
                        f"vol={s['vol_ratio']}x rr=1:{s['rr']}"
                    )

            entered      : set  = set()
            skip_reasons : dict = {}
            orders_opened = 0

            for sig in signals:
                sym = sig["symbol"]

                if sym in positions:
                    skip_reasons[sym] = "ya en posición"
                    continue
                if sym in entered:
                    skip_reasons[sym] = "ya intentado"
                    continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info(f"Max trades ({MAX_OPEN_TRADES}) alcanzado.")
                    break
                if balance < 2:
                    skip_reasons[sym] = f"balance bajo ({balance:.2f})"
                    break

                # Filtro de spread
                spread = get_spread_pct(sym)
                if spread > MAX_SPREAD_PCT:
                    reason = f"spread {spread:.3f}% > max {MAX_SPREAD_PCT}%"
                    log.info(f"Skip {sym}: {reason}")
                    skip_reasons[sym] = reason
                    continue

                side = "BUY" if sig["signal"] == "LONG" else "SELL"
                try:
                    set_lev(sym)

                    try:
                        live_price = get_live_price(sym)
                        log.info(f"Live price {sym}: scan={sig['close']:.6g} live={live_price:.6g}")
                    except Exception as ep:
                        skip_reasons[sym] = f"sin precio: {str(ep)[:60]}"
                        continue

                    sl_live, tp_live = recalc_sl_tp(sig, live_price)
                    if sl_live is None:
                        skip_reasons[sym] = f"SL/TP inválido live={live_price:.6g}"
                        continue

                    qty, notional = calc_qty(balance, live_price, sl_live)
                    if qty <= 0:
                        skip_reasons[sym] = "qty=0"
                        continue

                    log.info(
                        f"Orden {sym} {side} qty={qty:.4f} "
                        f"notional={notional:.2f} USDT "
                        f"live={live_price:.6g} sl={sl_live:.6g} tp={tp_live:.6g} "
                        f"spread={spread:.3f}% rr=1:{sig['rr']}"
                    )

                    res = open_order_with_retry(sym, side, qty, sl_live, tp_live, retries=1)
                    log.info(f"✅ {sym} {side} qty={qty:.4f} notional={notional:.2f} | {res}")

                    sig["close"]    = live_price
                    sig["sl"]       = sl_live
                    sig["tp"]       = tp_live
                    sig["dist_pct"] = round(abs(live_price - sl_live) / live_price * 100, 3)
                    sig["rr"]       = round(abs(tp_live - live_price) / abs(live_price - sl_live), 2)

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
                log.warning(f"Señales={len(signals)} pero 0 órdenes. Razones: {skip_reasons}")
                tg_diag(signals, skip_reasons)

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot V11 detenido</b>")
            break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors <= 3:
                tg(f"⚠️ <b>Error ciclo #{errors}</b>: <code>{str(e)[:200]}</code>")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: 10 errores. Detenido.</b>")
                break

        time.sleep(max(0, LOOP_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
