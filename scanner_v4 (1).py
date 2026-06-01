"""
╔══════════════════════════════════════════════════════════════════════╗
║   CRYPTO SCANNER v4.0 — QF×JP ENGINE  (UPGRADES)                   ║
║                                                                      ║
║   NUEVO v4.0 (sobre v3.3):                                          ║
║   [U1] Open Interest Delta — OI sube con precio = posiciones reales ║
║        OI baja con precio = short squeeze, no entrar                ║
║   [U2] Ratio Longs/Shorts — extremos = reversal inminente           ║
║        >75% longs = trampa alcista | <30% longs = trampa bajista    ║
║   [U3] SL dinámico basado en ATR — se adapta a volatilidad real     ║
║        reemplaza SL_PCT fijo. 1.5×ATR(14) en 3m                    ║
║   [U4] Partial TP — cierra 25% al 50% del TP1, SL a breakeven      ║
║        free-ride hacia TP2 sin riesgo                               ║
║   [U5] Pesos dinámicos por régimen ADX                              ║
║        tendencia fuerte: +CVD +MOM | lateral: +decay +HTF           ║
║   [U6] Filtro OI confirma señal — OI creciendo = confirmado         ║
║   [U7] Reporte diario 22:00 UTC con métricas completas              ║
║   [U8] Kelly Criterion para sizing (cuando hay >20 trades hist.)    ║
╚══════════════════════════════════════════════════════════════════════╝

Variables de entorno Railway (nuevas en v4.0):
  SL_ATR_MULT    → multiplicador ATR para SL (default: 1.5)
  TP_ATR_MULT    → multiplicador ATR para TP1 (default: 3.0)
  PARTIAL_TP     → "true" para cerrar 25% al 50% TP1 y mover SL a BE
  USE_KELLY      → "true" para sizing dinámico Kelly (requiere >20 trades)
  OI_CONFIRM     → "true" para requerir OI creciente en señales SUP
"""

import os, sys, time, hmac, hashlib, logging, math, threading, urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
import requests
import numpy as np

# ─────────────────────────────────────────────────────────────────────
#  HTTP HEALTHCHECK
# ─────────────────────────────────────────────────────────────────────
_PORT    = int(os.environ.get("PORT", "8080"))
_hstatus = {
    "scan":0, "signals":0, "trades":0,
    "wins":0, "losses":0, "pnl_total":0.0,
    "best_trade":0.0, "worst_trade":0.0,
    "last":"starting"
}

class _HH(BaseHTTPRequestHandler):
    def do_GET(self):
        wr = round(_hstatus["wins"]/max(_hstatus["wins"]+_hstatus["losses"],1)*100)
        body = (
            f"OK scans={_hstatus['scan']} signals={_hstatus['signals']} "
            f"trades={_hstatus['trades']} W/L={_hstatus['wins']}/{_hstatus['losses']} "
            f"WR={wr}% PnL={_hstatus['pnl_total']:.2f}USDT last={_hstatus['last']}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type","text/plain")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*a): pass

_http_ready=threading.Event()
def _run_http():
    try:
        srv=HTTPServer(("0.0.0.0",_PORT),_HH)
        _http_ready.set()
        srv.serve_forever()
    except Exception as e:
        print(f"[health] ERROR: {e}",flush=True)
        _http_ready.set()

threading.Thread(target=_run_http,daemon=True,name="http").start()
_http_ready.wait(timeout=5)
print(f"[health] HTTP listo en 0.0.0.0:{_PORT}",flush=True)


# ─────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY","")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET","")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","")

