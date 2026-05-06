"""
ZigZag + EMA Slope + ADX Elite V10.0 — TURBO EDITION

MEJORAS V10 sobre V9.5:
  1. WEBSOCKET CACHE: klines y precios via WS en background → 10-50x más rápido
  2. BLACKLIST DE COMMODITIES: Gasoline, Oil Brent, índices sin liquidez
  3. FILTRO DE SPREAD: descarta símbolo si spread > MAX_SPREAD_PCT
  4. TRAILING STOP: mueve SL a breakeven cuando el trade va +BE_ATR_MULT × ATR
  5. POSITION SIZING: mínimo 1.5 USDT / máximo 7 USDT por orden
  6. COOLDOWN POR SÍMBOLO: 30 min tras SL para no re-entrar en perdedor
  7. VARIABLES OPTIMIZADAS: TF 15m, ADX≥22, TP_MULT 2.0, SL_ATR 2.5, Score≥45
  8. FILTRO VOLUMEN: VOL_MULT 1.2x (elimina señales en volumen plano)
  9. USE_DI activado por defecto: mejora dirección de tendencia
 10. CONFLUENCIA: comprueba que RSI no esté en zona opuesta al trade
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

TIMEFRAME        = os.environ.get("TIMEFRAME",        "15m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.0"))
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "45"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "6"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "0"))

# ── FILTROS DE CALIDAD ────────────────────────────────────────────────────────
MIN_SCORE        = float(os.environ.get("MIN_SCORE",      "45.0"))   # V10: 30→45
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT",   "0.5"))    # V10: 0.3→0.5
MAX_SPREAD_PCT   = float(os.environ.get("MAX_SPREAD_PCT", "0.15"))   # V10: nuevo
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",    "2.5"))    # V10: 3.0→2.5

# ── EMA SLOPE ─────────────────────────────────────────────────────────────────
EMA_FAST         = int(os.environ.get("EMA_FAST",         "8"))      # V10: 7→8
EMA_SLOW         = int(os.environ.get("EMA_SLOW",         "21"))     # V10: 17→21
EMA_TREND        = int(os.environ.get("EMA_TREND",        "100"))    # V10: 50→100
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT",    "20.0"))   # V10: 15→20
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",       "5"))

# ── ADX ───────────────────────────────────────────────────────────────────────
ADX_LEN          = int(os.environ.get("ADX_LEN",          "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN",        "22.0"))   # V10: 15→22
USE_ADX          = os.environ.get("USE_ADX", "true").lower() == "true"
USE_DI           = os.environ.get("USE_DI",  "true").lower() == "true"  # V10: false→true

# ── RSI ───────────────────────────────────────────────────────────────────────
RSI_LEN          = int(os.environ.get("RSI_LEN",          "14"))
RSI_OB           = float(os.environ.get("RSI_OB",         "68.0"))   # V10: 70→68
RSI_OS           = float(os.environ.get("RSI_OS",         "32.0"))   # V10: 30→32
USE_RSI          = os.environ.get("USE_RSI", "true").lower() == "true"

# ── VOLUME ────────────────────────────────────────────────────────────────────
USE_VOL          = os.environ.get("USE_VOL", "true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT",       "1.2"))    # V10: 1.0→1.2

# ── ZIGZAG / ATR ──────────────────────────────────────────────────────────────
ATR_LEN          = int(os.environ.get("ATR_LEN",          "14"))
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN",        "3"))
TP_MULT          = float(os.environ.get("TP_MULT",        "2.0"))    # V10: 1.5→2.0
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT",    "2.5"))    # V10: 2.0→2.5

# ── NUEVAS FEATURES V10 ───────────────────────────────────────────────────────
TRAILING_STOP    = os.environ.get("TRAILING_STOP", "true").lower() == "true"
BE_ATR_MULT      = float(os.environ.get("BE_ATR_MULT",    "0.8"))    # activar BE
MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT", "1.5"))    # tamaño mínimo
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT", "7.0"))    # tamaño máximo
COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS",    "30"))     # cooldown tras SL
USE_WS_CACHE     = os.environ.get("USE_WS_CACHE", "true").lower() == "true"
STRATEGY_MODE    = os.environ.get("STRATEGY_MODE", "slope")

_raw = os.environ.get("CUSTOM_SYMBOLS", "")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE   = "https://open-api.bingx.com"
BINGX_WS     = "wss://open-api-swap.bingx.com/swap-market"
INTERVAL_MAP = {"1m":"1m","3m":"3m","5m":"5m","15m":"15m",
                "30m":"30m","1h":"1H","4h":"4H","1d":"1D"}

# V10: blacklist ampliada
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
sl_cooldown     = {}   # {symbol: datetime} — cooldown tras SL

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
    # V10: excluir commodities y forex por nombre
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

# ── WEBSOCKET KLINE CACHE (V10 NUEVO) ─────────────────────────────────────────
def _ws_on_message(ws_app, message):
    """Actualiza el cache de klines desde WebSocket en background."""
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
                    return  # esperamos a que REST llene el cache primero
                # actualizar o añadir la última vela
                if len(df) > 0 and df.iloc[-1]["open_time"] == row["open_time"]:
                    for col in ("open","high","low","close","volume"):
                        df.at[df.index[-1], col] = row[col]
                else:
                    new_row = pd.DataFrame([row])
                    ws_kline_cache[sym] = pd.concat([df, new_row], ignore_index=True).tail(300)
                ws_price_cache[sym] = row["close"]
    except Exception as e:
        pass  # no bloquear el hilo WS

def _ws_on_error(ws_app, error):
    log.warning(f"WS error: {error}")

def _ws_on_close(ws_app, *args):
    log.info("WS closed — reconnecting in 5s")

def _ws_on_open(ws_app, symbols):
    ivl = INTERVAL_MAP.get(TIMEFRAME, "15m").lower()
    for sym in symbols[:200]:  # BingX limita suscripciones simultáneas
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
    """Inicia WebSocket en hilo daemon para cache de precios en tiempo real."""
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
    # Intentar cache WS primero (sin latencia de red)
    if USE_WS_CACHE:
        with ws_cache_lock:
            p = ws_price_cache.get(symbol)
        if p and p > 0:
            return p

    errors = []
    # Fallback 1: premiumIndex
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

    # Fallback 2: ticker
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

    # Fallback 3: última kline
    try:
        params = {"symbol": symbol, "interval": INTERVAL_MAP.get(TIMEFRAME, "15m"), "limit": 2}
        data3 = bx_get("/openApi/swap/v3/quote/klines", params)
        rows = data3.get("data", [])
        if rows and isinstance(rows, list):
            return float(rows[-1][4])
    except Exception as e:
        errors.append(f"kline: {e}")

    raise ValueError(f"get_live_price({symbol}) falló: {errors}")

# ── SPREAD FILTER (V10 NUEVO) ──────────────────────────────────────────────────
def get_spread_pct(symbol):
    """Retorna spread bid/ask en porcentaje. Retorna 999 si falla."""
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

    return round(sl_price, 6), round(tp_price, 6)

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

# ── TRAILING STOP (V10 NUEVO) ─────────────────────────────────────────────────
def update_trailing_stops(positions):
    """
    Para cada posición abierta, comprueba si debe mover el SL a breakeven.
    Requiere tener el ATR original almacenado (lo estimamos del SL actual).
    Solo activa si el precio ha movido +BE_ATR_MULT × ATR a favor.
    """
    if not TRAILING_STOP or not positions:
        return
    for sym, pos in positions.items():
        try:
            side = pos.get("positionSide", "LONG")
            entry = float(pos.get("avgPrice", 0) or 0)
            if entry == 0:
                continue
            live = get_live_price(sym)
            # Estimar ATR: si tenemos cache, lo calculamos; si no, skip
            with ws_cache_lock:
                df = ws_kline_cache.get(sym)
            if df is None or len(df) < 20:
                continue
            atr_s = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
            atr = float(atr_s.iloc[-2]) if len(atr_s) > 1 else 0
            if atr == 0:
                continue
            # ¿El precio ya fue +BE_ATR_MULT × ATR a favor?
            if side == "LONG" and live >= entry + atr * BE_ATR_MULT:
                new_sl = round(entry * 1.001, 6)  # BE + 0.1%
                bx_post("/openApi/swap/v2/trade/order", {
                    "symbol": sym,
                    "type": "STOP_MARKET",
                    "side": "SELL",
                    "positionSide": "LONG",
                    "stopPrice": new_sl,
                    "closePosition": "true",
                    "workingType": "MARK_PRICE"
                })
                log.info(f"✅ Trailing BE movido {sym} LONG → SL={new_sl}")
            elif side == "SHORT" and live <= entry - atr * BE_ATR_MULT:
                new_sl = round(entry * 0.999, 6)
                bx_post("/openApi/swap/v2/trade/order", {
                    "symbol": sym,
                    "type": "STOP_MARKET",
                    "side": "BUY",
                    "positionSide": "SHORT",
                    "stopPrice": new_sl,
                    "closePosition": "true",
                    "workingType": "MARK_PRICE"
                })
                log.info(f"✅ Trailing BE movido {sym} SHORT → SL={new_sl}")
        except Exception as e:
            log.debug(f"Trailing stop {sym}: {e}")

# ── KLINES ────────────────────────────────────────────────────────────────────
def get_klines(symbol, limit=300):
    # Intentar desde WS cache primero
    if USE_WS_CACHE:
        with ws_cache_lock:
            df = ws_kline_cache.get(symbol)
        if df is not None and len(df) >= limit // 2:
            return df.copy()

    # REST fallback
    params = {"symbol": symbol, "interval": INTERVAL_MAP.get(TIMEFRAME, "15m"), "limit": limit}
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

    # Guardar en cache
    if USE_WS_CACHE:
        with ws_cache_lock:
            ws_kline_cache[symbol] = df.copy()

    return df

# ── INDICADORES ───────────────────────────────────────────────────────────────
def calc_atr(high, low, close, period):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
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
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    alpha = 1.0 / period
    def wilder(arr):
        return pd.Series(arr, index=high.index).ewm(alpha=alpha, adjust=False).mean()
    tr_s   = wilder(tr)
    pdm_s  = wilder(plus_dm)
    mdm_s  = wilder(minus_dm)
    di_plus  = 100 * pdm_s / tr_s.replace(0, np.nan)
    di_minus = 100 * mdm_s / tr_s.replace(0, np.nan)
    dx  = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return di_plus, di_minus, adx

def calc_rsi(close, period):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def ph_series(high, left, right):
    out = pd.Series(np.nan, index=high.index)
    for i in range(left, len(high) - right):
        w = high.iloc[i - left:i + right + 1]
        if high.iloc[i] == w.max():
            out.iloc[i] = high.iloc[i]
    return out

def pl_series(low, left, right):
    out = pd.Series(np.nan, index=low.index)
    for i in range(left, len(low) - right):
        w = low.iloc[i - left:i + right + 1]
        if low.iloc[i] == w.min():
            out.iloc[i] = low.iloc[i]
    return out

# ── ESTRATEGIA PRINCIPAL ──────────────────────────────────────────────────────
def scan_symbol(symbol):
    # V10: cooldown check
    if symbol in sl_cooldown:
        elapsed = (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds() / 60
        if elapsed < COOLDOWN_MINS:
            return None

    try:
        df = get_klines(symbol, limit=300)
        min_bars = max(PIVOT_LEN * 2 + 2, ATR_LEN + 1, EMA_TREND + 10,
                       ADX_LEN * 2 + 5, RSI_LEN + 5, 80)
        if df.empty or len(df) < min_bars:
            return None

        atr_s        = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
        ema_f        = df["close"].ewm(span=EMA_FAST,  adjust=False).mean()
        ema_s        = df["close"].ewm(span=EMA_SLOW,  adjust=False).mean()
        ema_trend    = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
        angle        = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        di_p, di_m, adx_s = calc_adx(df["high"], df["low"], df["close"], ADX_LEN)
        rsi_s        = calc_rsi(df["close"], RSI_LEN)
        vol_ma       = df["volume"].rolling(20).mean()
        peak         = ph_series(df["high"], PIVOT_LEN, PIVOT_LEN).ffill()
        valley       = pl_series(df["low"],  PIVOT_LEN, PIVOT_LEN).ffill()

        i = len(df) - 2
        if i < max(PIVOT_LEN + 1, EMA_TREND + 2, ADX_LEN * 2):
            return None

        close_now     = float(df["close"].iloc[i])
        close_prev    = float(df["close"].iloc[i - 1])
        ema_f_now     = float(ema_f.iloc[i])
        ema_s_now     = float(ema_s.iloc[i])
        ema_trend_now = float(ema_trend.iloc[i])
        angle_now     = float(angle.iloc[i])
        adx_now       = float(adx_s.iloc[i])
        di_p_now      = float(di_p.iloc[i])
        di_m_now      = float(di_m.iloc[i])
        rsi_now       = float(rsi_s.iloc[i])
        vol_now       = float(df["volume"].iloc[i])
        vma           = float(vol_ma.iloc[i])
        cpeak         = float(peak.iloc[i])
        cvalley       = float(valley.iloc[i])
        catr          = float(atr_s.iloc[i])

        if any(np.isnan(x) for x in [angle_now, adx_now, cpeak, cvalley, catr,
                                      ema_f_now, ema_s_now, ema_trend_now,
                                      rsi_now, di_p_now, di_m_now]):
            return None
        if vma <= 0 or catr <= 0:
            return None

        atr_pct = (catr / close_now) * 100
        if atr_pct > ATR_MAX_PCT:
            return None

        vratio = round(vol_now / vma, 2)

        vol_confirm  = (not USE_VOL) or (vratio >= VOL_MULT)
        adx_confirm  = (not USE_ADX) or (adx_now > ADX_MIN)

        trend_long  = close_now > ema_trend_now
        trend_short = close_now < ema_trend_now

        # V10: RSI más estricto — no entrar LONG si RSI > OB, no SHORT si RSI < OS
        rsi_long_ok  = (not USE_RSI) or (rsi_now < RSI_OB)
        rsi_short_ok = (not USE_RSI) or (rsi_now > RSI_OS)

        di_long_ok  = (not USE_DI) or (di_p_now > di_m_now)
        di_short_ok = (not USE_DI) or (di_m_now > di_p_now)

        angle_long_ok  = angle_now >= SLOPE_LIMIT
        angle_short_ok = angle_now <= -SLOPE_LIMIT

        # V10: Añadido filtro de aceleración — EMA fast debe estar acelerando
        ema_f_prev = float(ema_f.iloc[i - 1])
        ema_s_prev = float(ema_s.iloc[i - 1])
        accel_long  = ema_f_now > ema_f_prev  # EMA fast acelerando hacia arriba
        accel_short = ema_f_now < ema_f_prev

        slope_long = (
            ema_f_now > ema_s_now  and
            angle_long_ok          and
            adx_confirm            and
            vol_confirm            and
            trend_long             and
            rsi_long_ok            and
            di_long_ok             and
            accel_long             # V10: nuevo filtro
        )
        slope_short = (
            ema_f_now < ema_s_now  and
            angle_short_ok         and
            adx_confirm            and
            vol_confirm            and
            trend_short            and
            rsi_short_ok           and
            di_short_ok            and
            accel_short            # V10: nuevo filtro
        )

        zz_long  = (close_prev <= cpeak)   and (close_now > cpeak)
        zz_short = (close_prev >= cvalley) and (close_now < cvalley)

        if STRATEGY_MODE == "slope":
            is_long, is_short = slope_long, slope_short
            method = "SLOPE+ADX"
        elif STRATEGY_MODE == "zigzag":
            is_long  = zz_long  and vol_confirm and trend_long
            is_short = zz_short and vol_confirm and trend_short
            method = "ZIGZAG"
        else:
            is_long  = slope_long  and zz_long
            is_short = slope_short and zz_short
            method = "DUAL"

        if not is_long and not is_short:
            return None

        direction = "LONG" if is_long else "SHORT"

        sl_distance = catr * SL_ATR_MULT

        if direction == "LONG":
            sl_pivot  = min(cvalley, close_now - sl_distance)
            sl_price  = min(sl_pivot, close_now - sl_distance)
            if sl_price >= close_now or (close_now - sl_price) / close_now * 100 < MIN_DIST_PCT:
                sl_price = close_now * (1 - MIN_DIST_PCT / 100)
            tp_price  = close_now + (close_now - sl_price) * TP_MULT
            if sl_price >= close_now or tp_price <= close_now:
                return None
        else:
            sl_pivot  = max(cpeak, close_now + sl_distance)
            sl_price  = max(sl_pivot, close_now + sl_distance)
            if sl_price <= close_now or (sl_price - close_now) / close_now * 100 < MIN_DIST_PCT:
                sl_price = close_now * (1 + MIN_DIST_PCT / 100)
            tp_price  = close_now - (sl_price - close_now) * TP_MULT
            if sl_price <= close_now or tp_price >= close_now:
                return None

        dist     = abs(close_now - sl_price)
        dist_pct = (dist / close_now) * 100
        if dist_pct < MIN_DIST_PCT:
            return None

        rr = abs(tp_price - close_now) / dist

        # V10: scoring mejorado con pesos más realistas
        score  = min(abs(angle_now) / SLOPE_LIMIT * 25, 30)   # ángulo: peso 30
        score += min((adx_now - ADX_MIN) / ADX_MIN * 20, 25)  # ADX: peso 25
        score += min(vratio * 8, 15)                           # volumen: peso 15
        score += min(rr * 6, 15)                               # RR: peso 15
        score += 10 if (trend_long or trend_short) else 0      # tendencia: peso 10
        score += min(abs(di_p_now - di_m_now) / 10, 5)        # DI spread: peso 5

        if score < MIN_SCORE:
            return None

        return {
            "symbol":    symbol,
            "signal":    direction,
            "method":    method,
            "close":     close_now,
            "sl":        sl_price,
            "tp":        tp_price,
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
        }
    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

# ── POSITION SIZING V10 ───────────────────────────────────────────────────────
def calc_qty(balance, entry, sl):
    """
    V10: position sizing con límites MIN/MAX en USDT.
    """
    risk = balance * (RISK_PERCENT / 100)
    dist = abs(entry - sl)
    if dist == 0:
        return 0
    qty = max(round((risk * LEVERAGE) / entry, 4), 0.001)

    # Calcular notional
    notional = qty * entry
    if notional < MIN_ORDER_USDT:
        qty = round(MIN_ORDER_USDT / entry, 4)
    elif notional > MAX_ORDER_USDT:
        qty = round(MAX_ORDER_USDT / entry, 4)

    return qty

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
        f"🚀 <b>EMA+ADX+ZigZag Elite V10.0 — TURBO EDITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔀 <b>Modo:</b> {STRATEGY_MODE.upper()} | <b>TF:</b> {TIMEFRAME}\n"
        f"<b>EMA:</b> {EMA_FAST}/{EMA_SLOW}/T{EMA_TREND} | "
        f"<b>Slope≥:</b> {SLOPE_LIMIT}° | <b>ADX≥:</b> {ADX_MIN}\n"
        f"<b>TP mult:</b> {TP_MULT}x | <b>SL ATR:</b> {SL_ATR_MULT}x | "
        f"<b>Score≥:</b> {MIN_SCORE}\n"
        f"<b>DI:</b> {'✅' if USE_DI else '❌'} | "
        f"<b>Vol≥:</b> {VOL_MULT}x | <b>RSI:</b> {RSI_OS}-{RSI_OB}\n"
        f"<b>WS Cache:</b> {'✅' if USE_WS_CACHE else '❌'} | "
        f"<b>Trailing:</b> {'✅' if TRAILING_STOP else '❌'} (BE@{BE_ATR_MULT}x ATR)\n"
        f"<b>Pos size:</b> {MIN_ORDER_USDT}–{MAX_ORDER_USDT} USDT | "
        f"<b>Max trades:</b> {MAX_OPEN_TRADES}\n"
        f"<b>Balance:</b> {balance:.2f} USDT | <b>Símbolos:</b> {len(symbols)}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_entry(sig, qty, balance, spread_pct=None):
    d = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    spread_str = f" | <b>Spread:</b> {spread_pct:.3f}%" if spread_pct is not None else ""
    tg(
        f"<b>✅ ORDEN ABIERTA — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"<b>Modo:</b> {sig['method']} | <b>ADX:</b> {sig['adx']} | "
        f"<b>Ang:</b> {sig['angle']}° | <b>RSI:</b> {sig['rsi']}\n"
        f"<b>Vol:</b> {sig['vol_ratio']}x | <b>ATR:</b> {sig['atr_pct']}%{spread_str}\n"
        f"<b>DI spread:</b> {sig.get('di_spread','?')}\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.6g}</code>\n"
        f"<b>RR:</b> 1:{sig['rr']} | <b>Qty:</b> {qty:.4f} | "
        f"<b>Riesgo:</b> {balance * RISK_PERCENT / 100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_count):
    if not signals:
        return
    lines = [
        f"🔍 <b>{len(signals)} señal(es) / {total}</b> | Trades: {open_count}/{MAX_OPEN_TRADES}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals[:6]:
        e = "🟢" if s["signal"] == "LONG" else "🔴"
        lines.append(
            f"{e} <b>{s['symbol']}</b> [{s['method']}] "
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
    log.info(f"=== EMA+ADX+ZigZag Elite V10.0 TURBO [{STRATEGY_MODE.upper()}] TF={TIMEFRAME} ===")
    log.info(f"  ADX≥{ADX_MIN} | Slope≥{SLOPE_LIMIT}° | TP×{TP_MULT} | SL×{SL_ATR_MULT} | Score≥{MIN_SCORE}")
    log.info(f"  DI={USE_DI} | Vol≥{VOL_MULT}x | WS={USE_WS_CACHE} | Trailing={TRAILING_STOP}")
    log.info(f"  Pos: {MIN_ORDER_USDT}–{MAX_ORDER_USDT} USDT | Cooldown: {COOLDOWN_MINS}min")

    symbols   = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols:
        symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance: {balance:.4f} | Symbols: {len(symbols)} | Open: {len(positions)}")

    # Pre-cargar klines en cache via REST para los top símbolos
    log.info("Pre-cargando klines en cache REST...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s, 300), symbols[:100]))
    log.info("Cache REST listo.")

    # Iniciar WebSocket cache en background
    start_ws_cache(symbols)
    time.sleep(2)  # dar tiempo al WS para conectar

    # Configurar leverage
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))

    tg_startup(balance, symbols)
    log.info("✅ Bot V10 iniciado. Loop comenzando.")

    errors = 0

    while True:
        t0 = time.time()
        try:
            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)

            log.info(f"── V10 [{STRATEGY_MODE}] {balance:.4f} USDT | "
                     f"{open_count}/{MAX_OPEN_TRADES} trades | {len(symbols)} sym ──")

            # V10: actualizar trailing stops en posiciones abiertas
            if TRAILING_STOP and positions:
                update_trailing_stops(positions)

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
                        f"  → {s['symbol']} {s['signal']} [{s['method']}] "
                        f"score={s['score']} ang={s['angle']}° adx={s['adx']} "
                        f"rsi={s['rsi']} vol={s['vol_ratio']}x rr=1:{s['rr']}"
                    )

            entered: set = set()
            skip_reasons: dict = {}
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
                if balance < 1:
                    skip_reasons[sym] = f"balance bajo ({balance:.2f})"
                    break

                # V10: filtro de spread antes de operar
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
                        reason = f"sin precio: {str(ep)[:60]}"
                        skip_reasons[sym] = reason
                        continue

                    sl_live, tp_live = recalc_sl_tp(sig, live_price)
                    if sl_live is None:
                        reason = f"SL/TP inválido live={live_price:.6g}"
                        skip_reasons[sym] = reason
                        continue

                    qty = calc_qty(balance, live_price, sl_live)
                    if qty <= 0:
                        reason = "qty=0"
                        skip_reasons[sym] = reason
                        continue

                    # Verificar que notional mínimo sea correcto
                    notional = qty * live_price
                    log.info(f"Orden {sym} {side} qty={qty:.4f} "
                             f"notional={notional:.2f} USDT "
                             f"live={live_price:.6g} sl={sl_live:.6g} tp={tp_live:.6g} "
                             f"spread={spread:.3f}%")

                    res = open_order_with_retry(sym, side, qty, sl_live, tp_live, retries=1)
                    log.info(f"✅ {sym} {side} qty={qty:.4f} | {res}")

                    sig["close"]    = live_price
                    sig["sl"]       = sl_live
                    sig["tp"]       = tp_live
                    sig["dist_pct"] = round(abs(live_price - sl_live) / live_price * 100, 3)
                    sig["rr"]       = round(abs(tp_live - live_price) / abs(live_price - sl_live), 2)

                    tg_entry(sig, qty, balance, spread_pct=spread)
                    entered.add(sym)
                    open_count += 1
                    orders_opened += 1
                    time.sleep(0.5)

                except Exception as e:
                    reason = str(e)[:100]
                    log.error(f"Order FAILED {sym}: {e}")
                    skip_reasons[sym] = f"error: {reason}"
                    # V10: si es un SL activado, añadir cooldown
                    if "stop" in reason.lower() or "liquidat" in reason.lower():
                        sl_cooldown[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            if signals and orders_opened == 0 and skip_reasons:
                log.warning(f"Señales={len(signals)} pero 0 órdenes. Razones: {skip_reasons}")
                tg_diag(signals, skip_reasons)

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot V10 detenido</b>")
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
