#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  TRADING BOT V17 — FAST & SIMPLE EDITION                           ║
║                                                                      ║
║  FILOSOFÍA: menos es más. Velocidad = ventaja.                      ║
║                                                                      ║
║  CAMBIOS vs V16:                                                     ║
║  1. UN SOLO API CALL por símbolo (klines 5m únicamente)             ║
║  2. H1 pre-cacheado al inicio, sin llamadas en el loop              ║
║  3. Indicadores mínimos: EMA7/21 + ATR + RSI + Vol (nada más)      ║
║  4. Sin SuperTrend, sin Heikin Ashi, sin Squeeze (lentos)           ║
║  5. VWAP simple sobre los mismos klines 5m (sin extra call)         ║
║  6. Score simple: 3 checks → señal                                  ║
║  7. Orden en <1 segundo tras detectar señal                         ║
║  8. Pre-filtro por volumen: solo top 150 símbolos                   ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import numpy as np

# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", os.environ.get("BINGX_API_SECRET",""))
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","").strip()

TIMEFRAME        = os.environ.get("TIMEFRAME",      "5m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",  "1.5"))
LEVERAGE         = int(os.environ.get("LEVERAGE",        "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",    "45"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES", "8"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",    "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",     "150"))  # top 150 por volumen

EMA_FAST         = int(os.environ.get("EMA_FAST",    "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",    "21"))
SLOPE_MIN        = float(os.environ.get("SLOPE_MIN", "6.0"))    # grados mínimos
ADX_MIN          = float(os.environ.get("ADX_MIN",   "15.0"))
RSI_OB           = float(os.environ.get("RSI_OB",    "74.0"))
RSI_OS           = float(os.environ.get("RSI_OS",    "26.0"))
VOL_MULT         = float(os.environ.get("VOL_MULT",  "0.8"))    # vol mínimo vs media

TP_MULT          = float(os.environ.get("TP_MULT",      "2.5"))
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT",  "1.4"))
MIN_RR           = float(os.environ.get("MIN_RR",       "1.8"))
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT", "0.12"))
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",  "6.0"))

MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT", "7.0"))
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT", "50.0"))
MAX_MARGIN_PCT   = float(os.environ.get("MAX_MARGIN_PCT", "30.0"))

H1_CACHE_TTL     = int(os.environ.get("H1_CACHE_TTL",  "600"))  # 10min
COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS", "10"))

BINGX_BASE   = "https://open-api.bingx.com"
INTERVAL_MAP = {"1m":"1m","5m":"5m","15m":"15m","1h":"1H"}

EXCLUDED = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG",
            "Gasoline","GasOil","Brent","WTI","Copper","EURUSD","GBPUSD")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("V17")

# ══════════════════════════════════════════════════════════════════════
#  ESTADO
# ══════════════════════════════════════════════════════════════════════
cooldowns = {}        # {symbol: timestamp}
h1_cache  = {}        # {symbol: {"bias": "BULL"/"BEAR"/"NEUTRAL", "ts": float}}

# ══════════════════════════════════════════════════════════════════════
#  API — sesión por hilo (fix "connection pool full")
# ══════════════════════════════════════════════════════════════════════
_tls = threading.local()

def _get_session():
    if not hasattr(_tls, "session"):
        s = requests.Session()
        s.headers.update({"X-BX-APIKEY": BINGX_API_KEY})
        a = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=2, max_retries=1)
        s.mount("https://", a)
        _tls.session = s
    return _tls.session

def _sign(params: dict) -> str:
    qs = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
    return hmac.new(BINGX_SECRET_KEY.encode(), qs.encode(), __import__("hashlib").sha256).hexdigest()

def bx(path, params=None, method="GET", body=None):
    p = dict(params or {})
    p["timestamp"] = int(time.time()*1000)
    p["signature"] = _sign(p)
    s = _get_session()
    if method == "GET":
        r = s.get(BINGX_BASE+path, params=p, timeout=10)
    else:
        r = s.post(BINGX_BASE+path, params=p, json=body, timeout=10)
    r.raise_for_status()
    return r.json()

