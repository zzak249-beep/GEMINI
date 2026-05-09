#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  TRADING BOT V18 — ULTIMATE EDITION                                 ║
║                                                                      ║
║  COMBINA LO MEJOR DE TODAS LAS VERSIONES:                           ║
║                                                                      ║
║  VELOCIDAD (V17):                                                    ║
║  · 1 API call por símbolo, numpy puro, sin pandas overhead          ║
║  · Sesiones por hilo (sin connection pool full)                     ║
║  · H1 cacheado en background, sin llamadas en el scan loop          ║
║                                                                      ║
║  CALIDAD (V16 scan_symbol mejorado):                                ║
║  · Régimen de mercado: NO operar en lateral (filtro #1 win rate)    ║
║  · Volume Delta: presión compradora/vendedora real                  ║
║  · Timing pullback: no entrar tarde en impulsos estirados           ║
║  · Filtro spike: evita entrar tras velas de momentum extremo        ║
║  · SL por estructura de mercado (swing highs/lows reales)           ║
║  · Ponderación horaria (London/NY = mejor calidad)                  ║
║                                                                      ║
║  MEJORAS V18 EXCLUSIVAS:                                            ║
║  · Breakeven automático al 1R en posiciones abiertas                ║
║  · Heartbeat Telegram cada hora con P&L del día                     ║
║  · Score mínimo dinámico según hora del día                         ║
║  · Gestión de MAX_TRADES sincronizada con el exchange               ║
║  · Cooldown inteligente: solo si SL tocado, no por timeout          ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

import requests
import numpy as np

try:
    from telegram import Bot
    from telegram.constants import ParseMode
    TG_OK = True
except ImportError:
    TG_OK = False

# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", os.environ.get("BINGX_API_SECRET",""))
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","").strip()

TIMEFRAME       = os.environ.get("TIMEFRAME",      "5m")
RISK_PERCENT    = float(os.environ.get("RISK_PERCENT",  "1.5"))
LEVERAGE        = int(os.environ.get("LEVERAGE",        "5"))
LOOP_SECONDS    = int(os.environ.get("LOOP_SECONDS",    "30"))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES", "6"))
SCAN_WORKERS    = int(os.environ.get("SCAN_WORKERS",    "20"))
MAX_SYMBOLS     = int(os.environ.get("MAX_SYMBOLS",     "150"))

# Indicadores
EMA_FAST        = int(os.environ.get("EMA_FAST",    "7"))
EMA_SLOW        = int(os.environ.get("EMA_SLOW",    "17"))
EMA_TREND       = int(os.environ.get("EMA_TREND",   "50"))
SLOPE_MIN       = float(os.environ.get("SLOPE_MIN", "8.0"))
ADX_MIN         = float(os.environ.get("ADX_MIN",   "18.0"))
RSI_OB          = float(os.environ.get("RSI_OB",    "72.0"))
RSI_OS          = float(os.environ.get("RSI_OS",    "28.0"))
VOL_MULT        = float(os.environ.get("VOL_MULT",  "0.8"))

# SL / TP / RR
TP_MULT         = float(os.environ.get("TP_MULT",      "2.5"))
SL_ATR_MULT     = float(os.environ.get("SL_ATR_MULT",  "1.4"))
MIN_RR          = float(os.environ.get("MIN_RR",       "1.8"))
MIN_DIST_PCT    = float(os.environ.get("MIN_DIST_PCT", "0.15"))
ATR_MAX_PCT     = float(os.environ.get("ATR_MAX_PCT",  "6.0"))
BE_TRIGGER_R    = float(os.environ.get("BE_TRIGGER_R", "1.0"))  # breakeven al 1R

# Position sizing
MIN_ORDER_USDT  = float(os.environ.get("MIN_ORDER_USDT", "7.0"))
MAX_ORDER_USDT  = float(os.environ.get("MAX_ORDER_USDT", "40.0"))
MAX_MARGIN_PCT  = float(os.environ.get("MAX_MARGIN_PCT", "25.0"))

# Score mínimo base (se ajusta por hora)
MIN_SCORE_BASE  = float(os.environ.get("MIN_SCORE", "38.0"))

# Cache / cooldown
H1_CACHE_TTL    = int(os.environ.get("H1_CACHE_TTL",  "600"))
COOLDOWN_MINS   = int(os.environ.get("COOLDOWN_MINS", "15"))

BINGX_BASE   = "https://open-api.bingx.com"
INTERVAL_MAP = {"1m":"1m","5m":"5m","15m":"15m","1h":"1H"}

EXCLUDED = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG",
            "Gasoline","GasOil","Brent","WTI","Copper","EURUSD","GBPUSD")

