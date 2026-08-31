"""
indicators.py
=============
Réplica en Python, barra a barra, de la lógica del Pine Script v6 original
("ProBorsa: RSI & SuperTrend Özel Dip Stratejisi"), más un filtro de
tendencia OPCIONAL (TREND_FILTER_ENABLED) tomado del script
"Higher-Low tras Ruptura de Base + EMA50": no tomar el Doble Dip si el
precio sigue dentro de una base sin confirmar (la EMA de tendencia
todavía no sube, o el precio no ha roto la directriz bajista de la base
previa). Es el mismo "no compres el primer rebote, compra tras confirmar
estructura" aplicado como filtro sobre la señal de RSI en vez de como
señal propia.

Reglas de entrada/salida (idénticas al script fuente):
  - RSI(rsi_length) vía RMA (suavizado de Wilder), igual que ta.rma de Pine.
  - rsi_signal = SMA(RSI, sig_length)
  - bull_cross = cruce alcista de RSI sobre rsi_signal (ta.crossover)
  - Contador de "Doble Dip":
      * si RSI > trigger_level  -> contador = 0
      * si bull_cross y RSI < trigger_level -> contador += 1
      * specialBuy = bull_cross y RSI < trigger_level y contador == target_cross_count
      * tras specialBuy -> contador = 0
  - SuperTrend(factor, atr_period) réplica exacta del algoritmo estándar
    (idéntico al ta.supertrend de Pine: bandas ajustadas + persistencia de
    dirección). direction == 1 -> bajista, direction == -1 -> alcista.
  - stSell = la dirección de SuperTrend pasa de -1 a 1 (cambio > 0)
  - Filtro de tendencia (si TREND_FILTER_ENABLED): specialBuy final =
    specialBuy crudo Y ema_de_tendencia subiendo Y precio ya rompió la
    directriz bajista de la base previa (ver compute_trend_filter). La
    ruptura se invalida sola si aparece un nuevo mínimo por debajo del
    suelo de la ruptura, o si pasa demasiado tiempo sin confirmarse.

Todo el cálculo es secuencial (bar-by-bar) porque el contador, el
SuperTrend y el filtro de tendencia dependen de su propio valor previo,
igual que en Pine.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def rma(series: pd.Series, length: int) -> pd.Series:
    """Réplica exacta de ta.rma de Pine (suavizado de Wilder).

    Semilla = SMA de los primeros `length` valores; a partir de ahí,
    fórmula recursiva con alpha = 1/length.
    """
    values = series.to_numpy(dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    alpha = 1.0 / length

    if n < length:
        return pd.Series(out, index=series.index)

    seed = np.nanmean(values[:length])
    out[length - 1] = seed
    prev = seed
    for i in range(length, n):
        if np.isnan(values[i]):
            out[i] = prev
            continue
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return pd.Series(out, index=series.index)


def compute_rsi(close: pd.Series, length: int) -> pd.Series:
    """RSI clásico de Wilder, misma fórmula que el script fuente:
    down==0 -> 100 ; up==0 -> 0 ; si no, 100 - 100/(1+up/down)
    """
    delta = close.diff()
    up_move = delta.clip(lower=0)
    down_move = (-delta).clip(lower=0)

    avg_up = rma(up_move, length)
    avg_down = rma(down_move, length)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_up / avg_down
        rsi_calc = 100 - (100 / (1 + rs))

    rsi = np.where(
        avg_down.to_numpy() == 0,
        100.0,
        np.where(avg_up.to_numpy() == 0, 0.0, rsi_calc.to_numpy()),
    )
    return pd.Series(rsi, index=close.index)


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    """Igual que ta.crossover(a, b) de Pine."""
    return (a > b) & (a.shift(1) <= b.shift(1))


def compute_special_buy(
    rsi: pd.Series,
    rsi_signal: pd.Series,
    trigger_level: float,
    target_cross_count: int,
) -> tuple[pd.Series, pd.Series]:
    """Réplica exacta de la lógica de contador 'Doble Dip' del script."""
    bull_cross = crossover(rsi, rsi_signal)
    n = len(rsi)
    special_buy = np.zeros(n, dtype=bool)
    counts = np.zeros(n, dtype=int)

    cross_count = 0
    rsi_vals = rsi.to_numpy()
    sig_vals = rsi_signal.to_numpy()
    cross_vals = bull_cross.to_numpy()

    for i in range(n):
        if np.isnan(rsi_vals[i]) or np.isnan(sig_vals[i]):
            counts[i] = cross_count
            continue

        if rsi_vals[i] > trigger_level:
            cross_count = 0

        if cross_vals[i] and rsi_vals[i] < trigger_level:
            cross_count += 1

        sb = bool(
            cross_vals[i]
            and (rsi_vals[i] < trigger_level)
            and (cross_count == target_cross_count)
        )
        if sb:
            special_buy[i] = True
            cross_count = 0

        counts[i] = cross_count

    return (
        pd.Series(special_buy, index=rsi.index),
        pd.Series(counts, index=rsi.index),
    )


def compute_supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_period: int,
    factor: float,
) -> tuple[pd.Series, pd.Series]:
    """Réplica exacta del algoritmo estándar de SuperTrend (idéntico a
    ta.supertrend de Pine). direction: 1 = bajista (supertrend=upperBand),
    -1 = alcista (supertrend=lowerBand).
    """
    hl2 = (high + low) / 2.0
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = rma(tr, atr_period)

    upper_basic = (hl2 + factor * atr).to_numpy()
    lower_basic = (hl2 - factor * atr).to_numpy()
    close_vals = close.to_numpy()
    atr_vals = atr.to_numpy()

    n = len(close)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    supertrend = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)

    for i in range(n):
        if np.isnan(atr_vals[i]):
            continue

        prev_atr_valid = i > 0 and not np.isnan(atr_vals[i - 1])
        if not prev_atr_valid:
            final_upper[i] = upper_basic[i]
            final_lower[i] = lower_basic[i]
            direction[i] = 1
            supertrend[i] = final_upper[i]
            continue

        prev_lower = final_lower[i - 1]
        prev_upper = final_upper[i - 1]

        curr_lower = (
            lower_basic[i]
            if (lower_basic[i] > prev_lower or close_vals[i - 1] < prev_lower)
            else prev_lower
        )
        curr_upper = (
            upper_basic[i]
            if (upper_basic[i] < prev_upper or close_vals[i - 1] > prev_upper)
            else prev_upper
        )
        final_lower[i] = curr_lower
        final_upper[i] = curr_upper

        prev_supertrend = supertrend[i - 1]
        if prev_supertrend == prev_upper:
            direction[i] = -1 if close_vals[i] > curr_upper else 1
        else:
            direction[i] = 1 if close_vals[i] < curr_lower else -1

        supertrend[i] = curr_lower if direction[i] == -1 else curr_upper

    return (
        pd.Series(supertrend, index=close.index),
        pd.Series(direction, index=close.index),
    )


@dataclass
class SignalResult:
    """Resultado de evaluar la estrategia sobre la última vela CERRADA."""

    special_buy: bool
    st_sell: bool
    rsi: float
    rsi_signal: float
    cross_count: int
    supertrend: float
    direction: int
    close: float
    candle_time: pd.Timestamp
    trend_filter_ok: bool = True   # True si el filtro está desactivado o no bloqueó nada
    raw_special_buy: bool = False  # specialBuy ANTES del filtro de tendencia (para depurar)


def _pivots(series: pd.Series, left: int, right: int, mode: str) -> np.ndarray:
    """Pivotes estilo ta.pivothigh/ta.pivotlow de Pine: el valor se reporta
    `right` barras después de ocurrir (para no repintar)."""
    values = series.to_numpy(dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(left, n - right):
        window = values[i - left : i + right + 1]
        center = values[i]
        if mode == "low" and center == np.min(window) and np.sum(window == center) == 1:
            out[i + right] = center
        elif mode == "high" and center == np.max(window) and np.sum(window == center) == 1:
            out[i + right] = center
    return out


def compute_trend_filter(high: pd.Series, low: pd.Series, close: pd.Series, params: dict) -> pd.Series:
    """Filtro de tendencia tomado de "Higher-Low tras Ruptura de Base +
    EMA50": True solo cuando la EMA de tendencia está subiendo Y el precio
    ya rompió la directriz bajista que conectaba los 2 últimos pivote-altos
    de la base previa. Antes de esa ruptura (base sin confirmar, como el
    "Dont buy this" de la imagen de referencia) siempre da False.

    A diferencia del bot Higher-Low+EMA50 (donde esto ES la señal de
    entrada), aquí es solo un FILTRO sobre la señal de RSI - no exige que
    haya un higher-low exacto en la misma vela, solo que ya estemos en la
    fase confirmada de la tendencia.
    """
    ema = close.ewm(span=params["trend_ema_length"], adjust=False).mean()
    ema_vals = ema.to_numpy()
    n = len(close)

    ema_rising = np.zeros(n, dtype=bool)
    lb = params["trend_ema_slope_lookback"]
    for i in range(lb, n):
        ema_rising[i] = ema_vals[i] > ema_vals[i - lb]

    ph_raw = _pivots(high, params["trend_pivot_left"], params["trend_pivot_right"], "high")
    pl_raw = _pivots(low, params["trend_pivot_left"], params["trend_pivot_right"], "low")
    low_vals = low.to_numpy()
    close_vals = close.to_numpy()

    ph1 = ph2 = np.nan
    ph1_bar = ph2_bar = None
    pl1 = np.nan

    broken_above = False
    break_bar = None
    low_at_break = np.nan

    result = np.zeros(n, dtype=bool)

    for i in range(n):
        if not np.isnan(ph_raw[i]):
            ph2, ph2_bar = ph1, ph1_bar
            ph1, ph1_bar = ph_raw[i], i - params["trend_pivot_right"]

        new_pivot_low = not np.isnan(pl_raw[i])
        if new_pivot_low:
            pl1 = pl_raw[i]

        have_tl = not np.isnan(ph1) and not np.isnan(ph2) and ph1_bar != ph2_bar
        slope = (ph1 - ph2) / (ph1_bar - ph2_bar) if have_tl else np.nan
        tl_val = ph1 + slope * (i - ph1_bar) if have_tl else np.nan
        is_down_tl = have_tl and slope < 0

        breakout_now = False
        if is_down_tl and not np.isnan(tl_val) and i > 0:
            prev_tl_val = ph1 + slope * ((i - 1) - ph1_bar)
            if close_vals[i] > tl_val and close_vals[i - 1] <= prev_tl_val:
                breakout_now = True

        if breakout_now:
            broken_above = True
            break_bar = i
            low_at_break = pl1 if not np.isnan(pl1) else low_vals[i]

        if broken_above and new_pivot_low and pl1 < low_at_break:
            broken_above = False

        if broken_above and break_bar is not None and (i - break_bar) > params["trend_max_bars_after_break"]:
            broken_above = False

        result[i] = ema_rising[i] and broken_above

    return pd.Series(result, index=close.index)


def evaluate(df: pd.DataFrame, params: dict) -> SignalResult:
    """Calcula todos los indicadores sobre el DataFrame (columnas:
    open, high, low, close, volume; index = tiempo de apertura de vela,
    ascendente, SOLO velas cerradas) y devuelve el estado de la última vela.
    """
    rsi = compute_rsi(df["close"], params["rsi_length"])
    rsi_signal = rsi.rolling(params["sig_length"]).mean()
    raw_special_buy, counts = compute_special_buy(
        rsi, rsi_signal, params["trigger_level"], params["target_cross_count"]
    )
    supertrend, direction = compute_supertrend(
        df["high"], df["low"], df["close"], params["atr_period"], params["st_factor"]
    )
    st_sell = direction.diff() > 0

    if params.get("trend_filter_enabled"):
        trend_ok = compute_trend_filter(df["high"], df["low"], df["close"], params)
        special_buy = raw_special_buy & trend_ok
    else:
        trend_ok = pd.Series(True, index=df.index)
        special_buy = raw_special_buy

    i = len(df) - 1
    return SignalResult(
        special_buy=bool(special_buy.iloc[i]),
        st_sell=bool(st_sell.iloc[i]) if not pd.isna(st_sell.iloc[i]) else False,
        rsi=float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else float("nan"),
        rsi_signal=float(rsi_signal.iloc[i]) if not pd.isna(rsi_signal.iloc[i]) else float("nan"),
        cross_count=int(counts.iloc[i]),
        supertrend=float(supertrend.iloc[i]) if not pd.isna(supertrend.iloc[i]) else float("nan"),
        direction=int(direction.iloc[i]),
        close=float(df["close"].iloc[i]),
        candle_time=df.index[i],
        trend_filter_ok=bool(trend_ok.iloc[i]),
        raw_special_buy=bool(raw_special_buy.iloc[i]),
    )