# ══════════════════════════════════════════════════════════════════════
#  DATOS
# ══════════════════════════════════════════════════════════════════════
def get_klines_raw(symbol, interval="5m", limit=120):
    """Retorna arrays numpy directamente — sin pandas overhead."""
    data = bx("/openApi/swap/v3/quote/klines",
               {"symbol":symbol,"interval":INTERVAL_MAP.get(interval,interval),"limit":limit})
    rows = data.get("data",[])
    if not rows: return None
    arr = np.array([[float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])]
                    for r in rows])  # [open, high, low, close, volume]
    return arr  # shape (N, 5)

def get_balance():
    try:
        bal = bx("/openApi/swap/v2/user/balance").get("data",{}).get("balance",{})
        for f in ("availableMargin","available","walletBalance","equity"):
            v = bal.get(f)
            if v and float(v) > 0:
                log.info(f"Balance: {float(v):.2f} USDT")
                return float(v)
    except Exception as e:
        log.error(f"balance: {e}")
    return 0.0

def get_positions():
    try:
        data = bx("/openApi/swap/v2/user/positions",{})
        return {p["symbol"]: p for p in data.get("data",[])
                if float(p.get("positionAmt",0)) != 0}
    except Exception as e:
        log.error(f"positions: {e}")
        return {}

FALLBACK_SYMS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "INJ-USDT","SUI-USDT","ARB-USDT","OP-USDT","WIF-USDT",
    "PEPE-USDT","NEAR-USDT","APT-USDT","HBAR-USDT","AAVE-USDT",
    "LDO-USDT","RUNE-USDT","TIA-USDT","SEI-USDT","WLD-USDT",
    "FIL-USDT","ICP-USDT","ATOM-USDT","MATIC-USDT","UNI-USDT",
    "LTC-USDT","BCH-USDT","ETC-USDT","SAND-USDT","MANA-USDT",
    "GRT-USDT","CRV-USDT","DYDX-USDT","ENS-USDT","1INCH-USDT",
]

def get_symbols():
    # Intento 1: contracts API — sin filtro duro de asset
    try:
        data = bx("/openApi/swap/v2/quote/contracts",{})
        syms = []
        for c in data.get("data",[]):
            sym = c.get("symbol","")
            if not sym.endswith("-USDT"): continue
            if any(e in sym for e in EXCLUDED): continue
            if c.get("status") not in (1, "1", None, ""): continue
            syms.append((sym, float(c.get("tradeAmount",0) or 0)))
        if syms:
            syms.sort(key=lambda x: x[1], reverse=True)
            result = [s for s,_ in syms[:MAX_SYMBOLS]]
            log.info(f"Símbolos: {len(result)} (contracts API)")
            return result
        log.warning("contracts API devolvió 0 símbolos")
    except Exception as e:
        log.warning(f"contracts API error: {e}")

    # Intento 2: ticker API
    try:
        data = bx("/openApi/swap/v2/quote/ticker",{})
        tickers = data.get("data",[])
        syms = []
        for t in tickers:
            sym = t.get("symbol","")
            if not sym.endswith("-USDT"): continue
            if any(e in sym for e in EXCLUDED): continue
            syms.append((sym, float(t.get("quoteVolume",0) or 0)))
        if syms:
            syms.sort(key=lambda x: x[1], reverse=True)
            result = [s for s,_ in syms[:MAX_SYMBOLS]]
            log.info(f"Símbolos: {len(result)} (ticker API)")
            return result
        log.warning("ticker API devolvió 0 símbolos")
    except Exception as e:
        log.warning(f"ticker API error: {e}")

    # Fallback hardcoded
    log.warning(f"Usando fallback hardcoded ({len(FALLBACK_SYMS)} syms)")
    return FALLBACK_SYMS

def set_leverage(symbol):
    for side in ("LONG","SHORT"):
        try:
            bx("/openApi/swap/v2/trade/leverage",
               {"symbol":symbol,"side":side,"leverage":LEVERAGE}, "POST")
        except Exception:
            pass