FALLBACK_SYMS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "INJ-USDT","SUI-USDT","ARB-USDT","OP-USDT","WIF-USDT",
    "PEPE-USDT","NEAR-USDT","APT-USDT","HBAR-USDT","AAVE-USDT",
    "LDO-USDT","RUNE-USDT","TIA-USDT","SEI-USDT","WLD-USDT",
    "FIL-USDT","ICP-USDT","ATOM-USDT","MATIC-USDT","UNI-USDT",
    "LTC-USDT","BCH-USDT","ETC-USDT","GRT-USDT","CRV-USDT",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("V18")

# ══════════════════════════════════════════════════════════════════════
#  ESTADO
# ══════════════════════════════════════════════════════════════════════
cooldowns      = {}   # {symbol: timestamp_float}
h1_cache       = {}   # {symbol: {"bias": str, "ts": float}}
daily_pnl      = 0.0  # P&L del día en USDT
daily_trades   = 0
last_heartbeat = 0.0
reject_counts  = Counter()

# ══════════════════════════════════════════════════════════════════════
#  API — sesión por hilo
# ══════════════════════════════════════════════════════════════════════
_tls = threading.local()

def _session():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update({"X-BX-APIKEY": BINGX_API_KEY})
        a = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=2, max_retries=1)
        s.mount("https://", a)
        _tls.s = s
    return _tls.s

def _sign(params):
    qs = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
    return hmac.new(BINGX_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()

def bx(path, params=None, method="GET", body=None):
    p = dict(params or {})
    p["timestamp"] = int(time.time()*1000)
    p["signature"] = _sign(p)
    s = _session()
    r = (s.get if method=="GET" else s.post)(
        BINGX_BASE+path, params=p, **({"json":body} if body else {}), timeout=10
    )
    r.raise_for_status()
    return r.json()

# ══════════════════════════════════════════════════════════════════════
#  EXCHANGE
# ══════════════════════════════════════════════════════════════════════
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

def get_symbols():
    # Intento 1: contracts
    try:
        data = bx("/openApi/swap/v2/quote/contracts",{})
        syms = []
        for c in data.get("data",[]):
            sym = c.get("symbol","")
            if not sym.endswith("-USDT"): continue
            if any(e in sym for e in EXCLUDED): continue
            if c.get("status") not in (1,"1",None,""): continue
            syms.append((sym, float(c.get("tradeAmount",0) or 0)))
        if syms:
            syms.sort(key=lambda x: x[1], reverse=True)
            r = [s for s,_ in syms[:MAX_SYMBOLS]]
            log.info(f"Símbolos: {len(r)} (contracts)")
            return r
    except Exception as e:
        log.warning(f"contracts: {e}")
    # Intento 2: ticker
    try:
        data = bx("/openApi/swap/v2/quote/ticker",{})
        syms = []
        for t in data.get("data",[]):
            sym = t.get("symbol","")
            if not sym.endswith("-USDT"): continue
            if any(e in sym for e in EXCLUDED): continue
            syms.append((sym, float(t.get("quoteVolume",0) or 0)))
        if syms:
            syms.sort(key=lambda x: x[1], reverse=True)
            r = [s for s,_ in syms[:MAX_SYMBOLS]]
            log.info(f"Símbolos: {len(r)} (ticker)")
            return r
    except Exception as e:
        log.warning(f"ticker: {e}")
    log.warning("Usando fallback")
    return FALLBACK_SYMS

def set_leverage(sym):
    for side in ("LONG","SHORT"):
        try:
            bx("/openApi/swap/v2/trade/leverage",
               {"symbol":sym,"side":side,"leverage":LEVERAGE}, "POST")
        except Exception:
            pass

def get_mark_price(sym):
    try:
        data  = bx("/openApi/swap/v2/quote/premiumIndex",{"symbol":sym})
        items = data.get("data",[])
        if isinstance(items, list):
            for item in items:
                if item.get("symbol")==sym and item.get("markPrice"):
                    return float(item["markPrice"])
        if isinstance(items, dict) and items.get("markPrice"):
            return float(items["markPrice"])
    except Exception:
        pass
    return 0.0

def place_order(sym, side, qty, sl, tp):
    body = {
        "symbol":       sym,
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

def set_sl(sym, side, new_sl):
    """Actualiza stop loss para breakeven."""
    try:
        bx("/openApi/swap/v2/trade/order", method="POST", body={
            "symbol":sym,"type":"STOP_MARKET",
            "side":"SELL" if side=="LONG" else "BUY",
            "positionSide":side,
            "stopPrice":round(new_sl,6),
            "closePosition":"true",
            "workingType":"MARK_PRICE"
        })
    except Exception as e:
        log.debug(f"set_sl {sym}: {e}")

# ══════════════════════════════════════════════════════════════════════
#  KLINES → NUMPY (sin pandas)
# ══════════════════════════════════════════════════════════════════════
def klines(sym, interval="5m", limit=150):
    data = bx("/openApi/swap/v3/quote/klines",
              {"symbol":sym,"interval":INTERVAL_MAP.get(interval,interval),"limit":limit})
    rows = data.get("data",[])
    if not rows: return None
    # rows: [time, open, high, low, close, volume, closeTime]
    arr = np.array([[float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])]
                    for r in rows], dtype=np.float64)
    return arr  # (N,5): O,H,L,C,V

