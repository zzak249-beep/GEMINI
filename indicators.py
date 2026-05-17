"""
bot/indicators.py
Todos los indicadores del NEXUS Bot implementados en NumPy/Pandas.

Módulos:
  - Medias móviles: EMA, SMA, SAMA (Slope Adaptive MA)
  - Momentum: ATR, ADX/DMI, RSI, Bollinger Bands, STC
  - Institucionales: VWAP, RVOL, POC
  - CVD: Synthetic Cumulative Volume Delta + divergencias
  - Liquidity Sweeps: detección de stop hunts
  - SAMA Slope: ángulo normalizado para régimen de mercado
"""
import math
import numpy as np
import pandas as pd
from typing import Tuple


# ─────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────

def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """RMA / Wilder smoothing."""
    result = np.full(len(series), np.nan)
    vals = series.values
    # Primer valor válido
    start = period - 1
    valid = ~np.isnan(vals[:period])
    if valid.sum() < period:
        return pd.Series(result, index=series.index)
    result[start] = np.nanmean(vals[:period])
    alpha = 1.0 / period
    for i in range(start + 1, len(vals)):
        if np.isnan(vals[i]):
            result[i] = result[i - 1]
        else:
            result[i] = result[i - 1] * (1 - alpha) + vals[i] * alpha
    return pd.Series(result, index=series.index)


# ─────────────────────────────────────────────────────────────
# MEDIAS MÓVILES
# ─────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


# ─────────────────────────────────────────────────────────────
# SAMA — Slope Adaptive Moving Average
# Port directo del Pine Script MZ SAMA
# ─────────────────────────────────────────────────────────────

def sama(close: pd.Series, high: pd.Series, low: pd.Series,
         length: int = 200, maj_length: int = 14,
         min_length: int = 6) -> pd.Series:
    """
    Adaptive MA cuyo alpha se ajusta según la posición del precio
    dentro del rango HH/LL del período. Doble cuadratura del alpha
    hace la MA muy selectiva: solo reacciona en extremos del rango.
    """
    min_alpha = 2.0 / (min_length + 1)
    maj_alpha = 2.0 / (maj_length + 1)

    c  = close.values
    hh = close.rolling(length + 1).max().values   # SAMA usa close, no high
    ll = close.rolling(length + 1).min().values

    n      = len(c)
    result = np.full(n, np.nan)
    result[0] = c[0]

    for i in range(1, n):
        prev = result[i - 1] if not np.isnan(result[i - 1]) else c[i]
        if np.isnan(hh[i]) or np.isnan(ll[i]) or (hh[i] - ll[i]) == 0:
            result[i] = prev
            continue
        mult        = abs(2.0 * c[i] - ll[i] - hh[i]) / (hh[i] - ll[i])
        final       = mult * (min_alpha - maj_alpha) + maj_alpha
        final_alpha = final ** 2
        result[i]   = (c[i] - prev) * final_alpha + prev

    return pd.Series(result, index=close.index)


def sama_slope(sama_s: pd.Series, close: pd.Series, high: pd.Series,
               low: pd.Series, slope_period: int = 34,
               slope_range: int = 25) -> pd.Series:
    """
    Convierte la pendiente de la SAMA en grados angulares normalizados
    por el rango de precio. Positivo = alcista, negativo = bajista.
    """
    hh   = high.rolling(slope_period).max().values
    ll   = low.rolling(slope_period).min().values
    s    = sama_s.values
    c    = close.values
    n    = len(s)
    angles = np.full(n, np.nan)

    for i in range(2, n):
        if np.isnan(hh[i]) or np.isnan(ll[i]) or np.isnan(s[i]) or np.isnan(s[i - 2]):
            continue
        rng = hh[i] - ll[i]
        if rng == 0 or c[i] == 0:
            continue
        slope_rng = slope_range / rng * ll[i]
        dt        = (s[i - 2] - s[i]) / c[i] * slope_rng
        hyp       = math.sqrt(1.0 + dt * dt)
        x_angle   = round(180.0 * math.acos(1.0 / hyp) / math.pi)
        angles[i] = -x_angle if dt > 0 else x_angle

    return pd.Series(angles, index=sama_s.index)