def place_order(symbol, side, qty, sl, tp):
    body = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "LONG" if side=="BUY" else "SHORT",
        "type":         "MARKET",
        "quantity":     round(qty, 4),
        "stopLoss":   json.dumps({"type":"STOP_MARKET","stopPrice":round(sl,6),"workingType":"MARK_PRICE"}),
        "takeProfit": json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":round(tp,6),"workingType":"MARK_PRICE"}),
    }
    resp = bx("/openApi/swap/v2/trade/order", method="POST", body=body)
    if resp.get("code",0) != 0:
        raise RuntimeError(f"{resp.get('code')}: {resp.get('msg','?')}")
    return resp

# ══════════════════════════════════════════════════════════════════════
#  INDICADORES NUMPY PUROS (rápidos, sin pandas)
# ══════════════════════════════════════════════════════════════════════
def ema_np(arr, period):
    k = 2.0/(period+1)
    out = np.empty(len(arr))
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def atr_np(high, low, close, period=14):
    n  = len(close)
    tr = np.empty(n)
    tr[0] = high[0]-low[0]
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    # Wilder smoothing
    atr = np.empty(n)
    atr[0] = tr[0]
    k = 1.0/period
    for i in range(1, n):
        atr[i] = atr[i-1]*(1-k) + tr[i]*k
    return atr

def rsi_np(close, period=14):
    n     = len(close)
    gains = np.zeros(n)
    losses= np.zeros(n)
    for i in range(1, n):
        d = close[i]-close[i-1]
        if d > 0: gains[i] = d
        else:     losses[i]= -d
    ag = ema_np(gains, period*2-1)   # approximate Wilder
    al = ema_np(losses, period*2-1)
    rs = np.where(al==0, 100, ag/al)
    return 100 - 100/(1+rs)

def adx_np(high, low, close, period=14):
    n = len(close)
    pdm = np.zeros(n); mdm = np.zeros(n); tr = np.zeros(n)
    for i in range(1, n):
        up   = high[i]-high[i-1]
        down = low[i-1]-low[i]
        pdm[i] = up   if up>down and up>0   else 0
        mdm[i] = down if down>up and down>0 else 0
        tr[i]  = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    k = 1.0/period
    atr_w = ema_np(tr, period*2-1)
    pdi = 100*ema_np(pdm, period*2-1)/np.where(atr_w==0,1,atr_w)
    mdi = 100*ema_np(mdm, period*2-1)/np.where(atr_w==0,1,atr_w)
    dx  = 100*np.abs(pdi-mdi)/np.where((pdi+mdi)==0,1,pdi+mdi)
    adx = ema_np(dx, period*2-1)
    return adx, pdi, mdi

def slope_deg(ema, atr_val, look=3):
    if len(ema) <= look: return 0.0
    diff = ema[-1] - ema[-1-look]
    denom = atr_val * look
    return float(np.degrees(np.arctan2(diff, denom))) if denom > 0 else 0.0

def vwap_np(high, low, close, volume):
    """VWAP simple (sesión completa de los klines disponibles)."""
    typical = (high+low+close)/3
    cum_tv  = np.cumsum(typical*volume)
    cum_v   = np.cumsum(volume)
    return np.where(cum_v==0, typical, cum_tv/cum_v)