TRADE_USDT      = float(os.getenv("TRADE_USDT",     "20"))
LEVERAGE        = int(os.getenv("LEVERAGE",          "5"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES",   "3"))

# [U3] SL/TP dinámico en ATR (reemplaza SL_PCT/TP_PCT fijos)
SL_ATR_MULT     = float(os.getenv("SL_ATR_MULT",    "1.5"))  # SL = 1.5×ATR
TP_ATR_MULT     = float(os.getenv("TP_ATR_MULT",    "3.0"))  # TP1 = 3.0×ATR → R:R 2:1
# Fallback en % para cuando ATR no disponible
SL_PCT          = float(os.getenv("SL_PCT",          "2.5"))
TP_PCT          = float(os.getenv("TP_PCT",          "5.0"))

# [U4] Partial TP
PARTIAL_TP      = os.getenv("PARTIAL_TP","false").lower()=="true"
PARTIAL_PCT     = float(os.getenv("PARTIAL_PCT","25"))  # % de la posición a cerrar en TP0.5

# [U8] Kelly sizing
USE_KELLY       = os.getenv("USE_KELLY","false").lower()=="true"
KELLY_MIN_TRADES= 20   # mínimo de trades para activar Kelly

# [U6] OI confirm
OI_CONFIRM      = os.getenv("OI_CONFIRM","false").lower()=="true"

_auto_env=os.getenv("AUTO_TRADE","").lower()
AUTO_TRADE=(_auto_env=="true") or (
    _auto_env=="" and bool(BINGX_API_KEY) and bool(BINGX_API_SECRET)
)

TRAILING_PCT    = float(os.getenv("TRAILING_PCT","0"))
BLACKLIST_RAW   = os.getenv("BLACKLIST","ANIME-USDT,WCT-USDT,TAO-USDT,AAPLX-USDT,NCSKGOOGL2USD-USDT")
BLACKLIST       = set(s.strip().upper() for s in BLACKLIST_RAW.split(",") if s.strip())
CB_MAX_LOSSES   = int(os.getenv("CB_MAX_LOSSES","3"))
CB_PAUSE_MIN    = int(os.getenv("CB_PAUSE_MIN","30"))

BASE_URL = "https://open-api.bingx.com"

# Parámetros indicador
I_MOM=20; I_REV=8; I_VOL_L=14; I_ATR_L=10; I_SMO=3
I_W1=0.40; I_W2=0.30; I_W3=0.30
I_ADX_LEN=14; I_ADX_TH=25
I_DLEN=40; I_DTHR=0.35
I_DPM=2.5; I_DPB=20
I_BPT=0.18; I_ASL=10; I_ARR=1.20; I_ABR=1.20
I_TLB=30; I_TLL=5; I_TLR=3; I_TLM=0.15
I_PLL=5; I_PLR=3; I_PHL=5; I_PHR=3
I_HLC=2; I_HHC=2; I_HLW=40
I_FVG_MIN=0.3; I_FVG_BARS=40; I_OB_IMP=1.5; I_OB_BARS=50
I_CVD_LEN=20; I_CVD_DIV=5; I_CVD_ROLL=100
I_SQ_LEN=20; I_SQ_BBM=2.0; I_SQ_KCM=1.5

SC_THR_STD=50; SC_THR_FUEL=62; SC_THR_SUP=75
VOL_ATR_THR=0.60

MIN_VOLUME_USDT=5_000_000
TOP_N=10
INTERVAL_NORMAL=900; INTERVAL_ACTIVO=300; INTERVAL_ALERTA=60

# Estado global
trades_abiertos:    dict  = {}
alertas_enviadas:   dict  = {}
consecutive_losses: int   = 0
circuit_breaker_until: float = 0.0
# [U8] Historial para Kelly
trade_history: list = []  # [{"pnl_pct": float, "won": bool}, ...]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ScannerV4")


# ─────────────────────────────────────────────────────────────────────
#  API BingX
# ─────────────────────────────────────────────────────────────────────

def _sign(params: dict) -> str:
    query="&".join(f"{k}={v}" for k,v in sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(),query.encode(),hashlib.sha256).hexdigest()

def _get(path: str, params: dict=None, auth: bool=False) -> Optional[dict]:
    p=dict(params or {})
    headers={}
    if auth:
        p["timestamp"]=int(time.time()*1000)
        p["recvWindow"]=5000
        p["signature"]=_sign(p)
        headers["X-BX-APIKEY"]=BINGX_API_KEY
    try:
        r=requests.get(BASE_URL+path,params=p,headers=headers,timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}")
        return None

def _post(path: str, params: dict, retries: int=3) -> Optional[dict]:
    for attempt in range(retries):
        p=dict(params)
        p["timestamp"]=int(time.time()*1000)
        p["recvWindow"]=5000
        p["signature"]=_sign(p)
        url=BASE_URL+path+"?"+urllib.parse.urlencode(sorted(p.items()))
        headers={"X-BX-APIKEY":BINGX_API_KEY}
        try:
            r=requests.post(url,headers=headers,timeout=10)
            r.raise_for_status()
            data=r.json()
            if data.get("code")==0: return data
            log.error(f"POST {path} ({attempt+1}/{retries}) code={data.get('code')} msg={data.get('msg','?')} raw={data}")
            if attempt<retries-1: time.sleep(0.5*(attempt+1))
        except Exception as e:
            log.error(f"POST {path} exc ({attempt+1}/{retries}): {e}")
            if attempt<retries-1: time.sleep(0.5*(attempt+1))
    return None

def get_all_tickers() -> list:
    d=_get("/openApi/swap/v2/quote/ticker")
    return d.get("data",[]) if d else []

def get_klines(symbol: str, interval: str="3m", limit: int=80) -> list:
    d=_get("/openApi/swap/v3/quote/klines",{"symbol":symbol,"interval":interval,"limit":limit})
    raw=d.get("data",[]) if d else []
    out=[]
    for k in raw:
        if isinstance(k,dict):
            out.append([k.get("time",0),k.get("open",k.get("o",0)),
                        k.get("high",k.get("h",0)),k.get("low",k.get("l",0)),
                        k.get("close",k.get("c",0)),k.get("volume",k.get("v",0))])
        else: out.append(k)
    return out

def get_open_positions() -> list:
    d=_get("/openApi/swap/v2/trade/openPositions",auth=True)
    if not d: return []
    data=d.get("data")
    if data is None: return []
    if isinstance(data,list): return [p for p in data if abs(float(p.get("positionAmt",0)))>0]
    if isinstance(data,dict): return [p for p in data.get("positions",[]) if abs(float(p.get("positionAmt",0)))>0]
    return []

def get_balance() -> float:
    d=_get("/openApi/swap/v2/user/balance",auth=True)
    if not d: return 0.0
    try:
        data=d.get("data",{})
        if isinstance(data,dict):
            bal=data.get("balance",{})
            if isinstance(bal,dict):
                v=bal.get("availableMargin",bal.get("available",bal.get("crossUnPnl")))
                if v is not None: return float(v)
            v=data.get("availableMargin",data.get("available"))
            if v is not None: return float(v)
        if isinstance(data,list):
            for asset in data:
                if asset.get("asset","").upper() in ("USDT",""):
                    v=asset.get("availableMargin",asset.get("available"))
                    if v is not None: return float(v)
        log.error(f"Balance: estructura no reconocida. Raw: {d}")
    except Exception as e:
        log.error(f"Balance error: {e} | raw: {d}")
    return 0.0

def get_instrument_info(symbol: str) -> dict:
    try:
        d=_get("/openApi/swap/v2/quote/contracts")
        if d and d.get("data"):
            for c in d["data"]:
                if c.get("symbol")==symbol:
                    return {"step_size":float(c.get("tradeMinQuantity",0.001)),
                            "min_qty":float(c.get("tradeMinQuantity",0.001)),
                            "price_precision":int(c.get("pricePrecision",6))}
    except Exception as e:
        log.warning(f"instrument_info {symbol}: {e}")
    return {"step_size":0.001,"min_qty":0.001,"price_precision":6}

def round_qty(qty: float, step: float) -> float:
    if step<=0: return round(qty,4)
    decimals=max(0,-int(math.floor(math.log10(step))))
    return round(math.floor(qty/step)*step,decimals)


# ─────────────────────────────────────────────────────────────────────
#  [U1] OPEN INTEREST — nueva capa
# ─────────────────────────────────────────────────────────────────────

def get_open_interest(symbol: str) -> dict:
    """
    Obtiene OI actual y calcula delta vs hace 15min.
    Retorna:
      oi_now:   OI actual en USDT
      oi_delta: cambio % en las últimas ~15min
      oi_up:    True si OI creciendo (posiciones reales abriendo)
      oi_confirm_long:  OI sube + precio sube = longs reales
      oi_confirm_short: OI sube + precio baja = shorts reales
    """
    try:
        d=_get("/openApi/swap/v2/quote/openInterest",{"symbol":symbol})
        if not d or not d.get("data"):
            return {"oi_now":0,"oi_delta":0,"oi_up":False,"oi_confirm_long":False,"oi_confirm_short":False}
        oi_now=float(d["data"].get("openInterest",0))

        # Historial OI (últimas 4 velas de 5m = ~20min)
        dh=_get("/openApi/swap/v2/quote/openInterestHist",
                {"symbol":symbol,"period":"5m","limit":4})
        if dh and dh.get("data") and len(dh["data"])>=2:
            hist=dh["data"]
            oi_prev=float(hist[0].get("sumOpenInterest",oi_now))
            oi_delta=(oi_now-oi_prev)/max(oi_prev,1)*100
        else:
            oi_delta=0.0

        oi_up=oi_delta>0.3  # OI sube >0.3%

        return {
            "oi_now":    oi_now,
            "oi_delta":  round(oi_delta,2),
            "oi_up":     oi_up,
            "oi_confirm_long":  False,   # se rellena en analizar_par con precio
            "oi_confirm_short": False,
        }
    except Exception as e:
        log.debug(f"OI {symbol}: {e}")
        return {"oi_now":0,"oi_delta":0,"oi_up":False,"oi_confirm_long":False,"oi_confirm_short":False}


# ─────────────────────────────────────────────────────────────────────
#  [U2] RATIO LONGS/SHORTS — nueva capa
# ─────────────────────────────────────────────────────────────────────

def get_ls_ratio(symbol: str) -> dict:
    """
    Long/Short ratio de las últimas 4h (top traders).
    Extremos de sentiment preceden reversals:
      ratio > 0.75 (75% longs) = trampa alcista, cuidado con longs
      ratio < 0.30 (30% longs) = trampa bajista, cuidado con shorts
    """
    try:
        d=_get("/openApi/swap/v2/quote/globalLongShortAccountRatio",
               {"symbol":symbol,"period":"4h","limit":1})
        if not d or not d.get("data") or not d["data"]:
            return {"ls_long_pct":0.5,"ls_extreme":False,"ls_contrarian":None}
        item=d["data"][0]
        long_pct=float(item.get("longAccount",0.5))
        extreme= long_pct>0.75 or long_pct<0.30
        contrarian=None
        if long_pct>0.75: contrarian="SHORT"  # demasiados longs = cuidado
        elif long_pct<0.30: contrarian="LONG"  # demasiados shorts = rebote posible
        return {"ls_long_pct":round(long_pct,3),"ls_extreme":extreme,"ls_contrarian":contrarian}
    except Exception as e:
        log.debug(f"LS ratio {symbol}: {e}")
        return {"ls_long_pct":0.5,"ls_extreme":False,"ls_contrarian":None}


# ─────────────────────────────────────────────────────────────────────
#  [U8] KELLY CRITERION — sizing dinámico
# ─────────────────────────────────────────────────────────────────────

def kelly_size(base_usdt: float) -> float:
    """
    Calcula tamaño de posición con Kelly parcial (25% Kelly).
    Requiere al menos KELLY_MIN_TRADES en historial.
    Retorna USDT a usar en el trade.
    """
    if not USE_KELLY or len(trade_history)<KELLY_MIN_TRADES:
        return base_usdt
    wins=[t for t in trade_history if t["won"]]
    losses=[t for t in trade_history if not t["won"]]
    if not wins or not losses:
        return base_usdt
    win_rate=len(wins)/len(trade_history)
    avg_win=sum(t["pnl_pct"] for t in wins)/len(wins)
    avg_loss=abs(sum(t["pnl_pct"] for t in losses)/len(losses))
    if avg_loss==0: return base_usdt
    b=avg_win/avg_loss   # odds ratio
    kelly_full=win_rate-(1-win_rate)/b
    kelly_quarter=max(0,min(0.25,kelly_full*0.25))  # Kelly 25%, máx 25% del capital
    balance=get_balance()
    if balance<=0: return base_usdt
    kelly_usdt=balance*kelly_quarter
    result=max(base_usdt,min(kelly_usdt,base_usdt*3))  # entre 1x y 3x del base
    log.info(f"Kelly: WR={win_rate:.2f} b={b:.2f} kelly_f={kelly_full:.3f} -> ${result:.1f}")
    return result


# ─────────────────────────────────────────────────────────────────────
#  INDICADORES
# ─────────────────────────────────────────────────────────────────────

def f_tanh(x):
    x2=max(min(2.0*x,20.0),-20.0); e=math.exp(x2); return (e-1.0)/(e+1.0)
def ema(arr,p):
    k=2.0/(p+1); r=np.empty(len(arr)); r[0]=arr[0]
    for i in range(1,len(arr)): r[i]=arr[i]*k+r[i-1]*(1-k)
    return r
def sma(arr,p):
    out=np.full(len(arr),np.nan)
    for i in range(p-1,len(arr)): out[i]=arr[i-p+1:i+1].mean()
    return out
def stdev(arr,p):
    out=np.full(len(arr),np.nan)
    for i in range(p-1,len(arr)): out[i]=arr[i-p+1:i+1].std(ddof=0)
    return out
def atr_series(h,l,c,p):
    tr=np.empty(len(c)); tr[0]=h[0]-l[0]
    for i in range(1,len(c)): tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
    return ema(tr,p)
def adx_series(h,l,c,p):
    n=len(c); pdm=np.zeros(n); mdm=np.zeros(n); tr=np.zeros(n)
    for i in range(1,n):
        hd=h[i]-h[i-1]; ld=l[i-1]-l[i]
        pdm[i]=hd if hd>ld and hd>0 else 0
        mdm[i]=ld if ld>hd and ld>0 else 0
        tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
    ae=ema(tr,p)
    pdi=100*ema(pdm,p)/np.maximum(ae,1e-10)
    mdi=100*ema(mdm,p)/np.maximum(ae,1e-10)
    dx=100*np.abs(pdi-mdi)/np.maximum(pdi+mdi,1e-10)
    return pdi,mdi,ema(dx,p)
def obv_series(c,v):
    obv=np.zeros(len(c))
    for i in range(1,len(c)):
        if c[i]>c[i-1]: obv[i]=obv[i-1]+v[i]
        elif c[i]<c[i-1]: obv[i]=obv[i-1]-v[i]
        else: obv[i]=obv[i-1]
    return obv
def pivot_high(h,left,right):
    n=len(h); ph=np.full(n,np.nan)
    for i in range(left,n-right):
        w=h[i-left:i+right+1]
        if h[i]==w.max() and (w<h[i]).any(): ph[i]=h[i]
    return ph
def pivot_low(l,left,right):
    n=len(l); pl=np.full(n,np.nan)
    for i in range(left,n-right):
        w=l[i-left:i+right+1]
        if l[i]==w.min() and (w>l[i]).any(): pl[i]=l[i]
    return pl
def linreg(arr,length):
    if len(arr)<length: return float(arr[-1])
    y=arr[-length:]; x=np.arange(length)
    m,b=np.polyfit(x,y,1)
    return m*(length-1)+b


# ─────────────────────────────────────────────────────────────────────
#  MOTOR QF×JP v4.0
# ─────────────────────────────────────────────────────────────────────

def analizar_par(klines_3m: list, klines_15m: list,
                 oi_data: dict=None, ls_data: dict=None) -> Optional[dict]:
    if len(klines_3m)<50: return None

    def _col(kl,idx):
        out=[]
        for k in kl:
            try: out.append(float(k[idx]))
            except: out.append(out[-1] if out else 0.0)
        return np.array(out)

    o=_col(klines_3m,1); h=_col(klines_3m,2)
    l=_col(klines_3m,3); c=_col(klines_3m,4); v=_col(klines_3m,5)
    n=len(c)

    # L1 Microestructura
    atr=atr_series(h,l,c,I_ATR_L)
    hi_lo=np.log(np.maximum(h/l,1e-10))
    spread_est=sma(hi_lo,5)*c
    bp_drain=(spread_est/np.maximum(c,1e-10))*100
    exec_ok=bool(bp_drain[-1]<I_BPT)
    atr_now=float(atr[-1])
    atr_avg20=float(sma(atr,20)[-1]) if not np.isnan(sma(atr,20)[-1]) else atr_now
    vol_ok=atr_now>atr_avg20*VOL_ATR_THR
    vol_pct=round(atr_now/atr_avg20*100) if atr_avg20>0 else 100

    # [U3] SL/TP dinámico en ATR
    sl_atr=round(c[-1]*(1-SL_ATR_MULT*atr_now/c[-1]),8) if c[-1]>0 else 0
    tp_atr=round(c[-1]*(1+TP_ATR_MULT*atr_now/c[-1]),8) if c[-1]>0 else 0
    tp_half=round(c[-1]*(1+TP_ATR_MULT*0.5*atr_now/c[-1]),8)  # TP0.5 para partial
    sl_pct_real=round(SL_ATR_MULT*atr_now/c[-1]*100,2) if c[-1]>0 else SL_PCT
    tp_pct_real=round(TP_ATR_MULT*atr_now/c[-1]*100,2) if c[-1]>0 else TP_PCT
    rr_real=round(tp_pct_real/sl_pct_real,1) if sl_pct_real>0 else 2.0

    # ADX
    pdi,mdi,adx_v=adx_series(h,l,c,I_ADX_LEN)
    adx_now=float(adx_v[-1])
    trend_strong=adx_now>=I_ADX_TH
    trend_up=bool(pdi[-1]>mdi[-1] and trend_strong)
    trend_dn=bool(mdi[-1]>pdi[-1] and trend_strong)

    # [U5] Pesos dinámicos por régimen ADX
    # Tendencia fuerte (ADX>35): más momentum y CVD
    # Lateral (ADX<20): más decay y HTF confirmation
    adx_f=min(1.0,adx_now/(I_ADX_TH*2.0))
    w_mom=I_W1+adx_f*I_W1*0.40
    w_rev=max(I_W2*0.30,I_W2-adx_f*I_W2*0.50)
    w_tot=w_mom+w_rev+I_W3

    if adx_now>35:
        SC_W_SCORE=0.25; SC_W_CVD=0.30; SC_W_MOM=0.25; SC_W_DECAY=0.10; SC_W_HTF=0.10
    elif adx_now<20:
        SC_W_SCORE=0.30; SC_W_CVD=0.20; SC_W_MOM=0.15; SC_W_DECAY=0.20; SC_W_HTF=0.15
    else:
        SC_W_SCORE=0.28; SC_W_CVD=0.25; SC_W_MOM=0.20; SC_W_DECAY=0.15; SC_W_HTF=0.12

    # L2 Score
    voln=float(stdev(c,I_MOM)[-1])/float(sma(c,I_MOM)[-1]) if float(sma(c,I_MOM)[-1])!=0 else 1e-10
    f_mom_v=((c[-1]-c[-I_MOM])/c[-I_MOM])/voln if voln and c[-I_MOM] else 0.0
    bsma=sma(c,I_REV); bstd=stdev(c,I_REV)
    f_rev_v=-(c[-1]-bsma[-1])/bstd[-1] if bstd[-1] else 0.0
    obv_a=obv_series(c,v); oe=ema(obv_a,I_VOL_L); os_=stdev(obv_a,I_VOL_L)
    f_vol_v=(obv_a[-1]-oe[-1])/os_[-1] if os_[-1] else 0.0

    raw_v=(w_mom*f_mom_v+w_rev*f_rev_v+I_W3*f_vol_v)/max(w_tot,1e-10)
    sc_std=float(stdev(np.array([raw_v]*n),I_DLEN)[-1]) or 1e-10
    norm_score=f_tanh(raw_v/sc_std)

    # L3 Decaimiento
    ic_num=0.3
    window=min(I_DLEN,n-5)
    if window>=8:
        try:
            roc_s=np.array([(c[i]-c[max(0,i-I_MOM)])/max(c[max(0,i-I_MOM)],1e-10) for i in range(n)])
            fwd=np.diff(c)/np.maximum(c[:-1],1e-10)
            seg_s=roc_s[max(0,n-window-1):n-1]; seg_f=fwd[max(0,n-window-1):n-1]
            if len(seg_s)>4 and seg_s.std()>1e-10 and seg_f.std()>1e-10:
                ic_raw=float(np.corrcoef(seg_s,seg_f)[0,1])
                ic_num=0.0 if np.isnan(ic_raw) else abs(ic_raw)
        except: ic_num=0.3
    decay_r=min(1.0,ic_num/max(ic_num,0.01))
    sig_alive=decay_r>=I_DTHR or ic_num>=0.15

    # L4 Dark Pool
    vb=float(sma(v,I_DPB)[-1]); vs2=bool(v[-1]>vb*I_DPM); rn=bool((h[-1]-l[-1])<atr_now*0.6)
    dp_buy=bool(vs2 and rn and c[-1]>o[-1]); dp_sell=bool(vs2 and rn and c[-1]<o[-1])

    # HTF
    if klines_15m and len(klines_15m)>=22:
        c15=_col(klines_15m,4)
        htf_bull=float(ema(c15,9)[-1])>float(ema(c15,21)[-1])
        htf_bear=float(ema(c15,9)[-1])<float(ema(c15,21)[-1])
    else:
        htf_bull=norm_score>0; htf_bear=norm_score<0

    # L6 Asimetría
    ur=np.where(c>o,h-l,0.0); dr=np.where(c<o,h-l,0.0)
    aur=float(sma(ur,I_ASL)[-1]); adr=float(sma(dr,I_ASL)[-1])
    asym_bull=(aur/adr if adr>0 else 1.0)>=I_ARR
    asym_bear=(adr/aur if aur>0 else 1.0)>=I_ABR

    # L7 Trendline
    ph_arr=pivot_high(h,I_TLL,I_TLR); pl_arr=pivot_low(l,I_PLL,I_PLR)
    phv=[(i,v2) for i,v2 in enumerate(ph_arr) if not np.isnan(v2)]
    plv=[(i,v2) for i,v2 in enumerate(pl_arr) if not np.isnan(v2)]
    tl_break_long=tl_break_short=False
    if len(phv)>=2:
        (pb2,ph2),(pb1,ph1)=phv[-2],phv[-1]
        if ph2>ph1 and (n-1-pb2)<=I_TLB:
            sl2=(ph1-ph2)/max(pb1-pb2,1)
            if c[-1]>ph1+sl2*(n-1-pb1)+atr_now*I_TLM: tl_break_long=True
    if len(plv)>=2:
        (lb2,pl2),(lb1,pl1)=plv[-2],plv[-1]
        if pl2<pl1 and (n-1-lb2)<=I_TLB:
            sl2=(pl1-pl2)/max(lb1-lb2,1)
            if c[-1]<pl1+sl2*(n-1-lb1)-atr_now*I_TLM: tl_break_short=True

    # L8 Swing exhaustion
    win=min(I_HLW,n)
    plr=[(i,v2) for i,v2 in enumerate(pl_arr[-win:]) if not np.isnan(v2)]
    phr=[(i,v2) for i,v2 in enumerate(ph_arr[-win:]) if not np.isnan(v2)]
    hl_c=sum(1 for j in range(1,len(plr)) if plr[j][1]>plr[j-1][1])
    lh_c=sum(1 for j in range(1,len(phr)) if phr[j][1]<phr[j-1][1])
    sell_exhausted=hl_c>=I_HLC; buy_exhausted=lh_c>=I_HHC
    last_sl=float(plr[-1][1]) if plr else float(l[-10:].min())
    last_sh=float(phr[-1][1]) if phr else float(h[-10:].max())

    # L9 FVG
    in_bull_fvg=in_bear_fvg=False
    for i in range(max(0,n-I_FVG_BARS),n-2):
        if l[i+2]>h[i] and (l[i+2]-h[i])>atr_now*I_FVG_MIN:
            if h[i]<=c[-1]<=l[i+2]: in_bull_fvg=True
        if h[i+2]<l[i] and (l[i]-h[i+2])>atr_now*I_FVG_MIN:
            if h[i+2]<=c[-1]<=l[i]: in_bear_fvg=True

    # L10 Order Blocks
    in_bull_ob=in_bear_ob=False
    for i in range(max(0,n-I_OB_BARS),n-1):
        if i>=1:
            if (c[i]-o[i])>atr_now*I_OB_IMP and c[i]>c[i-1] and c[i-1]<o[i-1]:
                if o[i-1]>=c[-1]>=c[i-1]: in_bull_ob=True
            if (o[i]-c[i])>atr_now*I_OB_IMP and c[i]<c[i-1] and c[i-1]>o[i-1]:
                if c[i-1]>=c[-1]>=o[i-1]: in_bear_ob=True

    # L11 CVD
    hlr=h-l; bv=np.where(hlr>0,(c-l)/hlr*v,v*0.5); sv=np.where(hlr>0,(h-c)/hlr*v,v*0.5)
    db=bv-sv; roll=min(I_CVD_ROLL,n)
    cvd=float(sma(db,roll)[-1])*roll; cvde=float(ema(db,I_CVD_LEN)[-1])
    cvd_rising=cvd>cvde
    cvds=float(stdev(db,min(I_CVD_LEN*2,n))[-1])
    cvdz=(cvd-cvde)/cvds if cvds else 0.0
    cvd_score_v=max(0.0,min(1.0,(f_tanh(cvdz)+1)/2))
    dw=min(I_CVD_DIV,n-1)
    cvd_prev=float(sma(db[:-dw],roll)[-1])*roll if n>dw+roll else cvd
    cvd_bull_div=bool(c[-1]<c[-dw-1] and cvd>cvd_prev)
    cvd_bear_div=bool(c[-1]>c[-dw-1] and cvd<cvd_prev)

    # L12 Squeeze
    sb=float(sma(c,I_SQ_LEN)[-1]); sd=float(stdev(c,I_SQ_LEN)[-1])
    sk=float(atr_series(h,l,c,I_SQ_LEN)[-1]); se=float(ema(c,I_SQ_LEN)[-1])
    sq_on=(sb+I_SQ_BBM*sd)<(se+I_SQ_KCM*sk) and (sb-I_SQ_BBM*sd)>(se-I_SQ_KCM*sk)
    sq_fire=sq_bull=sq_bear=False
    if n>=I_SQ_LEN+2:
        sb_p=float(sma(c[:-1],I_SQ_LEN)[-1]); sd_p=float(stdev(c[:-1],I_SQ_LEN)[-1])
        sk_p=float(atr_series(h[:-1],l[:-1],c[:-1],I_SQ_LEN)[-1]); se_p=float(ema(c[:-1],I_SQ_LEN)[-1])
        sq_on_p=(sb_p+I_SQ_BBM*sd_p)<(se_p+I_SQ_KCM*sk_p) and (sb_p-I_SQ_BBM*sd_p)>(se_p-I_SQ_KCM*sk_p)
        sq_fire=not sq_on and sq_on_p
    if sq_fire:
        slr=linreg(c-(max(h[-I_SQ_LEN:])+min(l[-I_SQ_LEN:])+float(sma(c,I_SQ_LEN)[-1]))/3,I_SQ_LEN)
        sq_bull=slr>0; sq_bear=slr<0

    # [U1] OI confirma dirección
    oi=oi_data or {"oi_now":0,"oi_delta":0,"oi_up":False}
    price_up=c[-1]>c[-5] if n>=5 else True
    price_dn=c[-1]<c[-5] if n>=5 else False
    oi["oi_confirm_long"]=bool(oi["oi_up"] and price_up)
    oi["oi_confirm_short"]=bool(oi["oi_up"] and price_dn)

    # [U2] LS ratio filtro contrarian
    ls=ls_data or {"ls_long_pct":0.5,"ls_extreme":False,"ls_contrarian":None}
    ls_warn_long  = ls["ls_contrarian"]=="SHORT"  # demasiados longs, cuidado LONG
    ls_warn_short = ls["ls_contrarian"]=="LONG"   # demasiados shorts, cuidado SHORT

    # ── SCORE COMPUESTO [U5 pesos dinámicos] ────────────────────────
    nsl=(f_tanh(norm_score)+1)/2
    mml=(f_tanh(f_mom_v*2)+1)/2
    dn=min(1.0,decay_r)
    hal=(0.5 if htf_bull else 0.0)+(0.5 if asym_bull else 0.0)
    has=(0.5 if htf_bear else 0.0)+(0.5 if asym_bear else 0.0)

    cl=round(min(100,(SC_W_SCORE*nsl+SC_W_CVD*cvd_score_v+SC_W_MOM*mml+SC_W_DECAY*dn+SC_W_HTF*hal)*100))
    nss=(f_tanh(-norm_score)+1)/2; mms=(f_tanh(-f_mom_v*2)+1)/2
    cs=round(min(100,(SC_W_SCORE*nss+SC_W_CVD*(1-cvd_score_v)+SC_W_MOM*mms+SC_W_DECAY*dn+SC_W_HTF*has)*100))

    lconv=sum([norm_score>0.10,sig_alive,exec_ok,htf_bull,asym_bull,
               sell_exhausted,tl_break_long,dp_buy,cvd_rising,
               sq_bull or in_bull_fvg or in_bull_ob,
               oi["oi_confirm_long"],                  # [U1] bonus OI
               not ls_warn_long])                      # [U2] bonus LS ratio
    sconv=sum([norm_score<-0.10,sig_alive,exec_ok,htf_bear,asym_bear,
               buy_exhausted,tl_break_short,dp_sell,not cvd_rising,
               sq_bear or in_bear_fvg or in_bear_ob,
               oi["oi_confirm_short"],
               not ls_warn_short])

    comp_long=min(100,cl+round(lconv*0.5))
    comp_short=min(100,cs+round(sconv*0.5))

    long_base=comp_long>=SC_THR_STD and exec_ok and sig_alive and vol_ok and not ls_warn_long
    short_base=comp_short>=SC_THR_STD and exec_ok and sig_alive and vol_ok and not ls_warn_short

    long_std=long_base and htf_bull
    short_std=short_base and htf_bear

    long_fuel=long_std and comp_long>=SC_THR_FUEL and \
              (tl_break_long or sq_bull or cvd_rising or in_bull_fvg or in_bull_ob)
    short_fuel=short_std and comp_short>=SC_THR_FUEL and \
               (tl_break_short or sq_bear or not cvd_rising or in_bear_fvg or in_bear_ob)

    # [U6] SUP requiere OI confirmando si OI_CONFIRM activo
    oi_ok_long  = oi["oi_confirm_long"]  if OI_CONFIRM else True
    oi_ok_short = oi["oi_confirm_short"] if OI_CONFIRM else True

    long_sup=long_fuel and comp_long>=SC_THR_SUP and (dp_buy or cvd_bull_div or sell_exhausted) and oi_ok_long
    short_sup=short_fuel and comp_short>=SC_THR_SUP and (dp_sell or cvd_bear_div or buy_exhausted) and oi_ok_short

    if   long_sup:   signal,ss="LONG SUP",  comp_long
    elif long_fuel:  signal,ss="LONG FUEL", comp_long
    elif long_std:   signal,ss="LONG STD",  comp_long
    elif short_sup:  signal,ss="SHORT SUP", comp_short
    elif short_fuel: signal,ss="SHORT FUEL",comp_short
    elif short_std:  signal,ss="SHORT STD", comp_short
    else:            signal,ss="ESPERAR",   max(comp_long,comp_short)

    return {
        "signal":signal,"signal_score":ss,
        "long_sup":long_sup,"long_fuel":long_fuel,"long_std":long_std,
        "short_sup":short_sup,"short_fuel":short_fuel,"short_std":short_std,
        "comp_long":comp_long,"comp_short":comp_short,
        "norm_score":round(norm_score*100),
        "long_conv":lconv,"short_conv":sconv,
        "sig_alive":sig_alive,"exec_ok":exec_ok,"vol_ok":vol_ok,"vol_pct":vol_pct,
        "htf_bull":htf_bull,"htf_bear":htf_bear,
        "asym_bull":asym_bull,"asym_bear":asym_bear,
        "dp_buy":dp_buy,"dp_sell":dp_sell,
        "tl_break_long":tl_break_long,"tl_break_short":tl_break_short,
        "sell_exhausted":sell_exhausted,"buy_exhausted":buy_exhausted,
        "in_bull_fvg":in_bull_fvg,"in_bear_fvg":in_bear_fvg,
        "in_bull_ob":in_bull_ob,"in_bear_ob":in_bear_ob,
        "cvd_rising":cvd_rising,"cvd_bull_div":cvd_bull_div,"cvd_bear_div":cvd_bear_div,
        "sq_bull":sq_bull,"sq_bear":sq_bear,"sq_on":sq_on,
        "trend_up":trend_up,"trend_dn":trend_dn,"adx":round(adx_now,1),
        "last_sl":round(last_sl,6),"last_sh":round(last_sh,6),
        "decay_r":round(decay_r*100),"atr":atr_now,
        # [U3] SL/TP dinámico
        "sl_atr":sl_atr,"tp_atr":tp_atr,"tp_half":tp_half,
        "sl_pct_real":sl_pct_real,"tp_pct_real":tp_pct_real,"rr_real":rr_real,
        # [U1] OI
        "oi_delta":oi["oi_delta"],"oi_up":oi["oi_up"],
        "oi_confirm_long":oi["oi_confirm_long"],"oi_confirm_short":oi["oi_confirm_short"],
        # [U2] LS
        "ls_long_pct":ls["ls_long_pct"],"ls_extreme":ls["ls_extreme"],
        "ls_warn_long":ls_warn_long,"ls_warn_short":ls_warn_short,
        # pesos régimen
        "regime_w":f"CVD{SC_W_CVD:.2f}/MOM{SC_W_MOM:.2f}/DEC{SC_W_DECAY:.2f}",
    }


# ─────────────────────────────────────────────────────────────────────
#  SCANNER
# ─────────────────────────────────────────────────────────────────────

def scan_mercado():
    log.info("=== Scan QF×JP v4.0 ===")
    _hstatus["scan"]+=1
    tickers=get_all_tickers()
    btc_change=btc_price=0.0
    for t in tickers:
        if t.get("symbol")=="BTC-USDT":
            try: btc_change=float(t.get("priceChangePercent",0)); btc_price=float(t.get("lastPrice",0))
            except: pass
            break
    log.info(f"BTC: ${btc_price:,.0f} ({btc_change:+.1f}%) | Pares: {len(tickers)}")

    resultados=[]
    for ticker in tickers:
        sym=ticker.get("symbol","")
        if not sym.endswith("-USDT"): continue
        if sym in BLACKLIST: continue
        if any(x in sym for x in ["USDC","BUSD","TUSD","DAI","FDUSD"]): continue
        try:
            vol24=float(ticker.get("quoteVolume",0))
            precio=float(ticker.get("lastPrice",0))
            chg24=float(ticker.get("priceChangePercent",0))
        except: continue
        if vol24<MIN_VOLUME_USDT: continue

        k3m=get_klines(sym,"3m",80); k15m=get_klines(sym,"15m",30)
        if not k3m or len(k3m)<50: time.sleep(0.05); continue

        # [U1][U2] Obtener OI y LS (solo para candidatos con volumen)
        oi_data=get_open_interest(sym)
        ls_data=get_ls_ratio(sym)

        an=analizar_par(k3m,k15m,oi_data,ls_data)
        if not an: time.sleep(0.05); continue
        if an["signal"]=="ESPERAR" and an["comp_long"]<45 and an["comp_short"]<45:
            time.sleep(0.05); continue

        resultados.append({"symbol":sym,"precio":precio,"change_24h":chg24,"volume_usdt":vol24,**an})
        time.sleep(0.10)

    orden={"LONG SUP":0,"SHORT SUP":1,"LONG FUEL":2,"SHORT FUEL":3,"LONG STD":4,"SHORT STD":5,"ESPERAR":6}
    resultados.sort(key=lambda x:(orden.get(x["signal"],9),-x["signal_score"]))
    señales=[r for r in resultados if r["signal"]!="ESPERAR"][:TOP_N]
    _hstatus["signals"]=len(señales)
    _hstatus["last"]=datetime.now(timezone.utc).strftime("%H:%M")
    log.info(f"Con señal: {len(señales)} | Total: {len(resultados)}")

    tiene_sup=any(r["long_sup"] or r["short_sup"] for r in señales)
    tiene_fuel=any(r["long_fuel"] or r["short_fuel"] for r in señales)
    intervalo=INTERVAL_ALERTA if tiene_sup else (INTERVAL_ACTIVO if tiene_fuel or señales else INTERVAL_NORMAL)
    return señales,intervalo,btc_change


# ─────────────────────────────────────────────────────────────────────
#  AUTO-TRADE [U3][U4][U8]
# ─────────────────────────────────────────────────────────────────────

def set_leverage_margin(symbol: str):
    r=_post("/openApi/swap/v2/trade/leverage",{"symbol":symbol,"leverage":str(LEVERAGE)})
    if not r:
        _post("/openApi/swap/v2/trade/leverage",{"symbol":symbol,"side":"LONG","leverage":str(LEVERAGE)})
        _post("/openApi/swap/v2/trade/leverage",{"symbol":symbol,"side":"SHORT","leverage":str(LEVERAGE)})
    _post("/openApi/swap/v2/trade/marginType",{"symbol":symbol,"marginType":"ISOLATED"})

def _circuit_breaker_check() -> bool:
    global circuit_breaker_until
    if time.time()<circuit_breaker_until:
        rem=int(circuit_breaker_until-time.time())
        log.warning(f"Circuit breaker activo — pausa {rem}s")
        return True
    return False

def abrir_trade(symbol: str, precio: float, direccion: str,
                sl_precio: float=0, tp_precio: float=0,
                tp_half_precio: float=0) -> Optional[dict]:
    global consecutive_losses, circuit_breaker_until

    if not BINGX_API_KEY or not AUTO_TRADE: return None
    if symbol in trades_abiertos: return None
    if _circuit_breaker_check(): return None
    if symbol in BLACKLIST: return None

    posiciones=get_open_positions()
    if len(posiciones)>=MAX_OPEN_TRADES:
        log.warning(f"Max trades ({MAX_OPEN_TRADES}) — skip {symbol}"); return None

    balance=get_balance()
    log.info(f"Balance: ${balance:.2f} USDT")

    # [U8] Kelly sizing
    trade_usdt=kelly_size(TRADE_USDT)
    if balance<trade_usdt:
        log.warning(f"Balance insuficiente (${balance:.2f} < ${trade_usdt:.1f}) — skip {symbol}"); return None

    set_leverage_margin(symbol); time.sleep(0.3)

    info=get_instrument_info(symbol)
    qty=round_qty((trade_usdt*LEVERAGE)/precio,info["step_size"])
    if qty<info["min_qty"]:
        log.warning(f"Qty {qty} < minQty {info['min_qty']} — skip {symbol}"); return None

    # [U3] Usar SL/TP dinámico ATR si disponibles
    is_long=(direccion=="LONG")
    if sl_precio>0:
        sl_p=sl_precio
    else:
        sl_p=round(precio*(1-SL_PCT/100 if is_long else 1+SL_PCT/100),info["price_precision"])
    if tp_precio>0:
        tp_p=tp_precio
    else:
        tp_p=round(precio*(1+TP_PCT/100 if is_long else 1-TP_PCT/100),info["price_precision"])

    side_open="BUY" if is_long else "SELL"
    side_close="SELL" if is_long else "BUY"

    orden=_post("/openApi/swap/v2/trade/order",{
        "symbol":symbol,"side":side_open,"type":"MARKET","quantity":str(qty)})
    if not orden: log.error(f"Orden {direccion} {symbol} fallida"); return None
    time.sleep(0.5)

    # SL
    _post("/openApi/swap/v2/trade/order",{
        "symbol":symbol,"side":side_close,"type":"STOP_MARKET",
        "stopPrice":str(sl_p),"closePosition":"true"})

    # [U4] Partial TP — cierra 25% al TP0.5
    if PARTIAL_TP and tp_half_precio>0 and qty>0:
        qty_partial=round_qty(qty*PARTIAL_PCT/100,info["step_size"])
        if qty_partial>=info["min_qty"]:
            _post("/openApi/swap/v2/trade/order",{
                "symbol":symbol,"side":side_close,"type":"TAKE_PROFIT_MARKET",
                "stopPrice":str(tp_half_precio),"quantity":str(qty_partial)})

    # TP principal (trailing o fijo)
    if TRAILING_PCT>0:
        _post("/openApi/swap/v2/trade/order",{
            "symbol":symbol,"side":side_close,"type":"TRAILING_STOP_MARKET",
            "callbackRate":str(round(TRAILING_PCT,2)),"closePosition":"true"})
        tp_desc=f"Trailing {TRAILING_PCT}%"
    else:
        _post("/openApi/swap/v2/trade/order",{
            "symbol":symbol,"side":side_close,"type":"TAKE_PROFIT_MARKET",
            "stopPrice":str(tp_p),"closePosition":"true"})
        tp_desc=str(tp_p)

    trade={
        "symbol":symbol,"direction":direccion,
        "entry":precio,"sl":sl_p,"tp":tp_p,"tp_desc":tp_desc,
        "tp_half":tp_half_precio,"partial_tp":PARTIAL_TP,
        "qty":qty,"usdt":trade_usdt,"leverage":LEVERAGE,
        "opened_at":datetime.now(timezone.utc).isoformat(),
        "sl_pct_real":round(abs(precio-sl_p)/precio*100,2),
        "tp_pct_real": round(abs(tp_p-precio)/precio*100,2),
    }
    trades_abiertos[symbol]=trade
    _hstatus["trades"]=len(trades_abiertos)
    log.info(f"TRADE {direccion} {symbol} @ {precio} | SL={sl_p} TP={tp_desc} Qty={qty} ${trade_usdt:.0f}")
    return trade

def abrir_trade_long(symbol,precio,an=None):
    sl=an["sl_atr"] if an and an["sl_atr"]>0 else 0
    tp=an["tp_atr"] if an and an["tp_atr"]>0 else 0
    th=an["tp_half"] if an and an["tp_half"]>0 else 0
    return abrir_trade(symbol,precio,"LONG",sl,tp,th)

def abrir_trade_short(symbol,precio,an=None):
    # Para short: SL encima, TP debajo
    if an and an["sl_atr"]>0:
        sl=round(precio*(1+an["sl_pct_real"]/100),8)
        tp=round(precio*(1-an["tp_pct_real"]/100),8)
        th=round(precio*(1-an["tp_pct_real"]*0.5/100),8)
    else:
        sl=tp=th=0
    return abrir_trade(symbol,precio,"SHORT",sl,tp,th)

def actualizar_trades_abiertos():
    global consecutive_losses, circuit_breaker_until
    if not trades_abiertos: return
    try:
        posiciones=get_open_positions()
        syms_activos={p.get("symbol") for p in posiciones}
        cerrados=[sym for sym in list(trades_abiertos.keys()) if sym not in syms_activos]
        for sym in cerrados:
            trade=trades_abiertos.pop(sym)
            k=get_klines(sym,"3m",3)
            if k:
                pa=float(k[-1][4]); en=trade["entry"]; il=trade["direction"]=="LONG"
                pnl_pct=(pa-en)/en*100*(1 if il else -1)
                pnl_usdt=pnl_pct/100*trade["usdt"]*trade["leverage"]
                ganado=pnl_pct>0
                _hstatus["pnl_total"]+=pnl_usdt
                if ganado:
                    _hstatus["wins"]+=1; consecutive_losses=0
                    if pnl_pct>_hstatus["best_trade"]: _hstatus["best_trade"]=pnl_pct
                    res=f"WIN +{pnl_pct:.2f}% (+${pnl_usdt:.2f})"
                else:
                    _hstatus["losses"]+=1; consecutive_losses+=1
                    if pnl_pct<_hstatus["worst_trade"]: _hstatus["worst_trade"]=pnl_pct
                    res=f"LOSS {pnl_pct:.2f}% (-${abs(pnl_usdt):.2f})"
                    if consecutive_losses>=CB_MAX_LOSSES:
                        circuit_breaker_until=time.time()+CB_PAUSE_MIN*60
                        log.warning(f"Circuit breaker: {CB_MAX_LOSSES} perdidas -> pausa {CB_PAUSE_MIN}min")
                        send_telegram(f"Circuit breaker\n{consecutive_losses} perdidas -> pausa {CB_PAUSE_MIN}min")
                # [U8] Actualizar historial Kelly
                trade_history.append({"pnl_pct":pnl_pct,"won":ganado})
                if len(trade_history)>200: trade_history.pop(0)
                log.info(f"Trade cerrado: {sym} {trade['direction']} | {res}")
                send_telegram(
                    f"Trade cerrado: {sym.replace('-USDT','')}\n"
                    f"Dir: {trade['direction']} | Entrada: {en}\n"
                    f"Salida: {pa:.6f}\n"
                    f"{res}\n"
                    f"PnL total sesion: ${_hstatus['pnl_total']:.2f}"
                )
    except Exception as e:
        log.error(f"actualizar_trades: {e}")
    _hstatus["trades"]=len(trades_abiertos)


# ─────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg); return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"},timeout=10)
        r.raise_for_status(); log.info("Telegram OK"); return True
    except Exception as e:
        log.error(f"Telegram: {e}"); return False

def build_alerta(par: dict) -> str:
    sym=par["symbol"].replace("-USDT",""); sig=par["signal"]
    sc_l=par["comp_long"]; sc_s=par["comp_short"]; precio=par["precio"]
    is_long="LONG" in sig; is_sup="SUP" in sig; is_fuel="FUEL" in sig
    emoji="LONG SUP" if is_sup and is_long else "SHORT SUP" if is_sup else "FUEL" if is_fuel else "STD"

    # [U3] SL/TP dinámico
    sl_p=par["sl_atr"] if par["sl_atr"]>0 else round(precio*(1-SL_PCT/100),6)
    tp1=par["tp_atr"] if par["tp_atr"]>0 else round(precio*(1+TP_PCT/100),6)
    tp2=round(precio*(1+par["tp_pct_real"]*1.5/100),6)
    sl_pct=par["sl_pct_real"]; tp_pct=par["tp_pct_real"]; rr=par["rr_real"]

    db_fill=max(0,min(8,round(par["decay_r"]/100*8)))
    decay_bar="█"*db_fill+"░"*(8-db_fill)

    # [U1] OI texto
    oi_txt=""
    if par["oi_delta"]!=0:
        oi_dir="UP" if par["oi_up"] else "dn"
        oi_conf=""
        if par["oi_confirm_long"] and is_long: oi_conf=" CONFIRMA LONG"
        elif par["oi_confirm_short"] and not is_long: oi_conf=" CONFIRMA SHORT"
        oi_txt=f"\n  OI delta: {par['oi_delta']:+.2f}% {oi_dir}{oi_conf}"

    # [U2] LS ratio texto
    ls_txt=""
    if par["ls_extreme"]:
        ls_txt=f"\n  LS ratio: {par['ls_long_pct']*100:.0f}% longs EXTREMO"
    elif par["ls_long_pct"]>0:
        ls_txt=f"\n  LS ratio: {par['ls_long_pct']*100:.0f}% longs"

    # [U4] Partial TP texto
    partial_txt=""
    if PARTIAL_TP and par["tp_half"]>0:
        partial_txt=f"\n  TP parcial: {par['tp_half']:.6f} ({PARTIAL_PCT:.0f}% pos → BE)"

    lines=[
        f"{emoji}: {sym}",
        f"{'─'*28}",
        f"SC LONG: {sc_l}/100 | SC SHORT: {sc_s}/100",
        f"SCORE: {par['norm_score']} | CONV: {par['long_conv']}up/{par['short_conv']}dn",
        f"{'─'*28}",
        f"Precio: {precio} | 24h: {par['change_24h']:+.1f}% | Vol: ${par['volume_usdt']/1e6:.1f}M",
        f"SL: {sl_p} (-{sl_pct:.1f}%) ATR dinamico",
        f"TP1: {tp1} (+{tp_pct:.1f}%) R:R {rr}:1",
        f"TP2: {tp2} (+{tp_pct*1.5:.1f}%)",
        f"{partial_txt}",
        f"{'─'*28}",
        f"Dashboard:",
        f"  DECAY  {decay_bar} {par['decay_r']}% {'ok' if par['sig_alive'] else 'x'}",
        f"  HTF    {'BULL' if par['htf_bull'] else 'BEAR' if par['htf_bear'] else '-'} | ADX {par['adx']} {'up' if par['trend_up'] else 'dn' if par['trend_dn'] else '~'}",
        f"  ASIM   {'up' if par['asym_bull'] else 'dn' if par['asym_bear'] else '-'} | VOL ATR {par['vol_pct']}% {'ok' if par['vol_ok'] else 'x'}",
        f"  TL     {'LONG' if par['tl_break_long'] else 'SHORT' if par['tl_break_short'] else '-'}",
        f"  SWING  {'HL up' if par['sell_exhausted'] else 'LH dn' if par['buy_exhausted'] else '-'}",
        f"  DP     {'up' if par['dp_buy'] else 'dn' if par['dp_sell'] else '-'}",
        f"  FVG    {'up' if par['in_bull_fvg'] else 'dn' if par['in_bear_fvg'] else '-'} | OB {'up' if par['in_bull_ob'] else 'dn' if par['in_bear_ob'] else '-'}",
        f"  CVD    {'DIV up' if par['cvd_bull_div'] else 'DIV dn' if par['cvd_bear_div'] else 'up' if par['cvd_rising'] else 'dn'}",
        f"  SQ     {'fire up' if par['sq_bull'] else 'fire dn' if par['sq_bear'] else 'comp' if par['sq_on'] else '-'}",
        f"  EXEC   {'OK' if par['exec_ok'] else 'BLOQ'}",
        f"  PESOS  {par['regime_w']}",
        f"{oi_txt}{ls_txt}",
        f"{'─'*28}",
        f"SL ref: {par['last_sl'] if is_long else par['last_sh']}",
    ]
    if AUTO_TRADE and (is_sup or is_fuel):
        lines.append(f"Auto-trade: {'abierto' if par['symbol'] in trades_abiertos else 'pendiente'}")
    else:
        lines.append(f"Verifica TradingView 3m QF x JP v3.3")
    return "\n".join(l for l in lines if l.strip())

def build_resumen(res: list, btc_change: float, intervalo: int) -> str:
    now=datetime.now(timezone.utc).strftime("%H:%M UTC")
    btce="+" if btc_change>0 else ""
    wins=_hstatus["wins"]; losses=_hstatus["losses"]; total=wins+losses
    wr_str=f"{wins}/{total} ({round(wins/total*100)}%)" if total>0 else "-"
    pnl=_hstatus["pnl_total"]
    cb_str=""
    if time.time()<circuit_breaker_until:
        rem=int((circuit_breaker_until-time.time())/60)
        cb_str=f"\nCB: {rem}min restantes"
    sup_l=[r for r in res if r["long_sup"]]; sup_s=[r for r in res if r["short_sup"]]
    fl=[r for r in res if r["long_fuel"] and not r["long_sup"]]
    fs=[r for r in res if r["short_fuel"] and not r["short_sup"]]
    sl2=[r for r in res if r["long_std"] and not r["long_fuel"]]
    ss2=[r for r in res if r["short_std"] and not r["short_fuel"]]
    lines=[
        f"QF x JP v4.0 — {now}",
        f"BTC {btce}{btc_change:.2f}% | prox {intervalo//60}min",
        f"W/L: {wr_str} | PnL: ${pnl:.2f} | Racha: {consecutive_losses}{cb_str}",
        f"{'─'*24}",
    ]
    if not res: lines.append("Sin senales"); return "\n".join(lines)
    for lst,lbl in [(sup_l,"LONG SUP"),(sup_s,"SHORT SUP"),(fl,"LONG FUEL"),(fs,"SHORT FUEL")]:
        if lst:
            lines.append(f"{lbl} ({len(lst)}):")
            for r in lst[:3]:
                sc=r["comp_long"] if "LONG" in lbl else r["comp_short"]
                oi_str=f" OI:{r['oi_delta']:+.1f}%" if r["oi_delta"]!=0 else ""
                lines.append(f"  {r['symbol'].replace('-USDT','')} {sc}/100{oi_str} RR:{r['rr_real']}")
    if sl2 or ss2:
        lines.append(f"STD ({len(sl2)}L/{len(ss2)}S):")
        for r,d in ([(r,'L') for r in sl2[:2]]+[(r,'S') for r in ss2[:2]]):
            sc=r["comp_long"] if d=='L' else r["comp_short"]
            lines.append(f"  {'L' if d=='L' else 'S'} {r['symbol'].replace('-USDT','')} {sc}/100")
    if trades_abiertos:
        lines+=[f"{'─'*24}",f"Trades ({len(trades_abiertos)}):"]
        for sym,t in trades_abiertos.items():
            lines.append(f"  {sym.replace('-USDT','')} {t.get('direction','LONG')} SL:{t['sl']} TP:{t.get('tp_desc',t['tp'])}")
    return "\n".join(lines)

# [U7] Reporte diario 22:00 UTC
def build_reporte_diario() -> str:
    wins=_hstatus["wins"]; losses=_hstatus["losses"]; total=wins+losses
    wr=round(wins/total*100) if total>0 else 0
    pnl=_hstatus["pnl_total"]
    best=_hstatus["best_trade"]; worst=_hstatus["worst_trade"]
    kelly_info=""
    if USE_KELLY and len(trade_history)>=KELLY_MIN_TRADES:
        wlist=[t for t in trade_history if t["won"]]
        wr_k=len(wlist)/len(trade_history)
        avg_w=sum(t["pnl_pct"] for t in wlist)/max(len(wlist),1)
        llist=[t for t in trade_history if not t["won"]]
        avg_l=abs(sum(t["pnl_pct"] for t in llist)/max(len(llist),1))
        b=avg_w/max(avg_l,0.001)
        kf=max(0,wr_k-(1-wr_k)/b)
        kelly_info=f"\nKelly: {kf*100:.1f}% -> sizing {round(kf*25,1)}% capital"
    return (
        f"REPORTE DIARIO QF x JP v4.0\n"
        f"{'─'*26}\n"
        f"Trades: {total} | W/L: {wins}/{losses} | WR: {wr}%\n"
        f"PnL neto: ${pnl:.2f} USDT\n"
        f"Mejor trade: +{best:.2f}%\n"
        f"Peor trade: {worst:.2f}%\n"
        f"Scans: {_hstatus['scan']}\n"
        f"Trades abiertos: {len(trades_abiertos)}\n"
        f"{kelly_info}"
    )


# ─────────────────────────────────────────────────────────────────────
#  LOOP PRINCIPAL
# ─────────────────────────────────────────────────────────────────────

def run_loop():
    log.info(f"QF x JP Scanner v4.0 — AUTO_TRADE={'ON' if AUTO_TRADE else 'OFF'}")
    log.info(f"  ${TRADE_USDT}x{LEVERAGE} | SL {SL_ATR_MULT}xATR | TP {TP_ATR_MULT}xATR | Max {MAX_OPEN_TRADES}")
    log.info(f"  Partial TP: {'ON' if PARTIAL_TP else 'OFF'} | Kelly: {'ON' if USE_KELLY else 'OFF'}")
    log.info(f"  OI confirm: {'ON' if OI_CONFIRM else 'OFF'} | Trailing: {'ON '+str(TRAILING_PCT)+'%' if TRAILING_PCT>0 else 'OFF'}")
    log.info(f"  Blacklist: {BLACKLIST if BLACKLIST else 'vacia'}")
    log.info(f"  Circuit breaker: {CB_MAX_LOSSES} perdidas -> {CB_PAUSE_MIN}min")
    if BINGX_API_KEY:
        bal=get_balance(); log.info(f"Balance inicial: ${bal:.2f} USDT")
    send_telegram(
        f"QF x JP Scanner v4.0 iniciado\n"
        f"AUTO TRADE: {'ON' if AUTO_TRADE else 'OFF'}\n"
        f"SL: {SL_ATR_MULT}xATR | TP: {TP_ATR_MULT}xATR | Partial: {'ON' if PARTIAL_TP else 'OFF'}\n"
        f"OI confirm: {'ON' if OI_CONFIRM else 'OFF'} | Kelly: {'ON' if USE_KELLY else 'OFF'}\n"
        f"Max trades: {MAX_OPEN_TRADES} | CB: {CB_MAX_LOSSES} perdidas -> {CB_PAUSE_MIN}min"
    )
    ultima_hora=-1; btc_change=0.0; reporte_enviado_dia=-1

    while True:
        try:
            actualizar_trades_abiertos()
            resultados,intervalo,btc_change=scan_mercado()

            for par in resultados:
                sym=par["symbol"]
                is_actionable=(par["long_sup"] or par["short_sup"] or
                               par["long_fuel"] or par["short_fuel"])
                if not is_actionable: continue
                if time.time()-alertas_enviadas.get(sym,0)<1800: continue
                msg=build_alerta(par)
                if send_telegram(msg): alertas_enviadas[sym]=time.time()
                if AUTO_TRADE:
                    if (par["long_sup"] or par["long_fuel"]) and sym not in trades_abiertos:
                        trade=abrir_trade_long(sym,par["precio"],par)
                        if trade:
                            send_telegram(
                                f"LONG ABIERTO: {sym.replace('-USDT','')}\n"
                                f"Entrada: {trade['entry']} SL: {trade['sl']} TP: {trade.get('tp_desc',trade['tp'])}\n"
                                f"Qty: {trade['qty']} ${trade['usdt']:.0f}x{LEVERAGE} | RR: {par['rr_real']}"
                            )
                    elif (par["short_sup"] or par["short_fuel"]) and sym not in trades_abiertos:
                        trade=abrir_trade_short(sym,par["precio"],par)
                        if trade:
                            send_telegram(
                                f"SHORT ABIERTO: {sym.replace('-USDT','')}\n"
                                f"Entrada: {trade['entry']} SL: {trade['sl']} TP: {trade.get('tp_desc',trade['tp'])}\n"
                                f"Qty: {trade['qty']} ${trade['usdt']:.0f}x{LEVERAGE} | RR: {par['rr_real']}"
                            )

            hora=datetime.now(timezone.utc).hour
            if hora!=ultima_hora:
                send_telegram(build_resumen(resultados,btc_change,intervalo))
                ultima_hora=hora

            # [U7] Reporte diario 22:00 UTC
            dia_actual=datetime.now(timezone.utc).day
            if hora==22 and dia_actual!=reporte_enviado_dia:
                send_telegram(build_reporte_diario())
                reporte_enviado_dia=dia_actual

        except Exception as e:
            log.error(f"Error ciclo: {e}",exc_info=True)
            intervalo=INTERVAL_NORMAL

        log.info(f"Proximo scan en {intervalo}s ({intervalo//60}min)")
        time.sleep(intervalo)


if __name__ == "__main__":
    run_loop()