def adaptive_slope_threshold(adx: pd.Series, slope_base: float,
                              adx_trend: int, adx_range: int) -> pd.Series:
    """Umbral dinámico según régimen ADX."""
    thr = pd.Series(slope_base, index=adx.index)
    thr = thr.where(adx >= adx_range,  slope_base * 1.30)  # ranging → más exigente
    thr = thr.where(adx <= adx_trend,  slope_base * 0.85)  # trending → más permisivo
    return thr


# ─────────────────────────────────────────────────────────────
# ATR / ADX / DMI
# ─────────────────────────────────────────────────────────────

def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return _wilder_smooth(tr, period)


def adx_dmi(high: pd.Series, low: pd.Series, close: pd.Series,
            period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (plus_di, minus_di, adx)."""
    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr_s     = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr_w    = _wilder_smooth(tr_s, period)
    plus_di  = 100 * _wilder_smooth(plus_dm,  period) / atr_w.replace(0, np.nan)
    minus_di = 100 * _wilder_smooth(minus_dm, period) / atr_w.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val  = _wilder_smooth(dx.fillna(0), period)
    return plus_di, minus_di, adx_val


# ─────────────────────────────────────────────────────────────
# RSI / BOLLINGER BANDS / STC
# ─────────────────────────────────────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger_bands(close: pd.Series, period: int = 20,
                    mult: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    basis = sma(close, period)
    dev   = close.rolling(period).std()
    return basis + mult * dev, basis, basis - mult * dev


def stc(close: pd.Series, stc_len: int = 10,
        fast: int = 23, slow: int = 50) -> pd.Series:
    macd_line = ema(close, fast) - ema(close, slow)

    def _stoch(s: pd.Series, p: int) -> pd.Series:
        lo = s.rolling(p).min()
        hi = s.rolling(p).max()
        return 100 * (s - lo) / (hi - lo).replace(0, np.nan)

    d1 = ema(_stoch(macd_line, stc_len).fillna(50), 3)
    d2 = ema(_stoch(d1, stc_len).fillna(50), 3)
    return d2


# ─────────────────────────────────────────────────────────────
# VWAP / RVOL / POC
# ─────────────────────────────────────────────────────────────

def vwap(high: pd.Series, low: pd.Series, close: pd.Series,
         volume: pd.Series) -> pd.Series:
    typical = (high + low + close) / 3
    return (typical * volume).cumsum() / volume.cumsum().replace(0, np.nan)


def rvol(volume: pd.Series, period: int = 50) -> pd.Series:
    return volume / sma(volume, period).replace(0, np.nan)


def poc(close: pd.Series, volume: pd.Series, lookback: int) -> pd.Series:
    """Precio de cierre con mayor volumen en ventana lookback."""
    c, v = close.values, volume.values
    result = np.full(len(c), np.nan)
    for i in range(lookback, len(c)):
        idx         = np.argmax(v[i - lookback: i])
        result[i]   = c[i - lookback + idx]
    return pd.Series(result, index=close.index)


# ─────────────────────────────────────────────────────────────
# PIVOT HIGHS / LOWS
# ─────────────────────────────────────────────────────────────

def pivot_high(high: pd.Series, length: int) -> pd.Series:
    result = pd.Series(np.nan, index=high.index)
    h = high.values
    for i in range(length, len(h) - length):
        if h[i] == h[i - length: i + length + 1].max():
            result.iloc[i] = h[i]
    return result


def pivot_low(low: pd.Series, length: int) -> pd.Series:
    result = pd.Series(np.nan, index=low.index)
    l = low.values
    for i in range(length, len(l) - length):
        if l[i] == l[i - length: i + length + 1].min():
            result.iloc[i] = l[i]
    return result


# ─────────────────────────────────────────────────────────────
# ★ SYNTHETIC CVD (Cumulative Volume Delta)
# EDGE ESPECIAL: Presión compradora/vendedora real
# ─────────────────────────────────────────────────────────────

def synthetic_cvd(high: pd.Series, low: pd.Series, close: pd.Series,
                  volume: pd.Series) -> pd.Series:
    """
    CVD sintético usando posición del cierre dentro del rango de la vela.
    buy_vol  = volume * (close - low) / (high - low)
    sell_vol = volume * (high - close) / (high - low)
    CVD = cumsum(buy_vol - sell_vol)

    Sube cuando el dinero inteligente compra, baja cuando vende.
    No requiere datos de tick — funciona con OHLCV.
    """
    bar_range = (high - low).replace(0, np.nan)
    buy_pct   = ((close - low) / bar_range).clip(0, 1)
    delta     = volume * (2 * buy_pct - 1)   # +vol si cierre en máximos, -vol si en mínimos
    return delta.cumsum()


def cvd_slope(cvd: pd.Series, period: int = 8) -> pd.Series:
    """Pendiente del CVD: positiva = presión compradora, negativa = vendedora."""
    return cvd - cvd.shift(period)


def cvd_divergence(close: pd.Series, cvd: pd.Series,
                   lookback: int = 10) -> Tuple[pd.Series, pd.Series]:
    """
    ★ VETO DE DIVERGENCIA CVD ★
    Bearish divergence: precio hace nuevo máximo pero CVD no lo confirma.
    → Señal de debilidad alcista (veto de LONG o entrada SHORT).

    Bullish divergence: precio hace nuevo mínimo pero CVD no lo confirma.
    → Señal de debilidad bajista (veto de SHORT o entrada LONG).

    Returns: (bull_div, bear_div) — Series booleanas
    """
    price_new_high = close >= close.rolling(lookback).max()
    cvd_new_high   = cvd   >= cvd.rolling(lookback).max()
    price_new_low  = close <= close.rolling(lookback).min()
    cvd_new_low    = cvd   <= cvd.rolling(lookback).min()

    bear_div = price_new_high & ~cvd_new_high   # precio sube, CVD no confirma → BEARISH
    bull_div = price_new_low  & ~cvd_new_low    # precio baja, CVD no confirma → BULLISH

    return bull_div, bear_div


# ─────────────────────────────────────────────────────────────
# ★ LIQUIDITY SWEEPS (Stop Hunt Detection)
# EDGE ESPECIAL: Detecta caza de stops institucional
# ─────────────────────────────────────────────────────────────

def liquidity_sweep_long(high: pd.Series, low: pd.Series, close: pd.Series,
                         lookback: int = 20) -> pd.Series:
    """
    Sweep alcista (entrada LONG):
    - La mecha inferior rompe el mínimo reciente (caza stops bajistas)
    - Pero el cierre vuelve POR ENCIMA de ese nivel
    - Indica que los institucionales absorbieron la liquidez y van a subir

    Patrón: wick below recent_low + close > recent_low
    """
    recent_low = low.rolling(lookback).min().shift(1)
    sweep      = (low < recent_low) & (close > recent_low)
    return sweep.fillna(False)


def liquidity_sweep_short(high: pd.Series, low: pd.Series, close: pd.Series,
                          lookback: int = 20) -> pd.Series:
    """
    Sweep bajista (entrada SHORT):
    - La mecha superior rompe el máximo reciente (caza stops alcistas)
    - Pero el cierre vuelve POR DEBAJO de ese nivel
    - Indica que los institucionales distribuyeron en liquidez y van a bajar

    Patrón: wick above recent_high + close < recent_high
    """
    recent_high = high.rolling(lookback).max().shift(1)
    sweep       = (high > recent_high) & (close < recent_high)
    return sweep.fillna(False)


def sweep_strength(high: pd.Series, low: pd.Series, close: pd.Series,
                   lookback: int = 20) -> pd.Series:
    """
    Fuerza del sweep: cuánto sobrepasó el nivel en % del ATR.
    Sweeps más grandes → más liquidez cazada → señal más fuerte.
    """
    recent_low  = low.rolling(lookback).min().shift(1)
    recent_high = high.rolling(lookback).max().shift(1)
    atr_val     = atr(high, low, close, 14)

    long_over   = (recent_low - low).clip(lower=0)
    short_over  = (high - recent_high).clip(lower=0)
    strength    = (long_over + short_over) / atr_val.replace(0, np.nan)
    return strength.fillna(0)
