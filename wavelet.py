"""
Port of the indicator math from the Pine Script (WMRA-H-5m).

Important, and worth repeating here because it matters for how much
confidence to place in this: this is NOT an orthogonal Daubechies DWT like
the wavelet math discussed in the original thread. It's a redundant,
causal "a trous"-style approximation — differences of simple moving
averages at dyadic scales (1, 2, 4, 8 bars) — used as a cheap trend/noise
energy filter. That's a legitimate, well-worn technique in signal
processing, but it is a different (much simpler) thing than the Daubechies
CWT/DWT machinery, and it does not inherit the same theoretical guarantees.

Every rolling window below is causal (it only looks at bars up to and
including the current one), which mirrors Pine's default non-repainting
behaviour. Nothing here peeks into the future.
"""
import numpy as np
import pandas as pd


def haar_detail(series: pd.Series, length: int) -> pd.Series:
    """
    Equivalent of the Pine `haar(s, len)` function:
        avg_recent = sma(s, len)
        avg_prior  = sma(s[len], len)   // same average, shifted back `len` bars
        detail     = (avg_recent - avg_prior) / sqrt(2)
    """
    avg_recent = series.rolling(length).mean()
    avg_prior = series.shift(length).rolling(length).mean()
    return (avg_recent - avg_prior) / np.sqrt(2)


def rolling_energy(series: pd.Series, lookback: int) -> pd.Series:
    """Equivalent of Pine's `math.sum(x * x, lookback)`, with NaNs from
    warm-up treated as 0 (matches Pine's `nz()`)."""
    return (series.fillna(0.0) ** 2).rolling(lookback).sum()


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Wilder's smoothed ATR — matches Pine's built-in ta.atr(), which
    uses RMA (alpha = 1/length), not a plain SMA of true range."""
    tr = true_range(df)
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


class WaveletRegime:
    """Computes the trend/noise regime filter and the approximation line
    used for the crossover signal, for an entire OHLCV DataFrame at once."""

    def __init__(self, lookback_energy: int = 40, k_dominance: float = 1.5):
        self.lookback_energy = lookback_energy
        self.k_dominance = k_dominance
        # matches Pine's `min_bars_ready = bar_index > (16 + lookback_energy)`
        self.min_bars_required = 16 + lookback_energy

    def compute(self, df: pd.DataFrame, price_col: str = "close", atr_length: int = 14) -> pd.DataFrame:
        if len(df) < self.min_bars_required + 2:
            raise ValueError(
                f"Need at least {self.min_bars_required + 2} candles to "
                f"compute a reliable signal, got {len(df)}. Increase the "
                f"OHLCV fetch limit."
            )

        src = df[price_col].astype(float)
        out = pd.DataFrame(index=df.index)

        out["h1"] = haar_detail(src, 1)
        out["h2"] = haar_detail(src, 2)
        out["h4"] = haar_detail(src, 4)
        out["h8"] = haar_detail(src, 8)

        e1 = rolling_energy(out["h1"], self.lookback_energy)
        e2 = rolling_energy(out["h2"], self.lookback_energy)
        e4 = rolling_energy(out["h4"], self.lookback_energy)
        e8 = rolling_energy(out["h8"], self.lookback_energy)

        fine = e1 + e2
        coarse = e4 + e8
        out["fine"] = fine
        out["coarse"] = coarse

        bar_index = np.arange(len(df))
        ready = pd.Series(bar_index > self.min_bars_required, index=df.index)
        out["is_trending"] = ready & (coarse > self.k_dominance * fine)

        out["approx"] = src.rolling(8).mean()
        out["atr"] = atr(df, length=atr_length)

        out["cross_up"] = crossover(src, out["approx"])
        out["cross_down"] = crossunder(src, out["approx"])

        return out
