"""
ZigZag Channel Fade — V33 Elite
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Señal de entrada (todas deben cumplirse):
  1. Overshoot canal ZigZag en pips
  2. ADX < ADX_MAX  (fade funciona en laterales, NO en tendencia fuerte)
  3. RSI confirma overbought/oversold:
       SHORT → RSI > RSI_OB  (precio sobreextendido arriba)
       LONG  → RSI < RSI_OS  (precio sobreextendido abajo)
  4. Volumen > VOL_MULT × MA20
  5. RR >= MIN_RR
  6. pip_size dinámico por precio
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


def calc_atr(H, L, C, period=14):
    if len(C) < period + 1:
        return 0.0
    tr = np.maximum(H[1:]-L[1:], np.maximum(np.abs(H[1:]-C[:-1]), np.abs(L[1:]-C[:-1])))
    val = np.mean(tr[:period])
    for i in range(period, len(tr)):
        val = (val*(period-1)+tr[i])/period
    return float(val)


def calc_rsi(C, period=14):
    """RSI de Wilder — perfecto para detectar overbought/oversold en fade."""
    if len(C) < period + 2:
        return 50.0
    deltas = np.diff(C.astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = np.mean(gains[:period])
    avg_l  = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_g = (avg_g*(period-1) + gains[i])  / period
        avg_l = (avg_l*(period-1) + losses[i]) / period
    if avg_l < 1e-10:
        return 100.0
    return float(100 - 100/(1 + avg_g/avg_l))


def calc_adx(H, L, C, period=14):
    if len(C) < period*2+2:
        return 0.0
    tr  = np.maximum(H[1:]-L[1:], np.maximum(np.abs(H[1:]-C[:-1]), np.abs(L[1:]-C[:-1])))
    pdm = np.where((H[1:]-H[:-1])>(L[:-1]-L[1:]), np.maximum(H[1:]-H[:-1],0.0), 0.0)
    mdm = np.where((L[:-1]-L[1:])>(H[1:]-H[:-1]), np.maximum(L[:-1]-L[1:],0.0), 0.0)
    def _s(a):
        s = np.zeros(len(a))
        if len(a) < period: return s
        s[period-1] = np.sum(a[:period])
        for i in range(period, len(a)): s[i] = s[i-1]-s[i-1]/period+a[i]
        return s
    atr_s=_s(tr); pdm_s=_s(pdm); mdm_s=_s(mdm)
    pdi=100.0*pdm_s/(atr_s+1e-10); mdi=100.0*mdm_s/(atr_s+1e-10)
    dx=100.0*np.abs(pdi-mdi)/(pdi+mdi+1e-10)
    adx=np.zeros(len(dx))
    if len(dx)>=period:
        adx[period-1]=np.mean(dx[:period])
        for i in range(period,len(dx)): adx[i]=(adx[i-1]*(period-1)+dx[i])/period
    return float(adx[-1])


def find_pivots(H, L, pivot_len):
    ph: List[Tuple[float,int]] = []
    pl: List[Tuple[float,int]] = []
    for i in range(pivot_len, len(H)-pivot_len):
        if H[i] >= np.max(H[i-pivot_len:i+pivot_len+1]): ph.append((float(H[i]),i))
        if L[i] <= np.min(L[i-pivot_len:i+pivot_len+1]): pl.append((float(L[i]),i))
    return ph, pl


def _last(lst): return lst[-1][0] if lst else None


class ChannelFadeSignal:

    def compute(self, opens, highs, lows, closes, volumes,
                symbol: str = "") -> Optional[dict]:
        n = len(closes)
        min_bars = max(config.PIVOT_LEN*2+config.ATR_LEN+2,
                       config.ADX_LEN*2+2,
                       config.RSI_PERIOD+2)
        if n < min_bars:
            return None

        H=highs[:-1]; L=lows[:-1]; C=closes[:-1]; V=volumes[:-1]
        if len(C) < 30:
            return None

        # ── ATR ───────────────────────────────────────────────────────
        atr = calc_atr(H, L, C, config.ATR_LEN)
        if atr == 0:
            return None

        # ── ADX (máximo — fade NO opera en tendencia fuerte) ──────────
        adx = calc_adx(H, L, C, config.ADX_LEN)
        if adx > config.ADX_MAX:
            log.info(f"  [{symbol}] ✗ ADX={adx:.1f} > {config.ADX_MAX} (tendencia, no operar)")
            return None

        # ── RSI (reemplaza EMA alignment — mejor para overbought/sold) ─
        rsi = calc_rsi(C, config.RSI_PERIOD)

        # ── Volumen ───────────────────────────────────────────────────
        vol_ok    = True
        vol_ratio = 1.0
        if config.VOL_FILTER:
            vol_w     = min(20, len(V))
            vol_ma    = np.mean(V[-vol_w:]) if vol_w > 0 else 1.0
            vol_ratio = V[-1]/vol_ma if vol_ma > 0 else 1.0
            vol_ok    = vol_ratio >= config.VOL_MULT
            if not vol_ok:
                log.info(f"  [{symbol}] ✗ Vol={vol_ratio:.2f}x < {config.VOL_MULT}x")

        # ── Canal ZigZag ──────────────────────────────────────────────
        ph_list, pl_list = find_pivots(H, L, config.PIVOT_LEN)
        green = _last(ph_list)
        red   = _last(pl_list)
        if green is None or red is None or green <= red:
            log.info(f"  [{symbol}] ✗ Canal inválido g={green} r={red}")
            return None

        close   = C[-1]
        canal_w = green - red
        pip     = dynamic_pip_size(close)
        short_t = green + config.SHORT_PIPS * pip
        long_t  = red   - config.LONG_PIPS  * pip

        log.info(
            f"  [{symbol}] ADX={adx:.1f} RSI={rsi:.1f} Vol={vol_ratio:.2f}x | "
            f"close={close:.5g} SHORT>={short_t:.5g} LONG<={long_t:.5g}"
        )

        # ── SHORT: overshoot arriba + RSI overbought ──────────────────
        if close >= short_t and rsi >= config.RSI_OB and vol_ok:
            sl = close + atr * config.SL_ATR_MULT
            tp = red
            if tp < close:
                rr = abs(tp-close) / max(abs(sl-close), 1e-10)
                if rr < config.MIN_RR:
                    log.info(f"  [{symbol}] ✗ SHORT RR={rr:.2f} < {config.MIN_RR}")
                    return None
                log.info(f"  [{symbol}] 🔴 SHORT RSI={rsi:.1f} RR=1:{rr:.2f}")
                return {"side":"SELL","entry":close,"sl":sl,"tp":tp,"atr":atr,
                        "adx":adx,"rsi":rsi,"green":green,"red":red,
                        "trigger":short_t,"vol_ratio":vol_ratio,
                        "canal_width":canal_w,"rr":rr,"pip_size":pip}

        # ── LONG: overshoot abajo + RSI oversold ──────────────────────
        if close <= long_t and rsi <= config.RSI_OS and vol_ok:
            sl = close - atr * config.SL_ATR_MULT
            tp = green
            if tp > close:
                rr = abs(tp-close) / max(abs(close-sl), 1e-10)
                if rr < config.MIN_RR:
                    log.info(f"  [{symbol}] ✗ LONG RR={rr:.2f} < {config.MIN_RR}")
                    return None
                log.info(f"  [{symbol}] 🟢 LONG RSI={rsi:.1f} RR=1:{rr:.2f}")
                return {"side":"BUY","entry":close,"sl":sl,"tp":tp,"atr":atr,
                        "adx":adx,"rsi":rsi,"green":green,"red":red,
                        "trigger":long_t,"vol_ratio":vol_ratio,
                        "canal_width":canal_w,"rr":rr,"pip_size":pip}

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
                vol_score = _v(daily_klines[-1])/avg if avg > 0 else 1.0
            return price_change*2.0 + vol_score*3.0 + min(quote_vol/1e7, 5.0)
        except Exception:
            return 0.0