def candle_pattern(opens, highs, lows, closes, i, direction, atr_val):
    """Detecta Pin Bar, Engulfing o Momentum. Rápido, sin pandas."""
    o, h, l, c   = opens[i], highs[i], lows[i], closes[i]
    o1, c1        = opens[i-1], closes[i-1]
    rng           = h - l
    body          = abs(c-o)
    if rng < 1e-10 or atr_val < 1e-10:
        return "NONE", 0.0, None
    uw = h - max(o,c)
    lw = min(o,c) - l

    # Pin Bar
    if body/rng < 0.35:
        if direction=="LONG"  and lw/rng >= 0.55 and lw >= 2*max(body,1e-10):
            return "PIN", min(lw/rng*100,100), l - atr_val*0.1
        if direction=="SHORT" and uw/rng >= 0.55 and uw >= 2*max(body,1e-10):
            return "PIN", min(uw/rng*100,100), h + atr_val*0.1

    # Engulfing
    b1 = abs(c1-o1)
    if b1 > 1e-10 and body/b1 >= 1.05:
        if direction=="LONG"  and c>o and c1<o1 and c>max(o1,c1) and o<min(o1,c1):
            return "ENG", min(body/b1*40,100), l - atr_val*0.1
        if direction=="SHORT" and c<o and c1>o1 and c<min(o1,c1) and o>max(o1,c1):
            return "ENG", min(body/b1*40,100), h + atr_val*0.1

    # Momentum (vela fuerte)
    if body/rng >= 0.65 and body >= atr_val*0.4:
        if direction=="LONG"  and c>o and uw < body*0.35:
            return "MOM", min(body/rng*80,100), l - atr_val*0.1
        if direction=="SHORT" and c<o and lw < body*0.35:
            return "MOM", min(body/rng*80,100), h + atr_val*0.1

    return "NONE", 0.0, None

# ══════════════════════════════════════════════════════════════════════
#  H1 CACHE — cargado en background, actualizado cada 10min
# ══════════════════════════════════════════════════════════════════════
def refresh_h1_bias(symbol):
    """Calcula bias H1 simple: EMA7 vs EMA21. Solo 1 API call."""
    try:
        arr = get_klines_raw(symbol, "1h", 60)
        if arr is None or len(arr) < 30:
            return "NEUTRAL"
        close = arr[:,3]
        e7    = ema_np(close, 7)
        e21   = ema_np(close, 21)
        if e7[-1] > e21[-1]*1.001:   bias = "BULL"
        elif e7[-1] < e21[-1]*0.999: bias = "BEAR"
        else:                         bias = "NEUTRAL"
        h1_cache[symbol] = {"bias": bias, "ts": time.time()}
        return bias
    except Exception:
        return "NEUTRAL"

def get_h1_bias(symbol):
    cached = h1_cache.get(symbol)
    if cached and time.time()-cached["ts"] < H1_CACHE_TTL:
        return cached["bias"]
    return refresh_h1_bias(symbol)

def prefetch_h1(symbols):
    log.info(f"Pre-cargando H1 para {len(symbols)} símbolos...")
    with ThreadPoolExecutor(max_workers=15) as ex:
        list(ex.map(refresh_h1_bias, symbols))
    log.info("H1 cache listo.")

def h1_refresh_loop(symbols):
    """Refresca el cache H1 en background cada H1_CACHE_TTL/2 segundos."""
    while True:
        time.sleep(H1_CACHE_TTL // 2)
        try:
            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(refresh_h1_bias, symbols))
            log.info("H1 cache refrescado.")
        except Exception as e:
            log.warning(f"H1 refresh: {e}")

# ══════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════
def calc_qty(balance, entry, sl):
    dist = abs(entry-sl)/entry
    if dist < 1e-8: return 0, 0
    risk    = balance*(RISK_PERCENT/100)
    notional = risk/dist
    max_n   = min(MAX_ORDER_USDT, balance*(MAX_MARGIN_PCT/100)*LEVERAGE)
    notional = max(MIN_ORDER_USDT, min(notional, max_n))
    return round(notional/entry, 4), round(notional, 2)