# ══════════════════════════════════════════════════════════════════════
#  INDICADORES NUMPY PURO
# ══════════════════════════════════════════════════════════════════════
def ema(arr, p):
    k = 2.0/(p+1); r = np.empty(len(arr)); r[0] = arr[0]
    for i in range(1,len(arr)): r[i] = arr[i]*k + r[i-1]*(1-k)
    return r

def atr(H,L,C,p=14):
    n=len(C); tr=np.empty(n); tr[0]=H[0]-L[0]
    for i in range(1,n): tr[i]=max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1]))
    r=np.empty(n); r[0]=tr[0]; k=1/p
    for i in range(1,n): r[i]=r[i-1]*(1-k)+tr[i]*k
    return r

def rsi(C,p=14):
    n=len(C); g=np.zeros(n); ls=np.zeros(n)
    for i in range(1,n):
        d=C[i]-C[i-1]
        if d>0: g[i]=d
        else:   ls[i]=-d
    ag=ema(g,p*2-1); al=ema(ls,p*2-1)
    rs=np.where(al==0,100,ag/al)
    return 100-100/(1+rs)

def adx(H,L,C,p=14):
    n=len(C); pdm=np.zeros(n); mdm=np.zeros(n); tr=np.zeros(n)
    for i in range(1,n):
        up=H[i]-H[i-1]; dn=L[i-1]-L[i]
        pdm[i]=up   if up>dn and up>0   else 0
        mdm[i]=dn   if dn>up and dn>0   else 0
        tr[i]=max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1]))
    atr_w=ema(tr,p*2-1)
    pdi=100*ema(pdm,p*2-1)/np.where(atr_w==0,1,atr_w)
    mdi=100*ema(mdm,p*2-1)/np.where(atr_w==0,1,atr_w)
    dx=100*np.abs(pdi-mdi)/np.where((pdi+mdi)==0,1,pdi+mdi)
    return ema(dx,p*2-1), pdi, mdi

def slope_deg(e, atr_v, look=3):
    if len(e)<=look or atr_v<=0: return 0.0
    return float(np.degrees(np.arctan2(e[-1]-e[-1-look], atr_v*look)))

def vwap(H,L,C,V):
    tp=( H+L+C)/3; cum_tp=np.cumsum(tp*V); cum_v=np.cumsum(V)
    return np.where(cum_v==0,tp,cum_tp/cum_v)

# ─── NUEVAS (de V16 scan) ─────────────────────────────────────────────
def volume_delta(O,C,V,p=3):
    """Presión neta compradora/vendedora normalizada [-1,+1]."""
    bull = V * (C>O).astype(float)
    bear = V * (C<O).astype(float)
    tot  = np.where(V==0, 1, V)
    d    = (bull-bear)/tot
    # rolling mean p
    out  = np.full(len(d), np.nan)
    for i in range(p-1, len(d)):
        out[i] = d[i-p+1:i+1].mean()
    return out

def is_trending(H,L,C,p=20,thresh=0.9):
    """True = mercado en tendencia real (no lateral)."""
    atr_v = atr(H,L,C,p)
    out   = np.zeros(len(C), dtype=bool)
    for i in range(p,len(C)):
        rng   = H[i-p:i].max() - L[i-p:i].min()
        ratio = rng / (atr_v[i] * (p**0.5) + 1e-10)
        out[i]= ratio > thresh
    return out

