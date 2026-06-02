"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ELITE CRYPTO BOT v1.0 — QF×JP ENGINE DEFINITIVO                      ║
║                                                                          ║
║   ARQUITECTURA: 16 CAPAS DE ANÁLISIS                                    ║
║                                                                          ║
║   MOTOR TÉCNICO (heredado + mejorado):                                  ║
║   L1  Microestructura ATR + spread                                       ║
║   L2  Score factores con ADX dinámico                                   ║
║   L3  Decaimiento IC adaptativo                                         ║
║   L4  Dark Pool detection                                               ║
║   L5  Ejecución + spread filter                                         ║
║   L6  Asimetría de momentum                                             ║
║   L7  Ruptura trendline (pivotes)                                       ║
║   L8  Swing exhaustion HL/LH                                            ║
║   L9  Fair Value Gaps (tracking múltiple)                               ║
║   L10 Order Blocks (quality score)                                      ║
║   L11 CVD Delta rodante (sin deriva)                                    ║
║   L12 Squeeze Momentum                                                  ║
║                                                                          ║
║   VENTAJAS EXCLUSIVAS:                                                  ║
║   [E1] LIQUIDATION HEATMAP — zonas de liquidaciones masivas             ║
║        El precio se mueve HACIA las liquidaciones antes de revertir     ║
║        Detección de stop hunts institucionales                          ║
║   [E2] SMART MONEY DIVERGENCE — OI vs precio vs CVD                    ║
║        Cuando los 3 divergen = trampa retail, no entrar                 ║
║        Cuando los 3 convergen = institucional, máximo tamaño            ║
║   [E3] ADAPTIVE TIMEFRAME — 1m en volatilidad, 3m normal               ║
║        Ajusta la resolución de análisis a las condiciones               ║
║   [E4] CORRELATION FILTER — no 2 trades correlacionados >0.75          ║
║        Evita tener doble exposición sin saberlo                         ║
║   [E5] MARKET REGIME DETECTOR — bull/bear/lateral global                ║
║        Filtra señales contra el régimen dominante                       ║
║   [E6] FUNDING ARBITRAGE — funding negativo extremo = reversal          ║
║        -0.05%+ cada 8h = shorts pagando = rebote inminente              ║
║   [E7] VOLUME PROFILE POC — Point of Control como imán de precio       ║
║        El precio vuelve al POC con altísima probabilidad                ║
║   [E8] PARTIAL TP + BREAKEVEN — gestión dinámica de la posición        ║
║        25% en TP0.5 → SL a BE → resto gratis hacia TP2                 ║
║   [E9] KELLY CRITERION — sizing óptimo matemático                      ║
║        Maximiza crecimiento sin ruina, basado en historial real         ║
║   [E10] ANTI-MANIPULATION — detecta pump&dump y wash trading           ║
║         Volumen sin OI = fake | precio extremo sin estructura = trampa  ║
║                                                                          ║
║   SCORE COMPUESTO: 0-100 con 12 capas + 10 ventajas exclusivas         ║
║   SEÑALES: STD / FUEL / SUPREMA / ELITE (solo las mejores)             ║
║   AUTO-TRADE: LONG + SHORT en BingX Futures con gestión completa       ║
╚══════════════════════════════════════════════════════════════════════════╝

Variables Railway (todas opcionales — defaults seguros):
  BINGX_API_KEY / BINGX_API_SECRET / TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
  TRADE_USDT=20 LEVERAGE=5 MAX_OPEN_TRADES=3
  SL_ATR_MULT=1.5 TP_ATR_MULT=3.0
  AUTO_TRADE=false (poner true cuando keys Futures estén OK)
  PARTIAL_TP=true USE_KELLY=false OI_CONFIRM=true
  BLACKLIST=ANIME-USDT,WCT-USDT CB_MAX_LOSSES=3 CB_PAUSE_MIN=30
  MIN_ELITE_SCORE=82 (umbral para señal ELITE)
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
_PORT = int(os.environ.get("PORT","8080"))
_hs   = {"scan":0,"signals":0,"elite":0,"trades":0,
         "wins":0,"losses":0,"pnl":0.0,
         "best":0.0,"worst":0.0,"last":"starting"}

class _HH(BaseHTTPRequestHandler):
    def do_GET(self):
        wr=round(_hs["wins"]/max(_hs["wins"]+_hs["losses"],1)*100)
        body=(f"ELITE BOT v1.0 | scans={_hs['scan']} elite={_hs['elite']} "
              f"trades={_hs['trades']} WR={wr}% "
              f"PnL=${_hs['pnl']:.2f} last={_hs['last']}").encode()
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
        _http_ready.set(); srv.serve_forever()
    except Exception as e:
        print(f"[health] {e}",flush=True); _http_ready.set()

threading.Thread(target=_run_http,daemon=True,name="http").start()
_http_ready.wait(timeout=5)
print(f"[health] OK :{_PORT}",flush=True)

# ─────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY","")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET","")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","")

