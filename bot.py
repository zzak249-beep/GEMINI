"""
ZigZag + EMA Slope + ADX Elite V9.2 — BALANCED EDITION
MEJORAS WINRATE:
  1. Filtro de tendencia mayor: EMA50 en mismo TF (solo LONG si close>EMA50, SHORT si close<EMA50)
  2. RSI como confirmador: evita entrar en sobrecompra/sobreventa extrema
  3. SL mínimo de 1.5x ATR para dar más margen y evitar stop hunts
  4. Confirmación de cierre de vela: verifica que la vela anterior también confirme dirección
  5. Filtro de spread ATR: descarta señales cuando ATR/precio es demasiado alto (crypto volátil)
  6. SL separación mínima garantizada de 0.5% del precio para evitar error 101400
  7. Score mínimo en 30 (bajado de 40 para más señales)
  8. SLOPE_LOOK=5 para ángulo más estable
  9. Doble confirmación EMA: ema_fast y ema_slow ambas en dirección correcta

FIX CRÍTICO v9.1:
  - Error 101400 "SL Price must be greater than Last Price":
    Se fetcha el precio de mercado en vivo JUSTO ANTES de enviar la orden
    y se recalcula SL/TP sobre ese precio actualizado.

AJUSTE v9.2 — FILTROS RELAJADOS (más señales):
  - MIN_SCORE  : 40.0 → 30.0  (menos exigente)
  - ADX_MIN    : 20.0 → 15.0  (mercados menos tendenciales también válidos)
  - VOL_MULT   : 1.2  → 1.0   (volumen normal ya es suficiente)
  - SLOPE_LIMIT: 20.0 → 15.0  (ángulo más suave permitido)
"""
import os, time, hmac, hashlib, json, asyncio, logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np
from telegram import Bot
from telegram.constants import ParseMode

# ── CONFIG ────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

TIMEFRAME        = os.environ.get("TIMEFRAME",        "5m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.0"))
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "60"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "10"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "0"))
MIN_SCORE        = float(os.environ.get("MIN_SCORE",      "30.0"))   # ↓ bajado de 40 → más señales
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT",   "0.5"))

# ── EMA SLOPE ─────────────────────────────────────────────────────────────────
EMA_FAST         = int(os.environ.get("EMA_FAST",         "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",         "17"))
EMA_TREND        = int(os.environ.get("EMA_TREND",        "50"))
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT",    "15.0"))   # ↓ bajado de 20 → ángulo más suave
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",       "5"))

# ── ADX ───────────────────────────────────────────────────────────────────────
ADX_LEN          = int(os.environ.get("ADX_LEN",          "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN",        "15.0"))   # ↓ bajado de 20 → más tendencias válidas
USE_ADX          = os.environ.get("USE_ADX", "true").lower() == "true"

# ── RSI ───────────────────────────────────────────────────────────────────────
RSI_LEN          = int(os.environ.get("RSI_LEN",          "14"))
RSI_OB           = float(os.environ.get("RSI_OB",         "70.0"))
RSI_OS           = float(os.environ.get("RSI_OS",         "30.0"))
USE_RSI          = os.environ.get("USE_RSI", "true").lower() == "true"

# ── VOLUME ────────────────────────────────────────────────────────────────────
USE_VOL          = os.environ.get("USE_VOL", "true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT",       "1.0"))    # ↓ bajado de 1.2 → volumen normal OK

# ── ZIGZAG / ATR ──────────────────────────────────────────────────────────────
ATR_LEN          = int(os.environ.get("ATR_LEN",          "14"))
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN",        "3"))
TP_MULT          = float(os.environ.get("TP_MULT",        "1.5"))
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT",    "1.5"))
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",    "3.0"))

STRATEGY_MODE    = os.environ.get("STRATEGY_MODE", "slope")

_raw = os.environ.get("CUSTOM_SYMBOLS", "")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE   = "https://open-api.bingx.com"
INTERVAL_MAP = {"1m":"1m","3m":"3m","5m":"5m","15m":"15m",
                "30m":"30m","1h":"1H","4h":"4H","1d":"1D"}