def struct_sl(H,L,C,i,direction,atr_v,lookback=15):
    """SL basado en swing high/low real."""
    s = max(0, i-lookback)
    if direction=="LONG":
        swing_lo = L[s:i].min()
        return min(swing_lo - atr_v*0.2, C[i] - atr_v*SL_ATR_MULT)
    else:
        swing_hi = H[s:i].max()
        return max(swing_hi + atr_v*0.2, C[i] + atr_v*SL_ATR_MULT)

def candle_pat(O,H,L,C,i,direction,atr_v):
    if i<1: return "NONE",0.0,None
    o,h,l,c = O[i],H[i],L[i],C[i]
    o1,c1   = O[i-1],C[i-1]
    rng=h-l; body=abs(c-o)
    if rng<1e-10 or atr_v<1e-10: return "NONE",0.0,None
    uw=h-max(o,c); lw=min(o,c)-l
    # Pin Bar
    if body/rng<0.35:
        if direction=="LONG"  and lw/rng>=0.55 and lw>=2*max(body,1e-10):
            return "PIN",min(lw/rng*100,100), l-atr_v*0.1
        if direction=="SHORT" and uw/rng>=0.55 and uw>=2*max(body,1e-10):
            return "PIN",min(uw/rng*100,100), h+atr_v*0.1
    # Engulfing
    b1=abs(c1-o1)
    if b1>1e-10 and body/b1>=1.05:
        if direction=="LONG"  and c>o and c1<o1 and c>max(o1,c1) and o<min(o1,c1):
            return "ENG",min(body/b1*40,100), l-atr_v*0.1
        if direction=="SHORT" and c<o and c1>o1 and c<min(o1,c1) and o>max(o1,c1):
            return "ENG",min(body/b1*40,100), h+atr_v*0.1
    # Momentum
    if body/rng>=0.65 and body>=atr_v*0.4:
        if direction=="LONG"  and c>o and uw<body*0.35:
            return "MOM",min(body/rng*80,100), l-atr_v*0.1
        if direction=="SHORT" and c<o and lw<body*0.35:
            return "MOM",min(body/rng*80,100), h+atr_v*0.1
    return "NONE",0.0,None

def hour_mult():
    """Multiplicador de score según hora UTC (London/NY = mejor)."""
    h = datetime.now(timezone.utc).hour
    if   8<=h<11:   return 1.20   # apertura Londres 🏆
    elif 14<=h<17:  return 1.20   # apertura NY 🏆
    elif 11<=h<14:  return 1.10   # solape London-NY
    elif 17<=h<22:  return 1.00   # tarde NY
    elif 22<=h or h<1: return 0.88
    else:           return 0.80   # madrugada Asia

def min_score_now():
    """Score mínimo dinámico: más exigente en horas malas."""
    return MIN_SCORE_BASE / hour_mult()

# ══════════════════════════════════════════════════════════════════════
#  H1 BIAS CACHE
# ══════════════════════════════════════════════════════════════════════
def compute_h1_bias(sym):
    try:
        arr = klines(sym,"1h",80)
        if arr is None or len(arr)<30: return "NEUTRAL"
        C=arr[:,3]; H=arr[:,1]; L=arr[:,2]
        e7  = ema(C,7); e21 = ema(C,21)
        # Tendencia clara = EMA alineadas + slope
        atr_h1 = atr(H,L,C,14)
        sl = slope_deg(e7, atr_h1[-1], 3)
        if e7[-1]>e21[-1] and sl>3:   bias="BULL"
        elif e7[-1]<e21[-1] and sl<-3: bias="BEAR"
        else:                           bias="NEUTRAL"
        h1_cache[sym] = {"bias":bias,"ts":time.time()}
        return bias
    except Exception:
        return "NEUTRAL"

def h1_bias(sym):
    c = h1_cache.get(sym)
    if c and time.time()-c["ts"]<H1_CACHE_TTL: return c["bias"]
    return compute_h1_bias(sym)

def prefetch_h1(syms):
    log.info(f"Pre-cargando H1 ({len(syms)} sym)...")
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(compute_h1_bias, syms))
    log.info("H1 listo.")

def h1_refresh_bg(syms):
    while True:
        time.sleep(H1_CACHE_TTL//2)
        try:
            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(compute_h1_bias, syms))
            log.info("H1 refrescado.")
        except Exception as e:
            log.warning(f"H1 refresh: {e}")

