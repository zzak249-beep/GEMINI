"""
QF×JP Bot v6.5 — Indicators PREDATOR EDITION
Mejoras de velocidad y anticipación basadas en investigación:
  1. Funding Rate como filtro de sesgo SHORT/LONG
  2. StochRSI (señal más rápida que RSI clásico)
  3. VWAP Deviation — entrada en la media real del día
  4. Order Book Imbalance — presión compradora/vendedora
  5. Trailing exit dinámico con ATR
  6. Score ajustado a historial SHORT ganador
"""
import warnings
import math
import numpy as np
warnings.filterwarnings("ignore", category=RuntimeWarning)
from dataclasses import dataclass


@dataclass
class Signal:
    symbol: str
    direction: str
    score: float
    tier: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    atr: float
    adx: float
    mfi: float
    vdi: float
    cvd: float
    momentum: float
    htf_score: float
    structure: str
    tl_break: str
    stoch_rsi: float = 0.0
    funding_bias: str = "NEUTRAL"   # LONG_CROWDED | SHORT_CROWDED | NEUTRAL
    tl_break_active: bool = False
    circuit_breaker: bool = False
    reason: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(v, d=0.0):
    try:
        f = float(v)
        return f if math.isfinite(f) else d
    except Exception:
        return d

def _ema(arr, period):
    k = 2.0 / (period + 1)
    out = np.empty(len(arr), dtype=float)
    out[0] = float(arr[0])
    for i in range(1, len(arr)):
        out[i] = float(arr[i]) * k + out[i-1] * (1-k)
    return out

def _rma(arr, period):
    k = 1.0 / period
    out = np.empty(len(arr), dtype=float)
    out[0] = float(arr[0])
    for i in range(1, len(arr)):
        out[i] = float(arr[i]) * k + out[i-1] * (1-k)
    return out

def _sma(arr, period):
    out = np.full(len(arr), np.nan, dtype=float)
    for i in range(period-1, len(arr)):
        out[i] = arr[i-period+1:i+1].mean()
    return out

def _div(a, b, out=None):
    """División segura sin RuntimeWarning."""
    b_arr = np.asarray(b, dtype=float)
    a_arr = np.asarray(a, dtype=float)
    default = np.zeros_like(a_arr) if out is None else out
    return np.divide(a_arr, b_arr, out=default.copy(), where=b_arr != 0)

# ── ATR ───────────────────────────────────────────────────────────────────────

def calc_atr(h, l, c, period=10):
    h, l, c = np.asarray(h,float), np.asarray(l,float), np.asarray(c,float)
    tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    return _rma(np.concatenate([[tr[0]], tr]), period)

# ── ADX ───────────────────────────────────────────────────────────────────────

def calc_adx(h, l, c, period=14):
    h, l, c = np.asarray(h,float), np.asarray(l,float), np.asarray(c,float)
    up, down = h[1:]-h[:-1], l[:-1]-l[1:]
    pdm = np.where((up>down)&(up>0), up, 0.0)
    mdm = np.where((down>up)&(down>0), down, 0.0)
    tr  = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    pdm = np.concatenate([[0.0], pdm])
    mdm = np.concatenate([[0.0], mdm])
    tr  = np.concatenate([[tr[0]], tr])
    atr = _rma(tr, period)
    sa  = np.where(atr>1e-12, atr, 1e-12)
    pdi = 100 * _div(_rma(pdm,period), sa)
    mdi = 100 * _div(_rma(mdm,period), sa)
    dx  = 100 * _div(np.abs(pdi-mdi), pdi+mdi)
    return _rma(dx, period), pdi, mdi

# ── CVD ───────────────────────────────────────────────────────────────────────

def calc_cvd(o, c, v):
    o,c,v = np.asarray(o,float), np.asarray(c,float), np.asarray(v,float)
    bull = np.where(c>o, v, 0.0)
    bear = np.where(c<=o, v, 0.0)
    return _ema(_div(bull-bear, bull+bear), 5)

# ── Momentum ──────────────────────────────────────────────────────────────────

