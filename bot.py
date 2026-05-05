"""
ZigZag + EMA Slope + ADX Elite V9
Estrategia Pine Script: EMA7/17 + Ángulo≥30° + ADX>20 + Volumen + ZigZag timing
"""
import os, time, hmac, hashlib, json, asyncio, logging, math
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

TIMEFRAME        = os.environ.get("TIMEFRAME",        "15m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.0"))
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "120"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "5"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "0"))
MIN_SCORE        = float(os.environ.get("MIN_SCORE",      "35.0"))
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT",   "0.3"))

# ── EMA SLOPE (Pine Script exact) ─────────────────────────────────────────────
EMA_FAST         = int(os.environ.get("EMA_FAST",         "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",         "17"))
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT",    "30.0"))  # grados
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",       "3"))

# ── ADX ───────────────────────────────────────────────────────────────────────
ADX_LEN          = int(os.environ.get("ADX_LEN",          "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN",        "20.0"))  # fuerza mínima
USE_ADX          = os.environ.get("USE_ADX", "true").lower() == "true"

# ── VOLUME ────────────────────────────────────────────────────────────────────
USE_VOL          = os.environ.get("USE_VOL", "true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT",       "1.0"))   # vol > media*1.0

# ── ZIGZAG ────────────────────────────────────────────────────────────────────
ATR_LEN          = int(os.environ.get("ATR_LEN",          "14"))
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN",        "3"))
TP_MULT          = float(os.environ.get("TP_MULT",        "2.0"))

# Modo: "dual" = Slope+ADX+Vol + ZigZag timing | "slope" = solo Slope+ADX+Vol
STRATEGY_MODE    = os.environ.get("STRATEGY_MODE", "dual")

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
    base = sym.replace("-USDT","")
    return len(base) >= 2 and not any(base.startswith(p) for p in EXCLUDED_PREFIXES)

def _symbols_from_contracts():
    data = bx_get("/openApi/swap/v2/quote/contracts", {})
    contracts = data.get("data", [])
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("Empty contracts")
    usdt = [c for c in contracts if isinstance(c,dict) and c.get("asset","")=="USDT" and c.get("status")==1]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c,dict) and c.get("asset","")=="USDT"]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c,dict) and str(c.get("symbol","")).endswith("-USDT")]
    if not usdt:
        raise ValueError("No USDT contracts")
    usdt.sort(key=lambda x: float(x.get("tradeAmount",0) or 0), reverse=True)
    return [c["symbol"] for c in usdt if _is_valid(c.get("symbol",""))]

def _symbols_from_ticker():
    data = bx_get("/openApi/swap/v2/quote/ticker", {})
    tickers = data.get("data", [])
    if not isinstance(tickers, list) or not tickers:
        raise ValueError("Empty ticker")
    usdt = [t for t in tickers if isinstance(t,dict) and _is_valid(t.get("symbol",""))]
    if not usdt:
        raise ValueError("No valid tickers")
    usdt.sort(key=lambda x: float(x.get("quoteVolume",0) or 0), reverse=True)
    return [t["symbol"] for t in usdt]

def _symbols_from_index():
    data = bx_get("/openApi/swap/v2/quote/premiumIndex", {})
    items = data.get("data", [])
    if not isinstance(items, list) or not items:
        raise ValueError("Empty premiumIndex")
    syms = [i["symbol"] for i in items if isinstance(i,dict) and _is_valid(i.get("symbol",""))]
    if not syms:
        raise ValueError("No valid symbols in premiumIndex")
    return syms

def get_all_symbols(limit=0):
    for fn in (_symbols_from_contracts, _symbols_from_ticker, _symbols_from_index):
        try:
            syms = fn()
            if syms:
                result = syms if limit==0 else syms[:limit]
                log.info(f"✅ {len(result)} symbols via {fn.__name__}")
                return result
        except Exception as e:
            log.warning(f"{fn.__name__} failed: {e}")
    log.warning(f"⚠️ Using fallback ({len(FALLBACK_SYMBOLS)} syms)")
    return FALLBACK_SYMBOLS if limit==0 else FALLBACK_SYMBOLS[:limit]