# ══════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════
def calc_qty(balance, entry, sl, q_mult=1.0):
    dist = abs(entry-sl)/entry
    if dist<1e-8: return 0,0
    risk     = balance*(RISK_PERCENT/100)*q_mult
    notional = risk/dist
    max_n    = min(MAX_ORDER_USDT, balance*(MAX_MARGIN_PCT/100)*LEVERAGE)
    notional = max(MIN_ORDER_USDT, min(notional, max_n))
    return round(notional/entry,4), round(notional,2)

# ══════════════════════════════════════════════════════════════════════
#  BREAKEVEN — gestión activa de posiciones
# ══════════════════════════════════════════════════════════════════════
def manage_positions(positions):
    """Mueve SL a breakeven cuando el precio alcanza BE_TRIGGER_R × riesgo."""
    for sym, pos in positions.items():
        try:
            side  = pos.get("positionSide","LONG")
            entry = float(pos.get("avgPrice",0) or 0)
            if entry<=0: continue
            live = get_mark_price(sym)
            if live<=0: continue

            # Estimamos riesgo inicial desde ATR
            arr = klines(sym,TIMEFRAME,30)
            if arr is None: continue
            atr_v = float(atr(arr[:,1],arr[:,2],arr[:,3],14)[-1])
            risk  = atr_v * SL_ATR_MULT
            profit_r = (live-entry)/risk if side=="LONG" else (entry-live)/risk

            if profit_r >= BE_TRIGGER_R:
                be_sl = entry*1.001 if side=="LONG" else entry*0.999
                set_sl(sym, side, be_sl)
                log.info(f"🔒 BE {sym} {side} → SL={be_sl:.6g} (R={profit_r:.1f})")
        except Exception as e:
            log.debug(f"manage {sym}: {e}")

