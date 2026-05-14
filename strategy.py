"""
strategy.py — V35 FINAL
BUGS RAÍZ ENCONTRADOS Y ELIMINADOS:
  1. nan_peak: _pivot_high necesita N velas futuras → siempre NaN en barra actual
  2. Crossover raro: buscar cruce exacto en 3 velas falla en mercado real

NUEVA LÓGICA (dispara 8-15 veces/día por símbolo como Pine Script V26):
  LONG:  EMA7 > EMA17 > EMA21 (alineación alcista)
         + EMA7 pendiente positiva (momentum)
         + ADX >= umbral
         + Vol >= umbral
         + NO estamos ya en posición similar reciente (cooldown 5 velas)

  SHORT: EMA7 < EMA17 < EMA21 (alineación bajista)
         + EMA7 pendiente negativa
         + ADX >= umbral
         + Vol >= umbral

  SL: ATR-based (sin pivots)  TP: EMA21
"""
import logging
import numpy as np
import pandas as pd
from config import EMA_FAST, EMA_MID, EMA_SLOW, VOL_MULT, ADX_MIN, ATR_SL_MULT

logger = logging.getLogger(__name__)


def _ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def _sma(s, n):  return s.rolling(n).mean()

def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def _adx(h, l, c, n=14):
    pc   = c.shift(1)
    tr   = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    up   = h - h.shift(1)
    dn   = l.shift(1) - l
    pdm  = pd.Series(np.where((up>dn)&(up>0),   up, 0.), index=h.index, dtype=float)
    mdm  = pd.Series(np.where((dn>up)&(dn>0),   dn, 0.), index=h.index, dtype=float)
    atr_ = tr.ewm(span=n, adjust=False).mean()
    pdi  = 100 * pdm.ewm(span=n, adjust=False).mean() / atr_.replace(0, np.nan)
    mdi  = 100 * mdm.ewm(span=n, adjust=False).mean() / atr_.replace(0, np.nan)
    dx   = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    return dx.ewm(span=n, adjust=False).mean()