EXCLUDED_PREFIXES = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG")

FALLBACK_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "MATIC-USDT","UNI-USDT","LTC-USDT","BCH-USDT","ATOM-USDT",
    "XLM-USDT","ETC-USDT","NEAR-USDT","APT-USDT","OP-USDT",
    "ARB-USDT","FIL-USDT","ICP-USDT","HBAR-USDT","AAVE-USDT",
    "GRT-USDT","MKR-USDT","CRV-USDT","LDO-USDT","RUNE-USDT",
    "INJ-USDT","SUI-USDT","TIA-USDT","SEI-USDT","WIF-USDT",
    "PEPE-USDT","FLOKI-USDT","WLD-USDT","GMX-USDT","DYDX-USDT",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

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
        log.info(f"RAW balance: {json.dumps(data)[:400]}")
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
                    log.info(f"Balance(0-ok): {float(v):.4f} USDT")
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
        log.warning(f"Balance not found: {data}")
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
    return len(base) >= 2 and not any(base.startswith(p) for p in EXCLUDED_PREFIXES)

def _symbols_from_contracts():
    data = bx_get("/openApi/swap/v2/quote/contracts", {})
    contracts = data.get("data", [])
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("Empty contracts")
    usdt = [c for c in contracts if isinstance(c, dict) and c.get("asset", "") == "USDT" and c.get("status") == 1]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c, dict) and c.get("asset", "") == "USDT"]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c, dict) and str(c.get("symbol", "")).endswith("-USDT")]
    if not usdt:
        raise ValueError("No USDT contracts")
    usdt.sort(key=lambda x: float(x.get("tradeAmount", 0) or 0), reverse=True)
    return [c["symbol"] for c in usdt if _is_valid(c.get("symbol", ""))]