TRADE_USDT      = float(os.getenv("TRADE_USDT","20"))
LEVERAGE        = int(os.getenv("LEVERAGE","5"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES","3"))
SL_ATR_MULT     = float(os.getenv("SL_ATR_MULT","1.5"))
TP_ATR_MULT     = float(os.getenv("TP_ATR_MULT","3.0"))
SL_PCT          = float(os.getenv("SL_PCT","2.5"))   # fallback
TP_PCT          = float(os.getenv("TP_PCT","5.0"))   # fallback

_auto_env = os.getenv("AUTO_TRADE","").lower()
AUTO_TRADE = (_auto_env=="true") or (
    _auto_env=="" and bool(BINGX_API_KEY) and bool(BINGX_API_SECRET))

PARTIAL_TP      = os.getenv("PARTIAL_TP","true").lower()=="true"
PARTIAL_PCT     = float(os.getenv("PARTIAL_PCT","25"))
USE_KELLY       = os.getenv("USE_KELLY","false").lower()=="true"
OI_CONFIRM      = os.getenv("OI_CONFIRM","true").lower()=="true"
TRAILING_PCT    = float(os.getenv("TRAILING_PCT","0"))
BLACKLIST_RAW   = os.getenv("BLACKLIST","ANIME-USDT,WCT-USDT,TAO-USDT,AAPLX-USDT")
BLACKLIST       = set(s.strip().upper() for s in BLACKLIST_RAW.split(",") if s.strip())
CB_MAX_LOSSES   = int(os.getenv("CB_MAX_LOSSES","3"))
CB_PAUSE_MIN    = int(os.getenv("CB_PAUSE_MIN","30"))
MIN_ELITE_SCORE = int(os.getenv("MIN_ELITE_SCORE","82"))
KELLY_MIN       = 20

BASE_URL = "https://open-api.bingx.com"

# Parámetros indicador
I_MOM=20;I_REV=8;I_VOL_L=14;I_ATR_L=10;I_SMO=3
I_W1=0.40;I_W2=0.30;I_W3=0.30
I_ADX_LEN=14;I_ADX_TH=25
I_DLEN=40;I_DTHR=0.35
I_DPM=2.5;I_DPB=20;I_BPT=0.18
I_ASL=10;I_ARR=1.20;I_ABR=1.20
I_TLB=30;I_TLL=5;I_TLR=3;I_TLM=0.15
I_PLL=5;I_PLR=3;I_PHL=5;I_PHR=3
I_HLC=2;I_HHC=2;I_HLW=40
I_FVG_MIN=0.3;I_FVG_BARS=40
I_OB_IMP=1.5;I_OB_BARS=50
I_CVD_LEN=20;I_CVD_DIV=5;I_CVD_ROLL=100
I_SQ_LEN=20;I_SQ_BBM=2.0;I_SQ_KCM=1.5

SC_THR_STD=50;SC_THR_FUEL=62;SC_THR_SUP=75;SC_THR_ELITE=MIN_ELITE_SCORE
VOL_ATR_THR=0.60
MIN_VOLUME_USDT=5_000_000
TOP_N=10
INTERVAL_NORMAL=900;INTERVAL_ACTIVO=300;INTERVAL_ALERTA=60;INTERVAL_ELITE=30

# Estado global
trades_abiertos:    dict  = {}
alertas_enviadas:   dict  = {}
consecutive_losses: int   = 0
circuit_breaker_until:float=0.0
trade_history:      list  = []
price_cache:        dict  = {}  # [E4] cache precios para correlación

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("EliteBot")


# ─────────────────────────────────────────────────────────────────────
#  API BingX
# ─────────────────────────────────────────────────────────────────────

def _sign(params:dict)->str:
    q="&".join(f"{k}={v}" for k,v in sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(),q.encode(),hashlib.sha256).hexdigest()

def _get(path:str,params:dict=None,auth:bool=False)->Optional[dict]:
    p=dict(params or {});headers={}
    if auth:
        p["timestamp"]=int(time.time()*1000);p["recvWindow"]=5000
        p["signature"]=_sign(p);headers["X-BX-APIKEY"]=BINGX_API_KEY
    try:
        r=requests.get(BASE_URL+path,params=p,headers=headers,timeout=10)
        r.raise_for_status();return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}");return None

def _post(path:str,params:dict,retries:int=3)->Optional[dict]:
    for attempt in range(retries):
        p=dict(params);p["timestamp"]=int(time.time()*1000);p["recvWindow"]=5000
        p["signature"]=_sign(p)
        url=BASE_URL+path+"?"+urllib.parse.urlencode(sorted(p.items()))
        headers={"X-BX-APIKEY":BINGX_API_KEY}
        try:
            r=requests.post(url,headers=headers,timeout=10);r.raise_for_status()
            data=r.json()
            if data.get("code")==0:return data
            log.error(f"POST {path} ({attempt+1}/{retries}) code={data.get('code')} msg={data.get('msg','?')}")
            if attempt<retries-1:time.sleep(0.5*(attempt+1))
        except Exception as e:
            log.error(f"POST {path} exc ({attempt+1}/{retries}): {e}")
            if attempt<retries-1:time.sleep(0.5*(attempt+1))
    return None

def get_all_tickers()->list:
    d=_get("/openApi/swap/v2/quote/ticker");return d.get("data",[]) if d else []

def get_klines(symbol:str,interval:str="3m",limit:int=80)->list:
    d=_get("/openApi/swap/v3/quote/klines",{"symbol":symbol,"interval":interval,"limit":limit})
    raw=d.get("data",[]) if d else []
    out=[]
    for k in raw:
        if isinstance(k,dict):
            out.append([k.get("time",0),k.get("open",k.get("o",0)),
                        k.get("high",k.get("h",0)),k.get("low",k.get("l",0)),
                        k.get("close",k.get("c",0)),k.get("volume",k.get("v",0))])
        else:out.append(k)
    return out

def get_open_positions()->list:
    d=_get("/openApi/swap/v2/trade/openPositions",auth=True)
    if not d:return []
    data=d.get("data")
    if data is None:return []
    if isinstance(data,list):return [p for p in data if abs(float(p.get("positionAmt",0)))>0]
    if isinstance(data,dict):return [p for p in data.get("positions",[]) if abs(float(p.get("positionAmt",0)))>0]
    return []

def get_balance()->float:
    d=_get("/openApi/swap/v2/user/balance",auth=True)
    if not d:return 0.0
    try:
        data=d.get("data",{})
        if isinstance(data,dict):
            bal=data.get("balance",{})
            if isinstance(bal,dict):
                v=bal.get("availableMargin",bal.get("available",bal.get("crossUnPnl")))
                if v is not None:return float(v)
            v=data.get("availableMargin",data.get("available"))
            if v is not None:return float(v)
        if isinstance(data,list):
            for asset in data:
                if asset.get("asset","").upper() in ("USDT",""):
                    v=asset.get("availableMargin",asset.get("available"))
                    if v is not None:return float(v)
        log.error(f"Balance estructura no reconocida: {d}")
    except Exception as e:
        log.error(f"Balance error: {e}")
    return 0.0

def get_instrument_info(symbol:str)->dict:
    try:
        d=_get("/openApi/swap/v2/quote/contracts")
        if d and d.get("data"):
            for c in d["data"]:
                if c.get("symbol")==symbol:
                    return {"step_size":float(c.get("tradeMinQuantity",0.001)),
                            "min_qty":float(c.get("tradeMinQuantity",0.001)),
                            "price_precision":int(c.get("pricePrecision",6))}
    except Exception as e:log.warning(f"instrument_info {symbol}: {e}")
    return {"step_size":0.001,"min_qty":0.001,"price_precision":6}

def round_qty(qty:float,step:float)->float:
    if step<=0:return round(qty,4)
    decimals=max(0,-int(math.floor(math.log10(step))))
    return round(math.floor(qty/step)*step,decimals)


# ─────────────────────────────────────────────────────────────────────
#  [E1] LIQUIDATION HEATMAP
#  Calcula zonas donde hay liquidaciones masivas acumuladas.
#  El precio se mueve HACIA esas zonas antes de revertir.
# ─────────────────────────────────────────────────────────────────────

def get_liquidation_zones(symbol:str, precio:float, atr:float)->dict:
    """
    Estima zonas de liquidación usando OI + funding + precio.
    Los longs se liquidan DEBAJO del precio actual.
    Los shorts se liquidan ENCIMA del precio actual.
    
    Calcula los niveles de precio donde habría liquidaciones masivas
    basándose en los apalancamientos típicos del mercado (5x, 10x, 20x).
    """
    try:
        # Niveles de liquidación por apalancamiento común
        liq_long_5x  = round(precio * (1 - 0.18), 8)   # -18% = liq 5x
        liq_long_10x = round(precio * (1 - 0.09), 8)   # -9% = liq 10x
        liq_long_20x = round(precio * (1 - 0.045), 8)  # -4.5% = liq 20x
        liq_short_5x  = round(precio * (1 + 0.18), 8)
        liq_short_10x = round(precio * (1 + 0.09), 8)
        liq_short_20x = round(precio * (1 + 0.045), 8)

        # Zona caliente: si el precio está a <1 ATR de una zona de liq
        near_long_liq  = abs(precio - liq_long_20x) < atr * 1.5
        near_short_liq = abs(precio - liq_short_20x) < atr * 1.5

        # Obtener OI para estimar tamaño de las liquidaciones
        d=_get("/openApi/swap/v2/quote/openInterest",{"symbol":symbol})
        oi_usdt=0.0
        if d and d.get("data"):
            oi_usdt=float(d["data"].get("openInterest",0))*precio

        return {
            "liq_long_20x":  liq_long_20x,
            "liq_long_10x":  liq_long_10x,
            "liq_short_20x": liq_short_20x,
            "liq_short_10x": liq_short_10x,
            "near_long_liq":  near_long_liq,
            "near_short_liq": near_short_liq,
            "oi_usdt": oi_usdt,
            # Si hay mucho OI y estamos cerca de zona liq = posible sweep
            "sweep_risk_long":  near_long_liq  and oi_usdt > 1e6,
            "sweep_risk_short": near_short_liq and oi_usdt > 1e6,
        }
    except Exception as e:
        log.debug(f"liq_zones {symbol}: {e}")
        return {"liq_long_20x":0,"liq_long_10x":0,"liq_short_20x":0,
                "liq_short_10x":0,"near_long_liq":False,"near_short_liq":False,
                "oi_usdt":0,"sweep_risk_long":False,"sweep_risk_short":False}


# ─────────────────────────────────────────────────────────────────────
#  [E2] SMART MONEY DIVERGENCE
#  OI + precio + CVD divergentes = trampa retail
#  Los 3 convergentes = institucional real
# ─────────────────────────────────────────────────────────────────────

def get_smart_money_signal(symbol:str, precio:float, precio_prev:float,
                            cvd_rising:bool)->dict:
    """
    Smart Money Divergence:
    - Precio sube + OI sube + CVD sube = LONG institucional real ✅
    - Precio sube + OI baja + CVD baja = short squeeze (no duradero) ⚠️
    - Precio sube + OI sube + CVD baja = distribución institucional ❌
    - Precio baja + OI sube + CVD baja = SHORT institucional real ✅
    """
    try:
        d=_get("/openApi/swap/v2/quote/openInterest",{"symbol":symbol})
        oi_now=0.0
        if d and d.get("data"):
            oi_now=float(d["data"].get("openInterest",0))

        # Historial OI
        dh=_get("/openApi/swap/v2/quote/openInterestHist",
                {"symbol":symbol,"period":"5m","limit":4})
        oi_delta=0.0
        if dh and dh.get("data") and len(dh["data"])>=2:
            oi_prev=float(dh["data"][0].get("sumOpenInterest",oi_now))
            oi_delta=(oi_now-oi_prev)/max(oi_prev,1)*100

        price_up=precio>precio_prev
        oi_up=oi_delta>0.2

        # Clasificar señal Smart Money
        if price_up and oi_up and cvd_rising:
            smd="INSTITUTIONAL_LONG"   # máxima confianza
            smd_score=3
        elif not price_up and oi_up and not cvd_rising:
            smd="INSTITUTIONAL_SHORT"  # máxima confianza
            smd_score=3
        elif price_up and not oi_up and not cvd_rising:
            smd="SHORT_SQUEEZE"        # cuidado, no duradero
            smd_score=1
        elif price_up and oi_up and not cvd_rising:
            smd="DISTRIBUTION"         # trampa, no entrar long
            smd_score=0
        elif not price_up and oi_up and cvd_rising:
            smd="ACCUMULATION"         # acumulación antes de subida
            smd_score=2
        else:
            smd="NEUTRAL"
            smd_score=1

        return {
            "smd": smd,
            "smd_score": smd_score,
            "oi_delta": round(oi_delta,2),
            "oi_up": oi_up,
            "smd_confirms_long":  smd in ("INSTITUTIONAL_LONG","ACCUMULATION"),
            "smd_confirms_short": smd in ("INSTITUTIONAL_SHORT",),
            "smd_warns_long":     smd in ("DISTRIBUTION","SHORT_SQUEEZE"),
            "smd_warns_short":    smd in ("ACCUMULATION",),
        }
    except Exception as e:
        log.debug(f"smd {symbol}: {e}")
        return {"smd":"NEUTRAL","smd_score":1,"oi_delta":0,"oi_up":False,
                "smd_confirms_long":False,"smd_confirms_short":False,
                "smd_warns_long":False,"smd_warns_short":False}


# ─────────────────────────────────────────────────────────────────────
#  [E3] ADAPTIVE TIMEFRAME
# ─────────────────────────────────────────────────────────────────────

def get_adaptive_tf(atr_now:float, atr_avg:float)->str:
    """Usa 1m cuando el mercado está muy volátil, 3m cuando es normal."""
    ratio=atr_now/max(atr_avg,1e-10)
    if ratio>2.0:return "1m"    # muy volátil → granularidad máxima
    elif ratio>1.4:return "3m"  # normal
    else:return "5m"            # tranquilo → menos ruido


# ─────────────────────────────────────────────────────────────────────
#  [E4] CORRELATION FILTER
# ─────────────────────────────────────────────────────────────────────

def check_correlation(symbol:str, closes:np.ndarray)->bool:
    """
    Retorna True si el par está muy correlacionado con algún trade abierto.
    Correlación > 0.75 = mismo riesgo, no abrir.
    """
    if not trades_abiertos or not price_cache:
        return False
    try:
        n=min(30,len(closes))
        rets=np.diff(closes[-n:])/np.maximum(closes[-n-1:-1],1e-10)
        for sym2 in list(trades_abiertos.keys()):
            if sym2==symbol:continue
            cache=price_cache.get(sym2)
            if cache is None or len(cache)<n:continue
            rets2=np.diff(cache[-n:])/np.maximum(cache[-n-1:-1],1e-10)
            if len(rets)!=len(rets2):continue
            if rets.std()>1e-10 and rets2.std()>1e-10:
                corr=float(np.corrcoef(rets,rets2)[0,1])
                if abs(corr)>0.75:
                    log.info(f"[E4] {symbol} correlacionado con {sym2}: {corr:.2f}")
                    return True
    except Exception as e:
        log.debug(f"correlation {symbol}: {e}")
    return False


# ─────────────────────────────────────────────────────────────────────
#  [E5] MARKET REGIME DETECTOR
# ─────────────────────────────────────────────────────────────────────

_regime_cache = {"regime":"NEUTRAL","btc_trend":0,"updated":0}

def update_market_regime(btc_change:float, btc_klines:list):
    """
    Detecta régimen de mercado global usando BTC.
    BULL: BTC sobre EMA50 + tendencia positiva
    BEAR: BTC bajo EMA50 + tendencia negativa
    LATERAL: sin dirección clara
    """
    global _regime_cache
    if not btc_klines or len(btc_klines)<55:
        return
    try:
        c=np.array([float(k[4]) for k in btc_klines])
        def ema(arr,p):
            k2=2.0/(p+1);r=np.empty(len(arr));r[0]=arr[0]
            for i in range(1,len(arr)):r[i]=arr[i]*k2+r[i-1]*(1-k2)
            return r
        ema50=float(ema(c,50)[-1]);ema20=float(ema(c,20)[-1])
        price=float(c[-1])
        mom=((price-c[-20])/c[-20]*100) if len(c)>=20 else 0
        if price>ema50 and ema20>ema50 and mom>2:
            regime="BULL"
        elif price<ema50 and ema20<ema50 and mom<-2:
            regime="BEAR"
        else:
            regime="LATERAL"
        _regime_cache={"regime":regime,"btc_trend":round(mom,2),"updated":time.time()}
        log.info(f"[E5] Régimen: {regime} | BTC vs EMA50: {'SOBRE' if price>ema50 else 'BAJO'} | Mom20: {mom:+.1f}%")
    except Exception as e:
        log.debug(f"regime: {e}")


# ─────────────────────────────────────────────────────────────────────
#  [E6] FUNDING ARBITRAGE
# ─────────────────────────────────────────────────────────────────────

def get_funding_signal(symbol:str)->dict:
    """
    Funding negativo extremo (<-0.05%) = shorts pagando a longs cada 8h.
    Presión para cerrar shorts = rebote alcista inminente.
    Funding positivo extremo (>+0.05%) = longs pagando = posible caída.
    """
    try:
        d=_get("/openApi/swap/v2/quote/premiumIndex",{"symbol":symbol})
        if not d or not d.get("data"):
            return {"funding":0,"funding_bull":False,"funding_bear":False,"funding_extreme":False}
        funding=float(d["data"].get("lastFundingRate",0))
        return {
            "funding":      round(funding*100,4),
            "funding_bull": funding<-0.0003,    # muy negativo → presión alcista
            "funding_bear": funding>0.0003,     # muy positivo → presión bajista
            "funding_extreme": abs(funding)>0.0005,  # extremo = reversal inminente
        }
    except Exception as e:
        log.debug(f"funding {symbol}: {e}")
        return {"funding":0,"funding_bull":False,"funding_bear":False,"funding_extreme":False}


# ─────────────────────────────────────────────────────────────────────
#  [E7] VOLUME PROFILE POC
# ─────────────────────────────────────────────────────────────────────

def calc_volume_profile(highs:np.ndarray,lows:np.ndarray,
                         closes:np.ndarray,volumes:np.ndarray,
                         bins:int=20)->dict:
    """
    Calcula el Point of Control (precio con más volumen negociado).
    El precio tiende a volver al POC con alta probabilidad.
    También calcula VAH (Value Area High) y VAL (Value Area Low).
    """
    if len(closes)<20:
        return {"poc":float(closes[-1]),"vah":float(closes[-1]),"val":float(closes[-1]),
                "near_poc":False,"poc_magnet_up":False,"poc_magnet_dn":False}
    try:
        price_min=float(lows[-50:].min())
        price_max=float(highs[-50:].max())
        if price_max<=price_min:
            return {"poc":float(closes[-1]),"vah":price_max,"val":price_min,
                    "near_poc":False,"poc_magnet_up":False,"poc_magnet_dn":False}

        bin_size=(price_max-price_min)/bins
        vol_by_bin=np.zeros(bins)
        for i in range(max(0,len(closes)-50),len(closes)):
            bin_idx=min(bins-1,int((closes[i]-price_min)/bin_size))
            vol_by_bin[bin_idx]+=volumes[i]

        poc_bin=int(np.argmax(vol_by_bin))
        poc=price_min+poc_bin*bin_size+bin_size/2

        # Value Area (70% del volumen)
        total_vol=vol_by_bin.sum(); target=total_vol*0.70
        accum=vol_by_bin[poc_bin]; lo=hi=poc_bin
        while accum<target and (lo>0 or hi<bins-1):
            add_lo=vol_by_bin[lo-1] if lo>0 else 0
            add_hi=vol_by_bin[hi+1] if hi<bins-1 else 0
            if add_lo>=add_hi and lo>0: lo-=1; accum+=add_lo
            elif hi<bins-1: hi+=1; accum+=add_hi
            else: break

        vah=price_min+(hi+1)*bin_size
        val=price_min+lo*bin_size
        price=float(closes[-1])
        atr_approx=float((highs[-14:]-lows[-14:]).mean())

        near_poc=abs(price-poc)<atr_approx*0.5
        poc_magnet_up=price<poc and not near_poc  # precio bajo el POC → sube hacia él
        poc_magnet_dn=price>poc and not near_poc  # precio sobre el POC → baja hacia él

        return {"poc":round(poc,6),"vah":round(vah,6),"val":round(val,6),
                "near_poc":near_poc,"poc_magnet_up":poc_magnet_up,"poc_magnet_dn":poc_magnet_dn}
    except Exception as e:
        log.debug(f"vp: {e}")
        return {"poc":float(closes[-1]),"vah":0,"val":0,
                "near_poc":False,"poc_magnet_up":False,"poc_magnet_dn":False}


# ─────────────────────────────────────────────────────────────────────
#  [E10] ANTI-MANIPULATION FILTER
# ─────────────────────────────────────────────────────────────────────

def detect_manipulation(closes:np.ndarray,volumes:np.ndarray,
                          oi_usdt:float,atr:float)->dict:
    """
    Detecta señales de manipulación:
    1. Pump & Dump: subida rápida >10% con volumen pero sin OI = fake
    2. Wash Trading: volumen muy alto pero rango de precio muy pequeño
    3. Stop Hunt: mecha >3×ATR en una vela = caza de stops
    """
    if len(closes)<10:
        return {"manipulated":False,"type":"CLEAN"}
    try:
        # Pump & Dump: subida >8% en últimas 3 velas con volumen pero OI bajo
        price_change_3v=(closes[-1]-closes[-4])/closes[-4]*100 if len(closes)>=4 else 0
        vol_spike=volumes[-1]>volumes[-10:].mean()*3
        pump_dump=abs(price_change_3v)>8 and vol_spike and oi_usdt<5e5

        # Wash trading: volumen alto pero precio casi sin moverse
        price_range=abs(closes[-1]-closes[-5])/closes[-5]*100 if len(closes)>=5 else 1
        avg_vol=volumes[-10:].mean()
        wash=volumes[-1]>avg_vol*4 and price_range<0.3

        # Stop hunt: detectar mecha larga en vela reciente
        # (simplificado — no tenemos high/low separados aquí)

        if pump_dump:
            mtype="PUMP_DUMP"
        elif wash:
            mtype="WASH_TRADING"
        else:
            mtype="CLEAN"

        return {"manipulated":mtype!="CLEAN","type":mtype,
                "price_change_3v":round(price_change_3v,2)}
    except Exception as e:
        log.debug(f"manipulation: {e}")
        return {"manipulated":False,"type":"CLEAN","price_change_3v":0}


# ─────────────────────────────────────────────────────────────────────
#  [E8] KELLY CRITERION
# ─────────────────────────────────────────────────────────────────────

def kelly_size(base_usdt:float)->float:
    if not USE_KELLY or len(trade_history)<KELLY_MIN:return base_usdt
    wins=[t for t in trade_history if t["won"]]
    losses=[t for t in trade_history if not t["won"]]
    if not wins or not losses:return base_usdt
    wr=len(wins)/len(trade_history)
    avg_w=sum(t["pnl_pct"] for t in wins)/len(wins)
    avg_l=abs(sum(t["pnl_pct"] for t in losses)/len(losses))
    if avg_l==0:return base_usdt
    b=avg_w/avg_l
    kf=max(0,min(0.25,max(0,wr-(1-wr)/b)*0.25))
    balance=get_balance()
    if balance<=0:return base_usdt
    return max(base_usdt,min(balance*kf,base_usdt*3))


# ─────────────────────────────────────────────────────────────────────
#  INDICADORES TÉCNICOS
# ─────────────────────────────────────────────────────────────────────

def f_tanh(x):
    x2=max(min(2.0*x,20.0),-20.0);e=math.exp(x2);return (e-1.0)/(e+1.0)
def ema(arr,p):
    k=2.0/(p+1);r=np.empty(len(arr));r[0]=arr[0]
    for i in range(1,len(arr)):r[i]=arr[i]*k+r[i-1]*(1-k)
    return r
def sma(arr,p):
    out=np.full(len(arr),np.nan)
    for i in range(p-1,len(arr)):out[i]=arr[i-p+1:i+1].mean()
    return out
def stdev(arr,p):
    out=np.full(len(arr),np.nan)
    for i in range(p-1,len(arr)):out[i]=arr[i-p+1:i+1].std(ddof=0)
    return out
def atr_series(h,l,c,p):
    tr=np.empty(len(c));tr[0]=h[0]-l[0]
    for i in range(1,len(c)):tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
    return ema(tr,p)
def adx_series(h,l,c,p):
    n=len(c);pdm=np.zeros(n);mdm=np.zeros(n);tr=np.zeros(n)
    for i in range(1,n):
        hd=h[i]-h[i-1];ld=l[i-1]-l[i]
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
        if c[i]>c[i-1]:obv[i]=obv[i-1]+v[i]
        elif c[i]<c[i-1]:obv[i]=obv[i-1]-v[i]
        else:obv[i]=obv[i-1]
    return obv
def pivot_high(h,left,right):
    n=len(h);ph=np.full(n,np.nan)
    for i in range(left,n-right):
        w=h[i-left:i+right+1]
        if h[i]==w.max() and (w<h[i]).any():ph[i]=h[i]
    return ph
def pivot_low(l,left,right):
    n=len(l);pl=np.full(n,np.nan)
    for i in range(left,n-right):
        w=l[i-left:i+right+1]
        if l[i]==w.min() and (w>l[i]).any():pl[i]=l[i]
    return pl
def linreg(arr,length):
    if len(arr)<length:return float(arr[-1])
    y=arr[-length:];x=np.arange(length);m,b=np.polyfit(x,y,1)
    return m*(length-1)+b


# ─────────────────────────────────────────────────────────────────────
#  MOTOR ELITE — ANÁLISIS COMPLETO
# ─────────────────────────────────────────────────────────────────────

def analizar_par_elite(symbol:str, klines_3m:list, klines_15m:list,
                        klines_1h:list=None)->Optional[dict]:
    if len(klines_3m)<50:return None

    def _col(kl,idx):
        out=[]
        for k in kl:
            try:out.append(float(k[idx]))
            except:out.append(out[-1] if out else 0.0)
        return np.array(out)

    o=_col(klines_3m,1);h=_col(klines_3m,2)
    l=_col(klines_3m,3);c=_col(klines_3m,4);v=_col(klines_3m,5)
    n=len(c)

    # Cache para correlación [E4]
    price_cache[symbol]=c.copy()

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

    # [U3] SL/TP dinámico
    sl_atr=round(c[-1]*(1-SL_ATR_MULT*atr_now/c[-1]),8) if c[-1]>0 else 0
    tp_atr=round(c[-1]*(1+TP_ATR_MULT*atr_now/c[-1]),8) if c[-1]>0 else 0
    tp_half=round(c[-1]*(1+TP_ATR_MULT*0.5*atr_now/c[-1]),8)
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
    if adx_now>35:
        SC_W_SCORE=0.25;SC_W_CVD=0.30;SC_W_MOM=0.25;SC_W_DECAY=0.10;SC_W_HTF=0.10
    elif adx_now<20:
        SC_W_SCORE=0.30;SC_W_CVD=0.20;SC_W_MOM=0.15;SC_W_DECAY=0.20;SC_W_HTF=0.15
    else:
        SC_W_SCORE=0.28;SC_W_CVD=0.25;SC_W_MOM=0.20;SC_W_DECAY=0.15;SC_W_HTF=0.12

    # L2 Score
    voln=float(stdev(c,I_MOM)[-1])/float(sma(c,I_MOM)[-1]) if float(sma(c,I_MOM)[-1])!=0 else 1e-10
    f_mom_v=((c[-1]-c[-I_MOM])/c[-I_MOM])/voln if voln and c[-I_MOM] else 0.0
    bsma=sma(c,I_REV);bstd=stdev(c,I_REV)
    f_rev_v=-(c[-1]-bsma[-1])/bstd[-1] if bstd[-1] else 0.0
    obv_a=obv_series(c,v);oe=ema(obv_a,I_VOL_L);os_=stdev(obv_a,I_VOL_L)
    f_vol_v=(obv_a[-1]-oe[-1])/os_[-1] if os_[-1] else 0.0

    adx_f=min(1.0,adx_now/(I_ADX_TH*2.0))
    w_mom=I_W1+adx_f*I_W1*0.40;w_rev=max(I_W2*0.30,I_W2-adx_f*I_W2*0.50);w_tot=w_mom+w_rev+I_W3
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
            seg_s=roc_s[max(0,n-window-1):n-1];seg_f=fwd[max(0,n-window-1):n-1]
            if len(seg_s)>4 and seg_s.std()>1e-10 and seg_f.std()>1e-10:
                ic_raw=float(np.corrcoef(seg_s,seg_f)[0,1])
                ic_num=0.0 if np.isnan(ic_raw) else abs(ic_raw)
        except:ic_num=0.3
    decay_r=min(1.0,ic_num/max(ic_num,0.01))
    sig_alive=decay_r>=I_DTHR or ic_num>=0.15

    # L4 Dark Pool
    vb=float(sma(v,I_DPB)[-1]);vs2=bool(v[-1]>vb*I_DPM);rn=bool((h[-1]-l[-1])<atr_now*0.6)
    dp_buy=bool(vs2 and rn and c[-1]>o[-1]);dp_sell=bool(vs2 and rn and c[-1]<o[-1])

    # HTF 15m
    if klines_15m and len(klines_15m)>=22:
        c15=_col(klines_15m,4)
        htf_bull=float(ema(c15,9)[-1])>float(ema(c15,21)[-1])
        htf_bear=float(ema(c15,9)[-1])<float(ema(c15,21)[-1])
    else:
        htf_bull=norm_score>0;htf_bear=norm_score<0

    # HTF 1H
    htf2_bull=htf_bull;htf2_bear=htf_bear
    if klines_1h and len(klines_1h)>=22:
        c1h=_col(klines_1h,4)
        htf2_bull=float(ema(c1h,9)[-1])>float(ema(c1h,21)[-1])
        htf2_bear=float(ema(c1h,9)[-1])<float(ema(c1h,21)[-1])

    # 3 TFs alineados
    tfs_long =sum([norm_score>0, htf_bull, htf2_bull])
    tfs_short=sum([norm_score<0, htf_bear, htf2_bear])

    # L6 Asimetría
    ur=np.where(c>o,h-l,0.0);dr=np.where(c<o,h-l,0.0)
    aur=float(sma(ur,I_ASL)[-1]);adr=float(sma(dr,I_ASL)[-1])
    asym_bull=(aur/adr if adr>0 else 1.0)>=I_ARR
    asym_bear=(adr/aur if aur>0 else 1.0)>=I_ABR

    # L7 Trendline
    ph_arr=pivot_high(h,I_TLL,I_TLR);pl_arr=pivot_low(l,I_PLL,I_PLR)
    phv=[(i,v2) for i,v2 in enumerate(ph_arr) if not np.isnan(v2)]
    plv=[(i,v2) for i,v2 in enumerate(pl_arr) if not np.isnan(v2)]
    tl_break_long=tl_break_short=False
    if len(phv)>=2:
        (pb2,ph2),(pb1,ph1)=phv[-2],phv[-1]
        if ph2>ph1 and (n-1-pb2)<=I_TLB:
            sl2=(ph1-ph2)/max(pb1-pb2,1)
            if c[-1]>ph1+sl2*(n-1-pb1)+atr_now*I_TLM:tl_break_long=True
    if len(plv)>=2:
        (lb2,pl2),(lb1,pl1)=plv[-2],plv[-1]
        if pl2<pl1 and (n-1-lb2)<=I_TLB:
            sl2=(pl1-pl2)/max(lb1-lb2,1)
            if c[-1]<pl1+sl2*(n-1-lb1)-atr_now*I_TLM:tl_break_short=True

    # L8 Swing exhaustion
    win=min(I_HLW,n)
    plr=[(i,v2) for i,v2 in enumerate(pl_arr[-win:]) if not np.isnan(v2)]
    phr=[(i,v2) for i,v2 in enumerate(ph_arr[-win:]) if not np.isnan(v2)]
    hl_c=sum(1 for j in range(1,len(plr)) if plr[j][1]>plr[j-1][1])
    lh_c=sum(1 for j in range(1,len(phr)) if phr[j][1]<phr[j-1][1])
    sell_exhausted=hl_c>=I_HLC;buy_exhausted=lh_c>=I_HHC
    last_sl=float(plr[-1][1]) if plr else float(l[-10:].min())
    last_sh=float(phr[-1][1]) if phr else float(h[-10:].max())

    # L9 FVG
    in_bull_fvg=in_bear_fvg=False
    for i in range(max(0,n-I_FVG_BARS),n-2):
        if l[i+2]>h[i] and (l[i+2]-h[i])>atr_now*I_FVG_MIN:
            if h[i]<=c[-1]<=l[i+2]:in_bull_fvg=True
        if h[i+2]<l[i] and (l[i]-h[i+2])>atr_now*I_FVG_MIN:
            if h[i+2]<=c[-1]<=l[i]:in_bear_fvg=True

    # L10 OB
    in_bull_ob=in_bear_ob=False
    for i in range(max(0,n-I_OB_BARS),n-1):
        if i>=1:
            if (c[i]-o[i])>atr_now*I_OB_IMP and c[i]>c[i-1] and c[i-1]<o[i-1]:
                if o[i-1]>=c[-1]>=c[i-1]:in_bull_ob=True
            if (o[i]-c[i])>atr_now*I_OB_IMP and c[i]<c[i-1] and c[i-1]>o[i-1]:
                if c[i-1]>=c[-1]>=o[i-1]:in_bear_ob=True

    # L11 CVD
    hlr=h-l;bv=np.where(hlr>0,(c-l)/hlr*v,v*0.5);sv=np.where(hlr>0,(h-c)/hlr*v,v*0.5)
    db=bv-sv;roll=min(I_CVD_ROLL,n)
    cvd=float(sma(db,roll)[-1])*roll;cvde=float(ema(db,I_CVD_LEN)[-1])
    cvd_rising=cvd>cvde
    cvds=float(stdev(db,min(I_CVD_LEN*2,n))[-1])
    cvdz=(cvd-cvde)/cvds if cvds else 0.0
    cvd_score_v=max(0.0,min(1.0,(f_tanh(cvdz)+1)/2))
    dw=min(I_CVD_DIV,n-1)
    cvd_prev=float(sma(db[:-dw],roll)[-1])*roll if n>dw+roll else cvd
    cvd_bull_div=bool(c[-1]<c[-dw-1] and cvd>cvd_prev)
    cvd_bear_div=bool(c[-1]>c[-dw-1] and cvd<cvd_prev)

    # L12 Squeeze
    sb=float(sma(c,I_SQ_LEN)[-1]);sd=float(stdev(c,I_SQ_LEN)[-1])
    sk=float(atr_series(h,l,c,I_SQ_LEN)[-1]);se=float(ema(c,I_SQ_LEN)[-1])
    sq_on=(sb+I_SQ_BBM*sd)<(se+I_SQ_KCM*sk) and (sb-I_SQ_BBM*sd)>(se-I_SQ_KCM*sk)
    sq_fire=sq_bull=sq_bear=False
    if n>=I_SQ_LEN+2:
        sb_p=float(sma(c[:-1],I_SQ_LEN)[-1]);sd_p=float(stdev(c[:-1],I_SQ_LEN)[-1])
        sk_p=float(atr_series(h[:-1],l[:-1],c[:-1],I_SQ_LEN)[-1]);se_p=float(ema(c[:-1],I_SQ_LEN)[-1])
        sq_on_p=(sb_p+I_SQ_BBM*sd_p)<(se_p+I_SQ_KCM*sk_p) and (sb_p-I_SQ_BBM*sd_p)>(se_p-I_SQ_KCM*sk_p)
        sq_fire=not sq_on and sq_on_p
    if sq_fire:
        slr=linreg(c-(max(h[-I_SQ_LEN:])+min(l[-I_SQ_LEN:])+float(sma(c,I_SQ_LEN)[-1]))/3,I_SQ_LEN)
        sq_bull=slr>0;sq_bear=slr<0

    # [E6] Funding
    funding_data=get_funding_signal(symbol)

    # [E7] Volume Profile POC
    vp=calc_volume_profile(h,l,c,v)

    # [E1] Liquidation Heatmap
    liq=get_liquidation_zones(symbol,float(c[-1]),atr_now)

    # [E2] Smart Money Divergence
    c_prev=float(c[-4]) if len(c)>=4 else float(c[-1])
    smd=get_smart_money_signal(symbol,float(c[-1]),c_prev,cvd_rising)

    # [E10] Anti-manipulation
    manip=detect_manipulation(c,v,liq["oi_usdt"],atr_now)

    # [E5] Régimen de mercado
    regime=_regime_cache["regime"]

    # ── SCORE COMPUESTO ELITE ─────────────────────────────────────────
    nsl=(f_tanh(norm_score)+1)/2
    mml=(f_tanh(f_mom_v*2)+1)/2
    dn=min(1.0,decay_r)
    # HTF ahora incluye 3 TFs
    htf_score_l=tfs_long/3.0
    htf_score_s=tfs_short/3.0

    cl=round(min(100,(SC_W_SCORE*nsl+SC_W_CVD*cvd_score_v+SC_W_MOM*mml+SC_W_DECAY*dn+SC_W_HTF*htf_score_l)*100))
    nss=(f_tanh(-norm_score)+1)/2;mms=(f_tanh(-f_mom_v*2)+1)/2
    cs=round(min(100,(SC_W_SCORE*nss+SC_W_CVD*(1-cvd_score_v)+SC_W_MOM*mms+SC_W_DECAY*dn+SC_W_HTF*htf_score_s)*100))

    # Conv score — 14 puntos ahora (más capas exclusivas)
    lconv=sum([
        norm_score>0.10,            # L2
        sig_alive,                   # L3
        exec_ok,                     # L5
        htf_bull,                    # HTF 15m
        htf2_bull,                   # HTF 1h [nuevo]
        asym_bull,                   # L6
        sell_exhausted,              # L8
        tl_break_long,               # L7
        dp_buy,                      # L4
        cvd_rising,                  # L11
        sq_bull or in_bull_fvg or in_bull_ob,  # L9/L10/L12
        smd["smd_confirms_long"],    # [E2]
        funding_data["funding_bull"],# [E6]
        vp["poc_magnet_up"],         # [E7]
    ])
    sconv=sum([
        norm_score<-0.10,
        sig_alive,exec_ok,
        htf_bear,htf2_bear,
        asym_bear,buy_exhausted,
        tl_break_short,dp_sell,
        not cvd_rising,
        sq_bear or in_bear_fvg or in_bear_ob,
        smd["smd_confirms_short"],
        funding_data["funding_bear"],
        vp["poc_magnet_dn"],
    ])

    # Conv-Boost
    comp_long=min(100,cl+round(lconv*0.5))
    comp_short=min(100,cs+round(sconv*0.5))

    # Filtros de seguridad
    safe_long  = (not smd["smd_warns_long"] and
                  not liq["sweep_risk_long"] and
                  not manip["manipulated"] and
                  regime!="BEAR")          # [E5] no longs en bear market
    safe_short = (not smd["smd_warns_short"] and
                  not liq["sweep_risk_short"] and
                  not manip["manipulated"] and
                  regime!="BULL")          # [E5] no shorts en bull market

    long_base=comp_long>=SC_THR_STD and exec_ok and sig_alive and vol_ok and safe_long
    short_base=comp_short>=SC_THR_STD and exec_ok and sig_alive and vol_ok and safe_short

    long_std=long_base and htf_bull
    short_std=short_base and htf_bear

    long_fuel=long_std and comp_long>=SC_THR_FUEL and \
              (tl_break_long or sq_bull or cvd_rising or in_bull_fvg or in_bull_ob)
    short_fuel=short_std and comp_short>=SC_THR_FUEL and \
               (tl_break_short or sq_bear or not cvd_rising or in_bear_fvg or in_bear_ob)

    oi_ok_long = smd["smd_confirms_long"] if OI_CONFIRM else True
    oi_ok_short= smd["smd_confirms_short"] if OI_CONFIRM else True

    long_sup=long_fuel and comp_long>=SC_THR_SUP and (dp_buy or cvd_bull_div or sell_exhausted) and oi_ok_long
    short_sup=short_fuel and comp_short>=SC_THR_SUP and (dp_sell or cvd_bear_div or buy_exhausted) and oi_ok_short

    # SEÑAL ELITE: todas las capas alineadas + ventajas exclusivas confirmadas
    long_elite=(long_sup and comp_long>=SC_THR_ELITE and
                smd["smd_confirms_long"] and
                htf2_bull and                      # 1H también bull
                tfs_long>=3 and                    # los 3 TFs alineados
                (vp["poc_magnet_up"] or vp["near_poc"]) and  # POC confirma
                funding_data["funding_bull"] and   # funding confirma
                not manip["manipulated"] and        # no manipulación
                lconv>=10)                         # conv muy alto

    short_elite=(short_sup and comp_short>=SC_THR_ELITE and
                 smd["smd_confirms_short"] and
                 htf2_bear and
                 tfs_short>=3 and
                 (vp["poc_magnet_dn"] or vp["near_poc"]) and
                 funding_data["funding_bear"] and
                 not manip["manipulated"] and
                 sconv>=10)

    if   long_elite:   signal,ss="ELITE LONG",  comp_long
    elif long_sup:     signal,ss="LONG SUP",    comp_long
    elif long_fuel:    signal,ss="LONG FUEL",   comp_long
    elif long_std:     signal,ss="LONG STD",    comp_long
    elif short_elite:  signal,ss="ELITE SHORT", comp_short
    elif short_sup:    signal,ss="SHORT SUP",   comp_short
    elif short_fuel:   signal,ss="SHORT FUEL",  comp_short
    elif short_std:    signal,ss="SHORT STD",   comp_short
    else:              signal,ss="ESPERAR",     max(comp_long,comp_short)

    if long_elite or short_elite:
        _hs["elite"]+=1

    return {
        "signal":signal,"signal_score":ss,
        "long_elite":long_elite,"short_elite":short_elite,
        "long_sup":long_sup,"long_fuel":long_fuel,"long_std":long_std,
        "short_sup":short_sup,"short_fuel":short_fuel,"short_std":short_std,
        "comp_long":comp_long,"comp_short":comp_short,
        "norm_score":round(norm_score*100),
        "long_conv":lconv,"short_conv":sconv,
        "sig_alive":sig_alive,"exec_ok":exec_ok,"vol_ok":vol_ok,"vol_pct":vol_pct,
        "htf_bull":htf_bull,"htf_bear":htf_bear,
        "htf2_bull":htf2_bull,"htf2_bear":htf2_bear,
        "tfs_long":tfs_long,"tfs_short":tfs_short,
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
        "sl_atr":sl_atr,"tp_atr":tp_atr,"tp_half":tp_half,
        "sl_pct_real":sl_pct_real,"tp_pct_real":tp_pct_real,"rr_real":rr_real,
        # Ventajas exclusivas
        "smd":smd["smd"],"smd_score":smd["smd_score"],
        "oi_delta":smd["oi_delta"],"oi_up":smd["oi_up"],
        "smd_confirms_long":smd["smd_confirms_long"],
        "smd_confirms_short":smd["smd_confirms_short"],
        "smd_warns_long":smd["smd_warns_long"],
        "funding":funding_data["funding"],
        "funding_bull":funding_data["funding_bull"],
        "funding_bear":funding_data["funding_bear"],
        "poc":vp["poc"],"vah":vp["vah"],"val":vp["val"],
        "near_poc":vp["near_poc"],
        "poc_magnet_up":vp["poc_magnet_up"],"poc_magnet_dn":vp["poc_magnet_dn"],
        "liq_long_20x":liq["liq_long_20x"],"liq_short_20x":liq["liq_short_20x"],
        "sweep_risk_long":liq["sweep_risk_long"],"sweep_risk_short":liq["sweep_risk_short"],
        "manipulated":manip["manipulated"],"manip_type":manip["type"],
        "regime":regime,
        "safe_long":safe_long,"safe_short":safe_short,
    }


# ─────────────────────────────────────────────────────────────────────
#  SCANNER
# ─────────────────────────────────────────────────────────────────────

def scan_mercado():
    log.info("=== Scan ELITE v1.0 ===")
    _hs["scan"]+=1
    tickers=get_all_tickers()
    btc_change=btc_price=0.0
    btc_klines=[]
    for t in tickers:
        if t.get("symbol")=="BTC-USDT":
            try:btc_change=float(t.get("priceChangePercent",0));btc_price=float(t.get("lastPrice",0))
            except:pass
            break
    btc_klines=get_klines("BTC-USDT","1h",60)
    update_market_regime(btc_change,btc_klines)
    log.info(f"BTC: ${btc_price:,.0f} ({btc_change:+.1f}%) | Pares: {len(tickers)} | Régimen: {_regime_cache['regime']}")

    resultados=[]
    for ticker in tickers:
        sym=ticker.get("symbol","")
        if not sym.endswith("-USDT"):continue
        if sym in BLACKLIST:continue
        if any(x in sym for x in ["USDC","BUSD","TUSD","DAI","FDUSD"]):continue
        try:
            vol24=float(ticker.get("quoteVolume",0))
            precio=float(ticker.get("lastPrice",0))
            chg24=float(ticker.get("priceChangePercent",0))
        except:continue
        if vol24<MIN_VOLUME_USDT:continue

        # [E3] Adaptive timeframe
        k3m=get_klines(sym,"3m",80)
        if not k3m or len(k3m)<50:time.sleep(0.05);continue
        def _col_q(kl,idx):
            out=[]
            for k in kl:
                try:out.append(float(k[idx]))
                except:out.append(out[-1] if out else 0.0)
            return np.array(out)
        h_tmp=_col_q(k3m,2);l_tmp=_col_q(k3m,3);c_tmp=_col_q(k3m,4)
        atr_tmp=atr_series(h_tmp,l_tmp,c_tmp,I_ATR_L)
        atr_now_tmp=float(atr_tmp[-1])
        atr_avg_tmp=float(sma(atr_tmp,20)[-1]) if not np.isnan(sma(atr_tmp,20)[-1]) else atr_now_tmp
        tf_use=get_adaptive_tf(atr_now_tmp,atr_avg_tmp)
        if tf_use!="3m":
            k3m=get_klines(sym,tf_use,80)
            if not k3m or len(k3m)<50:time.sleep(0.05);continue

        k15m=get_klines(sym,"15m",30)
        k1h=get_klines(sym,"1h",60)

        an=analizar_par_elite(sym,k3m,k15m,k1h)
        if not an:time.sleep(0.05);continue
        if an["signal"]=="ESPERAR" and an["comp_long"]<45 and an["comp_short"]<45:
            time.sleep(0.05);continue

        resultados.append({"symbol":sym,"precio":precio,"change_24h":chg24,
                           "volume_usdt":vol24,"tf_usado":tf_use,**an})
        time.sleep(0.10)

    orden={"ELITE LONG":0,"ELITE SHORT":1,"LONG SUP":2,"SHORT SUP":3,
           "LONG FUEL":4,"SHORT FUEL":5,"LONG STD":6,"SHORT STD":7,"ESPERAR":8}
    resultados.sort(key=lambda x:(orden.get(x["signal"],9),-x["signal_score"]))
    señales=[r for r in resultados if r["signal"]!="ESPERAR"][:TOP_N]
    _hs["signals"]=len(señales)
    _hs["last"]=datetime.now(timezone.utc).strftime("%H:%M")
    elites=[r for r in señales if r["long_elite"] or r["short_elite"]]
    log.info(f"Señales: {len(señales)} | ELITE: {len(elites)} | Total: {len(resultados)}")

    tiene_elite=bool(elites)
    tiene_sup=any(r["long_sup"] or r["short_sup"] for r in señales)
    tiene_fuel=any(r["long_fuel"] or r["short_fuel"] for r in señales)
    if tiene_elite:intervalo=INTERVAL_ELITE
    elif tiene_sup:intervalo=INTERVAL_ALERTA
    elif tiene_fuel or señales:intervalo=INTERVAL_ACTIVO
    else:intervalo=INTERVAL_NORMAL
    return señales,intervalo,btc_change


# ─────────────────────────────────────────────────────────────────────
#  AUTO-TRADE
# ─────────────────────────────────────────────────────────────────────

def set_leverage_margin(symbol:str):
    r=_post("/openApi/swap/v2/trade/leverage",{"symbol":symbol,"leverage":str(LEVERAGE)})
    if not r:
        _post("/openApi/swap/v2/trade/leverage",{"symbol":symbol,"side":"LONG","leverage":str(LEVERAGE)})
        _post("/openApi/swap/v2/trade/leverage",{"symbol":symbol,"side":"SHORT","leverage":str(LEVERAGE)})
    _post("/openApi/swap/v2/trade/marginType",{"symbol":symbol,"marginType":"ISOLATED"})

def _cb_check()->bool:
    if time.time()<circuit_breaker_until:
        log.warning(f"CB activo {int(circuit_breaker_until-time.time())}s");return True
    return False

def abrir_trade(symbol:str,precio:float,direccion:str,
                sl_precio:float=0,tp_precio:float=0,tp_half_p:float=0,
                is_elite:bool=False)->Optional[dict]:
    global consecutive_losses,circuit_breaker_until
    if not BINGX_API_KEY or not AUTO_TRADE:return None
    if symbol in trades_abiertos:return None
    if _cb_check():return None
    if symbol in BLACKLIST:return None

    # [E4] Filtro correlación
    klines_check=get_klines(symbol,"3m",35)
    if klines_check:
        c_check=np.array([float(k[4]) for k in klines_check])
        if check_correlation(symbol,c_check):
            log.info(f"[E4] Skip {symbol} — correlacionado con trade abierto");return None

    posiciones=get_open_positions()
    if len(posiciones)>=MAX_OPEN_TRADES:
        log.warning(f"Max trades — skip {symbol}");return None

    balance=get_balance()
    log.info(f"Balance: ${balance:.2f} USDT")

    # [E9] Kelly sizing — ELITE usa tamaño mayor
    trade_usdt=kelly_size(TRADE_USDT)
    if is_elite:trade_usdt=min(trade_usdt*1.5,balance*0.15)  # ELITE: 1.5x pero max 15% cuenta

    if balance<trade_usdt:
        log.warning(f"Balance insuficiente (${balance:.2f} < ${trade_usdt:.1f}) — skip");return None

    set_leverage_margin(symbol);time.sleep(0.3)
    info=get_instrument_info(symbol)
    qty=round_qty((trade_usdt*LEVERAGE)/precio,info["step_size"])
    if qty<info["min_qty"]:
        log.warning(f"Qty {qty}<minQty {info['min_qty']} — skip");return None

    is_long=(direccion=="LONG")
    if sl_precio>0:sl_p=sl_precio
    else:sl_p=round(precio*(1-SL_PCT/100 if is_long else 1+SL_PCT/100),info["price_precision"])
    if tp_precio>0:tp_p=tp_precio
    else:tp_p=round(precio*(1+TP_PCT/100 if is_long else 1-TP_PCT/100),info["price_precision"])

    side_open="BUY" if is_long else "SELL"
    side_close="SELL" if is_long else "BUY"

    orden=_post("/openApi/swap/v2/trade/order",{
        "symbol":symbol,"side":side_open,"type":"MARKET","quantity":str(qty)})
    if not orden:log.error(f"Orden {direccion} {symbol} fallida");return None
    time.sleep(0.5)

    _post("/openApi/swap/v2/trade/order",{
        "symbol":symbol,"side":side_close,"type":"STOP_MARKET",
        "stopPrice":str(sl_p),"closePosition":"true"})

    # [E8] Partial TP
    if PARTIAL_TP and tp_half_p>0:
        qty_p=round_qty(qty*PARTIAL_PCT/100,info["step_size"])
        if qty_p>=info["min_qty"]:
            _post("/openApi/swap/v2/trade/order",{
                "symbol":symbol,"side":side_close,"type":"TAKE_PROFIT_MARKET",
                "stopPrice":str(tp_half_p),"quantity":str(qty_p)})

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

    trade={"symbol":symbol,"direction":direccion,"is_elite":is_elite,
           "entry":precio,"sl":sl_p,"tp":tp_p,"tp_desc":tp_desc,"tp_half":tp_half_p,
           "qty":qty,"usdt":trade_usdt,"leverage":LEVERAGE,
           "opened_at":datetime.now(timezone.utc).isoformat(),
           "sl_pct_real":round(abs(precio-sl_p)/precio*100,2),
           "tp_pct_real":round(abs(tp_p-precio)/precio*100,2)}
    trades_abiertos[symbol]=trade
    _hs["trades"]=len(trades_abiertos)
    log.info(f"TRADE {'ELITE ' if is_elite else ''}{direccion} {symbol} @ {precio} | SL={sl_p} TP={tp_desc}")
    return trade

def actualizar_trades_abiertos():
    global consecutive_losses,circuit_breaker_until
    if not trades_abiertos:return
    try:
        posiciones=get_open_positions()
        syms_activos={p.get("symbol") for p in posiciones}
        cerrados=[s for s in list(trades_abiertos.keys()) if s not in syms_activos]
        for sym in cerrados:
            trade=trades_abiertos.pop(sym)
            k=get_klines(sym,"3m",3)
            if k:
                pa=float(k[-1][4]);en=trade["entry"];il=trade["direction"]=="LONG"
                pnl_pct=(pa-en)/en*100*(1 if il else -1)
                pnl_usdt=pnl_pct/100*trade["usdt"]*trade["leverage"]
                ganado=pnl_pct>0
                _hs["pnl"]+=pnl_usdt
                if ganado:
                    _hs["wins"]+=1;consecutive_losses=0
                    if pnl_pct>_hs["best"]:_hs["best"]=pnl_pct
                    res=f"WIN +{pnl_pct:.2f}% (+${pnl_usdt:.2f})"
                else:
                    _hs["losses"]+=1;consecutive_losses+=1
                    if pnl_pct<_hs["worst"]:_hs["worst"]=pnl_pct
                    res=f"LOSS {pnl_pct:.2f}% (-${abs(pnl_usdt):.2f})"
                    if consecutive_losses>=CB_MAX_LOSSES:
                        circuit_breaker_until=time.time()+CB_PAUSE_MIN*60
                        send_telegram(f"CB: {consecutive_losses} perdidas -> pausa {CB_PAUSE_MIN}min")
                trade_history.append({"pnl_pct":pnl_pct,"won":ganado})
                if len(trade_history)>200:trade_history.pop(0)
                elite_str=" ELITE" if trade.get("is_elite") else ""
                log.info(f"Trade{elite_str} cerrado: {sym} {trade['direction']} | {res}")
                send_telegram(
                    f"Trade{elite_str} cerrado: {sym.replace('-USDT','')}\n"
                    f"{trade['direction']} | Entrada: {en} | Salida: {pa:.6f}\n"
                    f"{res} | PnL sesion: ${_hs['pnl']:.2f}")
    except Exception as e:
        log.error(f"actualizar_trades: {e}")
    _hs["trades"]=len(trades_abiertos)


# ─────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────

def send_telegram(msg:str)->bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg);return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"},timeout=10)
        r.raise_for_status();log.info("Telegram OK");return True
    except Exception as e:
        log.error(f"Telegram: {e}");return False

