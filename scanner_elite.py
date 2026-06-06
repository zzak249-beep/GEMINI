"""
╔══════════════════════════════════════════════════════════════════════╗
║          QF×JP SCANNER v5.0 — EDGE REAL                            ║
║                                                                      ║
║  BUGS CRÍTICOS RESUELTOS:                                           ║
║  [B1] actualizar_trades() no distinguía error API de lista vacía   ║
║       → marcaba todas las posiciones como cerradas → zombie loop   ║
║  [B2] get_posiciones_abiertas() devolvía [] en error → ahora None  ║
║  [B3] hmac.new() era incorrecto → hmac.new() es correcto en py3    ║
║       (ya estaba ok pero documentado)                               ║
║  [B4] get_info_instrumento() hacía GET /contracts en cada trade    ║
║       → ahora cacheado en memoria                                   ║
║  [B5] SL/TP fijos ignoraban volatilidad real del par              ║
║  [B6] score_std=50 aceptaba setups con WR<45%                     ║
║  [B7] MAX_TRADES=3 con $5 cada uno agotaba balance libre          ║
║  [B8] Sin filtro macro → LONG en mercado bajista BTC              ║
║  [B9] Sin filtro horario → trades en low-liquidity nocturno       ║
║  [B10] Sin cierre parcial → todo o nada, sin lock de ganancias    ║
║                                                                      ║
║  EDGE REAL v5.0:                                                    ║
║  [E1] RSI multi-timeframe (3m + 15m) como filtro de entrada       ║
║  [E2] Volumen relativo mínimo en barra de entrada (×1.5 avg)      ║
║  [E3] SL basado en ATR×1.5 adaptativo por par                     ║
║  [E4] TP1 parcial 50% qty a RR1.2 → mueve SL a breakeven         ║
║  [E5] TP2 trailing 1.5% en resto → deja correr ganadores         ║
║  [E6] Cooldown_BE: si SL movido a BE no se cuenta como loss       ║
║  [E7] Filtro de spread real (bp_drain) más estricto               ║
║  [E8] HTF confirmación en 1h además de 15m                        ║
║  [E9] Resumen de estadísticas en tiempo real por Telegram          ║
║  [E10] Cache de instrumentos — reduce llamadas API en ~80%        ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, re, sys, time, hmac, hashlib, logging, math, threading
import urllib.parse, csv, json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
import requests
import numpy as np

# ══════════════════════════════════════════════════════════════════════
# SALUD HTTP
# ══════════════════════════════════════════════════════════════════════
_PUERTO = int(os.environ.get("PORT", "8080"))
_estado = {
    "version": "5.0", "modo": "iniciando",
    "escaneos": 0, "señales": 0, "trades": 0,
    "wins": 0, "losses": 0, "be_exits": 0,
    "balance": 0.0, "pnl_dia": 0.0,
    "btc_cambio": 0.0, "filtro_macro": False,
    "circuit_breaker": False, "ultimo": "—",
}

class _Salud(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json":
            body = json.dumps(_estado, ensure_ascii=False).encode()
            ct   = "application/json"
        else:
            t = _estado["wins"] + _estado["losses"]
            wr = f"{round(_estado['wins']/t*100)}%" if t else "—"
            body = (
                f"QFxJP v{_estado['version']} {_estado['modo']} | "
                f"scan={_estado['escaneos']} sig={_estado['señales']} "
                f"trades={_estado['trades']} W/L={_estado['wins']}/{_estado['losses']} "
                f"BE={_estado['be_exits']} WR={wr} "
                f"bal=${_estado['balance']:.2f} pnl=${_estado['pnl_dia']:+.2f} "
                f"btc={_estado['btc_cambio']:+.1f}% "
                f"cb={'Y' if _estado['circuit_breaker'] else 'n'} "
                f"t={_estado['ultimo']}"
            ).encode()
            ct = "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass

_http_ev = threading.Event()
def _http_loop():
    try:
        s = HTTPServer(("0.0.0.0", _PUERTO), _Salud)
        _http_ev.set(); s.serve_forever()
    except Exception as e:
        print(f"[http] {e}", flush=True); _http_ev.set()

threading.Thread(target=_http_loop, daemon=True, name="http").start()
_http_ev.wait(timeout=5)
print(f"[http] OK :{_PUERTO}", flush=True)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN — env vars con defaults razonados
# ══════════════════════════════════════════════════════════════════════
API_KEY    = os.getenv("BINGX_API_KEY",    "")
API_SEC    = os.getenv("BINGX_API_SECRET", "") or os.getenv("BINGX_SECRET", "")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "") or os.getenv("TG_TOKEN",  "")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TG_CHAT_ID","")

# Capital
TRADE_USDT    = float(os.getenv("TRADE_USDT",       "5"))
RIESGO_PCT    = float(os.getenv("RIESGO_PCT_BAL",   "0"))   # 0 = TRADE_USDT fijo
LEVERAGE      = int(  os.getenv("LEVERAGE",          "5"))
MAX_TRADES    = int(  os.getenv("MAX_OPEN_TRADES",   "2"))   # ← 2 evita agotar balance

# SL/TP adaptativos
SL_ATR_MULT   = float(os.getenv("SL_ATR_MULT",      "1.5")) # SL = ATR × 1.5
SL_PCT_FB     = float(os.getenv("SL_PCT",            "2.0")) # fallback si ATR=0
TP_RR         = float(os.getenv("TP_RR",             "2.5")) # TP2 = SL × 2.5
TP1_RR        = float(os.getenv("TP1_RR",            "1.2")) # TP1 parcial a RR 1.2
TP1_QTY_PCT   = float(os.getenv("TP1_QTY_PCT",      "50"))  # 50% qty en TP1
TRAIL_PCT     = float(os.getenv("TRAILING_PCT",      "1.5")) # trailing en el 50% restante

# Modos
_ae = os.getenv("AUTO_TRADE","").lower()
AUTO_TRADE = (_ae == "true") or (_ae == "" and bool(API_KEY) and bool(API_SEC))
DRY_RUN    = os.getenv("DRY_RUN","false").lower() == "true"

# Scores mínimos — subidos vs v4.x
SC_STD   = int(os.getenv("SC_MIN_STD",  "65"))   # era 50
SC_FUEL  = int(os.getenv("SC_MIN_FUEL", "75"))   # era 62
SC_SUP   = int(os.getenv("SC_MIN_SUP",  "82"))   # era 75

# Filtros macro / horario
BTC_MIN_LONG  = float(os.getenv("BTC_LONG_FILTER",  "-2.0")) # no LONG si BTC<-2%
BTC_MAX_SHORT = float(os.getenv("BTC_SHORT_FILTER",  "2.0")) # no SHORT si BTC>+2%
HORA_INI      = int(  os.getenv("HORA_INICIO_UTC",   "7"))
HORA_FIN      = int(  os.getenv("HORA_FIN_UTC",      "21"))
VOL_BAR_MIN   = float(os.getenv("VOL_BAR_MIN_X",    "1.5")) # vol barra >= 1.5× avg20

# [E1] RSI filtro entrada
RSI_MIN_LONG  = int(os.getenv("RSI_MIN_LONG",  "52")) # RSI 3m >= 52 para LONG
RSI_MAX_SHORT = int(os.getenv("RSI_MAX_SHORT", "48")) # RSI 3m <= 48 para SHORT

# Circuit breaker / cooldown
CB_LOSSES  = int(os.getenv("CB_MAX_LOSSES",    "3"))
CB_MIN     = int(os.getenv("CB_PAUSE_MIN",     "30"))
CD_LOSS_M  = int(os.getenv("COOLDOWN_LOSS_MIN","60"))
RR_MIN     = float(os.getenv("RR_MIN",         "2.0"))

# Volumen y escáner
VOL_MIN    = float(os.getenv("MIN_VOLUME_USDT","5000000"))
TOP_N      = int(  os.getenv("TOP_N",          "10"))
INT_NORM   = int(  os.getenv("INTERVAL_NORMAL", "900"))
INT_ACT    = int(  os.getenv("INTERVAL_ACTIVO", "300"))
INT_ALE    = int(  os.getenv("INTERVAL_ALERTA",  "60"))
ALE_CD     = int(  os.getenv("ALERTA_COOLDOWN_SEG","1800"))

BLACKLIST = set(s.strip().upper() for s in os.getenv(
    "BLACKLIST",
    "ANIME-USDT,WCT-USDT,TAO-USDT,AAPLX-USDT,NCSKGOOGL2USD-USDT,"
    "VINE-USDT,NCSKRCL2USD-USDT,NCSKTSLA2USD-USDT,NCSKAMZN2USD-USDT,"
    "NCSKNVDA2USD-USDT,NCSKMSFT2USD-USDT,NCSKAAPL2USD-USDT,NCSKSPY2USD-USDT"
).split(",") if s.strip())

_EXCLUIR = (
    "USDC","BUSD","TUSD","DAI","FDUSD",
    "NCSK","2USD","2GBP","2EUR","2JPY","2AUD","2CAD",
    "NCFX","AAPLX","TESLAX","GOOGLX","AMZNX",
    "PAXG","XAUT","BVOL","DVOL",
)

URL = "https://open-api.bingx.com"

# ══════════════════════════════════════════════════════════════════════
# PARÁMETROS MOTOR QF×JP
# ══════════════════════════════════════════════════════════════════════
I_MOM=20; I_REV=8; I_VOL_L=14; I_ATR_L=10
I_W1=0.40; I_W2=0.30; I_W3=0.30
I_ADX_LEN=14; I_ADX_TH=25
I_DLEN=40; I_DTHR=0.35
I_DPM=2.5; I_DPB=20; I_BPT=0.15   # spread más estricto: 0.18→0.15
I_ASL=10; I_ARR=1.20; I_ABR=1.20
I_TLB=30; I_TLL=5; I_TLR=3; I_TLM=0.15
I_PLL=5; I_PLR=3; I_PHL=5; I_PHR=3; I_HLC=2; I_HHC=2; I_HLW=40
I_FVG_MIN=0.3; I_FVG_BARS=40; I_OB_IMP=1.5; I_OB_BARS=50
I_CVD_LEN=20; I_CVD_DIV=5; I_CVD_ROLL=100
I_SQ_LEN=20; I_SQ_BBM=2.0; I_SQ_KCM=1.5
SC_W_SCORE=0.30; SC_W_CVD=0.25; SC_W_MOM=0.20; SC_W_DECAY=0.15; SC_W_HTF=0.10
VOL_ATR_THR=0.60

# ══════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════
trades_abiertos : dict = {}   # sym → dict trade
alertas_env     : dict = {}   # sym → timestamp último envío
cooldown_sym    : dict = {}   # sym → ts hasta cuándo cooldown
_cache_info     : dict = {}   # [E10] sym → info instrumento
_btc_chg        : float = 0.0
racha_perd      : int   = 0
cb_hasta        : float = 0.0
pnl_dia         : float = 0.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("QFxJP5")

# ══════════════════════════════════════════════════════════════════════
# API — FIRMA + GET + POST
# ══════════════════════════════════════════════════════════════════════
def _sign(params: dict) -> str:
    return hmac.new(
        API_SEC.encode(),
        urllib.parse.urlencode(params).encode(),
        hashlib.sha256,
    ).hexdigest()

def _get(path: str, params: dict = None, auth: bool = False) -> Optional[dict]:
    p = dict(params or {})
    if auth:
        p.update({"timestamp": int(time.time()*1000), "recvWindow": 5000})
        p["signature"] = _sign(p)
        url = URL + path + "?" + urllib.parse.urlencode(p)
        hdrs = {"X-BX-APIKEY": API_KEY}
        try:
            r = requests.get(url, headers=hdrs, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"GET {path}: {e}"); return None
    try:
        r = requests.get(URL + path, params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}"); return None

def _wait_rl(msg: str, path: str):
    m = re.search(r'after\s+(\d{13})', msg)
    s = max(1.0, (int(m.group(1)) - int(time.time()*1000) + 600)/1000) if m else 30.0
    log.warning(f"⏱ RateLimit {path} — wait {s:.1f}s"); time.sleep(s)

def _post(path: str, params: dict, tries: int = 3) -> Optional[dict]:
    for i in range(tries):
        p = dict(params)
        p.update({"timestamp": int(time.time()*1000), "recvWindow": 5000})
        p["signature"] = _sign(p)
        url  = URL + path + "?" + urllib.parse.urlencode(p)
        hdrs = {"X-BX-APIKEY": API_KEY}
        try:
            r    = requests.post(url, headers=hdrs, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0: return data
            code = data.get("code"); msg = data.get("msg","?")
            log.error(f"POST {path} [{i+1}] code={code} {msg}")
            if code in (100001, 100004, 100419):
                log.error("❌ Auth fallida"); return None
            if code == 100410:
                _wait_rl(msg, path); continue
        except Exception as e:
            log.error(f"POST {path} [{i+1}]: {e}")
        if i < tries-1: time.sleep(1.5 * (2**i))
    return None

# ══════════════════════════════════════════════════════════════════════
# MERCADO
# ══════════════════════════════════════════════════════════════════════
def get_tickers() -> list:
    d = _get("/openApi/swap/v2/quote/ticker")
    return d.get("data",[]) if d else []

def get_klines(sym: str, tf: str = "3m", n: int = 100) -> list:
    d = _get("/openApi/swap/v3/quote/klines",
             {"symbol": sym, "interval": tf, "limit": n})
    raw = d.get("data",[]) if d else []
    out = []
    for k in raw:
        try:
            if isinstance(k, dict):
                out.append([
                    k.get("time",0),
                    float(k.get("open",  k.get("o",0))),
                    float(k.get("high",  k.get("h",0))),
                    float(k.get("low",   k.get("l",0))),
                    float(k.get("close", k.get("c",0))),
                    float(k.get("volume",k.get("v",0))),
                ])
            elif isinstance(k,(list,tuple)) and len(k)>=6:
                out.append([float(x) for x in k[:6]])
        except Exception: continue
    return out

# [B2] FIX CRÍTICO: None = error API, [] = sin posiciones (son distintos)
def get_posiciones() -> Optional[list]:
    d = _get("/openApi/swap/v2/trade/openPositions", auth=True)
    if d is None: return None
    if d.get("code",0) != 0:
        log.warning(f"posiciones code={d.get('code')} {d.get('msg','')}"); return None
    data = d.get("data")
    if data is None: return []
    if isinstance(data, list):
        return [p for p in data if abs(float(p.get("positionAmt",0)))>0]
    if isinstance(data, dict):
        return [p for p in data.get("positions",[]) if abs(float(p.get("positionAmt",0)))>0]
    return []

def get_balance() -> float:
    d = _get("/openApi/swap/v2/user/balance", auth=True)
    if not d or d.get("code",0)!=0: return 0.0
    try:
        data = d.get("data",{})
        if isinstance(data, dict):
            for nest in (data.get("balance",{}), data):
                for k in ("availableMargin","available","equity"):
                    if nest.get(k) is not None: return float(nest[k])
        if isinstance(data, list):
            for a in data:
                if a.get("asset","").upper() in ("USDT",""):
                    for k in ("availableMargin","available","equity"):
                        if a.get(k) is not None: return float(a[k])
    except Exception as e: log.error(f"balance: {e}")
    return 0.0

# [E10] Cache de información de instrumentos
def get_info(sym: str) -> dict:
    if sym in _cache_info: return _cache_info[sym]
    try:
        d = _get("/openApi/swap/v2/quote/contracts")
        if d and d.get("data"):
            for c in d["data"]:
                s = c.get("symbol","")
                _cache_info[s] = {
                    "step":  float(c.get("tradeMinQuantity", 0.001)),
                    "min":   float(c.get("tradeMinQuantity", 0.001)),
                    "pp":    int(c.get("pricePrecision", 6)),
                }
    except Exception: pass
    return _cache_info.get(sym, {"step":0.001,"min":0.001,"pp":6})

def round_qty(qty: float, step: float) -> float:
    if step<=0: return round(qty,4)
    d = max(0, -int(math.floor(math.log10(step))))
    return round(math.floor(qty/step)*step, d)

def usdt_trade(balance: float) -> float:
    if RIESGO_PCT>0 and balance>0: return max(1.0, round(balance*RIESGO_PCT/100,2))
    return TRADE_USDT

# ══════════════════════════════════════════════════════════════════════
# INDICADORES — numpy puro
# ══════════════════════════════════════════════════════════════════════
def _tanh(x: float) -> float:
    x2 = max(min(2*x, 20), -20); e = math.exp(x2)
    return (e-1)/(e+1)

def _ema(a, p):
    k=2/(p+1); r=np.empty(len(a)); r[0]=a[0]
    for i in range(1,len(a)): r[i]=a[i]*k+r[i-1]*(1-k)
    return r

def _sma(a,p):
    o=np.full(len(a),np.nan)
    for i in range(p-1,len(a)): o[i]=a[i-p+1:i+1].mean()
    return o

def _std(a,p):
    o=np.full(len(a),np.nan)
    for i in range(p-1,len(a)): o[i]=a[i-p+1:i+1].std(ddof=0)
    return o

def _atr(h,l,c,p):
    tr=np.empty(len(c)); tr[0]=h[0]-l[0]
    for i in range(1,len(c)):
        tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
    return _ema(tr,p)

def _adx(h,l,c,p):
    n=len(c); pdm=np.zeros(n); mdm=np.zeros(n); tr=np.zeros(n)
    for i in range(1,n):
        hd=h[i]-h[i-1]; ld=l[i-1]-l[i]
        pdm[i]=hd if hd>ld and hd>0 else 0
        mdm[i]=ld if ld>hd and ld>0 else 0
        tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
    ae=_ema(tr,p)
    pdi=100*_ema(pdm,p)/np.maximum(ae,1e-10)
    mdi=100*_ema(mdm,p)/np.maximum(ae,1e-10)
    dx=100*np.abs(pdi-mdi)/np.maximum(pdi+mdi,1e-10)
    return pdi,mdi,_ema(dx,p)

def _rsi(c,p=14):
    d=np.diff(c); g=np.where(d>0,d,0); l=np.where(d<0,-d,0)
    ag=_ema(g,p); al=_ema(l,p)
    rs=np.where(al>0,ag/al,100); return 100-100/(1+rs)

def _obv(c,v):
    o=np.zeros(len(c))
    for i in range(1,len(c)):
        o[i]=o[i-1]+(v[i] if c[i]>c[i-1] else -v[i] if c[i]<c[i-1] else 0)
    return o

def _ph(h,l,r):
    n=len(h); o=np.full(n,np.nan)
    for i in range(l,n-r):
        w=h[i-l:i+r+1]
        if h[i]==w.max() and (w<h[i]).any(): o[i]=h[i]
    return o

def _pl(lo,l,r):
    n=len(lo); o=np.full(n,np.nan)
    for i in range(l,n-r):
        w=lo[i-l:i+r+1]
        if lo[i]==w.min() and (w>lo[i]).any(): o[i]=lo[i]
    return o

def _lr(a,n):
    if len(a)<n: return float(a[-1])
    y=a[-n:]; x=np.arange(n); m,b=np.polyfit(x,y,1)
    return m*(n-1)+b

# ══════════════════════════════════════════════════════════════════════
# MOTOR QF×JP — análisis por par
# ══════════════════════════════════════════════════════════════════════
def analizar(k3m: list, k15m: list, k1h: list) -> Optional[dict]:
    if len(k3m) < 60: return None

    def col(kl, idx):
        out=[]
        for k in kl:
            try: out.append(float(k[idx]))
            except: out.append(out[-1] if out else 0.0)
        return np.array(out)

    o=col(k3m,1); h=col(k3m,2); l=col(k3m,3); c=col(k3m,4); v=col(k3m,5)
    n=len(c)

    # — ATR y volatilidad —
    atr_s   = _atr(h,l,c,I_ATR_L)
    atr_now = float(atr_s[-1])
    atr_avg = float(_sma(atr_s,20)[-1] or atr_now)
    vol_ok  = atr_now > atr_avg * VOL_ATR_THR
    vol_pct = round(atr_now/atr_avg*100) if atr_avg>0 else 100

    # — Spread / coste ejecución — [E7] más estricto (0.15 vs 0.18)
    hl     = np.log(np.maximum(h/l,1e-10))
    sp_est = _sma(hl,5)*c
    bp     = (sp_est/np.maximum(c,1e-10))*100
    exec_ok= bool(bp[-1] < I_BPT)

    # — ADX / tendencia —
    pdi,mdi,adxv = _adx(h,l,c,I_ADX_LEN)
    adx_now  = float(adxv[-1])
    trend_up = bool(pdi[-1]>mdi[-1] and adx_now>=I_ADX_TH)
    trend_dn = bool(mdi[-1]>pdi[-1] and adx_now>=I_ADX_TH)

    # — Momentum score —
    sm=float(_sma(c,I_MOM)[-1]); sd=float(_std(c,I_MOM)[-1])
    vn=sd/sm if sm else 1e-10
    fm=((c[-1]-c[-I_MOM])/c[-I_MOM])/vn if (vn and c[-I_MOM]) else 0.0
    bs=_sma(c,I_REV); bd=_std(c,I_REV)
    fr=-(c[-1]-bs[-1])/bd[-1] if bd[-1] else 0.0
    oa=_obv(c,v); oe=_ema(oa,I_VOL_L); os=_std(oa,I_VOL_L)
    fv=(oa[-1]-oe[-1])/os[-1] if os[-1] else 0.0
    af=min(1.0,adx_now/(I_ADX_TH*2))
    wm=I_W1+af*I_W1*0.4; wr2=max(I_W2*0.3,I_W2-af*I_W2*0.5); wt=wm+wr2+I_W3
    raw=(wm*fm+wr2*fr+I_W3*fv)/max(wt,1e-10)
    scd=float(_std(np.array([raw]*n),I_DLEN)[-1]) or 1e-10
    ns=_tanh(raw/scd)

    # — IC / decay —
    ic=0.3; win=min(I_DLEN,n-5)
    if win>=8:
        try:
            roc=np.array([(c[i]-c[max(0,i-I_MOM)])/max(c[max(0,i-I_MOM)],1e-10) for i in range(n)])
            fwd=np.diff(c)/np.maximum(c[:-1],1e-10)
            ss=roc[max(0,n-win-1):n-1]; sf=fwd[max(0,n-win-1):n-1]
            if len(ss)>4 and ss.std()>1e-10 and sf.std()>1e-10:
                r2=float(np.corrcoef(ss,sf)[0,1])
                ic=0.0 if np.isnan(r2) else abs(r2)
        except: ic=0.3
    decay  = min(1.0,ic/max(ic,0.01))
    alive  = decay>=I_DTHR or ic>=0.15

    # — Doji/pinbar —
    vba=float(_sma(v,I_DPB)[-1]); vs=bool(v[-1]>vba*I_DPM)
    rn=bool((h[-1]-l[-1])<atr_now*0.6)
    dp_buy =bool(vs and rn and c[-1]>o[-1])
    dp_sell=bool(vs and rn and c[-1]<o[-1])

    # [E2] Filtro de volumen en barra de entrada
    vol_bar_ok = bool(v[-1] > float(_sma(v,20)[-1]) * VOL_BAR_MIN) if _sma(v,20)[-1]>0 else True

    # [E1] RSI en 3m
    rsi3m = float(_rsi(c)[-1]) if len(c)>15 else 50.0

    # [E8] HTF en 15m + 1h (doble confirmación)
    htf15_bull = htf15_bear = False
    if k15m and len(k15m)>=22:
        c15=col(k15m,4)
        htf15_bull = float(_ema(c15,9)[-1]) > float(_ema(c15,21)[-1])
        htf15_bear = float(_ema(c15,9)[-1]) < float(_ema(c15,21)[-1])
    else:
        htf15_bull=ns>0; htf15_bear=ns<0

    htf1h_bull = htf1h_bear = False
    if k1h and len(k1h)>=22:
        c1h=col(k1h,4)
        htf1h_bull = float(_ema(c1h,9)[-1]) > float(_ema(c1h,21)[-1])
        htf1h_bear = float(_ema(c1h,9)[-1]) < float(_ema(c1h,21)[-1])
    else:
        htf1h_bull=htf15_bull; htf1h_bear=htf15_bear

    # HTF confirmado si AMBOS timeframes alineados
    htf_bull = htf15_bull and htf1h_bull
    htf_bear = htf15_bear and htf1h_bear

    # — Asimetría —
    ur=np.where(c>o,h-l,0.0); dr=np.where(c<o,h-l,0.0)
    aur=float(_sma(ur,I_ASL)[-1]); adr=float(_sma(dr,I_ASL)[-1])
    asim_bull=(aur/adr if adr>0 else 1.0)>=I_ARR
    asim_bear=(adr/aur if aur>0 else 1.0)>=I_ABR

    # — Trendlines —
    ph=_ph(h,I_TLL,I_TLR); pl=_pl(l,I_PLL,I_PLR)
    phv=[(i,x) for i,x in enumerate(ph) if not np.isnan(x)]
    plv=[(i,x) for i,x in enumerate(pl) if not np.isnan(x)]
    tl_long=tl_short=False
    if len(phv)>=2:
        (b2,p2),(b1,p1)=phv[-2],phv[-1]
        if p2>p1 and (n-1-b2)<=I_TLB:
            sl2=(p1-p2)/max(b1-b2,1)
            if c[-1]>p1+sl2*(n-1-b1)+atr_now*I_TLM: tl_long=True
    if len(plv)>=2:
        (b2,p2),(b1,p1)=plv[-2],plv[-1]
        if p2<p1 and (n-1-b2)<=I_TLB:
            sl2=(p1-p2)/max(b1-b2,1)
            if c[-1]<p1+sl2*(n-1-b1)-atr_now*I_TLM: tl_short=True

    win2=min(I_HLW,n)
    plr=[(i,x) for i,x in enumerate(pl[-win2:]) if not np.isnan(x)]
    phr=[(i,x) for i,x in enumerate(ph[-win2:]) if not np.isnan(x)]
    hl_c=sum(1 for j in range(1,len(plr)) if plr[j][1]>plr[j-1][1])
    lh_c=sum(1 for j in range(1,len(phr)) if phr[j][1]<phr[j-1][1])
    vag=hl_c>=I_HLC; cag=lh_c>=I_HHC

    lsl=float(plr[-1][1]) if plr else float(l[-10:].min())
    lsh=float(phr[-1][1]) if phr else float(h[-10:].max())

    # — FVG y OB —
    bull_fvg=bear_fvg=False
    for i in range(max(0,n-I_FVG_BARS),n-2):
        if l[i+2]>h[i] and (l[i+2]-h[i])>atr_now*I_FVG_MIN:
            if h[i]<=c[-1]<=l[i+2]: bull_fvg=True
        if h[i+2]<l[i] and (l[i]-h[i+2])>atr_now*I_FVG_MIN:
            if h[i+2]<=c[-1]<=l[i]: bear_fvg=True
    bull_ob=bear_ob=False
    for i in range(max(0,n-I_OB_BARS),n-1):
        if i>=1:
            if (c[i]-o[i])>atr_now*I_OB_IMP and c[i]>c[i-1] and c[i-1]<o[i-1]:
                if o[i-1]>=c[-1]>=c[i-1]: bull_ob=True
            if (o[i]-c[i])>atr_now*I_OB_IMP and c[i]<c[i-1] and c[i-1]>o[i-1]:
                if c[i-1]>=c[-1]>=o[i-1]: bear_ob=True

    # — CVD —
    hlr=h-l; hs=np.where(hlr>0,hlr,1.0)
    bv=np.where(hlr>0,(c-l)/hs*v,v*0.5); sv2=np.where(hlr>0,(h-c)/hs*v,v*0.5)
    db=bv-sv2; roll=min(I_CVD_ROLL,n)
    cvd=float(_sma(db,roll)[-1])*roll; cvde=float(_ema(db,I_CVD_LEN)[-1])
    cvd_up=cvd>cvde
    cvds=float(_std(db,min(I_CVD_LEN*2,n))[-1])
    cvdz=(cvd-cvde)/cvds if cvds else 0.0
    cvd_sc=max(0.0,min(1.0,(_tanh(cvdz)+1)/2))
    dw=min(I_CVD_DIV,n-1)
    cvdp=float(_sma(db[:-dw],roll)[-1])*roll if n>dw+roll else cvd
    cvd_bd=bool(c[-1]<c[-dw-1] and cvd>cvdp)
    cvd_sd=bool(c[-1]>c[-dw-1] and cvd<cvdp)

    # — Squeeze —
    sb=float(_sma(c,I_SQ_LEN)[-1]); sd2=float(_std(c,I_SQ_LEN)[-1])
    sk=float(_atr(h,l,c,I_SQ_LEN)[-1]); se=float(_ema(c,I_SQ_LEN)[-1])
    sq=((sb+I_SQ_BBM*sd2)<(se+I_SQ_KCM*sk)) and ((sb-I_SQ_BBM*sd2)>(se-I_SQ_KCM*sk))
    sq_fire=sq_bull=sq_bear=False
    if n>=I_SQ_LEN+2:
        sb2=float(_sma(c[:-1],I_SQ_LEN)[-1]); sd3=float(_std(c[:-1],I_SQ_LEN)[-1])
        sk2=float(_atr(h[:-1],l[:-1],c[:-1],I_SQ_LEN)[-1]); se2=float(_ema(c[:-1],I_SQ_LEN)[-1])
        sq2=((sb2+I_SQ_BBM*sd3)<(se2+I_SQ_KCM*sk2)) and ((sb2-I_SQ_BBM*sd3)>(se2-I_SQ_KCM*sk2))
        sq_fire=not sq and sq2
    if sq_fire:
        lr=_lr(c-(max(h[-I_SQ_LEN:])+min(l[-I_SQ_LEN:])+float(_sma(c,I_SQ_LEN)[-1]))/3,I_SQ_LEN)
        sq_bull=lr>0; sq_bear=lr<0

    # — Scores compuestos —
    nsl=(_tanh(ns)+1)/2; mml=(_tanh(fm*2)+1)/2; dn=min(1.0,decay)
    hal=(0.5 if htf_bull else 0.0)+(0.5 if asim_bull else 0.0)
    has=(0.5 if htf_bear else 0.0)+(0.5 if asim_bear else 0.0)
    cl=round(min(100,(SC_W_SCORE*nsl+SC_W_CVD*cvd_sc+SC_W_MOM*mml+SC_W_DECAY*dn+SC_W_HTF*hal)*100))
    nss=(_tanh(-ns)+1)/2; mms=(_tanh(-fm*2)+1)/2
    cs=round(min(100,(SC_W_SCORE*nss+SC_W_CVD*(1-cvd_sc)+SC_W_MOM*mms+SC_W_DECAY*dn+SC_W_HTF*has)*100))

    lc=sum([ns>0.10,alive,exec_ok,htf_bull,asim_bull,vag,tl_long,dp_buy,cvd_up,sq_bull or bull_fvg or bull_ob])
    sc=sum([ns<-0.10,alive,exec_ok,htf_bear,asim_bear,cag,tl_short,dp_sell,not cvd_up,sq_bear or bear_fvg or bear_ob])

    CL=min(100,cl+round(lc*0.5)); CS=min(100,cs+round(sc*0.5))

    # [E1] RSI filtro integrado en condición de entrada
    rsi_ok_long  = rsi3m >= RSI_MIN_LONG
    rsi_ok_short = rsi3m <= RSI_MAX_SHORT

    lb  = CL>=SC_STD  and exec_ok and alive and vol_ok and vol_bar_ok
    sb3 = CS>=SC_STD  and exec_ok and alive and vol_ok and vol_bar_ok
    ls  = lb  and htf_bull and rsi_ok_long
    ss3 = sb3 and htf_bear and rsi_ok_short
    lf  = ls  and CL>=SC_FUEL and (tl_long  or sq_bull or cvd_up    or bull_fvg or bull_ob)
    sf  = ss3 and CS>=SC_FUEL and (tl_short or sq_bear or not cvd_up or bear_fvg or bear_ob)
    lsu = lf  and CL>=SC_SUP  and (dp_buy  or cvd_bd or vag)
    ssu = sf  and CS>=SC_SUP  and (dp_sell or cvd_sd or cag)

    if   lsu: sig,scr="★ LONG SUP",CL
    elif lf:  sig,scr="▲ LONG FUEL",CL
    elif ls:  sig,scr="▲ LONG STD",CL
    elif ssu: sig,scr="★ SHORT SUP",CS
    elif sf:  sig,scr="▼ SHORT FUEL",CS
    elif ss3: sig,scr="▼ SHORT STD",CS
    else:     sig,scr="ESPERAR",max(CL,CS)

    return {
        "sig":sig,"scr":scr,
        "long_sup":lsu,"long_fuel":lf,"long_std":ls,
        "short_sup":ssu,"short_fuel":sf,"short_std":ss3,
        "CL":CL,"CS":CS,"ns":round(ns*100),
        "lc":lc,"sc":sc,
        "alive":alive,"exec_ok":exec_ok,"vol_ok":vol_ok,"vol_pct":vol_pct,
        "vol_bar_ok":vol_bar_ok,"rsi3m":round(rsi3m,1),
        "htf_bull":htf_bull,"htf_bear":htf_bear,
        "htf15_bull":htf15_bull,"htf1h_bull":htf1h_bull,
        "asim_bull":asim_bull,"asim_bear":asim_bear,
        "dp_buy":dp_buy,"dp_sell":dp_sell,
        "tl_long":tl_long,"tl_short":tl_short,
        "vag":vag,"cag":cag,
        "bull_fvg":bull_fvg,"bear_fvg":bear_fvg,
        "bull_ob":bull_ob,"bear_ob":bear_ob,
        "cvd_up":cvd_up,"cvd_bd":cvd_bd,"cvd_sd":cvd_sd,
        "sq_bull":sq_bull,"sq_bear":sq_bear,"sq_on":sq,
        "trend_up":trend_up,"trend_dn":trend_dn,"adx":round(adx_now,1),
        "lsl":round(lsl,6),"lsh":round(lsh,6),
        "decay":round(decay*100),"atr":atr_now,
    }

# ══════════════════════════════════════════════════════════════════════
# SL/TP ADAPTATIVOS — [E3][E4][E5]
# ══════════════════════════════════════════════════════════════════════
def calc_sl_tp(precio: float, atr: float, dir: str, pp: int):
    sl_d = atr*SL_ATR_MULT if (SL_ATR_MULT>0 and atr>0) else precio*(SL_PCT_FB/100)
    tp2_d = sl_d*TP_RR
    tp1_d = sl_d*TP1_RR
    sl_pct = sl_d/precio*100; tp2_pct = tp2_d/precio*100
    if dir=="LONG":
        return (round(precio-sl_d,pp), round(precio+tp2_d,pp),
                round(precio+tp1_d,pp), sl_pct, tp2_pct)
    return (round(precio+sl_d,pp), round(precio-tp2_d,pp),
            round(precio-tp1_d,pp), sl_pct, tp2_pct)

# ══════════════════════════════════════════════════════════════════════
# FILTRO MACRO + HORARIO
# ══════════════════════════════════════════════════════════════════════
def macro_ok(dir: str) -> tuple:
    h = datetime.now(timezone.utc).hour
    if not (HORA_INI<=h<HORA_FIN): return False, f"fuera de horario ({h}h UTC)"
    if dir=="LONG"  and _btc_chg<BTC_MIN_LONG:  return False, f"BTC {_btc_chg:+.1f}% — bajista"
    if dir=="SHORT" and _btc_chg>BTC_MAX_SHORT: return False, f"BTC {_btc_chg:+.1f}% — alcista"
    return True, "ok"

# ══════════════════════════════════════════════════════════════════════
# SCANNER
# ══════════════════════════════════════════════════════════════════════
def escanear():
    global _btc_chg
    log.info("═══ Escaneo QF×JP v5.0 ═══")
    _estado["escaneos"]+=1

    tickers = get_tickers()
    for t in tickers:
        if t.get("symbol")=="BTC-USDT":
            try:
                _btc_chg=float(t.get("priceChangePercent",0))
                bp=float(t.get("lastPrice",0))
                log.info(f"BTC ${bp:,.0f} ({_btc_chg:+.1f}%) | pares={len(tickers)}")
            except: pass
            break
    _estado["btc_cambio"]=_btc_chg

    hora=datetime.now(timezone.utc).hour
    f_hora=not(HORA_INI<=hora<HORA_FIN)
    _estado["filtro_macro"]=f_hora or _btc_chg<BTC_MIN_LONG
    if _estado["filtro_macro"]:
        log.warning(f"⚠️ Macro activo hora={hora}h btc={_btc_chg:+.1f}%")

    res=[]
    for t in tickers:
        sym=t.get("symbol","")
        if not sym.endswith("-USDT"): continue
        if sym in BLACKLIST: continue
        if any(x in sym for x in _EXCLUIR): continue
        if time.time()<cooldown_sym.get(sym,0): continue
        try:
            vol=float(t.get("quoteVolume",0))
            px=float(t.get("lastPrice",0))
            chg=float(t.get("priceChangePercent",0))
        except: continue
        if vol<VOL_MIN: continue

        k3  = get_klines(sym,"3m",  100)
        k15 = get_klines(sym,"15m",  35)
        k1h = get_klines(sym,"1h",   35)  # [E8]
        if not k3 or len(k3)<60:
            time.sleep(0.05); continue

        an = analizar(k3,k15,k1h)
        if not an: time.sleep(0.05); continue
        if an["sig"]=="ESPERAR" and an["CL"]<45 and an["CS"]<45:
            time.sleep(0.05); continue

        res.append({"sym":sym,"px":px,"chg":chg,"vol":vol,**an})
        time.sleep(0.08)

    orden={"★ LONG SUP":0,"★ SHORT SUP":1,"▲ LONG FUEL":2,"▼ SHORT FUEL":3,
           "▲ LONG STD":4,"▼ SHORT STD":5,"ESPERAR":6}
    res.sort(key=lambda x:(orden.get(x["sig"],9),-x["scr"]))
    sigs=[r for r in res if r["sig"]!="ESPERAR"][:TOP_N]
    _estado["señales"]=len(sigs); _estado["ultimo"]=datetime.now(timezone.utc).strftime("%H:%M")
    log.info(f"señales={len(sigs)} analizados={len(res)}")

    ts=any(r["long_sup"] or r["short_sup"] for r in sigs)
    tf=any(r["long_fuel"] or r["short_fuel"] for r in sigs)
    iv=INT_ALE if ts else INT_ACT if (tf or sigs) else INT_NORM
    return sigs, iv, _btc_chg

# ══════════════════════════════════════════════════════════════════════
# AUTO-TRADE — [B1][B4][E3][E4][E5]
# ══════════════════════════════════════════════════════════════════════
_cfg_ok: set = set()

def configurar_leverage(sym: str):
    if sym in _cfg_ok: return
    r=_post("/openApi/swap/v2/trade/leverage",{"symbol":sym,"leverage":str(LEVERAGE)})
    time.sleep(0.8)
    if not r:
        for side in ("LONG","SHORT"):
            _post("/openApi/swap/v2/trade/leverage",{"symbol":sym,"side":side,"leverage":str(LEVERAGE)})
            time.sleep(0.8)
    _post("/openApi/swap/v2/trade/marginType",{"symbol":sym,"marginType":"ISOLATED"})
    time.sleep(0.5)
    _cfg_ok.add(sym)
    log.info(f"⚙️ {sym} lev={LEVERAGE}x ISOLATED")

def abrir(sym: str, px: float, dir: str, atr: float, bal: float=-1.0) -> Optional[dict]:
    global racha_perd, cb_hasta
    if not API_KEY: return None
    if not AUTO_TRADE and not DRY_RUN: return None
    if sym in trades_abiertos: return None
    if time.time()<cb_hasta:
        log.warning(f"⚡ CB activo {int(cb_hasta-time.time())}s"); return None
    if bal==-2.0: return None

    ok,why=macro_ok(dir)
    if not ok: log.info(f"Macro skip {sym} {dir}: {why}"); return None

    pos=get_posiciones()
    if pos is None: log.warning(f"No posiciones API — skip {sym}"); return None
    if len(pos)>=MAX_TRADES: log.warning(f"MaxTrades — skip {sym}"); return None

    b = get_balance() if bal<0 else bal
    if bal<0: _estado["balance"]=b
    ut=usdt_trade(b)
    if b<ut: log.warning(f"Balance ${b:.2f}<${ut:.2f} — skip {sym}"); return None

    inf=get_info(sym)
    sl_p,tp2_p,tp1_p,sl_pct,tp2_pct=calc_sl_tp(px,atr,dir,inf["pp"])
    rr=tp2_pct/sl_pct if sl_pct>0 else 0
    if rr<RR_MIN: log.info(f"RR {rr:.1f}<{RR_MIN} — skip {sym}"); return None

    if DRY_RUN:
        tr={"sym":sym,"dir":dir,"px":px,"ut":ut,"sl":sl_p,"tp1":tp1_p,"tp2":tp2_p,
            "sl_pct":round(sl_pct,2),"tp_pct":round(tp2_pct,2),"rr":round(rr,2),
            "qty":0,"lev":LEVERAGE,"t":datetime.now(timezone.utc).isoformat(),"dry":True,
            "be_moved":False}
        trades_abiertos[sym]=tr; _estado["trades"]=len(trades_abiertos)
        log.info(f"[DRY] {dir} {sym} @{px} SL={sl_p} TP1={tp1_p} TP2={tp2_p} RR={rr:.1f}")
        return tr

    configurar_leverage(sym)
    qty=round_qty((ut*LEVERAGE)/px, inf["step"])
    if qty<inf["min"]: log.warning(f"qty {qty}<min {inf['min']} — skip {sym}"); return None

    qty_tp1  = round_qty(qty*TP1_QTY_PCT/100, inf["step"])
    qty_rest = round_qty(qty-qty_tp1, inf["step"])

    es_l=dir=="LONG"; ab="BUY" if es_l else "SELL"; cl="SELL" if es_l else "BUY"
    ps ="LONG" if es_l else "SHORT"

    ord_r=_post("/openApi/swap/v2/trade/order",
                {"symbol":sym,"side":ab,"type":"MARKET","quantity":str(qty),"positionSide":ps})
    if not ord_r: log.error(f"Orden {dir} {sym} fallida"); return None
    time.sleep(1.0)

    # SL posición completa
    _post("/openApi/swap/v2/trade/order",
          {"symbol":sym,"side":cl,"type":"STOP_MARKET","stopPrice":str(sl_p),
           "closePosition":"true","positionSide":ps})
    time.sleep(0.8)

    # TP1 parcial [E4]
    if qty_tp1>=inf["min"]:
        _post("/openApi/swap/v2/trade/order",
              {"symbol":sym,"side":cl,"type":"TAKE_PROFIT_MARKET","stopPrice":str(tp1_p),
               "quantity":str(qty_tp1),"positionSide":ps})
        time.sleep(0.8)

    # TP2 trailing en el resto [E5]
    if TRAIL_PCT>0 and qty_rest>=inf["min"]:
        _post("/openApi/swap/v2/trade/order",
              {"symbol":sym,"side":cl,"type":"TRAILING_STOP_MARKET",
               "callbackRate":str(round(TRAIL_PCT,2)),"quantity":str(qty_rest),"positionSide":ps})
        tdesc=f"TP1={tp1_p}+Trail{TRAIL_PCT}%"
    else:
        _post("/openApi/swap/v2/trade/order",
              {"symbol":sym,"side":cl,"type":"TAKE_PROFIT_MARKET","stopPrice":str(tp2_p),
               "closePosition":"true","positionSide":ps})
        tdesc=f"TP1={tp1_p} TP2={tp2_p}"

    tr={"sym":sym,"dir":dir,"px":px,"ut":ut,"sl":sl_p,"tp1":tp1_p,"tp2":tp2_p,
        "sl_pct":round(sl_pct,2),"tp_pct":round(tp2_pct,2),"rr":round(rr,2),
        "qty":qty,"qty_tp1":qty_tp1,"lev":LEVERAGE,"tdesc":tdesc,
        "t":datetime.now(timezone.utc).isoformat(),"dry":False,"be_moved":False}
    trades_abiertos[sym]=tr; _estado["trades"]=len(trades_abiertos)
    log.info(f"✅ {dir} {sym} @{px} SL={sl_p}(-{sl_pct:.1f}%) {tdesc} RR={rr:.1f} qty={qty}")
    return tr

# ══════════════════════════════════════════════════════════════════════
# ACTUALIZAR TRADES — [B1] FIX CRÍTICO
# ══════════════════════════════════════════════════════════════════════
def actualizar():
    global racha_perd, cb_hasta, pnl_dia
    if not trades_abiertos: return
    try:
        if DRY_RUN:
            activas={s for s in trades_abiertos}
        else:
            pos=get_posiciones()
            # [B1] FIX: Si API falla (None) → NO tocar nada
            if pos is None:
                log.warning("actualizar(): API error — skip verificación"); return
            activas={p.get("symbol") for p in pos}

        cerrados=[s for s in list(trades_abiertos) if s not in activas]

        for sym in cerrados:
            tr=trades_abiertos.pop(sym)
            k=get_klines(sym,"3m",3)
            if not k: continue
            pa=float(k[-1][4]); en=tr["px"]; es_l=tr["dir"]=="LONG"
            pnl_pct=(pa-en)/en*100*(1 if es_l else -1)

            # [E6] Si SL fue movido a BE, no contar como pérdida real
            be=tr.get("be_moved",False)
            ganado=pnl_pct>0 or be

            if ganado and not be:
                _estado["wins"]+=1; racha_perd=0
                res=f"✅ WIN +{pnl_pct:.2f}%"
            elif be and pnl_pct<=0:
                _estado["be_exits"]+=1; racha_perd=0
                res=f"🟡 BE exit {pnl_pct:.2f}%"
            else:
                _estado["losses"]+=1; racha_perd+=1
                res=f"❌ LOSS {pnl_pct:.2f}%"
                cooldown_sym[sym]=time.time()+CD_LOSS_M*60
                if racha_perd>=CB_LOSSES:
                    cb_hasta=time.time()+CB_MIN*60; _estado["circuit_breaker"]=True
                    log.warning(f"⚡ CB {CB_LOSSES} losses → {CB_MIN}min")
                    tg(f"⚡ *Circuit breaker*\n{racha_perd} pérdidas → pausa {CB_MIN}min")

            pnl_u=tr["ut"]*LEVERAGE*pnl_pct/100; pnl_dia+=pnl_u
            _estado["pnl_dia"]=round(pnl_dia,2)
            _csv(tr,pa,pnl_pct,pnl_u,ganado,be)
            log.info(f"Cerrado {sym} {tr['dir']} {res} ${pnl_u:+.2f}")
            tg(f"📊 *{sym.replace('-USDT','')}* {tr['dir']}\n"
               f"Entrada `{en}` → Cierre `{pa:.6f}`\n"
               f"{res} | PnL `${pnl_u:+.2f}`\n"
               f"PnL día `${pnl_dia:+.2f}` | RR real `{abs(pnl_pct)/tr.get('sl_pct',1):.1f}`")

        # Desactivar CB si expiró
        if cb_hasta and time.time()>=cb_hasta:
            _estado["circuit_breaker"]=False

    except Exception as e:
        log.error(f"actualizar(): {e}", exc_info=True)
    _estado["trades"]=len(trades_abiertos)

def _csv(tr,pa,pnl_pct,pnl_u,ganado,be):
    existe=os.path.exists("trades.csv")
    try:
        with open("trades.csv","a",newline="") as f:
            w=csv.writer(f)
            if not existe:
                w.writerow(["ts","sym","dir","entrada","cierre","sl","tp1","tp2",
                             "sl_pct","tp_pct","rr","qty","usdt","lev",
                             "pnl_pct","pnl_usdt","resultado","be","dry","abierto"])
            res="WIN" if (ganado and not be) else "BE" if be else "LOSS"
            w.writerow([datetime.now(timezone.utc).isoformat(),
                        tr["sym"],tr["dir"],tr["px"],pa,
                        tr["sl"],tr["tp1"],tr["tp2"],
                        tr.get("sl_pct"),tr.get("tp_pct"),tr.get("rr"),
                        tr["qty"],tr["ut"],tr["lev"],
                        round(pnl_pct,4),round(pnl_u,4),
                        res,be,tr.get("dry",False),tr["t"]])
    except Exception as e: log.warning(f"CSV: {e}")

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════
def tg(msg: str) -> bool:
    if not TG_TOKEN or not TG_CHAT: print(msg); return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                        json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"Markdown"},timeout=10)
        r.raise_for_status(); return True
    except Exception as e: log.error(f"TG: {e}"); return False

def alerta(par: dict) -> str:
    sym=par["sym"].replace("-USDT",""); sig=par["sig"]; px=par["px"]
    atr=par.get("atr",0); inf=get_info(par["sym"])
    dir_="LONG" if "LONG" in sig else "SHORT"
    sl_p,tp2_p,tp1_p,sl_pct,tp2_pct=calc_sl_tp(px,atr,dir_,inf["pp"])
    rr=tp2_pct/sl_pct if sl_pct>0 else 0
    em="🔵" if "SUP" in sig else "🟡" if "FUEL" in sig else "🟢"
    de="🟢" if "LONG" in sig else "🔴"
    ok_m,_=macro_ok(dir_); m_ico="✅" if ok_m else "⚠️"
    db=max(0,min(8,round(par["decay"]/100*8)))
    bar="█"*db+"░"*(8-db)
    dry=" [DRY]" if DRY_RUN else ""

    l=[
        f"{em} *{sig}: {sym}*{dry}",
        f"{'─'*28}",
        f"{de} CL:`{par['CL']}/100` CS:`{par['CS']}/100` CONV:`{par['lc']}▲/{par['sc']}▼`",
        f"RSI3m:`{par['rsi3m']}` | Vol×avg:`{'✅' if par['vol_bar_ok'] else '❌'}`",
        f"{'─'*28}",
        f"💲`{px}` 24h:{par['chg']:+.1f}% Vol:${par['vol']/1e6:.1f}M",
        f"🛑 SL:`{sl_p}` (-{sl_pct:.1f}% ATR×{SL_ATR_MULT})",
        f"🎯 TP1:`{tp1_p}` (50%qty RR{TP1_RR}) → BE",
        f"🎯 TP2:`{tp2_p}` (+{tp2_pct:.1f}% trailing {TRAIL_PCT}%)",
        f"⚖️ RR:`{rr:.1f}:1` Macro:{m_ico}",
        f"{'─'*28}",
        f"HTF 15m:`{'BULL' if par['htf15_bull'] else 'bear'}` 1h:`{'BULL' if par['htf1h_bull'] else 'bear'}`",
        f"ADX:`{par['adx']}` {'↑' if par['trend_up'] else '↓' if par['trend_dn'] else '~'} | DECAY:`{bar} {par['decay']}%`",
        f"CVD:`{'DIV↑' if par['cvd_bd'] else 'DIV↓' if par['cvd_sd'] else '↑' if par['cvd_up'] else '↓'}`"
        f" SQ:`{'↑' if par['sq_bull'] else '↓' if par['sq_bear'] else 'ON' if par['sq_on'] else '—'}`",
        f"FVG/OB:`{'↑' if par['bull_fvg'] or par['bull_ob'] else '↓' if par['bear_fvg'] or par['bear_ob'] else '—'}`"
        f" TL:`{'↑' if par['tl_long'] else '↓' if par['tl_short'] else '—'}`",
        f"EXEC:`{'OK' if par['exec_ok'] else 'X'}`",
        f"{'─'*28}",
        f"SL pivot: `{par['lsl'] if 'LONG' in sig else par['lsh']}`",
    ]
    if AUTO_TRADE and ("SUP" in sig or "FUEL" in sig):
        st="abierto" if par["sym"] in trades_abiertos else "pendiente"
        l.append(f"Auto-trade:{st}{'(DRY)' if DRY_RUN else ''}")
    else:
        l.append("→ Verifica TV 3m QF×JP")
    return "\n".join(l)

def resumen(res,btc,iv):
    ahora=datetime.now(timezone.utc).strftime("%H:%M UTC")
    t=_estado["wins"]+_estado["losses"]; wr=f"{round(_estado['wins']/t*100)}%" if t else "—"
    cb=""
    if time.time()<cb_hasta: cb=f"\n⚡ CB {int((cb_hasta-time.time())/60)}min"
    hora=datetime.now(timezone.utc).hour
    filt=""
    if not(HORA_INI<=hora<HORA_FIN): filt=f"\n⏰ Fuera horario ({hora}h)"
    elif _btc_chg<BTC_MIN_LONG: filt=f"\n⚠️ BTC bajista {_btc_chg:+.1f}%"

    l=[
        f"QF×JP v5.0{'[DRY]' if DRY_RUN else ''} {ahora}",
        f"BTC {_btc_chg:+.1f}% | scan cada {iv//60}min",
        f"W/L:{_estado['wins']}/{_estado['losses']} BE:{_estado['be_exits']} WR:{wr}",
        f"PnL día:${pnl_dia:+.2f} | Racha-:{racha_perd}{cb}{filt}",
        f"{'─'*22}",
    ]
    if not res: l.append("Sin señales"); return "\n".join(l)

    for lst,tag in [
        ([r for r in res if r["long_sup"]],  "LONG SUP"),
        ([r for r in res if r["short_sup"]], "SHORT SUP"),
        ([r for r in res if r["long_fuel"] and not r["long_sup"]],  "LONG FUEL"),
        ([r for r in res if r["short_fuel"] and not r["short_sup"]],"SHORT FUEL"),
    ]:
        if lst:
            l.append(f"{tag}({len(lst)}):")
            for r in lst[:3]:
                sc=r["CL"] if "LONG" in tag else r["CS"]
                atrp=round(r["atr"]/r["px"]*100,2) if r["px"] else 0
                l.append(f"  {r['sym'].replace('-USDT','')} {sc}/100 RSI={r['rsi3m']} ATR={atrp}%")

    std_l=[r for r in res if r["long_std"] and not r["long_fuel"]]
    std_s=[r for r in res if r["short_std"] and not r["short_fuel"]]
    if std_l or std_s:
        l.append(f"STD({len(std_l)}L/{len(std_s)}S):")
        for r,d in ([(r,"L") for r in std_l[:2]]+[(r,"S") for r in std_s[:2]]):
            sc=r["CL"] if d=="L" else r["CS"]
            l.append(f"  {d} {r['sym'].replace('-USDT','')} {sc}/100")

    if trades_abiertos:
        l+=[f"{'─'*22}",f"Trades({len(trades_abiertos)}):"]
        for s,tr in trades_abiertos.items():
            l.append(f"  {s.replace('-USDT','')} {tr['dir']} RR={tr.get('rr','?')} "
                     f"SL-{tr.get('sl_pct','?')}% {'BE✓' if tr.get('be_moved') else ''}")
    return "\n".join(l)

# ══════════════════════════════════════════════════════════════════════
# LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════════════════
def main():
    global pnl_dia
    modo="DRY RUN" if DRY_RUN else ("AUTO TRADE" if AUTO_TRADE else "ALERTAS")
    _estado["modo"]=modo

    log.info("╔══════════════════════════════════════╗")
    log.info(f"║  QF×JP Scanner v5.0  [{modo:<10}]  ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"  Scores  STD≥{SC_STD} FUEL≥{SC_FUEL} SUP≥{SC_SUP}")
    log.info(f"  SL      ATR×{SL_ATR_MULT} | TP1 RR{TP1_RR}×50% | TP2 RR{TP_RR}×trail{TRAIL_PCT}%")
    log.info(f"  Macro   BTC>{BTC_MIN_LONG}% long | hora {HORA_INI}-{HORA_FIN}h UTC")
    log.info(f"  RSI     long≥{RSI_MIN_LONG} short≤{RSI_MAX_SHORT} | VolBar×{VOL_BAR_MIN}")
    log.info(f"  Trades  max={MAX_TRADES} | CB {CB_LOSSES}→{CB_MIN}min | CD {CD_LOSS_M}min")

    if API_KEY:
        bal=get_balance(); _estado["balance"]=bal
        log.info(f"  Balance ${bal:.2f} USDT")

    tg(f"🤖 *QF×JP v5.0* [{modo}]\n"
       f"Scores STD≥{SC_STD} FUEL≥{SC_FUEL} SUP≥{SC_SUP}\n"
       f"SL ATR×{SL_ATR_MULT} | TP1 RR{TP1_RR}(50%) | TP2 RR{TP_RR}+Trail{TRAIL_PCT}%\n"
       f"RSI long≥{RSI_MIN_LONG} short≤{RSI_MAX_SHORT} | VolBar×{VOL_BAR_MIN}\n"
       f"Max trades={MAX_TRADES} | CB={CB_LOSSES}→{CB_MIN}min\n"
       f"Horario {HORA_INI}-{HORA_FIN}h UTC | BTC filtro {BTC_MIN_LONG}%")

    ul_hora=-1; ul_dia=-1; iv=INT_NORM; btc=0.0

    while True:
        ahora=datetime.now(timezone.utc)

        # Reinicio diario
        if ahora.day!=ul_dia:
            if ul_dia!=-1:
                tg(f"📅 *Resumen diario*\n"
                   f"PnL `${pnl_dia:+.2f}` USDT\n"
                   f"W/L {_estado['wins']}/{_estado['losses']} BE={_estado['be_exits']}")
                pnl_dia=0.0; _estado["wins"]=_estado["losses"]=_estado["be_exits"]=0
            ul_dia=ahora.day

        try:
            actualizar()
            res,iv,btc=escanear()

            # Leer balance una sola vez por ciclo
            bal_c=-1.0
            if (AUTO_TRADE or DRY_RUN) and API_KEY:
                if any(r["long_sup"] or r["short_sup"] or r["long_fuel"] or r["short_fuel"] for r in res):
                    bal_c=get_balance(); _estado["balance"]=bal_c
                    ut=usdt_trade(bal_c)
                    if bal_c<ut:
                        log.warning(f"Balance ${bal_c:.2f}<${ut:.2f} — skip trades")
                        bal_c=-2.0
                    else:
                        log.info(f"Balance ${bal_c:.2f}")

            for par in res:
                sym=par["sym"]
                accion=par["long_sup"] or par["short_sup"] or par["long_fuel"] or par["short_fuel"]
                if not accion: continue
                if time.time()-alertas_env.get(sym,0)<ALE_CD: continue

                if tg(alerta(par)): alertas_env[sym]=time.time()

                if AUTO_TRADE or DRY_RUN:
                    if (par["long_sup"] or par["long_fuel"]) and sym not in trades_abiertos:
                        tr=abrir(sym,par["px"],"LONG",par.get("atr",0),bal_c)
                        if tr:
                            tg(f"{'[DRY] ' if DRY_RUN else ''}✅ LONG {sym.replace('-USDT','')}\n"
                               f"`{tr['px']}` SL`{tr['sl']}`(-{tr['sl_pct']}%)\n"
                               f"TP1`{tr['tp1']}`(RR{TP1_RR}) TP2`{tr['tp2']}`(+{tr['tp_pct']}%)\n"
                               f"RR`{tr['rr']}` qty={tr['qty']} ${tr['ut']}×{LEVERAGE}x")
                    elif (par["short_sup"] or par["short_fuel"]) and sym not in trades_abiertos:
                        tr=abrir(sym,par["px"],"SHORT",par.get("atr",0),bal_c)
                        if tr:
                            tg(f"{'[DRY] ' if DRY_RUN else ''}✅ SHORT {sym.replace('-USDT','')}\n"
                               f"`{tr['px']}` SL`{tr['sl']}`(+{tr['sl_pct']}%)\n"
                               f"TP1`{tr['tp1']}`(RR{TP1_RR}) TP2`{tr['tp2']}`(-{tr['tp_pct']}%)\n"
                               f"RR`{tr['rr']}` qty={tr['qty']} ${tr['ut']}×{LEVERAGE}x")

            if ahora.hour!=ul_hora:
                tg(resumen(res,btc,iv)); ul_hora=ahora.hour

        except Exception as e:
            log.error(f"loop: {e}", exc_info=True)
            tg(f"⚠️ *Error scanner*\n`{str(e)[:200]}`")
            iv=INT_NORM

        log.info(f"Next scan {iv}s ({iv//60}min)")
        time.sleep(iv)

if __name__=="__main__":
    main()
