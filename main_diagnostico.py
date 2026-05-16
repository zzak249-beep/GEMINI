"""
MODO DIAGNÓSTICO — reemplaza main.py temporalmente
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Descarga klines reales de BingX y corre la estrategia.
NO abre órdenes. Imprime exactamente por qué cada par
no genera señal. Despliega en Railway, lee los logs,
luego restaura el main.py original.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import sys
import time
import urllib.parse
import os
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("diag")

# ── Config desde env ──────────────────────────────────────────────────
API_KEY    = os.getenv("BINGX_API_KEY", "")
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL   = "https://open-api.bingx.com"
PORT       = int(os.getenv("PORT", "8080"))

PAIRS = [
    "BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT",
    "XRP-USDT","DOGE-USDT","ADA-USDT","AVAX-USDT",
    "LINK-USDT","DOT-USDT","LTC-USDT","BCH-USDT",
]

# ── Indicadores ───────────────────────────────────────────────────────
def calc_atr(H, L, C, p=14):
    if len(C) < p+1: return 0.0
    tr = np.maximum(H[1:]-L[1:], np.maximum(np.abs(H[1:]-C[:-1]), np.abs(L[1:]-C[:-1])))
    v = np.mean(tr[:p])
    for i in range(p, len(tr)): v = (v*(p-1)+tr[i])/p
    return float(v)

def calc_ema(arr, p):
    if len(arr) < p: return np.zeros(len(arr))
    k = 2.0/(p+1); r = np.zeros(len(arr))
    r[p-1] = np.mean(arr[:p])
    for i in range(p, len(arr)): r[i] = arr[i]*k + r[i-1]*(1-k)
    return r

def calc_adx(H, L, C, p=14):
    if len(C) < p*2+2: return 0.0
    tr  = np.maximum(H[1:]-L[1:], np.maximum(np.abs(H[1:]-C[:-1]), np.abs(L[1:]-C[:-1])))
    pdm = np.where((H[1:]-H[:-1])>(L[:-1]-L[1:]), np.maximum(H[1:]-H[:-1],0.0), 0.0)
    mdm = np.where((L[:-1]-L[1:])>(H[1:]-H[:-1]), np.maximum(L[:-1]-L[1:],0.0), 0.0)
    def _s(a):
        s=np.zeros(len(a))
        if len(a)<p: return s
        s[p-1]=np.sum(a[:p])
        for i in range(p,len(a)): s[i]=s[i-1]-s[i-1]/p+a[i]
        return s
    atr_s=_s(tr); pdm_s=_s(pdm); mdm_s=_s(mdm)
    pdi=100*pdm_s/(atr_s+1e-10); mdi=100*mdm_s/(atr_s+1e-10)
    dx=100*np.abs(pdi-mdi)/(pdi+mdi+1e-10)
    adx=np.zeros(len(dx))
    if len(dx)>=p:
        adx[p-1]=np.mean(dx[:p])
        for i in range(p,len(dx)): adx[i]=(adx[i-1]*(p-1)+dx[i])/p
    return float(adx[-1])

def find_pivots(H, L, pl=5):
    ph=[]; plo=[]; n=len(H)
    for i in range(pl, n-pl):
        if H[i] >= np.max(H[i-pl:i+pl+1]): ph.append(float(H[i]))
        if L[i] <= np.min(L[i-pl:i+pl+1]): plo.append(float(L[i]))
    return ph, plo

def pip_size(price):
    if price >= 10_000: return 1.0
    if price >= 1_000:  return 0.1
    if price >= 100:    return 0.01
    if price >= 1:      return 0.001
    if price >= 0.1:    return 0.0001
    return 0.00001

def parse_klines(raw):
    opens=[]; highs=[]; lows=[]; closes=[]; vols=[]
    for k in raw:
        try:
            if isinstance(k, dict):
                o=float(k.get("open",0)); h=float(k.get("high",0))
                l=float(k.get("low",0));  c=float(k.get("close",0))
                v=float(k.get("volume",0))
            elif isinstance(k,(list,tuple)) and len(k)>=6:
                o,h,l,c,v=float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5])
            else: continue
            if h<l or c<=0: continue
            opens.append(o); highs.append(h); lows.append(l)
            closes.append(c); vols.append(v)
        except: continue
    return (np.array(opens), np.array(highs), np.array(lows),
            np.array(closes), np.array(vols))

# ── BingX client mínimo ───────────────────────────────────────────────
def sign(params, secret):
    qs = "&".join(f"{k}={urllib.parse.quote(str(params[k]),safe='')}"
                  for k in sorted(params.keys()))
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

async def get_klines(session, symbol, interval="3m", limit=120):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with session.get(f"{BASE_URL}/openApi/swap/v3/quote/klines",
                           params=params) as r:
        data = await r.json(content_type=None)
        return data.get("data", []) or []

async def get_balance(session):
    params = {"timestamp": int(time.time()*1000)}
    params["signature"] = sign(params, SECRET_KEY)
    headers = {"X-BX-APIKEY": API_KEY}
    async with session.get(f"{BASE_URL}/openApi/swap/v2/user/balance",
                           params=params, headers=headers) as r:
        data = await r.json(content_type=None)
    try:
        d = data.get("data",{})
        b = d.get("balance",{}) if isinstance(d,dict) else {}
        return float(b.get("availableMargin", d.get("availableMargin",0)))
    except: return 0.0

# ── Análisis de señal con diagnóstico completo ────────────────────────
def analyze(symbol, opens, highs, lows, closes, vols):
    H=highs[:-1]; L=lows[:-1]; C=closes[:-1]; V=vols[:-1]
    if len(C)<30:
        return f"  ✗ Pocas velas: {len(C)}"

    atr = calc_atr(H,L,C,14)
    if atr==0: return "  ✗ ATR=0"

    adx = calc_adx(H,L,C,14)

    ema_f = calc_ema(C,7)
    ema_m = calc_ema(C,17)
    ext_up   = ema_f[-1] > ema_m[-1]   # sobreextendido arriba → SHORT
    ext_down = ema_f[-1] < ema_m[-1]   # sobreextendido abajo  → LONG

    ph, pl = find_pivots(H,L,5)
    if not ph or not pl:
        return f"  ✗ Sin pivots (ph={len(ph)} pl={len(pl)})"

    green = ph[-1]; red = pl[-1]
    if green <= red:
        return f"  ✗ Canal inválido: green={green:.5g} <= red={red:.5g}"

    close = C[-1]
    pip   = pip_size(close)
    short_off = max(20*pip, atr*0.3)
    long_off  = max(15*pip, atr*0.3)
    short_t   = green + short_off
    long_t    = red   - long_off

    canal_pct = (green-red)/close*100

    reasons = []
    signal = None

    # SHORT check
    if close >= short_t:
        if not ext_up:
            reasons.append(f"SHORT precio✓ pero EMA no sobreextendida (ema_f={ema_f[-1]:.5g} ema_m={ema_m[-1]:.5g})")
        else:
            sl=close+atr*1.5; tp=red
            if tp>=close: reasons.append("SHORT TP>=close")
            else:
                rr=abs(tp-close)/max(abs(sl-close),1e-10)
                if rr<1.0: reasons.append(f"SHORT RR={rr:.2f}<1.0")
                else: signal=f"🔴 SHORT RR=1:{rr:.2f} ADX={adx:.1f}"
    else:
        dist_short = short_t - close
        reasons.append(f"SHORT: precio={close:.5g} necesita {short_t:.5g} (faltan {dist_short:.5g} = {dist_short/atr:.2f}×ATR)")

    # LONG check
    if close <= long_t:
        if not ext_down:
            reasons.append(f"LONG precio✓ pero EMA no sobreextendida (ema_f={ema_f[-1]:.5g} ema_m={ema_m[-1]:.5g})")
        else:
            sl=close-atr*1.5; tp=green
            if tp<=close: reasons.append("LONG TP<=close")
            else:
                rr=abs(tp-close)/max(abs(close-sl),1e-10)
                if rr<1.0: reasons.append(f"LONG RR={rr:.2f}<1.0")
                else: signal=f"🟢 LONG RR=1:{rr:.2f} ADX={adx:.1f}"
    else:
        dist_long = close - long_t
        reasons.append(f"LONG:  precio={close:.5g} necesita {long_t:.5g} (sobran {dist_long:.5g} = {dist_long/atr:.2f}×ATR)")

    if signal:
        return f"  ✅ {signal} | canal={canal_pct:.2f}% ATR={atr:.5g}"

    summary = f"  ADX={adx:.1f} ATR={atr:.5g} canal={canal_pct:.2f}% ext_up={ext_up} ext_down={ext_down}\n"
    summary += "\n".join(f"    → {r}" for r in reasons)
    return summary

# ── Health server ─────────────────────────────────────────────────────
async def health(request):
    return web.Response(text="DIAG OK")

async def run_health():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# ── Main ──────────────────────────────────────────────────────────────
async def main():
    await run_health()
    log.info(f"🔍 MODO DIAGNÓSTICO — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    log.info(f"API_KEY configurada: {'✅ SÍ' if API_KEY else '❌ NO'}")

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:

        # Balance
        bal = await get_balance(session)
        log.info(f"💵 Balance: {bal:.4f} USDT")
        if bal == 0:
            log.error("❌ Balance=0 — verifica BINGX_API_KEY y BINGX_SECRET_KEY")

        cycle = 0
        while True:
            cycle += 1
            log.info(f"\n{'━'*55}")
            log.info(f"CICLO #{cycle} — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
            log.info(f"{'━'*55}")

            signals_found = 0
            for sym in PAIRS:
                try:
                    raw = await get_klines(session, sym, "3m", 120)
                    if not raw or len(raw) < 50:
                        log.info(f"[{sym}] ✗ Sin datos ({len(raw) if raw else 0} velas)")
                        continue
                    opens, highs, lows, closes, vols = parse_klines(raw)
                    if len(closes) < 50:
                        log.info(f"[{sym}] ✗ Pocas velas parseadas: {len(closes)}")
                        continue
                    result = analyze(sym, opens, highs, lows, closes, vols)
                    log.info(f"[{sym}]\n{result}")
                    if "✅" in result:
                        signals_found += 1
                except Exception as e:
                    log.error(f"[{sym}] ERROR: {e}")
                await asyncio.sleep(0.3)

            log.info(f"\n📊 RESUMEN CICLO #{cycle}: {signals_found} señales encontradas")
            if signals_found == 0:
                log.info("⚠️  Sin señales — mercado dentro de canales o EMA no alineada")
            log.info(f"Próximo ciclo en 60s...\n")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