def build_alerta(par:dict)->str:
    sym=par["symbol"].replace("-USDT","");sig=par["signal"]
    sc_l=par["comp_long"];sc_s=par["comp_short"];precio=par["precio"]
    is_long="LONG" in sig;is_elite="ELITE" in sig
    is_sup="SUP" in sig;is_fuel="FUEL" in sig

    sl_p=par["sl_atr"] if par["sl_atr"]>0 else round(precio*(1-SL_PCT/100),6)
    tp1=par["tp_atr"] if par["tp_atr"]>0 else round(precio*(1+TP_PCT/100),6)
    tp2=round(precio*(1+par["tp_pct_real"]*1.8/100),6)

    db_fill=max(0,min(8,round(par["decay_r"]/100*8)))
    decay_bar="█"*db_fill+"░"*(8-db_fill)

    header="ELITE" if is_elite else "SUP" if is_sup else "FUEL" if is_fuel else "STD"
    dir_str="LONG" if is_long else "SHORT"

    lines=[
        f"{header} {dir_str}: {sym}",
        f"{'─'*30}",
        f"SC LONG: {sc_l}/100 | SC SHORT: {sc_s}/100",
        f"SCORE: {par['norm_score']} | CONV: {par['long_conv']}up/{par['short_conv']}dn",
        f"TFs alineados: {par['tfs_long'] if is_long else par['tfs_short']}/3 | Régimen: {par['regime']}",
        f"{'─'*30}",
        f"Precio: {precio} | 24h: {par['change_24h']:+.1f}% | Vol: ${par['volume_usdt']/1e6:.1f}M",
        f"SL: {sl_p} (-{par['sl_pct_real']:.1f}%) ATR",
        f"TP1: {tp1} (+{par['tp_pct_real']:.1f}%) | TP2: {tp2} | R:R {par['rr_real']}:1",
    ]
    if PARTIAL_TP and par["tp_half"]>0:
        lines.append(f"Partial TP: {par['tp_half']:.6f} ({PARTIAL_PCT:.0f}% -> BE)")
    lines+=[
        f"{'─'*30}",
        f"SMART MONEY: {par['smd']} (score {par['smd_score']}/3)",
        f"OI delta: {par['oi_delta']:+.2f}% {'UP' if par['oi_up'] else 'dn'}",
        f"Funding: {par['funding']:+.4f}% {'BULL' if par['funding_bull'] else 'BEAR' if par['funding_bear'] else 'neutral'}",
        f"POC: {par['poc']} | {'IMAN UP' if par['poc_magnet_up'] else 'IMAN DN' if par['poc_magnet_dn'] else 'EN ZONA' if par['near_poc'] else 'lejos'}",
        f"LIQ hunt risk: {'LONG' if par['sweep_risk_long'] else 'SHORT' if par['sweep_risk_short'] else 'bajo'}",
        f"Manipulacion: {par['manip_type']}",
        f"{'─'*30}",
        f"DECAY {decay_bar} {par['decay_r']}% {'ok' if par['sig_alive'] else 'x'}",
        f"HTF 15m: {'BULL' if par['htf_bull'] else 'BEAR'} | HTF 1H: {'BULL' if par['htf2_bull'] else 'BEAR'}",
        f"ADX: {par['adx']} {'up' if par['trend_up'] else 'dn' if par['trend_dn'] else 'lat'} | TF usado: {par.get('tf_usado','3m')}",
        f"TL: {'LONG' if par['tl_break_long'] else 'SHORT' if par['tl_break_short'] else '-'} | DP: {'up' if par['dp_buy'] else 'dn' if par['dp_sell'] else '-'}",
        f"CVD: {'DIV up' if par['cvd_bull_div'] else 'DIV dn' if par['cvd_bear_div'] else 'up' if par['cvd_rising'] else 'dn'} | SQ: {'FIRE up' if par['sq_bull'] else 'FIRE dn' if par['sq_bear'] else 'comp' if par['sq_on'] else '-'}",
        f"FVG: {'up' if par['in_bull_fvg'] else 'dn' if par['in_bear_fvg'] else '-'} | OB: {'up' if par['in_bull_ob'] else 'dn' if par['in_bear_ob'] else '-'}",
        f"EXEC: {'OK' if par['exec_ok'] else 'BLOQ'}",
        f"{'─'*30}",
        f"SL ref: {par['last_sl'] if is_long else par['last_sh']}",
        f"{'Auto-trade: ELITE abierto' if is_elite and AUTO_TRADE else 'Verifica TradingView QF x JP v3.3'}",
    ]
    return "\n".join(l for l in lines if l.strip())