def set_lev(symbol):
    for side in ("LONG","SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol":symbol,"side":side,"leverage":LEVERAGE})
        except Exception:
            pass

# ── FIX PRINCIPAL: stopLoss type=STOP_MARKET, takeProfit type=TAKE_PROFIT_MARKET ──
def open_order(symbol, side, qty, sl, tp):
    payload = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "LONG" if side=="BUY" else "SHORT",
        "type":         "MARKET",
        "quantity":     round(qty, 4),
        "stopLoss":     json.dumps({
            "type":        "STOP_MARKET",        # ← CORREGIDO (antes: "MARK_PRICE")
            "stopPrice":   round(sl, 6),
            "workingType": "MARK_PRICE"
        }),
        "takeProfit":   json.dumps({
            "type":        "TAKE_PROFIT_MARKET", # ← CORREGIDO (antes: "MARK_PRICE")
            "stopPrice":   round(tp, 6),
            "workingType": "MARK_PRICE"
        }),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    code = resp.get("code", -1)
    if code != 0:
        raise ValueError(f"BingX code={code}: {resp.get('msg','unknown')}")
    return resp

def get_klines(symbol, limit=250):
    params = {"symbol":symbol, "interval":INTERVAL_MAP.get(TIMEFRAME,"15m"), "limit":limit}
    data = bx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data", [])
    if not rows or not isinstance(rows, list):
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.sort_values("open_time").reset_index(drop=True)

