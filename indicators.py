"""
indicators.py
=============
Réplica en Python, barra a barra, de la lógica del Pine Script v6 original
("ProBorsa: RSI & SuperTrend Özel Dip Stratejisi").

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

Todo el cálculo es secuencial (bar-by-bar) porque el contador y el
SuperTrend dependen de su propio valor previo, igual que en Pine.
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


def evaluate(df: pd.DataFrame, params: dict) -> SignalResult:
    """Calcula todos los indicadores sobre el DataFrame (columnas:
    open, high, low, close, volume; index = tiempo de apertura de vela,
    ascendente, SOLO velas cerradas) y devuelve el estado de la última vela.
    """
    rsi = compute_rsi(df["close"], params["rsi_length"])
    rsi_signal = rsi.rolling(params["sig_length"]).mean()
    special_buy, counts = compute_special_buy(
        rsi, rsi_signal, params["trigger_level"], params["target_cross_count"]
    )
    supertrend, direction = compute_supertrend(
        df["high"], df["low"], df["close"], params["atr_period"], params["st_factor"]
    )
    st_sell = direction.diff() > 0

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
    )