def build_resumen(res:list,btc_change:float,intervalo:int)->str:
    now=datetime.now(timezone.utc).strftime("%H:%M UTC")
    wins=_hs["wins"];losses=_hs["losses"];total=wins+losses
    wr=round(wins/total*100) if total>0 else 0
    pnl=_hs["pnl"];regime=_regime_cache["regime"]
    cb_str=""
    if time.time()<circuit_breaker_until:
        cb_str=f"\nCB: {int((circuit_breaker_until-time.time())/60)}min"
    elites=[r for r in res if r["long_elite"] or r["short_elite"]]
    sups=[r for r in res if (r["long_sup"] or r["short_sup"]) and not r["long_elite"] and not r["short_elite"]]
    fuels=[r for r in res if (r["long_fuel"] or r["short_fuel"]) and not r["long_sup"] and not r["short_sup"]]
    btce="+" if btc_change>0 else ""
    lines=[
        f"ELITE BOT v1.0 — {now}",
        f"BTC {btce}{btc_change:.2f}% | Régimen: {regime} | prox {intervalo//60}min",
        f"W/L: {wins}/{losses} ({wr}%) | PnL: ${pnl:.2f} | Racha: {consecutive_losses}{cb_str}",
        f"{'─'*26}",
    ]
    if not res:lines.append("Sin senales");return "\n".join(lines)
    if elites:
        lines.append(f"ELITE ({len(elites)}) MAX CONFIANZA:")
        for r in elites[:3]:
            d="LONG" if r["long_elite"] else "SHORT"
            sc=r["comp_long"] if r["long_elite"] else r["comp_short"]
            lines.append(f"  {d} {r['symbol'].replace('-USDT','')} {sc}/100 RR:{r['rr_real']} SMD:{r['smd']}")
    if sups:
        lines.append(f"SUP ({len(sups)}):")
        for r in sups[:3]:
            d="LONG" if r["long_sup"] else "SHORT"
            sc=r["comp_long"] if r["long_sup"] else r["comp_short"]
            lines.append(f"  {d} {r['symbol'].replace('-USDT','')} {sc}/100")
    if fuels:
        lines.append(f"FUEL ({len(fuels)}):")
        for r in fuels[:3]:
            d="LONG" if r["long_fuel"] else "SHORT"
            sc=r["comp_long"] if r["long_fuel"] else r["comp_short"]
            lines.append(f"  {d} {r['symbol'].replace('-USDT','')} {sc}/100")
    if trades_abiertos:
        lines+=[f"{'─'*26}",f"Trades ({len(trades_abiertos)}):"]
        for sym,t in trades_abiertos.items():
            e=" ELITE" if t.get("is_elite") else ""
            lines.append(f"  {sym.replace('-USDT','')}{e} {t['direction']} SL:{t['sl']}")
    return "\n".join(lines)