def calc_momentum(c, period=10):
    c = np.asarray(c, float)
    mom = np.zeros_like(c)
    for i in range(period, len(c)):
        d = c[i-period] if c[i-period] != 0 else 1e-9
        mom[i] = (c[i]-c[i-period]) / d
    return mom

# ── OBV ───────────────────────────────────────────────────────────────────────

def calc_obv(c, v):
    c,v = np.asarray(c,float), np.asarray(v,float)
    return np.cumsum(np.concatenate([[0], np.sign(np.diff(c))]) * v)

# ── MFI ───────────────────────────────────────────────────────────────────────

def calc_mfi(h, l, c, v, period=14):
    h,l,c,v = (np.asarray(x,float) for x in (h,l,c,v))
    tp = (h+l+c)/3
    mf = tp*v
    out = np.full_like(c, 50.0)
    for i in range(period, len(c)):
        sl, slp = slice(i-period+1,i+1), slice(i-period,i)
        up = tp[sl] > tp[slp]
        pos = mf[sl][up].sum()
        neg = mf[sl][~up].sum()
        out[i] = 100.0 if neg == 0 else 100 - 100/(1+pos/(neg+1e-12))
    return out

# ── VDI ───────────────────────────────────────────────────────────────────────

def calc_vdi(c, v, period=20):
    c,v = np.asarray(c,float), np.asarray(v,float)
    delta = (c - _sma(c,period)) * v
    std = np.nanstd(delta[-period:])
    return np.nan_to_num(_div(delta, std+1e-9), nan=0.0, posinf=0.0, neginf=0.0)

# ── StochRSI — MÁS RÁPIDO QUE RSI CLÁSICO ────────────────────────────────────

def calc_stoch_rsi(c, rsi_period=14, stoch_period=14, smooth_k=3, smooth_d=3):
    """
    StochRSI: aplica Stochastic al RSI.
    Más sensible que RSI puro → señales más tempranas.
    Retorna (k, d) — valores 0-100.
    """
    c = np.asarray(c, float)
    # RSI
    delta = np.diff(c)
    gain  = np.where(delta>0, delta, 0.0)
    loss  = np.where(delta<0, -delta, 0.0)
    avg_gain = _rma(np.concatenate([[gain[:rsi_period].mean()], gain]), rsi_period)
    avg_loss = _rma(np.concatenate([[loss[:rsi_period].mean()], loss]), rsi_period)
    rs  = _div(avg_gain, np.where(avg_loss>0, avg_loss, 1e-9))
    rsi = 100 - _div(100, 1+rs)

    # Stochastic del RSI
    stoch_k = np.full_like(rsi, 50.0)
    for i in range(stoch_period, len(rsi)):
        window = rsi[i-stoch_period:i+1]
        mn, mx = window.min(), window.max()
        stoch_k[i] = 100*(rsi[i]-mn)/(mx-mn+1e-9) if mx > mn else 50.0

    k = _ema(stoch_k, smooth_k)
    d = _ema(k,       smooth_d)
    return k, d

# ── VWAP Deviation — ENTRADA ÓPTIMA RESPECTO A MEDIA REAL ────────────────────

def calc_vwap_dev(h, l, c, v, period=20):
    """
    Desviación del precio respecto al VWAP del período.
    > 0 → precio por encima (caro) → SHORT bias
    < 0 → precio por debajo (barato) → LONG bias
    Retorna desviación normalizada en ATR.
    """
    h,l,c,v = (np.asarray(x,float) for x in (h,l,c,v))
    tp = (h+l+c)/3
    vwap = _div(
        np.array([np.sum(tp[max(0,i-period):i+1]*v[max(0,i-period):i+1])
                  for i in range(len(tp))]),
        np.array([np.sum(v[max(0,i-period):i+1])
                  for i in range(len(v))]) + 1e-9
    )
    atr = calc_atr(h, l, c, period)
    dev = _div(c - vwap, atr + 1e-9)
    return np.nan_to_num(dev, nan=0.0, posinf=0.0, neginf=0.0)

# ── Estructura ────────────────────────────────────────────────────────────────

