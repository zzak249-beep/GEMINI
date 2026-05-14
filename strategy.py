"""
ZigZag Channel Fade — V32
━━━━━━━━━━━━━━━━━━━━━━━━━

LÓGICA (Channel Fade):
  SHORT: precio supera el TECHO del canal + EMA sobreextendida arriba
  LONG:  precio cae bajo el SUELO del canal + EMA sobreextendida abajo

  EMA fade correcto:
    SHORT → ema_fast > ema_med  (precio ha subido, EMA lo refleja)
    LONG  → ema_fast < ema_med  (precio ha caído, EMA lo refleja)

  ADX: solo informativo — si < ADX_MIN el trader usa 50% de size.
       NO bloquea señales. NO hay límite superior de ADX.

  VOL_FILTER: False por defecto (3m es ruidoso, Vol varía 0.03x-2x)
"""
import logging
import numpy as np
from typing import Optional, Tuple, List
import config

log = logging.getLogger("strategy")


def dynamic_pip_size(price: float) -> float:
    if price >= 10_000: return 1.0
    if price >= 1_000:  return 0.1
    if price >= 100:    return 0.01
    if price >= 1:      return 0.001
    if price >= 0.1:    return 0.0001
    return 0.00001


def parse_klines(raw: list) -> Tuple[np.ndarray, ...]:
    if not raw:
        return (np.array([]),) * 5
    opens, highs, lows, closes, volumes = [], [], [], [], []
    for k in raw:
        try:
            if isinstance(k, dict):
                o = float(k.get("open",   k.get("o", 0)))
                h = float(k.get("high",   k.get("h", 0)))
                l = float(k.get("low",    k.get("l", 0)))
                c = float(k.get("close",  k.get("c", 0)))
                v = float(k.get("volume", k.get("v", 0)))
            elif isinstance(k, (list, tuple)) and len(k) >= 6:
                o,h,l,c,v = float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5])
            else:
                continue
            if h < l or c <= 0:
                continue
            opens.append(o); highs.append(h); lows.append(l)
            closes.append(c); volumes.append(v)
        except (TypeError, ValueError):
            continue
    return (np.array(opens), np.array(highs), np.array(lows),
            np.array(closes), np.array(volumes))


def calc_atr(H, L, C, period=14) -> float:
    if len(C) < period + 1: return 0.0
    tr = np.maximum(H[1:]-L[1:],
         np.maximum(np.abs(H[1:]-C[:-1]), np.abs(L[1:]-C[:-1])))
    v = np.mean(tr[:period])
    for i in range(period, len(tr)):
        v = (v*(period-1) + tr[i]) / period
    return float(v)