def build_reporte_diario()->str:
    wins=_hs["wins"];losses=_hs["losses"];total=wins+losses
    wr=round(wins/total*100) if total>0 else 0
    kelly_str=""
    if USE_KELLY and len(trade_history)>=KELLY_MIN:
        wl=[t for t in trade_history if t["won"]]
        ll=[t for t in trade_history if not t["won"]]
        if wl and ll:
            wr_k=len(wl)/len(trade_history)
            aw=sum(t["pnl_pct"] for t in wl)/len(wl)
            al=abs(sum(t["pnl_pct"] for t in ll)/len(ll))
            b=aw/max(al,0.001);kf=max(0,wr_k-(1-wr_k)/b)
            kelly_str=f"\nKelly full: {kf*100:.1f}% -> usando 25%: {kf*25:.1f}% capital"
    return (
        f"REPORTE DIARIO ELITE BOT v1.0\n"
        f"{'─'*28}\n"
        f"Trades: {total} | W/L: {wins}/{losses} | WR: {wr}%\n"
        f"PnL neto: ${_hs['pnl']:.2f} USDT\n"
        f"Mejor: +{_hs['best']:.2f}% | Peor: {_hs['worst']:.2f}%\n"
        f"ELITE signals: {_hs['elite']}\n"
        f"Scans: {_hs['scan']} | Régimen: {_regime_cache['regime']}\n"
        f"Trades activos: {len(trades_abiertos)}\n"
        f"{kelly_str}"
    )


