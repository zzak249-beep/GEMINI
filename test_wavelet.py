import numpy as np
import pandas as pd
import pytest

from bot.wavelet import WaveletRegime, atr, crossover, crossunder, haar_detail, rolling_energy


def _make_ohlcv(n=200, seed=42):
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.002, n)
    close = 100 * np.cumprod(1 + returns)
    high = close * (1 + np.abs(rng.normal(0, 0.001, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.001, n)))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.uniform(100, 1000, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


def test_haar_detail_constant_series_is_zero():
    s = pd.Series([5.0] * 20)
    d = haar_detail(s, 4).dropna()
    assert np.allclose(d, 0.0, atol=1e-9)


def test_haar_detail_length_matches_input():
    s = pd.Series(np.arange(20, dtype=float))
    assert len(haar_detail(s, 4)) == len(s)


def test_rolling_energy_nonnegative():
    s = pd.Series(np.random.default_rng(0).normal(0, 1, 100))
    e = rolling_energy(s, 10).dropna()
    assert (e >= 0).all()


def test_crossover_crossunder_basic():
    a = pd.Series([1, 2, 3, 2, 1])
    b = pd.Series([2, 2, 2, 2, 2])
    assert crossover(a, b).iloc[2] == True  # noqa: E712
    assert crossunder(a, b).iloc[4] == True  # noqa: E712


def test_atr_nonnegative():
    df = _make_ohlcv(100)
    assert (atr(df, length=14).dropna() >= 0).all()


def test_regime_requires_minimum_bars():
    df = _make_ohlcv(30)
    regime = WaveletRegime(lookback_energy=40, k_dominance=1.5)
    with pytest.raises(ValueError):
        regime.compute(df)


def test_regime_output_columns_present():
    df = _make_ohlcv(200)
    regime = WaveletRegime(lookback_energy=40, k_dominance=1.5)
    out = regime.compute(df)
    for col in ["h1", "h2", "h4", "h8", "fine", "coarse", "is_trending", "approx", "atr", "cross_up", "cross_down"]:
        assert col in out.columns


def test_regime_is_causal_no_lookahead():
    """This is the property that actually matters for live trading: every
    value computed for bar i must depend only on bars <= i. We check it by
    recomputing on a truncated history and confirming nothing already
    computed changes. If this test ever fails after an edit, the indicator
    has started repainting and must not be trusted live."""
    df = _make_ohlcv(200)
    regime = WaveletRegime(lookback_energy=40, k_dominance=1.5)

    full = regime.compute(df)
    cutoff = 150
    partial = regime.compute(df.iloc[:cutoff])

    cols = ["h1", "h2", "h4", "h8", "approx", "is_trending", "atr"]
    pd.testing.assert_frame_equal(
        full.iloc[:cutoff][cols], partial[cols], check_exact=False, rtol=1e-9
    )