def detect_structure(h, l, c, lookback=5):
    h,l,c = np.asarray(h,float), np.asarray(l,float), np.asarray(c,float)
    if len(h) < lookback*2+5:
        return "NONE"
    ph = h[-lookback*2-1:-lookback-1].max()
    pl = l[-lookback*2-1:-lookback-1].min()
    ch = h[-lookback-1:].max()
    cl = l[-lookback-1:].min()
    cc, pc = c[-1], c[-lookback-2]
    if cc > ph and ch > ph: return "BoS↑" if pc > pl else "CHoCH↑"
    if cc < pl and cl < pl: return "BoS↓" if pc < ph else "CHoCH↓"
    return "NONE"

# ── TL Ruptura ────────────────────────────────────────────────────────────────

def detect_tl_break(h, l, c, lookback=20):
    h,l,c = np.asarray(h,float), np.asarray(l,float), np.asarray(c,float)
    if len(h) < lookback+5:
        return "NONE"
    hh, ll = h[-lookback:], l[-lookback:]
    bear_now = hh[0] + (hh[-2]-hh[0])/lookback*(lookback-1)
    bull_now = ll[0] + (ll[-2]-ll[0])/lookback*(lookback-1)
    if c[-1] > bear_now and c[-2] <= bear_now: return "LONG"
    if c[-1] < bull_now and c[-2] >= bull_now: return "SHORT"
    return "NONE"

# ── FVG ───────────────────────────────────────────────────────────────────────

def detect_fvg(h, l):
    h,l = np.asarray(h,float), np.asarray(l,float)
    for i in range(len(h)-1, max(len(h)-6,1), -1):
        if l[i] > h[i-2]: return "BULL"
        if h[i] < l[i-2]: return "BEAR"
    return "NONE"

# ── Circuit Breaker ───────────────────────────────────────────────────────────

def check_circuit_breaker(h, l, atr, mult=3.0, bars=10):
    h,l = np.asarray(h,float), np.asarray(l,float)
    for i in range(len(h)-1, max(len(h)-bars-1,0), -1):
        if atr[i] > 0 and (h[i]-l[i]) > mult*atr[i]:
            return True
    return False

# ── HTF Score ─────────────────────────────────────────────────────────────────

def htf_score(k15m, k1h, k4h):
    scores, weights = [], []
    for kl, w in [(k15m,1),(k1h,2),(k4h,4)]:
        if len(kl) < 30: continue
        a  = np.asarray(kl, dtype=float)
        cc = a[:,4]
        e20 = _ema(cc, 20)
        e50 = _ema(cc, 50) if len(cc)>=50 else e20
        trend = 1 if e20[-1] > e50[-1] else -1
        mom = _safe(calc_momentum(cc,10)[-1])
        scores.append((0.5+0.5*trend*min(abs(mom)*10,1.0))*w)
        weights.append(w)
    return sum(scores)/sum(weights) if weights else 0.5

# ── Score compuesto v6.5 ──────────────────────────────────────────────────────
# Nuevas fuentes: StochRSI + VWAP + funding bias