def _symbols_from_ticker():
    data = bx_get("/openApi/swap/v2/quote/ticker", {})
    tickers = data.get("data", [])
    if not isinstance(tickers, list) or not tickers:
        raise ValueError("Empty ticker")
    usdt = [t for t in tickers if isinstance(t, dict) and _is_valid(t.get("symbol", ""))]
    if not usdt:
        raise ValueError("No valid tickers")
    usdt.sort(key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
    return [t["symbol"] for t in usdt]

def _symbols_from_index():
    data = bx_get("/openApi/swap/v2/quote/premiumIndex", {})
    items = data.get("data", [])
    if not isinstance(items, list) or not items:
        raise ValueError("Empty premiumIndex")
    syms = [i["symbol"] for i in items if isinstance(i, dict) and _is_valid(i.get("symbol", ""))]
    if not syms:
        raise ValueError("No valid symbols in premiumIndex")
    return syms

def get_all_symbols(limit=0):
    for fn in (_symbols_from_contracts, _symbols_from_ticker, _symbols_from_index):
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

# ── PRECIO EN VIVO ────────────────────────────────────────────────────────────
# FIX error 101400: obtener precio de mercado actual justo antes de enviar orden
def get_live_price(symbol):
    """
    Obtiene el Mark Price actual de BingX para el símbolo dado.
    Se usa para recalcular SL/TP en el momento exacto de la orden,
    evitando el error 101400 causado por el desfase entre el precio
    del scan y el precio real en el momento de la ejecución.
    """
    try:
        data = bx_get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        items = data.get("data", [])
        if isinstance(items, list):
            for item in items:
                if item.get("symbol") == symbol:
                    return float(item["markPrice"])
        # Fallback: último precio del ticker
        data2 = bx_get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        tickers = data2.get("data", [])
        if isinstance(tickers, list):
            for t in tickers:
                if t.get("symbol") == symbol:
                    return float(t["lastPrice"])
        raise ValueError(f"No se encontró precio en vivo para {symbol}")
    except Exception as e:
        raise ValueError(f"get_live_price({symbol}) falló: {e}")


def recalc_sl_tp(sig, live_price):
    """
    Recalcula SL y TP usando el precio en vivo en lugar del close_now del scan.

    El close_now puede tener 60+ segundos de antigüedad. Si el precio se movió
    en ese tiempo (especialmente en SHORTs, donde SL debe estar POR ENCIMA del
    precio), BingX rechaza la orden con código 101400.

    Retorna (sl_price, tp_price) redondeados, o (None, None) si no son válidos.
    """
    catr      = sig["atr"]
    direction = sig["signal"]
    sl_distance = catr * SL_ATR_MULT

    if direction == "LONG":
        # SL debe estar POR DEBAJO del precio actual
        sl_price = live_price - sl_distance
        # Garantía de separación mínima
        if (live_price - sl_price) / live_price * 100 < MIN_DIST_PCT:
            sl_price = live_price * (1 - MIN_DIST_PCT / 100)
        tp_price = live_price + (live_price - sl_price) * TP_MULT
        # Validación final
        if sl_price >= live_price or tp_price <= live_price:
            log.warning(f"recalc_sl_tp LONG inválido: price={live_price} sl={sl_price} tp={tp_price}")
            return None, None
    else:
        # SHORT: SL debe estar POR ENCIMA del precio actual ← aquí estaba el error 101400
        sl_price = live_price + sl_distance
        # Garantía de separación mínima
        if (sl_price - live_price) / live_price * 100 < MIN_DIST_PCT:
            sl_price = live_price * (1 + MIN_DIST_PCT / 100)
        tp_price = live_price - (sl_price - live_price) * TP_MULT
        # Validación final
        if sl_price <= live_price or tp_price >= live_price:
            log.warning(f"recalc_sl_tp SHORT inválido: price={live_price} sl={sl_price} tp={tp_price}")
            return None, None

    dist_pct = abs(live_price - sl_price) / live_price * 100
    if dist_pct < MIN_DIST_PCT:
        log.warning(f"recalc_sl_tp dist_pct {dist_pct:.3f}% < mínimo {MIN_DIST_PCT}%")
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

def get_klines(symbol, limit=300):
    params = {"symbol": symbol, "interval": INTERVAL_MAP.get(TIMEFRAME, "5m"), "limit": limit}
    data = bx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data", [])
    if not rows or not isinstance(rows, list):
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open", "high", "low", "close", "volume"], inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

# ── INDICADORES ───────────────────────────────────────────────────────────────
def calc_atr(high, low, close, period):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()

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

        close_now    = float(df["close"].iloc[i])
        close_prev   = float(df["close"].iloc[i - 1])
        ema_f_now    = float(ema_f.iloc[i])
        ema_s_now    = float(ema_s.iloc[i])
        ema_trend_now= float(ema_trend.iloc[i])
        angle_now    = float(angle.iloc[i])
        angle_prev   = float(angle.iloc[i - 1])
        adx_now      = float(adx_s.iloc[i])
        di_p_now     = float(di_p.iloc[i])
        di_m_now     = float(di_m.iloc[i])
        rsi_now      = float(rsi_s.iloc[i])
        vol_now      = float(df["volume"].iloc[i])
        vma          = float(vol_ma.iloc[i])
        cpeak        = float(peak.iloc[i])
        cvalley      = float(valley.iloc[i])
        catr         = float(atr_s.iloc[i])

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

        rsi_long_ok  = (not USE_RSI) or (rsi_now < RSI_OB)
        rsi_short_ok = (not USE_RSI) or (rsi_now > RSI_OS)

        di_long_ok  = di_p_now > di_m_now
        di_short_ok = di_m_now > di_p_now

        angle_long_ok  = (angle_now >= SLOPE_LIMIT)  and (angle_prev >= SLOPE_LIMIT * 0.5)
        angle_short_ok = (angle_now <= -SLOPE_LIMIT) and (angle_prev <= -SLOPE_LIMIT * 0.5)

        slope_long = (
            ema_f_now > ema_s_now and
            angle_long_ok         and
            adx_confirm           and
            vol_confirm           and
            trend_long            and
            rsi_long_ok           and
            di_long_ok
        )
        slope_short = (
            ema_f_now < ema_s_now and
            angle_short_ok        and
            adx_confirm           and
            vol_confirm           and
            trend_short           and
            rsi_short_ok          and
            di_short_ok
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

        # SL/TP basado en close_now para calcular score y referencias del scan
        # (el SL/TP REAL se recalculará con precio en vivo antes de la orden)
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

        score  = min(abs(angle_now) / SLOPE_LIMIT * 20, 25)
        score += min((adx_now - ADX_MIN) / ADX_MIN * 15, 20)
        score += min(vratio * 10, 20)
        score += min(rr * 8, 15)
        score += 10 if trend_long or trend_short else 0
        score += min(abs(di_p_now - di_m_now) / 10, 10)

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
        }
    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

def calc_qty(balance, entry, sl):
    risk = balance * (RISK_PERCENT / 100)
    dist = abs(entry - sl)
    if dist == 0:
        return 0
    return max(round((risk * LEVERAGE) / entry, 4), 0.001)

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def _send(msg):
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def tg(msg):
    try:
        asyncio.run(_send(msg))
    except Exception as e:
        log.warning(f"Telegram error: {e}")

def tg_startup(balance, symbols):
    tg(
        f"🚀 <b>EMA+ADX+ZigZag Elite V9.2 — BALANCED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔀 <b>Modo:</b> {STRATEGY_MODE.upper()} | <b>TF:</b> {TIMEFRAME}\n"
        f"<b>EMA:</b> {EMA_FAST}/{EMA_SLOW}/T{EMA_TREND} | "
        f"<b>Slope≥:</b> {SLOPE_LIMIT}° | <b>Look:</b> {SLOPE_LOOK}\n"
        f"<b>ADX≥:</b> {ADX_MIN} ({'✅' if USE_ADX else '❌'}) | "
        f"<b>Vol≥:</b> {VOL_MULT}x ({'✅' if USE_VOL else '❌'}) | "
        f"<b>RSI:</b> {RSI_OS}-{RSI_OB} ({'✅' if USE_RSI else '❌'})\n"
        f"<b>Score≥:</b> {MIN_SCORE} | <b>SL min:</b> {MIN_DIST_PCT}% | "
        f"<b>SL ATR:</b> {SL_ATR_MULT}x | <b>ATR max:</b> {ATR_MAX_PCT}%\n"
        f"<b>TP:</b> 1:{TP_MULT} | <b>Lev:</b> {LEVERAGE}x | "
        f"<b>Monedas:</b> {len(symbols)} | <b>Max trades:</b> {MAX_OPEN_TRADES}\n"
        f"<b>Balance:</b> {balance:.2f} USDT\n"
        f"🔧 <b>Fix 101400:</b> SL/TP recalculado con precio en vivo ✅\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_count):
    if not signals:
        return
    lines = [
        f"🔍 <b>{len(signals)} señal(es) / {total}</b> | Trades: {open_count}/{MAX_OPEN_TRADES}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals[:8]:
        e = "🟢" if s["signal"] == "LONG" else "🔴"
        lines.append(
            f"{e} <b>{s['symbol']}</b> [{s['method']}] "
            f"Score:{s['score']} Ang:{s['angle']}° ADX:{s['adx']} "
            f"RSI:{s['rsi']} Vol:{s['vol_ratio']}x"
        )
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_entry(sig, qty, balance):
    d = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    tg(
        f"<b>✅ ORDEN EN BINGX — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"<b>Modo:</b> {sig['method']} | <b>ADX:</b> {sig['adx']} | "
        f"<b>Ang:</b> {sig['angle']}° | <b>RSI:</b> {sig['rsi']}\n"
        f"<b>Vol:</b> {sig['vol_ratio']}x | <b>ATR:</b> {sig['atr_pct']}%\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.6g}</code>\n"
        f"<b>RR:</b> 1:{sig['rr']} | <b>Qty:</b> {qty} | "
        f"<b>Riesgo:</b> {balance * RISK_PERCENT / 100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_debug(balance, positions, symbols):
    pos_list = list(positions.keys()) if positions else ["ninguna"]
    tg(
        f"🔧 <b>DEBUG V9.2 BALANCED — {STRATEGY_MODE.upper()}</b>\n"
        f"<b>Balance:</b> {balance:.4f} USDT\n"
        f"<b>Posiciones:</b> {len(positions)} → {', '.join(pos_list[:5])}\n"
        f"<b>Símbolos:</b> {len(symbols)}\n"
        f"<b>EMA {EMA_FAST}/{EMA_SLOW}/T{EMA_TREND}</b> Slope≥{SLOPE_LIMIT}° "
        f"ADX≥{ADX_MIN} Vol≥{VOL_MULT}x RSI {RSI_OS}-{RSI_OB} Score≥{MIN_SCORE}\n"
        f"<b>SL ATR mult:</b> {SL_ATR_MULT}x | <b>TP mult:</b> {TP_MULT}x\n"
        f"<b>MAX_OPEN_TRADES:</b> {MAX_OPEN_TRADES} | <b>TF:</b> {TIMEFRAME}\n"
        f"<b>Fix 101400:</b> precio en vivo antes de orden ✅"
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info(f"=== EMA+ADX+ZigZag Elite V9.2 BALANCED [{STRATEGY_MODE.upper()}] ===")

    symbols = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols:
        symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance: {balance:.4f} | Symbols: {len(symbols)} | Open: {len(positions)}")

    tg_debug(balance, positions, symbols)
    tg_startup(balance, symbols)

    log.info("Setting leverage...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))
    log.info("Ready. Main loop starting.")

    errors  = 0
    entered: set = set()

    while True:
        t0 = time.time()
        try:
            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)

            log.info(f"── [{STRATEGY_MODE}] {balance:.4f} USDT | "
                     f"{open_count}/{MAX_OPEN_TRADES} trades | {len(symbols)} sym ──")

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
                        f"rsi={s['rsi']} vol={s['vol_ratio']}x "
                        f"sl={s['sl']:.6g} tp={s['tp']:.6g}"
                    )

            for sig in signals:
                sym = sig["symbol"]
                if sym in positions or sym in entered:
                    continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info(f"Max trades ({MAX_OPEN_TRADES}) alcanzado.")
                    break
                if balance < 5:
                    log.warning(f"Balance demasiado bajo ({balance:.2f})")
                    break

                side = "BUY" if sig["signal"] == "LONG" else "SELL"
                try:
                    set_lev(sym)

                    # ── FIX ERROR 101400 ──────────────────────────────────────
                    # Obtener precio en vivo y recalcular SL/TP justo antes de
                    # enviar la orden, para que BingX no rechace por SL obsoleto
                    live_price = get_live_price(sym)
                    sl_live, tp_live = recalc_sl_tp(sig, live_price)

                    if sl_live is None:
                        log.warning(f"SL/TP inválido con precio en vivo para {sym} "
                                    f"(live={live_price:.6g}), señal descartada.")
                        continue

                    log.info(f"Precio en vivo {sym}: scan={sig['close']:.6g} "
                             f"live={live_price:.6g} | "
                             f"SL scan={sig['sl']:.6g} → SL live={sl_live:.6g}")

                    qty = calc_qty(balance, live_price, sl_live)
                    if qty <= 0:
                        continue

                    res = open_order(sym, side, qty, sl_live, tp_live)
                    log.info(f"✅ {sym} {side} qty={qty} live={live_price:.6g} "
                             f"sl={sl_live:.6g} tp={tp_live:.6g} | {res}")

                    # Actualizar sig con valores reales para el mensaje Telegram
                    sig["close"]    = live_price
                    sig["sl"]       = sl_live
                    sig["tp"]       = tp_live
                    sig["dist_pct"] = round(abs(live_price - sl_live) / live_price * 100, 3)
                    sig["rr"]       = round(abs(tp_live - live_price) / abs(live_price - sl_live), 2)

                    tg_entry(sig, qty, balance)
                    entered.add(sym)
                    open_count += 1
                    time.sleep(0.5)

                except Exception as e:
                    log.error(f"Order FAILED {sym}: {e}")
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            entered.clear()
            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot detenido</b>")
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
