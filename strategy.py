"""
ZigZag Channel Fade — V32 Apex Quantum Shield
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Filtros combinados:
  1. ZigZag pivot channel (línea verde/roja)
  2. Overshoot en pips (SHORT > verde+45p | LONG < roja-30p)
  3. ADX > 25 (evita mercados laterales)
  4. EMA 7 × EMA 17 crossover (confirma el giro)
  5. Volumen institucional > 1.5× MA20
  6. ATR SL × 1.5 (ajustado a V32)
  7. Time-stop externo en trader.py (45 min = 15 velas×3m)
"""
import logging
import numpy as np
from typing import Optional, Tuple, List
import config

log = logging.getLogger("strategy")


# ─────────────────────────────────────────────────────────────────────
# PARSER DE KLINES
# ─────────────────────────────────────────────────────────────────────
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
                o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
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


# ─────────────────────────────────────────────────────────────────────
# ATR (Wilder)
# ─────────────────────────────────────────────────────────────────────
def calc_atr(H: np.ndarray, L: np.ndarray, C: np.ndarray, period: int = 14) -> float:
    if len(C) < period + 1:
        return 0.0
    tr = np.maximum(H[1:] - L[1:],
         np.maximum(np.abs(H[1:] - C[:-1]),
                    np.abs(L[1:] - C[:-1])))
    val = np.mean(tr[:period])
    for i in range(period, len(tr)):
        val = (val * (period - 1) + tr[i]) / period
    return float(val)


# ─────────────────────────────────────────────────────────────────────
# EMA
# ─────────────────────────────────────────────────────────────────────
def calc_ema(arr: np.ndarray, period: int) -> np.ndarray:
    """Retorna array EMA del mismo tamaño; primeras (period-1) posiciones = 0."""
    if len(arr) < period:
        return np.zeros(len(arr))
    k = 2.0 / (period + 1)
    result = np.zeros(len(arr))
    result[period - 1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        result[i] = arr[i] * k + result[i - 1] * (1.0 - k)
    return result


# ─────────────────────────────────────────────────────────────────────
# ADX (Wilder, igual que DMI de TradingView)
# ─────────────────────────────────────────────────────────────────────
def calc_adx(H: np.ndarray, L: np.ndarray, C: np.ndarray, period: int = 14) -> float:
    """Retorna el valor ADX actual (última barra)."""
    n = len(C)
    if n < period * 2 + 2:
        return 0.0

    # True Range y DM
    tr   = np.maximum(H[1:] - L[1:],
           np.maximum(np.abs(H[1:] - C[:-1]),
                      np.abs(L[1:] - C[:-1])))
    pdm  = np.where((H[1:] - H[:-1]) > (L[:-1] - L[1:]),
                    np.maximum(H[1:] - H[:-1], 0.0), 0.0)
    mdm  = np.where((L[:-1] - L[1:]) > (H[1:] - H[:-1]),
                    np.maximum(L[:-1] - L[1:], 0.0), 0.0)

    # Wilder smoothing
    def _smooth(arr):
        s = np.zeros(len(arr))
        s[period - 1] = np.sum(arr[:period])
        for i in range(period, len(arr)):
            s[i] = s[i - 1] - s[i - 1] / period + arr[i]
        return s

    atr_s = _smooth(tr)
    pdm_s = _smooth(pdm)
    mdm_s = _smooth(mdm)

    pdi = 100.0 * pdm_s / (atr_s + 1e-10)
    mdi = 100.0 * mdm_s / (atr_s + 1e-10)
    dx  = 100.0 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)

    # ADX = Wilder-smooth sobre DX
    adx = np.zeros(len(dx))
    if len(dx) >= period:
        adx[period - 1] = np.mean(dx[:period])
        for i in range(period, len(dx)):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return float(adx[-1])