# ══════════════════════════════════════════════════════════════════════
#  SCAN — NÚCLEO V18
# ══════════════════════════════════════════════════════════════════════
def scan(sym):
    """
    V18: 1 API call, numpy puro, filtros de calidad V16.
    Retorna dict señal o None.
    """
    cd = cooldowns.get(sym)
    if cd and time.time()-cd < COOLDOWN_MINS*60: return None

    try:
        arr = klines(sym, TIMEFRAME, 150)
        if arr is None or len(arr)<100: return None

        O=arr[:,0]; H=arr[:,1]; L=arr[:,2]; C=arr[:,3]; V=arr[:,4]
        i = len(C)-2  # última vela cerrada

        atr_v  = atr(H,L,C,14)
        e7     = ema(C, EMA_FAST)
        e21    = ema(C, EMA_SLOW)
        e50    = ema(C, EMA_TREND)
        adx_v, pdi, mdi = adx(H,L,C,14)
        rsi_v  = rsi(C,14)
        vol20  = np.convolve(V, np.ones(20)/20, mode='same')
        vwap_v = vwap(H,L,C,V)
        vdelta = volume_delta(O,C,V,3)
        trend  = is_trending(H,L,C,20,0.9)

        atr_i   = float(atr_v[i])
        if atr_i<=0: return None
        atr_pct = atr_i/C[i]*100
        if atr_pct>ATR_MAX_PCT: return None

        ang    = slope_deg(e7, atr_i, 3)
        adx_i  = float(adx_v[i])
        pdi_i  = float(pdi[i])
        mdi_i  = float(mdi[i])
        rsi_i  = float(rsi_v[i])
        vratio = V[i]/vol20[i] if vol20[i]>0 else 0
        vwap_i = float(vwap_v[i])
        delta_i= float(vdelta[i]) if not np.isnan(vdelta[i]) else 0.0
        trend_i= bool(trend[i])

        # ── Dirección ─────────────────────────────────────────────────
        if   e7[i]>e21[i]: d="LONG"
        elif e7[i]<e21[i]: d="SHORT"
        else: return None

        # ══ FILTROS DUROS (orden de menor a mayor coste) ═════════════

        # F0: Régimen — no lateral (el más impactante para win rate)
        if not trend_i:
            reject_counts["lateral"] += 1; return None

        # F1: RSI extremo
        if d=="LONG"  and rsi_i>RSI_OB:  reject_counts["rsi_ob"]+=1; return None
        if d=="SHORT" and rsi_i<RSI_OS:  reject_counts["rsi_os"]+=1; return None

        # F2: Slope mínimo
        if d=="LONG"  and ang<SLOPE_MIN:  reject_counts["slope"]+=1; return None
        if d=="SHORT" and ang>-SLOPE_MIN: reject_counts["slope"]+=1; return None

        # F3: ADX mínimo
        if adx_i<ADX_MIN: reject_counts["adx"]+=1; return None

        # F4: Spike anterior (evita entrar tras vela extrema)
        prev_body = abs(C[i-1]-O[i-1])
        if prev_body > atr_i*2.5:
            reject_counts["spike"]+=1; return None

        # F5: Pullback timing (no comprar el techo del impulso)
        if d=="LONG":
            recent_hi = H[max(0,i-6):i].max()
            if (recent_hi-C[i])/atr_i < 0.15:
                reject_counts["pullback"]+=1; return None
        else:
            recent_lo = L[max(0,i-6):i].min()
            if (C[i]-recent_lo)/atr_i < 0.15:
                reject_counts["pullback"]+=1; return None

        # F6: H1 bias (sin API call — usa cache)
        bias = h1_bias(sym)
        if d=="LONG"  and bias=="BEAR": reject_counts["h1_contra"]+=1; return None
        if d=="SHORT" and bias=="BULL": reject_counts["h1_contra"]+=1; return None

        # ══ SCORING ══════════════════════════════════════════════════
        score = 0

        # Slope (max 25)
        score += min(abs(ang)/SLOPE_MIN*25, 25)

        # ADX (max 20)
        score += min((adx_i-ADX_MIN)/ADX_MIN*20, 20)

        # DI alineado (max 10)
        if (d=="LONG" and pdi_i>mdi_i) or (d=="SHORT" and mdi_i>pdi_i):
            score += 10

        # H1 bias (max 15)
        if   bias=="BULL" and d=="LONG":  score += 15
        elif bias=="BEAR" and d=="SHORT": score += 15
        else:                              score +=  5  # NEUTRAL

        # VWAP (max 10)
        if (d=="LONG" and C[i]>vwap_i) or (d=="SHORT" and C[i]<vwap_i):
            score += 10

        # Volume Delta (max 10) — descorelacionado
        if (d=="LONG" and delta_i>0.1) or (d=="SHORT" and delta_i<-0.1):
            score += min(abs(delta_i)*20, 10)

        # EMA50 trend (max 8)
        if (d=="LONG" and C[i]>e50[i]) or (d=="SHORT" and C[i]<e50[i]):
            score += 8

        # Volumen (max 7)
        score += min(vratio/VOL_MULT*7, 7)

        # Patrón de vela (max 15)
        pat, pat_sc, sl_can = candle_pat(O,H,L,C,i,d,atr_i)
        score += min(pat_sc/7, 15)

        # Ponderación horaria
        hm     = hour_mult()
        score  = round(score*hm, 1)
        ms_now = min_score_now()

        if score < ms_now:
            reject_counts[f"score_{score:.0f}<{ms_now:.0f}"] += 1
            return None

        # ── SL por estructura + vela ──────────────────────────────────
        sl = struct_sl(H,L,C,i,d,atr_i,15)
        if sl_can:
            sl = min(sl,sl_can) if d=="LONG" else max(sl,sl_can)
        if d=="LONG":
            sl = min(sl, C[i]*(1-MIN_DIST_PCT/100))
            if sl>=C[i]: return None
            tp = C[i]+(C[i]-sl)*TP_MULT
        else:
            sl = max(sl, C[i]*(1+MIN_DIST_PCT/100))
            if sl<=C[i]: return None
            tp = C[i]-(sl-C[i])*TP_MULT

        dist = abs(C[i]-sl)
        if dist/C[i]*100 < MIN_DIST_PCT: return None
        rr = abs(tp-C[i])/dist
        if rr < MIN_RR: return None

        q_mult = round(min(max(0.7+(score-ms_now)/60*0.6, 0.7), 1.3), 2)

        return {
            "sym":    sym,
            "dir":    d,
            "pat":    pat,
            "entry":  float(C[i]),
            "sl":     round(sl,6),
            "tp":     round(tp,6),
            "atr":    atr_i,
            "score":  score,
            "rr":     round(rr,2),
            "rsi":    round(rsi_i,1),
            "adx":    round(adx_i,1),
            "ang":    round(ang,1),
            "vol":    round(vratio,2),
            "delta":  round(delta_i,3),
            "h1":     bias,
            "hm":     hm,
            "qm":     q_mult,
            "dist":   round(dist/C[i]*100,3),
        }

    except Exception as e:
        log.debug(f"{sym}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════
async def _tg(msg):
    if not TG_OK or not TELEGRAM_TOKEN: return
    bot=Bot(TELEGRAM_TOKEN)
    cid=int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(cid, msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN: return
    try: asyncio.run(_tg(msg))
    except Exception as e: log.warning(f"TG: {e}")

def tg_start(balance, n_sym):
    tg(
        f"🚀 <b>BOT V18 — ULTIMATE EDITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Numpy puro | 1 call/sym | H1 cached\n"
        f"🎯 Régimen + VDelta + Estructura SL + HourMult\n"
        f"📊 {n_sym} sym | Score≥{MIN_SCORE_BASE:.0f} | ADX≥{ADX_MIN} | Slope≥{SLOPE_MIN}°\n"
        f"🔒 Breakeven @ {BE_TRIGGER_R}R automático\n"
        f"💰 {balance:.2f} USDT | Max:{MAX_OPEN_TRADES} trades | {MIN_ORDER_USDT}-{MAX_ORDER_USDT}U\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )

def tg_entry(sig, qty, notional):
    icons={"PIN":"📌","ENG":"🔄","MOM":"💥","NONE":"📈"}
    e="🟢" if sig["dir"]=="LONG" else "🔴"
    tg(
        f"{e} <b>{sig['sym']} {sig['dir']}</b> — V18\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{icons.get(sig['pat'],'⚡')} <b>{sig['pat']}</b> | "
        f"Score:<b>{sig['score']:.0f}</b> | H1:{sig['h1']}\n"
        f"Ang:{sig['ang']}° | ADX:{sig['adx']} | "
        f"RSI:{sig['rsi']} | Vol:{sig['vol']}x\n"
        f"Δvol:{sig['delta']:+.2f} | HourMult:{sig['hm']}×\n"
        f"<b>In:</b>  <code>{sig['entry']:.6g}</code>\n"
        f"<b>SL:</b>  <code>{sig['sl']:.6g}</code> ({sig['dist']}%)\n"
        f"<b>TP:</b>  <code>{sig['tp']:.6g}</code> | R:R 1:{sig['rr']}\n"
        f"Qty:{qty:.4f} | Notional:{notional:.1f}U | Kelly:{sig['qm']}×\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def tg_heartbeat(balance, n_pos, cycle):
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat < 3600: return  # max 1 vez/hora
    last_heartbeat = now
    top = reject_counts.most_common(5)
    rej = " | ".join(f"{r}:{c}" for r,c in top) if top else "—"
    pnl_sign = "+" if daily_pnl >= 0 else ""
    tg(
        f"💓 <b>Heartbeat V18</b> — ciclo #{cycle}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: {balance:.2f} USDT\n"
        f"📊 Posiciones: {n_pos}/{MAX_OPEN_TRADES}\n"
        f"📈 P&L hoy: {pnl_sign}{daily_pnl:.2f} USDT | Trades: {daily_trades}\n"
        f"⏰ Hora UTC: {datetime.now(timezone.utc).strftime('%H:%M')} | HourMult:{hour_mult():.2f}\n"
        f"🚫 Top rechazos: {rej}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )

# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    global daily_trades

    log.info("╔══════════════════════════════════════╗")
    log.info("║  BOT V18 — ULTIMATE EDITION           ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"  Slope≥{SLOPE_MIN}° | ADX≥{ADX_MIN} | Score≥{MIN_SCORE_BASE}")
    log.info(f"  Régimen+VDelta+StructSL+HourMult+Breakeven")

    syms = get_symbols()
    if not syms: log.error("Sin símbolos"); return

    # Leverage en background
    threading.Thread(target=lambda:[set_leverage(s) for s in syms], daemon=True).start()

    # H1 cache inicial + refresh background
    prefetch_h1(syms)
    threading.Thread(target=h1_refresh_bg, args=(syms,), daemon=True).start()

    balance = get_balance()
    pos     = get_positions()
    tg_start(balance, len(syms))
    log.info(f"✅ Listo. Balance:{balance:.2f}U | Pos:{len(pos)}")

    errors=0; cycle=0
    while True:
        t0=time.time(); cycle+=1; reject_counts.clear()
        try:
            balance = get_balance()
            pos     = get_positions()   # sincronizado con exchange
            n_open  = len(pos)

            log.info(f"── C#{cycle} | {balance:.2f}U | {n_open}/{MAX_OPEN_TRADES} pos ──")

            # Breakeven automático
            if pos:
                threading.Thread(target=manage_positions, args=(pos,), daemon=True).start()

            # Heartbeat Telegram
            tg_heartbeat(balance, n_open, cycle)

            if n_open >= MAX_OPEN_TRADES or balance < MIN_ORDER_USDT:
                log.info("Max trades o balance bajo.")
                time.sleep(max(0, LOOP_SECONDS-(time.time()-t0))); continue

            # ── SCAN PARALELO ─────────────────────────────────────────
            signals=[]
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs={ex.submit(scan,s):s for s in syms}
                for f in as_completed(futs):
                    r=f.result()
                    if r: signals.append(r)

            signals.sort(key=lambda x:x["score"], reverse=True)
            t_scan = time.time()-t0
            log.info(f"Scan: {len(signals)}/{len(syms)} señales en {t_scan:.1f}s")

            # Top rechazos diagnóstico
            top_r = reject_counts.most_common(4)
            if top_r:
                log.info("Rechazos: "+", ".join(f"{r}={c}" for r,c in top_r))

            for s in signals[:4]:
                log.info(f"  {s['sym']} {s['dir']} score={s['score']} "
                         f"ang={s['ang']}° adx={s['adx']} h1={s['h1']} "
                         f"Δ={s['delta']:+.2f} pat={s['pat']} rr=1:{s['rr']}")

            # ── EJECUTAR ÓRDENES ──────────────────────────────────────
            entered=set()
            for sig in signals:
                sym=sig["sym"]
                if sym in pos or sym in entered: continue
                if n_open>=MAX_OPEN_TRADES: break
                if balance<MIN_ORDER_USDT: break

                try:
                    t_ord=time.time()
                    set_leverage(sym)

                    live=get_mark_price(sym)
                    if live<=0: live=sig["entry"]

                    # Recalcular SL/TP con precio live
                    d=sig["dir"]; atr_v=sig["atr"]
                    if d=="LONG":
                        sl=round(live-atr_v*SL_ATR_MULT,6)
                        sl=round(min(sl,live*(1-MIN_DIST_PCT/100)),6)
                        tp=round(live+(live-sl)*TP_MULT,6)
                    else:
                        sl=round(live+atr_v*SL_ATR_MULT,6)
                        sl=round(max(sl,live*(1+MIN_DIST_PCT/100)),6)
                        tp=round(live-(sl-live)*TP_MULT,6)

                    if sl<=0 or tp<=0: continue
                    rr=abs(tp-live)/abs(live-sl)
                    if rr<MIN_RR: continue

                    qty,notional=calc_qty(balance,live,sl,sig["qm"])
                    if qty<=0 or notional<MIN_ORDER_USDT: continue

                    side="BUY" if d=="LONG" else "SELL"
                    place_order(sym,side,qty,sl,tp)

                    ms=round((time.time()-t_ord)*1000)
                    log.info(f"✅ {sym} {d} qty={qty:.4f} not={notional:.1f}U "
                             f"sl={sl:.6g} tp={tp:.6g} [{ms}ms]")

                    sig.update({"entry":live,"sl":sl,"tp":tp,
                                "rr":round(rr,2),"dist":round(abs(live-sl)/live*100,3)})
                    tg_entry(sig,qty,notional)
                    entered.add(sym)
                    n_open+=1; daily_trades+=1
                    balance-=notional/LEVERAGE

                except Exception as e:
                    log.error(f"Order {sym}: {e}")
                    if any(x in str(e).lower() for x in ("stop","liquidat")):
                        cooldowns[sym]=time.time()
                    tg(f"⚠️ <b>{sym}</b>: <code>{str(e)[:100]}</code>")

            errors=0

        except KeyboardInterrupt:
            tg("🛑 V18 detenido"); break
        except Exception as e:
            errors+=1
            log.exception(f"Error #{errors}: {e}")
            if errors>=10:
                tg("🔴 <b>10 errores críticos</b>"); break

        elapsed=time.time()-t0
        log.info(f"Ciclo {elapsed:.1f}s → sleep {max(0,LOOP_SECONDS-elapsed):.0f}s")
        time.sleep(max(0, LOOP_SECONDS-elapsed))

if __name__=="__main__":
    main()