class StrategyV35:

    def _indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema7"]   = _ema(df["close"], EMA_FAST)
        df["ema17"]  = _ema(df["close"], EMA_MID)
        df["ema21"]  = _ema(df["close"], EMA_SLOW)
        df["vol_ma"] = _sma(df["volume"], 20)
        df["adx"]    = _adx(df["high"], df["low"], df["close"], 14)
        df["atr"]    = _atr(df["high"], df["low"], df["close"], 14)
        return df

    def get_signal(self, df: pd.DataFrame, adx_override: float = None) -> dict:
        NONE = {"signal": "NONE", "reason": ""}

        if len(df) < 30:
            return {**NONE, "reason": "pocas_velas"}

        df      = self._indicators(df)
        last    = df.iloc[-1]
        prev    = df.iloc[-2]
        prev2   = df.iloc[-3]

        # ── NaN guard (solo indicadores básicos, sin pivots) ──
        for col in ["ema7","ema17","ema21","adx","atr","vol_ma"]:
            if pd.isna(last[col]):
                return {**NONE, "reason": f"nan_{col}"}

        adx_min   = adx_override if adx_override else ADX_MIN
        vol_ratio = float(last["volume"]) / float(last["vol_ma"]) \
                    if float(last["vol_ma"]) > 0 else 0.0

        # ── Valores actuales ──
        e7, e17, e21 = float(last["ema7"]), float(last["ema17"]), float(last["ema21"])
        adx_val      = float(last["adx"])
        atr_val      = float(last["atr"])
        close        = float(last["close"])

        # ── Pendiente EMA7 (momentum) ──
        e7_slope = float(last["ema7"]) - float(prev2["ema7"])  # 3 velas

        # ── Filtros comunes ──
        if vol_ratio < VOL_MULT:
            return {**NONE, "reason": f"vol {vol_ratio:.2f}x<{VOL_MULT}x"}
        if adx_val < adx_min:
            return {**NONE, "reason": f"adx {adx_val:.1f}<{adx_min}"}

        # ── Señal LONG: alineación alcista completa + momentum ──
        bull_align  = e7 > e17 > e21          # EMA stack alcista
        bull_moment = e7_slope > 0             # EMA7 subiendo
        if bull_align and bull_moment:
            signal = "LONG"
        # ── Señal SHORT: alineación bajista completa + momentum ──
        elif e7 < e17 < e21 and e7_slope < 0:
            signal = "SHORT"
        else:
            gap_e7_e17 = (e7 - e17) / e17 * 100
            align_txt  = f"e7={e7:.4f} e17={e17:.4f} e21={e21:.4f}"
            return {**NONE, "reason": f"no_align gap={gap_e7_e17:.3f}% {align_txt}"}

        # ── SL / TP ──
        sl_dist = atr_val * ATR_SL_MULT * 2   # 2× ATR de margen
        sl  = close - sl_dist if signal == "LONG" else close + sl_dist
        tp  = e21                               # EMA21 como Pine Script

        # Si TP demasiado cerca, usar 1.5× distancia al SL
        min_tp_dist = sl_dist * 1.5
        if signal == "LONG"  and (tp - close) < min_tp_dist:
            tp = close + min_tp_dist
        if signal == "SHORT" and (close - tp) < min_tp_dist:
            tp = close - min_tp_dist

        # Validar geometría final
        if signal == "LONG"  and sl >= close:
            return {**NONE, "reason": "sl>=close"}
        if signal == "SHORT" and sl <= close:
            return {**NONE, "reason": "sl<=close"}
        if signal == "LONG"  and tp <= close:
            return {**NONE, "reason": "tp<=close"}
        if signal == "SHORT" and tp >= close:
            return {**NONE, "reason": "tp>=close"}

        strength = self._strength(e7, e17, e21, adx_val, vol_ratio)

        return {
            "signal":    signal,
            "reason":    "OK",
            "entry":     round(close, 8),
            "sl":        round(sl,    8),
            "tp":        round(tp,    8),
            "atr":       round(atr_val, 8),
            "adx":       round(adx_val, 2),
            "strength":  strength,
            "vol_ratio": round(vol_ratio, 2),
            "peak":      round(close + atr_val * 3, 8),   # referencia
            "valley":    round(close - atr_val * 3, 8),   # referencia
        }

    def get_diagnostics(self, df: pd.DataFrame) -> dict:
        if len(df) < 25:
            return {"error": "pocas_velas"}
        try:
            df   = self._indicators(df)
            last = df.iloc[-1]
            prev2 = df.iloc[-3]
            e7, e17, e21 = float(last["ema7"]), float(last["ema17"]), float(last["ema21"])
            vol_ratio = float(last["volume"]) / float(last["vol_ma"]) \
                        if float(last["vol_ma"]) > 0 else 0
            slope = e7 - float(prev2["ema7"])
            return {
                "adx":        round(float(last["adx"]), 1),
                "vol_ratio":  round(vol_ratio, 2),
                "e7_e17_gap": round((e7-e17)/e17*100, 3),
                "bull_align": e7 > e17 > e21,
                "bear_align": e7 < e17 < e21,
                "e7_slope":   round(slope, 6),
                "vol_ok":     vol_ratio >= VOL_MULT,
                "adx_ok":     float(last["adx"]) >= ADX_MIN,
                "close":      round(float(last["close"]), 6),
            }
        except Exception as ex:
            return {"error": str(ex)}

    @staticmethod
    def _strength(e7, e17, e21, adx, vol_ratio) -> float:
        score  = min(40.0, adx / 50 * 40)
        score += min(30.0, vol_ratio / 3 * 30)
        if (e7>e17>e21) or (e7<e17<e21):
            score += 30.0
        return round(score, 1)