# ─────────────────────────────────────────────────────────────────────
# PIVOT DETECTION
# ─────────────────────────────────────────────────────────────────────
def find_pivots(H: np.ndarray, L: np.ndarray, pivot_len: int):
    n = len(H)
    ph: List[Tuple[float, int]] = []
    pl: List[Tuple[float, int]] = []
    for i in range(pivot_len, n - pivot_len):
        wh = H[i - pivot_len: i + pivot_len + 1]
        wl = L[i - pivot_len: i + pivot_len + 1]
        if H[i] >= np.max(wh):
            ph.append((float(H[i]), i))
        if L[i] <= np.min(wl):
            pl.append((float(L[i]), i))
    return ph, pl


def _last(lst: list) -> Optional[float]:
    return lst[-1][0] if lst else None


# ─────────────────────────────────────────────────────────────────────
# SEÑAL CHANNEL FADE — V32 APEX QUANTUM SHIELD
# ─────────────────────────────────────────────────────────────────────
class ChannelFadeSignal:
    """
    Condiciones de entrada:

    SHORT:
      ① close > green + SHORT_PIPS × PIP_SIZE  (overshoot arriba)
      ② EMA_FAST acaba de cruzar por debajo de EMA_MED (giro bajista)
      ③ ADX > ADX_MIN (hay tendencia, no lateral)
      ④ Volumen > VOL_MULT × MA20 (confirmación institucional)
      SL = entry + ATR × SL_ATR_MULT
      TP = red line

    LONG:
      ① close < red - LONG_PIPS × PIP_SIZE  (overshoot abajo)
      ② EMA_FAST acaba de cruzar por encima de EMA_MED (giro alcista)
      ③ ADX > ADX_MIN
      ④ Volumen > VOL_MULT × MA20
      SL = entry - ATR × SL_ATR_MULT
      TP = green line
    """

    def compute(self,
                opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                closes: np.ndarray, volumes: np.ndarray) -> Optional[dict]:

        n = len(closes)
        min_bars = max(config.PIVOT_LEN * 2 + config.ATR_LEN + 2,
                       config.ADX_LEN * 2 + 2,
                       config.EMA_MED + 2)
        if n < min_bars:
            log.debug(f"  ✗ Pocas velas: {n} < {min_bars}")
            return None

        # Ignorar última vela (abierta en BingX)
        H = highs[:-1];  L = lows[:-1];  C = closes[:-1]
        O = opens[:-1];  V = volumes[:-1]
        if len(C) < 3:
            return None

        # ── 1. ATR ───────────────────────────────────────────────────
        atr = calc_atr(H, L, C, config.ATR_LEN)
        if atr == 0:
            return None

        # ── 2. ADX ───────────────────────────────────────────────────
        adx = calc_adx(H, L, C, config.ADX_LEN)
        if adx < config.ADX_MIN:
            log.debug(f"  ✗ ADX={adx:.1f} < {config.ADX_MIN} (mercado lateral)")
            return None

        # ── 3. EMAs ──────────────────────────────────────────────────
        ema_fast = calc_ema(C, config.EMA_FAST)
        ema_med  = calc_ema(C, config.EMA_MED)

        if ema_fast[-1] == 0 or ema_med[-1] == 0:
            return None

        # Crossover en la última barra cerrada
        cross_bear = (ema_fast[-2] >= ema_med[-2]) and (ema_fast[-1] < ema_med[-1])
        cross_bull = (ema_fast[-2] <= ema_med[-2]) and (ema_fast[-1] > ema_med[-1])

        # ── 4. Volumen institucional ─────────────────────────────────
        vol_ok = True
        vol_ratio = 1.0
        if config.VOL_FILTER:
            vol_window = min(20, len(V))
            vol_ma     = np.mean(V[-vol_window:]) if vol_window > 0 else 1.0
            vol_ratio  = V[-1] / vol_ma if vol_ma > 0 else 1.0
            vol_ok     = vol_ratio >= config.VOL_MULT
            if not vol_ok:
                log.debug(f"  ✗ Vol={vol_ratio:.2f}x < {config.VOL_MULT}x (retail)")

        # ── 5. Canal ZigZag ──────────────────────────────────────────
        ph_list, pl_list = find_pivots(H, L, config.PIVOT_LEN)
        green = _last(ph_list)
        red   = _last(pl_list)

        if green is None or red is None or green <= red:
            log.debug(f"  ✗ Canal inválido: green={green} red={red}")
            return None

        close     = C[-1]
        canal_w   = green - red

        # ── SHORT ────────────────────────────────────────────────────
        short_trigger = green + config.SHORT_PIPS * config.PIP_SIZE
        if (close >= short_trigger and cross_bear and adx >= config.ADX_MIN and vol_ok):
            sl = close + atr * config.SL_ATR_MULT
            tp = red
            if tp < close < sl:
                rr = abs(tp - close) / abs(sl - close)
                log.info(
                    f"  🔴 SHORT V32 | close={close:.4f} green+pips={short_trigger:.4f} "
                    f"ADX={adx:.1f} Vol={vol_ratio:.2f}x EMA_cross=✓ "
                    f"| TP={tp:.4f} SL={sl:.4f} RR=1:{rr:.2f}"
                )
                return {
                    "side": "SELL", "entry": close, "sl": sl, "tp": tp,
                    "atr": atr, "adx": adx, "green": green, "red": red,
                    "trigger": short_trigger, "vol_ratio": vol_ratio,
                    "canal_width": canal_w, "rr": rr,
                    "ema_fast": float(ema_fast[-1]), "ema_med": float(ema_med[-1])
                }

        # ── LONG ─────────────────────────────────────────────────────
        long_trigger = red - config.LONG_PIPS * config.PIP_SIZE
        if (close <= long_trigger and cross_bull and adx >= config.ADX_MIN and vol_ok):
            sl = close - atr * config.SL_ATR_MULT
            tp = green
            if sl < close < tp:
                rr = abs(tp - close) / abs(close - sl)
                log.info(
                    f"  🟢 LONG V32 | close={close:.4f} red-pips={long_trigger:.4f} "
                    f"ADX={adx:.1f} Vol={vol_ratio:.2f}x EMA_cross=✓ "
                    f"| TP={tp:.4f} SL={sl:.4f} RR=1:{rr:.2f}"
                )
                return {
                    "side": "BUY", "entry": close, "sl": sl, "tp": tp,
                    "atr": atr, "adx": adx, "green": green, "red": red,
                    "trigger": long_trigger, "vol_ratio": vol_ratio,
                    "canal_width": canal_w, "rr": rr,
                    "ema_fast": float(ema_fast[-1]), "ema_med": float(ema_med[-1])
                }

        log.debug(
            f"  · close={close:.4f} | green+p={short_trigger:.4f} red-p={long_trigger:.4f} "
            f"ADX={adx:.1f} Vol={vol_ratio:.2f}x bear={cross_bear} bull={cross_bull}"
        )
        return None


# ─────────────────────────────────────────────────────────────────────
# EXPLOSION SCORER (para scanner diario)
# ─────────────────────────────────────────────────────────────────────
class ExplosionScorer:
    def score(self, ticker: dict, daily_klines: list) -> float:
        try:
            price_change = abs(float(ticker.get("priceChangePercent", 0)))
            quote_vol    = float(ticker.get("quoteVolume", 0))
            vol_score    = 1.0
            if len(daily_klines) >= 2:
                def _v(k):
                    return float(k.get("volume", 0)) if isinstance(k, dict) else (
                           float(k[5]) if isinstance(k, (list, tuple)) and len(k) > 5 else 0.0)
                avg = np.mean([_v(k) for k in daily_klines[:-1]])
                vol_score = _v(daily_klines[-1]) / avg if avg > 0 else 1.0
            return price_change * 2.0 + vol_score * 3.0 + min(quote_vol / 1e7, 5.0)
        except Exception:
            return 0.0