# ── INDICADORES ───────────────────────────────────────────────────────────────
def calc_atr(high, low, close, period):
    tr = pd.concat([high-low,
                    (high-close.shift()).abs(),
                    (low-close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calc_ema_angle(ema_s, atr_s, look):
    """Réplica exacta Pine: atan(price_change / (atr * lookback)) * (180/pi)"""
    price_change = ema_s - ema_s.shift(look)
    denom = atr_s * look
    angle = np.degrees(np.arctan2(price_change.values, denom.values))
    return pd.Series(angle, index=ema_s.index)

def calc_adx(high, low, close, period):
    """
    ADX = Wilder smoothed DX
    DI+ = 100 * smooth(+DM) / smooth(TR)
    DI- = 100 * smooth(-DM) / smooth(TR)
    DX  = 100 * |DI+ - DI-| / (DI+ + DI-)
    ADX = EMA(DX, period)
    """
    up   = high.diff()
    down = -low.diff()
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = pd.concat([high-low,
                    (high-close.shift()).abs(),
                    (low-close.shift()).abs()], axis=1).max(axis=1)

    alpha = 1.0 / period
    def wilder(arr):
        s = pd.Series(arr, index=high.index)
        return s.ewm(alpha=alpha, adjust=False).mean()

    tr_s   = wilder(tr)
    pdm_s  = wilder(plus_dm)
    mdm_s  = wilder(minus_dm)

    di_plus  = 100 * pdm_s / tr_s.replace(0, np.nan)
    di_minus = 100 * mdm_s / tr_s.replace(0, np.nan)
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return di_plus, di_minus, adx

def ph_series(high, left, right):
    out = pd.Series(np.nan, index=high.index)
    for i in range(left, len(high)-right):
        w = high.iloc[i-left:i+right+1]
        if high.iloc[i] == w.max():
            out.iloc[i] = high.iloc[i]
    return out

def pl_series(low, left, right):
    out = pd.Series(np.nan, index=low.index)
    for i in range(left, len(low)-right):
        w = low.iloc[i-left:i+right+1]
        if low.iloc[i] == w.min():
            out.iloc[i] = low.iloc[i]
    return out

# ── ESTRATEGIA PRINCIPAL ──────────────────────────────────────────────────────
def scan_symbol(symbol):
    """
    Pine Script V9 logic:
      LONG:  EMA7>EMA17  AND angle7 >= +SLOPE_LIMIT  AND adx>ADX_MIN  AND vol>volAvg
      SHORT: EMA7<EMA17  AND angle7 <= -SLOPE_LIMIT  AND adx>ADX_MIN  AND vol>volAvg

    + ZigZag breakout de pivot para timing de entrada (modo dual)

    SL/TP: basado en ATR y pivot más cercano (como en bot anterior)
    """
    try:
        df = get_klines(symbol)
        min_bars = max(PIVOT_LEN*2+2, ATR_LEN+1, EMA_SLOW+10, ADX_LEN*2+5, 60)
        if df.empty or len(df) < min_bars:
            return None

        atr_s  = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
        ema_f  = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        ema_s  = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
        angle  = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        _, _, adx_s = calc_adx(df["high"], df["low"], df["close"], ADX_LEN)
        vol_ma = df["volume"].rolling(20).mean()
        peak   = ph_series(df["high"], PIVOT_LEN, PIVOT_LEN).ffill()
        valley = pl_series(df["low"],  PIVOT_LEN, PIVOT_LEN).ffill()

        i = len(df) - 2   # última vela cerrada
        if i < max(PIVOT_LEN+1, EMA_SLOW+2, ADX_LEN*2):
            return None

        close_now  = float(df["close"].iloc[i])
        close_prev = float(df["close"].iloc[i-1])
        ema_f_now  = float(ema_f.iloc[i])
        ema_s_now  = float(ema_s.iloc[i])
        angle_now  = float(angle.iloc[i])
        adx_now    = float(adx_s.iloc[i])
        vol_now    = float(df["volume"].iloc[i])
        vma        = float(vol_ma.iloc[i])
        cpeak      = float(peak.iloc[i])
        cvalley    = float(valley.iloc[i])
        catr       = float(atr_s.iloc[i])

        if any(np.isnan(x) for x in [angle_now, adx_now, cpeak, cvalley, catr, ema_f_now, ema_s_now]):
            return None
        if vma <= 0:
            return None

        vratio = round(vol_now / vma, 2)

        # ── Pine Script logic ─────────────────────────────────────────────────
        vol_confirm  = (not USE_VOL) or (vratio >= VOL_MULT)
        adx_confirm  = (not USE_ADX) or (adx_now > ADX_MIN)

        slope_long  = (ema_f_now > ema_s_now) and (angle_now >= SLOPE_LIMIT)  and adx_confirm and vol_confirm
        slope_short = (ema_f_now < ema_s_now) and (angle_now <= -SLOPE_LIMIT) and adx_confirm and vol_confirm

        # ── ZigZag breakout ───────────────────────────────────────────────────
        zz_long  = (close_prev <= cpeak)   and (close_now > cpeak)
        zz_short = (close_prev >= cvalley) and (close_now < cvalley)

        # ── Combinar según modo ───────────────────────────────────────────────
        if STRATEGY_MODE == "slope":
            is_long, is_short = slope_long, slope_short
            method = "SLOPE+ADX"
        elif STRATEGY_MODE == "zigzag":
            is_long  = zz_long  and vol_confirm
            is_short = zz_short and vol_confirm
            method = "ZIGZAG"
        else:  # dual — slope da dirección, zigzag da timing
            is_long  = slope_long  and zz_long
            is_short = slope_short and zz_short
            method = "DUAL"

        if not is_long and not is_short:
            return None

        direction = "LONG" if is_long else "SHORT"

        # ── SL / TP via ATR + pivot ───────────────────────────────────────────
        if direction == "LONG":
            sl_price = max(cvalley, close_now - catr * 2)
            tp_price = close_now + (close_now - sl_price) * TP_MULT
        else:
            sl_price = min(cpeak, close_now + catr * 2)
            tp_price = close_now - (sl_price - close_now) * TP_MULT

        dist = abs(close_now - sl_price)
        if dist == 0:
            return None

        dist_pct = (dist / close_now) * 100
        if dist_pct < MIN_DIST_PCT:
            return None

        rr = abs(tp_price - close_now) / dist

        # Score compuesto
        score  = min(abs(angle_now) / SLOPE_LIMIT * 25, 30)   # ángulo (30)
        score += min((adx_now - ADX_MIN) / ADX_MIN * 20, 25)   # ADX (25)
        score += min(vratio * 15, 25)                            # volumen (25)
        score += min(rr * 10, 20)                                # RR (20)

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
            "vol_ratio": vratio,
            "angle":     round(angle_now, 1),
            "adx":       round(adx_now, 1),
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
        f"🚀 <b>EMA+ADX+ZigZag Elite V9</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔀 <b>Modo:</b> {STRATEGY_MODE.upper()}\n"
        f"<b>EMA:</b> {EMA_FAST}/{EMA_SLOW} | <b>Slope≥:</b> {SLOPE_LIMIT}° | <b>Look:</b> {SLOPE_LOOK}\n"
        f"<b>ADX≥:</b> {ADX_MIN} ({'✅' if USE_ADX else '❌'}) | "
        f"<b>Vol≥:</b> {VOL_MULT}x ({'✅' if USE_VOL else '❌'})\n"
        f"<b>Score≥:</b> {MIN_SCORE} | <b>SL min:</b> {MIN_DIST_PCT}%\n"
        f"<b>Pivot:</b> {PIVOT_LEN} | <b>TP:</b> 1:{TP_MULT} | <b>Lev:</b> {LEVERAGE}x\n"
        f"<b>Monedas:</b> {len(symbols)} | <b>TF:</b> {TIMEFRAME}\n"
        f"<b>Balance:</b> {balance:.2f} USDT | <b>Max trades:</b> {MAX_OPEN_TRADES}\n"
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
        e = "🟢" if s["signal"]=="LONG" else "🔴"
        lines.append(
            f"{e} <b>{s['symbol']}</b> [{s['method']}] "
            f"Score:{s['score']} Ang:{s['angle']}° ADX:{s['adx']} Vol:{s['vol_ratio']}x"
        )
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_entry(sig, qty, balance):
    d = "🟢 LONG" if sig["signal"]=="LONG" else "🔴 SHORT"
    tg(
        f"<b>✅ ORDEN EN BINGX — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"<b>Modo:</b> {sig['method']} | <b>ADX:</b> {sig['adx']} | "
        f"<b>Ang:</b> {sig['angle']}° | <b>Vol:</b> {sig['vol_ratio']}x\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.6g}</code>\n"
        f"<b>RR:</b> 1:{sig['rr']} | <b>Qty:</b> {qty} | "
        f"<b>Riesgo:</b> {balance*RISK_PERCENT/100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_debug(balance, positions, symbols):
    pos_list = list(positions.keys()) if positions else ["ninguna"]
    tg(
        f"🔧 <b>DEBUG V9 — {STRATEGY_MODE.upper()}</b>\n"
        f"<b>Balance:</b> {balance:.4f} USDT\n"
        f"<b>Posiciones:</b> {len(positions)} → {', '.join(pos_list[:5])}\n"
        f"<b>Símbolos:</b> {len(symbols)}\n"
        f"<b>EMA {EMA_FAST}/{EMA_SLOW}</b> Slope≥{SLOPE_LIMIT}° "
        f"ADX≥{ADX_MIN} Vol≥{VOL_MULT}x Score≥{MIN_SCORE}\n"
        f"<b>MAX_OPEN_TRADES:</b> {MAX_OPEN_TRADES} | <b>TF:</b> {TIMEFRAME}"
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info(f"=== EMA+ADX+ZigZag Elite V9 [{STRATEGY_MODE.upper()}] ===")

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

    errors = 0
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
                    log.info(f"  → {s['symbol']} {s['signal']} [{s['method']}] "
                             f"score={s['score']} ang={s['angle']}° adx={s['adx']} vol={s['vol_ratio']}x")

            for sig in signals:
                sym = sig["symbol"]
                if sym in positions or sym in entered:
                    continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info(f"Max trades ({MAX_OPEN_TRADES}) reached.")
                    break
                if balance < 5:
                    log.warning(f"Balance too low ({balance:.2f})")
                    break

                qty = calc_qty(balance, sig["close"], sig["sl"])
                if qty <= 0:
                    continue

                side = "BUY" if sig["signal"]=="LONG" else "SELL"
                try:
                    set_lev(sym)
                    res = open_order(sym, side, qty, sig["sl"], sig["tp"])
                    log.info(f"✅ {sym} {side} qty={qty} | {res}")
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