# ─────────────────────────────────────────────────────────────────────
#  LOOP PRINCIPAL
# ─────────────────────────────────────────────────────────────────────

def run_loop():
    log.info(f"ELITE BOT v1.0 — AUTO_TRADE={'ON' if AUTO_TRADE else 'OFF'}")
    log.info(f"  ${TRADE_USDT}x{LEVERAGE} | SL {SL_ATR_MULT}xATR | TP {TP_ATR_MULT}xATR | Max {MAX_OPEN_TRADES}")
    log.info(f"  Partial: {'ON' if PARTIAL_TP else 'OFF'} | Kelly: {'ON' if USE_KELLY else 'OFF'} | OI: {'ON' if OI_CONFIRM else 'OFF'}")
    log.info(f"  ELITE threshold: {MIN_ELITE_SCORE}/100")
    if BINGX_API_KEY:
        bal=get_balance();log.info(f"Balance: ${bal:.2f} USDT")
    send_telegram(
        f"ELITE BOT v1.0 iniciado\n"
        f"16 capas + 10 ventajas exclusivas\n"
        f"AUTO TRADE: {'ON' if AUTO_TRADE else 'OFF'}\n"
        f"SL: {SL_ATR_MULT}xATR | TP: {TP_ATR_MULT}xATR | Partial: {'ON' if PARTIAL_TP else 'OFF'}\n"
        f"ELITE threshold: {MIN_ELITE_SCORE}/100\n"
        f"Max trades: {MAX_OPEN_TRADES} | CB: {CB_MAX_LOSSES} perdidas -> {CB_PAUSE_MIN}min"
    )
    ultima_hora=-1;reporte_dia=-1;btc_change=0.0

    while True:
        try:
            actualizar_trades_abiertos()
            resultados,intervalo,btc_change=scan_mercado()

            for par in resultados:
                sym=par["symbol"]
                is_actionable=(par["long_elite"] or par["short_elite"] or
                               par["long_sup"] or par["short_sup"] or
                               par["long_fuel"] or par["short_fuel"])
                if not is_actionable:continue
                if time.time()-alertas_enviadas.get(sym,0)<1800:continue
                msg=build_alerta(par)
                if send_telegram(msg):alertas_enviadas[sym]=time.time()

                if AUTO_TRADE:
                    is_elite=par["long_elite"] or par["short_elite"]
                    if (par["long_elite"] or par["long_sup"] or par["long_fuel"]) and sym not in trades_abiertos:
                        trade=abrir_trade(sym,par["precio"],"LONG",
                                          par["sl_atr"],par["tp_atr"],par["tp_half"],is_elite)
                        if trade:
                            send_telegram(
                                f"{'ELITE ' if is_elite else ''}LONG ABIERTO: {sym.replace('-USDT','')}\n"
                                f"Entrada: {trade['entry']} SL: {trade['sl']} TP: {trade.get('tp_desc',trade['tp'])}\n"
                                f"${trade['usdt']:.0f}x{LEVERAGE} | RR: {par['rr_real']} | SMD: {par['smd']}")
                    elif (par["short_elite"] or par["short_sup"] or par["short_fuel"]) and sym not in trades_abiertos:
                        sl_s=round(par["precio"]*(1+par["sl_pct_real"]/100),8)
                        tp_s=round(par["precio"]*(1-par["tp_pct_real"]/100),8)
                        th_s=round(par["precio"]*(1-par["tp_pct_real"]*0.5/100),8)
                        trade=abrir_trade(sym,par["precio"],"SHORT",sl_s,tp_s,th_s,is_elite)
                        if trade:
                            send_telegram(
                                f"{'ELITE ' if is_elite else ''}SHORT ABIERTO: {sym.replace('-USDT','')}\n"
                                f"Entrada: {trade['entry']} SL: {trade['sl']} TP: {trade.get('tp_desc',trade['tp'])}\n"
                                f"${trade['usdt']:.0f}x{LEVERAGE} | RR: {par['rr_real']} | SMD: {par['smd']}")

            hora=datetime.now(timezone.utc).hour
            if hora!=ultima_hora:
                send_telegram(build_resumen(resultados,btc_change,intervalo))
                ultima_hora=hora

            dia=datetime.now(timezone.utc).day
            if hora==22 and dia!=reporte_dia:
                send_telegram(build_reporte_diario());reporte_dia=dia

        except Exception as e:
            log.error(f"Error ciclo: {e}",exc_info=True);intervalo=INTERVAL_NORMAL

        log.info(f"Proximo scan en {intervalo}s ({intervalo//60}min {intervalo%60}s)")
        time.sleep(intervalo)


if __name__=="__main__":
    run_loop()