def composite_score(direction, adx, cvd, momentum, mfi, vdi,
                    structure, tl_break, htf_s, fvg,
                    stoch_k=50.0, vwap_dev=0.0, funding_bias="NEUTRAL",
                    obi=0.0):
    s = 0.0
    d = direction

    # ADX (18 pts)
    s += min(_safe(adx)/40.0, 1.0) * 18

    # CVD (18 pts) — confirmación de volumen direccional
    cvd_v = _safe(cvd)
    s += (max(0.0, min(-cvd_v,1.0)) if d=="SHORT" else max(0.0, min(cvd_v,1.0))) * 18

    # StochRSI (12 pts) — señal rápida sin lag
    sk = _safe(stoch_k, 50.0)
    if d == "SHORT":
        # Queremos StochRSI > 80 (overbought → caída inminente)
        s += max(0.0, (sk - 60) / 40) * 12
    else:
        # Queremos StochRSI < 20 (oversold → rebote inminente)
        s += max(0.0, (40 - sk) / 40) * 12

    # VWAP Deviation (10 pts) — entrada cerca de la media real
    vd = _safe(vwap_dev)
    if d == "SHORT":
        # Precio muy por encima del VWAP → sobreextendido → SHORT
        s += max(0.0, min(vd/2.0, 1.0)) * 10
    else:
        # Precio muy por debajo del VWAP → oversold → LONG
        s += max(0.0, min(-vd/2.0, 1.0)) * 10

    # Momentum (12 pts)
    mom = _safe(momentum)
    s += (max(0.0, min(-mom*30,1.0)) if d=="SHORT" else max(0.0, min(mom*30,1.0))) * 12

    # MFI (8 pts)
    mfi_v = _safe(mfi, 50.0)
    s += (max(0.0,(50-mfi_v)/50) if d=="SHORT" else max(0.0,(mfi_v-50)/50)) * 8

    # VDI (6 pts)
    vdi_v = _safe(vdi)
    s += (max(0.0,min(-vdi_v/3.0,1.0)) if d=="SHORT" else max(0.0,min(vdi_v/3.0,1.0))) * 6

    # Estructura (12 pts) — BoS/CHoCH más fiable
    struct_map = {
        "CHoCH↑": (12 if d=="LONG" else 0), "CHoCH↓": (12 if d=="SHORT" else 0),
        "BoS↑":   (8  if d=="LONG" else 0), "BoS↓":   (8  if d=="SHORT" else 0),
    }
    s += struct_map.get(structure, 0)

    # HTF (6 pts)
    htf_v = _safe(htf_s, 0.5)
    s += ((1.0-htf_v) if d=="SHORT" else htf_v) * 6

    # Funding Rate bias (5 pts) — filtro de sentimiento de mercado
    # SHORT_CROWDED → longs pagan → SHORT bajo presión → evitar
    # LONG_CROWDED  → shorts pagan → SHORT favorecido
    if funding_bias == "LONG_CROWDED" and d == "SHORT":
        s += 5   # mercado largo sobrecargado → SHORT es la operación contrarian
    elif funding_bias == "SHORT_CROWDED" and d == "LONG":
        s += 5
    elif funding_bias == "SHORT_CROWDED" and d == "SHORT":
        s -= 3   # funding negativo → peligroso para SHORT

    # OBI — Order Book Imbalance (5 pts) — señal más rápida disponible
    # Mide presión real de compradores vs vendedores en el libro de órdenes
    obi_v = float(obi) if abs(float(obi)) <= 1.0 else 0.0
    if d == "SHORT":
        s += max(0.0, -obi_v) * 5   # OBI negativo → más vendedores → SHORT
    else:
        s += max(0.0,  obi_v) * 5   # OBI positivo → más compradores → LONG

    # FVG (2 pts)
    if (d=="LONG" and fvg=="BULL") or (d=="SHORT" and fvg=="BEAR"):
        s += 2

    return round(min(max(s, 0.0), 100.0), 1)


def score_to_tier(score):
    import config as C
    if not math.isfinite(score): return "NONE"
    if score >= C.SUP_SCORE:  return "SUP"
    if score >= C.FUEL_SCORE: return "FUEL"
    if score >= C.MIN_SCORE:  return "STD"
    return "NONE"

# ── Función principal ─────────────────────────────────────────────────────────

