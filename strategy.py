"""
ZigZag Channel Fade Strategy — 3m
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Línea VERDE = último pivot HIGH confirmado
Línea ROJA  = último pivot LOW  confirmado

SHORT: close supera green + SHORT_PIPS → TP en red line
LONG : close rompe red - LONG_PIPS    → TP en green line
SL   : ATR × SL_ATR_MULT en dirección contraria
"""
import logging
import numpy as np
from typing import Optional, Tuple, List
import config

log = logging.getLogger("strategy")


# ─────────────────────────────────────────────────────────────────────
# PARSER DE KLINES (dict Y lista, vela abierta ignorada)
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
    return (np.array(opens),  np.array(highs), np.array(lows),
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
# PIVOT DETECTION
# ─────────────────────────────────────────────────────────────────────
def find_pivots(H: np.ndarray, L: np.ndarray, pivot_len: int):
    """Retorna (ph_list, pl_list) → listas de (valor, índice)."""
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
# SEÑAL CHANNEL FADE
# ─────────────────────────────────────────────────────────────────────
class ChannelFadeSignal:
    """
    SHORT: close > green + SHORT_PIPS × PIP_SIZE  (overshoot arriba → fade)
           TP = red line   SL = entry + SL_ATR × ATR

    LONG : close < red - LONG_PIPS × PIP_SIZE     (overshoot abajo → fade)
           TP = green line  SL = entry - SL_ATR × ATR
    """

    def compute(self,
                opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                closes: np.ndarray, volumes: np.ndarray) -> Optional[dict]:

        n = len(closes)
        min_bars = config.PIVOT_LEN * 2 + config.ATR_LEN + 2
        if n < min_bars:
            log.debug(f"  ✗ Pocas velas: {n}")
            return None

        # Ignorar última vela (abierta en BingX)
        H = highs[:-1];  L = lows[:-1];  C = closes[:-1]
        O = opens[:-1];  V = volumes[:-1]
        if len(C) < 3:
            return None

        # ATR
        atr = calc_atr(H, L, C, config.ATR_LEN)
        if atr == 0:
            return None

        # Niveles del canal
        ph_list, pl_list = find_pivots(H, L, config.PIVOT_LEN)
        green = _last(ph_list)   # línea verde = último pivot HIGH
        red   = _last(pl_list)   # línea roja  = último pivot LOW

        if green is None or red is None or green <= red:
            log.debug(f"  ✗ Canal no disponible: green={green} red={red}")
            return None

        close  = C[-1]
        is_bull = close > O[-1]
        is_bear = close < O[-1]

        # Filtro de volumen (opcional, controlado por VOL_FILTER)
        vol_ratio = 1.0
        if config.VOL_FILTER:
            vol_ma    = np.mean(V[-20:]) if len(V) >= 20 else np.mean(V)
            vol_ratio = V[-1] / vol_ma if vol_ma > 0 else 1.0
            if vol_ratio < config.VOL_MULT:
                log.debug(f"  ✗ Vol insuficiente: {vol_ratio:.2f}x < {config.VOL_MULT}x")
                return None

        canal_w = green - red

        # ── SHORT ────────────────────────────────────────────────────
        short_trigger = green + config.SHORT_PIPS * config.PIP_SIZE
        if close >= short_trigger and is_bear:
            sl = close + atr * config.SL_ATR_MULT
            tp = red
            if tp < close < sl:
                log.info(
                    f"  🔴 SHORT fade | close={close:.4f} > green+{config.SHORT_PIPS}p={short_trigger:.4f} "
                    f"| TP={tp:.4f} SL={sl:.4f} canal={canal_w:.2f}"
                )
                return {"side": "SELL", "entry": close, "sl": sl, "tp": tp,
                        "atr": atr, "green": green, "red": red,
                        "trigger": short_trigger, "vol_ratio": vol_ratio,
                        "canal_width": canal_w}

        # ── LONG ─────────────────────────────────────────────────────
        long_trigger = red - config.LONG_PIPS * config.PIP_SIZE
        if close <= long_trigger and is_bull:
            sl = close - atr * config.SL_ATR_MULT
            tp = green
            if sl < close < tp:
                log.info(
                    f"  🟢 LONG fade | close={close:.4f} < red-{config.LONG_PIPS}p={long_trigger:.4f} "
                    f"| TP={tp:.4f} SL={sl:.4f} canal={canal_w:.2f}"
                )
                return {"side": "BUY", "entry": close, "sl": sl, "tp": tp,
                        "atr": atr, "green": green, "red": red,
                        "trigger": long_trigger, "vol_ratio": vol_ratio,
                        "canal_width": canal_w}

        log.debug(
            f"  · close={close:.4f} green={green:.4f}(+{config.SHORT_PIPS}p→{short_trigger:.4f}) "
            f"red={red:.4f}(-{config.LONG_PIPS}p→{long_trigger:.4f})"
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
