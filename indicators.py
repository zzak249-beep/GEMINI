"""
indicators.py — Score compuesto · Decaimiento · CVD Delta
Los tres pilares del bot
"""
import numpy as np
import pandas as pd


# ── Helpers ───────────────────────────────────────────────────

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def sma(s: pd.Series, p: int) -> pd.Series:
    return s.rolling(p).mean()

def stdev(s: pd.Series, p: int) -> pd.Series:
    return s.rolling(p).std(ddof=0)

def f_tanh(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, -10, 10)
    e2x = np.exp(2 * x)
    return (e2x - 1) / (e2x + 1)


# ── ATR ───────────────────────────────────────────────────────

def atr(high: pd.Series, low: pd.Series, close: pd.Series, p: int = 10) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()


# ── SCORE COMPUESTO (L2) ──────────────────────────────────────

def composite_score(close: pd.Series, volume: pd.Series,
                    mom_p: int, rev_p: int, vol_p: int,
                    w1: float, w2: float, w3: float,
                    smooth: int, decay_len: int):
    """
    Combina Momentum + Mean-Reversion + OBV Volume.
    Devuelve norm_score (-1 a +1) y los tres factores individuales.
    """
    # Momentum: ROC normalizado por volatilidad
    roc    = (close - close.shift(mom_p)) / close.shift(mom_p)
    vol_n  = stdev(close, mom_p) / sma(close, mom_p)
    f_mom  = (roc / vol_n.replace(0, np.nan)).fillna(0)

    # Mean-Reversion: Z-score invertido
    basis  = sma(close, rev_p)
    b_std  = stdev(close, rev_p)
    f_rev  = (-(close - basis) / b_std.replace(0, np.nan)).fillna(0)

    # Volumen: OBV desviado de su media
    direction = np.sign(close.diff()).fillna(0)
    obv_s  = (direction * volume).cumsum()
    obv_ma = ema(obv_s, vol_p)
    obv_sd = stdev(obv_s, vol_p)
    f_vol  = ((obv_s - obv_ma) / obv_sd.replace(0, np.nan)).fillna(0)

    # Combinar y normalizar
    raw    = w1 * f_mom + w2 * f_rev + w3 * f_vol
    comp   = ema(raw, smooth)
    sc_std = stdev(comp, decay_len)
    norm   = (comp / sc_std.replace(0, np.nan)).fillna(0)
    score  = pd.Series(f_tanh(norm.values), index=close.index)

    return score, f_mom, f_rev, f_vol


# ── DECAIMIENTO (L3) ──────────────────────────────────────────

def signal_decay(score: pd.Series, close: pd.Series,
                 decay_len: int, smooth: int, thr: float):
    """
    Mide si el score todavía predice el futuro.
    Devuelve: sig_alive (bool series), decay_ratio (0-1)
    """
    fwd_ret  = close.pct_change()
    ic_raw   = score.shift(1).rolling(decay_len).corr(fwd_ret)
    ic_roll  = ema(ic_raw.abs(), smooth)
    ic_peak  = ic_roll.rolling(decay_len).max()
    decay_r  = (ic_roll / ic_peak.replace(0, np.nan)).fillna(0.5)
    sig_alive= decay_r >= thr
    return sig_alive, decay_r


# ── CVD DELTA (L11) ───────────────────────────────────────────

def cvd_delta(high: pd.Series, low: pd.Series,
              close: pd.Series, volume: pd.Series,
              ema_len: int, div_len: int):
    """
    Estimación de volumen comprador/vendedor vela a vela.

    CVD rising  → presión compradora dominante
    bull_div    → precio baja pero CVD sube (acumulación oculta) → FUERTE LONG
    bear_div    → precio sube pero CVD baja (distribución oculta) → NO ENTRAR LONG

    Devuelve: cvd_rising, bull_div, bear_div, cvd_raw, cvd_ema_s
    """
    hl = (high - low).replace(0, np.nan)

    # Volumen estimado comprador y vendedor desde estructura de vela
    bvol = ((close - low)  / hl * volume).fillna(volume * 0.5)
    svol = ((high - close) / hl * volume).fillna(volume * 0.5)

    delta     = (bvol - svol).cumsum()
    delta_ema = ema(delta, ema_len)

    cvd_rising = delta > delta_ema
    cvd_slope  = delta - delta.shift(3)  # velocidad del CVD

    # Divergencias
    bull_div = (close < close.shift(div_len)) & (delta > delta.shift(div_len))
    bear_div = (close > close.shift(div_len)) & (delta < delta.shift(div_len))

    return cvd_rising, bull_div, bear_div, delta, delta_ema


# ── HTF RÉGIMEN ───────────────────────────────────────────────

def htf_regime(close_htf: pd.Series, fast: int = 9, slow: int = 21):
    """EMA9 vs EMA21 en timeframe superior"""
    f = close_htf.ewm(span=fast, adjust=False).mean()
    s = close_htf.ewm(span=slow, adjust=False).mean()
    return bool(f.iloc[-1] > s.iloc[-1]), bool(f.iloc[-1] < s.iloc[-1])