def analyze(symbol, klines_3m, klines_15m, klines_1h, klines_4h, funding_rate=0.0):
    """
    funding_rate: tasa de financiación actual (float). Positivo = longs pagan.
    """
    import config as C
    import logging
    _log = logging.getLogger("indicators")

    def _no(reason):
        _log.debug("[%s] descartado: %s", symbol, reason)
        return Signal(symbol=symbol, direction="NONE", score=0, tier="NONE",
                      entry=0, sl=0, tp1=0, tp2=0, atr=0, adx=0, mfi=50,
                      vdi=0, cvd=0, momentum=0, htf_score=0,
                      structure="NONE", tl_break="NONE", reason=reason)

    if len(klines_3m) < 60:
        return _no("insufficient_data")

    arr = np.asarray(klines_3m, dtype=float)
    o,h,l,c,v = arr[:,1],arr[:,2],arr[:,3],arr[:,4],arr[:,5]

    atr_arr          = calc_atr(h,l,c,C.ATR_LEN)
    adx_arr,pdi,mdi  = calc_adx(h,l,c,C.ADX_LEN)
    atr  = _safe(atr_arr[-1])
    adx  = _safe(adx_arr[-1])

    if atr <= 0 or not math.isfinite(atr):
        return _no("invalid_atr")

    cvd_val = _safe(calc_cvd(o,c,v)[-1])
    mom_val = _safe(calc_momentum(c,10)[-1])
    mfi_val = _safe(calc_mfi(h,l,c,v,14)[-1], 50.0)
    vdi_val = _safe(calc_vdi(c,v,20)[-1])

    # Nuevos indicadores v6.5
    stoch_k_arr, stoch_d_arr = calc_stoch_rsi(c, 14, 14, 3, 3)
    stoch_k_val = _safe(stoch_k_arr[-1], 50.0)
    vwap_dev_val = _safe(calc_vwap_dev(h,l,c,v,20)[-1])

    # Funding bias
    fr = _safe(funding_rate, 0.0)
    if fr > 0.0003:      funding_bias = "LONG_CROWDED"    # longs muy sobrecargados
    elif fr < -0.0003:   funding_bias = "SHORT_CROWDED"
    else:                funding_bias = "NEUTRAL"

    cb        = C.CB_ENABLED and check_circuit_breaker(h,l,atr_arr,C.CB_ATR_MULT,C.CB_BARS)
    structure = detect_structure(h,l,c,5)
    tl_break  = detect_tl_break(h,l,c,20)
    fvg       = detect_fvg(h,l)
    htf_s     = _safe(htf_score(klines_15m,klines_1h,klines_4h), 0.5)

    if C.REQUIRE_TL_BREAK and tl_break == "NONE":
        return _no("no_tl_break")

    direction = tl_break if tl_break != "NONE" else ("LONG" if pdi[-1]>mdi[-1] else "SHORT")

    # HTF mínimo
    htf_aligned = 0
    for kl,_ in [(klines_15m,1),(klines_1h,2),(klines_4h,4)]:
        if len(kl) < 30: continue
        a = np.asarray(kl,dtype=float); cc=a[:,4]
        e20=_ema(cc,20); e50=_ema(cc,50) if len(cc)>=50 else e20
        if (direction=="LONG" and e20[-1]>e50[-1]) or (direction=="SHORT" and e20[-1]<e50[-1]):
            htf_aligned += 1
    if htf_aligned < C.HTF_MIN_ALIGNED:
        return _no(f"htf_not_aligned({htf_aligned}/{C.HTF_MIN_ALIGNED})")

    score = composite_score(
        direction, adx, cvd_val, mom_val, mfi_val, vdi_val,
        structure, tl_break, htf_s, fvg,
        stoch_k=stoch_k_val, vwap_dev=vwap_dev_val,
        funding_bias=funding_bias, obi=0.0   # obi se pasa desde scanner
    )
    tier  = score_to_tier(score)
    entry = _safe(c[-1])

    sl_m  = getattr(C, "SL_ATR_MULT",  0.8)
    tp1_m = getattr(C, "TP1_ATR_MULT", 1.2)
    tp2_m = getattr(C, "TP2_ATR_MULT", 2.5)

    if direction == "LONG":
        sl,tp1,tp2 = entry-atr*sl_m, entry+atr*tp1_m, entry+atr*tp2_m
    else:
        sl,tp1,tp2 = entry+atr*sl_m, entry-atr*tp1_m, entry-atr*tp2_m

    return Signal(
        symbol=symbol, direction=direction, score=score, tier=tier,
        entry=entry, sl=sl, tp1=tp1, tp2=tp2, atr=atr, adx=adx,
        mfi=mfi_val, vdi=vdi_val, cvd=cvd_val, momentum=mom_val,
        htf_score=htf_s, structure=structure, tl_break=tl_break,
        stoch_rsi=stoch_k_val, funding_bias=funding_bias,
        tl_break_active=(tl_break!="NONE"), circuit_breaker=cb,
        reason="ok",
    )