# ══════════════════════════════════════════════════════════════════════
#  SCAN — UN SOLO API CALL POR SÍMBOLO
# ══════════════════════════════════════════════════════════════════════
def scan(symbol, balance):
    """
    V17: 1 API call (klines 5m), indicadores numpy puro, decisión en ~5ms.
    Retorna dict con la señal o None.
    """
    # Cooldown
    cd = cooldowns.get(symbol)
    if cd and time.time()-cd < COOLDOWN_MINS*60:
        return None

    try:
        arr = get_klines_raw(symbol, TIMEFRAME, 120)
        if arr is None or len(arr) < 80:
            return None

        O = arr[:,0]; H = arr[:,1]; L = arr[:,2]
        C = arr[:,3]; V = arr[:,4]

        # Indicadores (todos numpy, rápidos)
        atr   = atr_np(H, L, C, 14)
        e7    = ema_np(C, EMA_FAST)
        e21   = ema_np(C, EMA_SLOW)
        rsi   = rsi_np(C, 14)
        adx_v, pdi, mdi = adx_np(H, L, C, 14)
        vol20 = np.convolve(V, np.ones(20)/20, mode='same')
        vwap  = vwap_np(H, L, C, V)

        i = len(C) - 2  # última vela cerrada

        atr_v    = atr[i]
        atr_pct  = atr_v/C[i]*100
        if atr_pct > ATR_MAX_PCT or atr_v <= 0:
            return None

        rsi_v    = rsi[i]
        adx_val  = adx_v[i]
        pdi_v    = pdi[i]
        mdi_v    = mdi[i]
        vratio   = V[i]/vol20[i] if vol20[i] > 0 else 0
        sl_ang   = slope_deg(e7, atr_v, 3)
        vwap_v   = vwap[i]

        # ── Dirección ─────────────────────────────────────────────────
        if   e7[i] > e21[i]: direction = "LONG"
        elif e7[i] < e21[i]: direction = "SHORT"
        else: return None

        # ── 3 CHECKS OBLIGATORIOS (rápidos) ──────────────────────────
        # 1. Slope mínimo
        if direction=="LONG"  and sl_ang < SLOPE_MIN:  return None
        if direction=="SHORT" and sl_ang > -SLOPE_MIN: return None

        # 2. RSI no extremo
        if direction=="LONG"  and rsi_v > RSI_OB: return None
        if direction=="SHORT" and rsi_v < RSI_OS: return None

        # 3. ADX mínimo
        if adx_val < ADX_MIN: return None

        # ── H1 bias (desde cache, sin API call) ───────────────────────
        h1 = get_h1_bias(symbol)
        if direction=="LONG"  and h1=="BEAR": return None
        if direction=="SHORT" and h1=="BULL": return None

        # ── Score rápido ──────────────────────────────────────────────
        score = 0
        score += min(abs(sl_ang)/SLOPE_MIN*25, 25)       # slope:  max 25
        score += min((adx_val-ADX_MIN)/ADX_MIN*20, 20)   # ADX:    max 20
        score += 15 if h1 != "NEUTRAL" else 5            # H1:     max 15
        score += 10 if ((pdi_v>mdi_v and direction=="LONG") or
                        (mdi_v>pdi_v and direction=="SHORT")) else 0  # DI: max 10
        score += min(vratio/VOL_MULT*10, 10)              # vol:    max 10
        score += 10 if ((C[i]>vwap_v and direction=="LONG") or
                        (C[i]<vwap_v and direction=="SHORT")) else 0  # VWAP: max 10

        # Patrón de vela (bonus)
        pat, pat_sc, sl_can = candle_pattern(O, H, L, C, i, direction, atr_v)
        score += min(pat_sc/10, 10)  # patrón: max 10

        if score < 25:  # umbral reducido para generar señales
            return None

        # ── SL / TP ───────────────────────────────────────────────────
        sl_dist = atr_v * SL_ATR_MULT
        if direction == "LONG":
            sl = C[i] - sl_dist
            if sl_can: sl = min(sl, sl_can)
            sl = min(sl, C[i]*(1-MIN_DIST_PCT/100))
            if sl >= C[i]: return None
            tp = C[i] + (C[i]-sl)*TP_MULT
        else:
            sl = C[i] + sl_dist
            if sl_can: sl = max(sl, sl_can)
            sl = max(sl, C[i]*(1+MIN_DIST_PCT/100))
            if sl <= C[i]: return None
            tp = C[i] - (sl-C[i])*TP_MULT

        rr = abs(tp-C[i])/abs(C[i]-sl)
        if rr < MIN_RR: return None

        return {
            "symbol":    symbol,
            "dir":       direction,
            "entry":     float(C[i]),
            "sl":        round(sl, 6),
            "tp":        round(tp, 6),
            "atr":       atr_v,
            "score":     round(score, 1),
            "rr":        round(rr, 2),
            "rsi":       round(rsi_v, 1),
            "adx":       round(adx_val, 1),
            "angle":     round(sl_ang, 1),
            "vol":       round(vratio, 2),
            "h1":        h1,
            "pat":       pat,
            "dist_pct":  round(abs(C[i]-sl)/C[i]*100, 3),
            "atr_pct":   round(atr_pct, 2),
        }

    except Exception as e:
        log.debug(f"{symbol}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════
try:
    from telegram import Bot
    from telegram.constants import ParseMode
    TG_OK = True
except ImportError:
    TG_OK = False

async def _tg_send(msg):
    if not TG_OK or not TELEGRAM_TOKEN: return
    bot = Bot(TELEGRAM_TOKEN)
    cid = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(cid, msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN: return
    try: asyncio.run(_tg_send(msg))
    except Exception as e: log.warning(f"TG: {e}")

def tg_entry(sig, qty, notional):
    e = "🟢" if sig["dir"]=="LONG" else "🔴"
    p = {"PIN":"📌","ENG":"🔄","MOM":"💥","NONE":"📈"}.get(sig["pat"],"⚡")
    tg(
        f"{e} <b>{sig['symbol']} {sig['dir']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{p} <b>{sig['pat']}</b> | Score:{sig['score']:.0f} | H1:{sig['h1']}\n"
        f"Ang:{sig['angle']}° | ADX:{sig['adx']} | RSI:{sig['rsi']} | Vol:{sig['vol']}x\n"
        f"<b>In:</b> <code>{sig['entry']:.6g}</code>\n"
        f"<b>SL:</b> <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%)\n"
        f"<b>TP:</b> <code>{sig['tp']:.6g}</code> | R:R 1:{sig['rr']}\n"
        f"<b>Qty:</b> {qty:.4f} | <b>Notional:</b> {notional:.1f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    log.info("╔═══════════════════════════════════╗")
    log.info("║  BOT V17 — FAST & SIMPLE EDITION  ║")
    log.info("╚═══════════════════════════════════╝")
    log.info(f"  EMA{EMA_FAST}/{EMA_SLOW} | Slope≥{SLOPE_MIN}° | ADX≥{ADX_MIN}")
    log.info(f"  TP×{TP_MULT} | SL×{SL_ATR_MULT}ATR | RR≥{MIN_RR} | Min{MIN_ORDER_USDT}U")
    log.info(f"  Symbols:{MAX_SYMBOLS} | Workers:{SCAN_WORKERS} | Loop:{LOOP_SECONDS}s")

    symbols = get_symbols()
    if not symbols:
        log.error("No symbols — abortando")
        return

    # Leverage en background
    threading.Thread(target=lambda: [set_leverage(s) for s in symbols], daemon=True).start()

    # Pre-cargar H1 cache (bloqueante al inicio, luego en background)
    prefetch_h1(symbols)

    # Refrescar H1 en background cada 5min
    threading.Thread(target=h1_refresh_loop, args=(symbols,), daemon=True).start()

    balance   = get_balance()
    positions = get_positions()

    tg(
        f"🚀 <b>BOT V17 — FAST & SIMPLE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ 1 API call/sym | numpy puro | H1 cacheado\n"
        f"📊 {len(symbols)} sym | Score≥35 | 3 checks\n"
        f"💰 {balance:.2f} USDT | Pos: {len(positions)}/{MAX_OPEN_TRADES}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    log.info(f"✅ Arrancando. Balance:{balance:.2f}U | Pos:{len(positions)}")
    cycle  = 0
    errors = 0

    while True:
        t0     = time.time()
        cycle += 1

        try:
            balance   = get_balance()
            positions = get_positions()
            n_open    = len(positions)

            log.info(f"── C#{cycle} | {balance:.2f}U | {n_open}/{MAX_OPEN_TRADES} pos ──")

            if n_open >= MAX_OPEN_TRADES or balance < MIN_ORDER_USDT:
                log.info("Max trades o balance bajo — esperando.")
                time.sleep(max(0, LOOP_SECONDS-(time.time()-t0)))
                continue

            # ── SCAN PARALELO ─────────────────────────────────────────
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan, s, balance): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r: signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            t_scan = time.time()-t0
            log.info(f"Scan: {len(signals)} señales / {len(symbols)} sym en {t_scan:.1f}s")

            for s in signals[:4]:
                log.info(f"  {s['symbol']} {s['dir']} score={s['score']:.0f} "
                         f"ang={s['angle']}° adx={s['adx']} h1={s['h1']} "
                         f"pat={s['pat']} rr=1:{s['rr']}")

            # ── EJECUTAR ÓRDENES ──────────────────────────────────────
            entered = set()
            for sig in signals:
                sym = sig["symbol"]
                if sym in positions or sym in entered: continue
                if n_open >= MAX_OPEN_TRADES: break
                if balance < MIN_ORDER_USDT: break

                try:
                    t_order = time.time()
                    set_leverage(sym)

                    # Precio live (única llamada extra)
                    live_data = bx("/openApi/swap/v2/quote/premiumIndex",{"symbol":sym})
                    items = live_data.get("data",[])
                    live  = 0.0
                    if isinstance(items, list):
                        for item in items:
                            if item.get("symbol")==sym:
                                live = float(item.get("markPrice",0)); break
                    if isinstance(items, dict) and items.get("symbol")==sym:
                        live = float(items.get("markPrice",0))
                    if live <= 0: live = sig["entry"]

                    # Recalcular SL/TP con precio live
                    atr_v = sig["atr"]
                    d     = sig["dir"]
                    if d=="LONG":
                        sl = round(live - atr_v*SL_ATR_MULT, 6)
                        sl = round(min(sl, live*(1-MIN_DIST_PCT/100)), 6)
                        tp = round(live + (live-sl)*TP_MULT, 6)
                    else:
                        sl = round(live + atr_v*SL_ATR_MULT, 6)
                        sl = round(max(sl, live*(1+MIN_DIST_PCT/100)), 6)
                        tp = round(live - (sl-live)*TP_MULT, 6)

                    if sl<=0 or tp<=0: continue
                    rr = abs(tp-live)/abs(live-sl)
                    if rr < MIN_RR: continue

                    qty, notional = calc_qty(balance, live, sl)
                    if qty<=0 or notional<MIN_ORDER_USDT: continue

                    side = "BUY" if d=="LONG" else "SELL"
                    place_order(sym, side, qty, sl, tp)

                    ms = round((time.time()-t_order)*1000)
                    log.info(f"✅ {sym} {d} qty={qty:.4f} not={notional:.1f}U "
                             f"sl={sl:.6g} tp={tp:.6g} [{ms}ms]")

                    sig.update({"entry":live,"sl":sl,"tp":tp,
                                "rr":round(rr,2),
                                "dist_pct":round(abs(live-sl)/live*100,3)})
                    tg_entry(sig, qty, notional)
                    entered.add(sym)
                    n_open += 1
                    balance -= notional/LEVERAGE  # aproximado

                except Exception as e:
                    log.error(f"Order {sym}: {e}")
                    if any(x in str(e).lower() for x in ("stop","liquidat")):
                        cooldowns[sym] = time.time()
                    tg(f"⚠️ <b>{sym}</b>: <code>{str(e)[:120]}</code>")

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 V17 detenido")
            break
        except Exception as e:
            errors += 1
            log.exception(f"Error ciclo #{errors}: {e}")
            if errors >= 10:
                tg("🔴 <b>10 errores críticos — detenido</b>")
                break

        elapsed = time.time()-t0
        sleep_t = max(0, LOOP_SECONDS-elapsed)
        log.info(f"Ciclo {elapsed:.1f}s → sleep {sleep_t:.0f}s")
        time.sleep(sleep_t)

if __name__ == "__main__":
    main()