def calc_ema(arr, period) -> np.ndarray:
    if len(arr) < period: return np.zeros(len(arr))
    k = 2.0/(period+1)
    r = np.zeros(len(arr))
    r[period-1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        r[i] = arr[i]*k + r[i-1]*(1-k)
    return r


def calc_adx(H, L, C, period=14) -> float:
    if len(C) < period*2+2: return 0.0
    tr  = np.maximum(H[1:]-L[1:],
          np.maximum(np.abs(H[1:]-C[:-1]), np.abs(L[1:]-C[:-1])))
    pdm = np.where((H[1:]-H[:-1])>(L[:-1]-L[1:]),
                   np.maximum(H[1:]-H[:-1],0.0), 0.0)
    mdm = np.where((L[:-1]-L[1:])>(H[1:]-H[:-1]),
                   np.maximum(L[:-1]-L[1:],0.0), 0.0)
    def _s(a):
        s=np.zeros(len(a))
        if len(a)<period: return s
        s[period-1]=np.sum(a[:period])
        for i in range(period,len(a)): s[i]=s[i-1]-s[i-1]/period+a[i]
        return s
    atr_s=_s(tr); pdm_s=_s(pdm); mdm_s=_s(mdm)
    pdi=100*pdm_s/(atr_s+1e-10); mdi=100*mdm_s/(atr_s+1e-10)
    dx=100*np.abs(pdi-mdi)/(pdi+mdi+1e-10)
    adx=np.zeros(len(dx))
    if len(dx)>=period:
        adx[period-1]=np.mean(dx[:period])
        for i in range(period,len(dx)): adx[i]=(adx[i-1]*(period-1)+dx[i])/period
    return float(adx[-1])


def find_pivots(H, L, pivot_len) -> Tuple[List, List]:
    ph: List[Tuple[float,int]] = []
    pl: List[Tuple[float,int]] = []
    n = len(H)
    for i in range(pivot_len, n-pivot_len):
        if H[i] >= np.max(H[i-pivot_len:i+pivot_len+1]):
            ph.append((float(H[i]), i))
        if L[i] <= np.min(L[i-pivot_len:i+pivot_len+1]):
            pl.append((float(L[i]), i))
    return ph, pl


def _last(lst):
    return lst[-1][0] if lst else None


class ChannelFadeSignal:

    def compute(self, opens, highs, lows, closes, volumes,
                symbol: str = "") -> Optional[dict]:

        n = len(closes)
        min_bars = max(config.PIVOT_LEN*2 + config.ATR_LEN + 2,
                       config.ADX_LEN*2 + 2,
                       config.EMA_MED + 2)
        if n < min_bars:
            return None

        H = highs[:-1]; L = lows[:-1]; C = closes[:-1]; V = volumes[:-1]
        if len(C) < 30:
            return None

        # ── ATR ───────────────────────────────────────────────────────
        atr = calc_atr(H, L, C, config.ATR_LEN)
        if atr == 0:
            return None

        # ── ADX — solo informativo, nunca bloquea ─────────────────────
        adx    = calc_adx(H, L, C, config.ADX_LEN)
        adx_ok = adx >= config.ADX_MIN  # solo para ajuste de size

        # ── EMA — lógica fade ─────────────────────────────────────────
        ema_fast = calc_ema(C, config.EMA_FAST)
        ema_med  = calc_ema(C, config.EMA_MED)
        if ema_fast[-1] == 0 or ema_med[-1] == 0:
            return None
        ext_up   = ema_fast[-1] > ema_med[-1]   # sobreextendido arriba → SHORT
        ext_down = ema_fast[-1] < ema_med[-1]   # sobreextendido abajo  → LONG

        # ── Volumen — opcional ────────────────────────────────────────
        vol_ratio = 1.0
        if config.VOL_FILTER:
            vol_window = min(20, len(V))
            vol_ma    = np.mean(V[-vol_window:]) if vol_window > 0 else 1.0
            vol_ratio = V[-1] / vol_ma if vol_ma > 0 else 1.0
            if vol_ratio < config.VOL_MULT:
                log.info(f"  [{symbol}] ✗ Vol={vol_ratio:.2f}x < {config.VOL_MULT}x")
                return None

        # ── Canal ZigZag ──────────────────────────────────────────────
        ph_list, pl_list = find_pivots(H, L, config.PIVOT_LEN)
        green = _last(ph_list)
        red   = _last(pl_list)
        if green is None or red is None or green <= red:
            log.info(f"  [{symbol}] ✗ Canal inválido (green={green} red={red})")
            return None

        close   = C[-1]
        canal_w = green - red
        pip     = dynamic_pip_size(close)

        short_offset  = max(config.SHORT_PIPS * pip, atr * 0.3)
        long_offset   = max(config.LONG_PIPS  * pip, atr * 0.3)
        short_trigger = green + short_offset
        long_trigger  = red   - long_offset

        log.info(
            f"  [{symbol}] ADX={adx:.1f}({'✓' if adx_ok else 'weak'}) "
            f"Vol={vol_ratio:.2f}x ext_up={ext_up} ext_down={ext_down} | "
            f"close={close:.5g} SHORT>={short_trigger:.5g} LONG<={long_trigger:.5g}"
        )

        # ── SHORT ─────────────────────────────────────────────────────
        if close >= short_trigger and ext_up:
            sl = close + atr * config.SL_ATR_MULT
            tp = red
            if tp >= close:
                log.info(f"  [{symbol}] ✗ SHORT TP>=close")
                return None
            rr = abs(tp-close) / max(abs(sl-close), 1e-10)
            if rr < config.MIN_RR:
                log.info(f"  [{symbol}] ✗ SHORT RR={rr:.2f}<{config.MIN_RR}")
                return None
            log.info(f"  [{symbol}] 🔴 SHORT RR=1:{rr:.2f} ADX={'OK' if adx_ok else 'WEAK'}")
            return {
                "side":"SELL","entry":close,"sl":sl,"tp":tp,
                "atr":atr,"adx":adx,"adx_ok":adx_ok,
                "green":green,"red":red,"trigger":short_trigger,
                "vol_ratio":vol_ratio,"canal_width":canal_w,
                "rr":rr,"pip_size":pip,
                "ema_fast":float(ema_fast[-1]),"ema_med":float(ema_med[-1])
            }

        # ── LONG ──────────────────────────────────────────────────────
        if close <= long_trigger and ext_down:
            sl = close - atr * config.SL_ATR_MULT
            tp = green
            if tp <= close:
                log.info(f"  [{symbol}] ✗ LONG TP<=close")
                return None
            rr = abs(tp-close) / max(abs(close-sl), 1e-10)
            if rr < config.MIN_RR:
                log.info(f"  [{symbol}] ✗ LONG RR={rr:.2f}<{config.MIN_RR}")
                return None
            log.info(f"  [{symbol}] 🟢 LONG RR=1:{rr:.2f} ADX={'OK' if adx_ok else 'WEAK'}")
            return {
                "side":"BUY","entry":close,"sl":sl,"tp":tp,
                "atr":atr,"adx":adx,"adx_ok":adx_ok,
                "green":green,"red":red,"trigger":long_trigger,
                "vol_ratio":vol_ratio,"canal_width":canal_w,
                "rr":rr,"pip_size":pip,
                "ema_fast":float(ema_fast[-1]),"ema_med":float(ema_med[-1])
            }

        return None


class ExplosionScorer:
    def score(self, ticker: dict, daily_klines: list) -> float:
        try:
            price_change = abs(float(ticker.get("priceChangePercent", 0)))
            quote_vol    = float(ticker.get("quoteVolume", 0))
            vol_score    = 1.0
            if len(daily_klines) >= 2:
                def _v(k):
                    return float(k.get("volume",0)) if isinstance(k,dict) else (
                           float(k[5]) if isinstance(k,(list,tuple)) and len(k)>5 else 0.0)
                avg = np.mean([_v(k) for k in daily_klines[:-1]])
                vol_score = _v(daily_klines[-1]) / avg if avg > 0 else 1.0
            return price_change*2.0 + vol_score*3.0 + min(quote_vol/1e7, 5.0)
        except Exception:
            return 0.0
