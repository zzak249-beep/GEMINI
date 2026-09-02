"""
Motor de señales: replica en Python el filtro Wavelet MRA Haar 5m del script
Pine, calculado directamente sobre velas de BingX. Sustituye a TradingView
como origen de la señal (no requiere plan de pago).

Importante sobre la salida: aquí solo se calcula la ENTRADA. El cierre se
delega al propio exchange — la orden se manda con `stopLoss`/`takeProfit`
embebidos (bingx_client.place_market_order), así que BingX cierra la
posición solo cuando toque SL o TP. El bot detecta ese cierre por
reconciliación periódica (ver poller.job_reconcile_closed_positions) y
actualiza el circuit breaker con el PnL realizado real, no simulado.
"""
import numpy as np
import pandas as pd


def _haar_detail(s: pd.Series, length: int) -> pd.Series:
    """Igual que haar_detail() del Pine: diferencia de dos SMA desplazadas."""
    avg_recent = s.rolling(length).mean()
    avg_prior = s.shift(length).rolling(length).mean()
    return (avg_recent - avg_prior) / np.sqrt(2)


def _rma(s: pd.Series, length: int) -> pd.Series:
    """Wilder's RMA — lo que usa ta.atr() de Pine internamente."""
    return s.ewm(alpha=1 / length, adjust=False).mean()


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return _rma(tr, length)


def compute_signal(df: pd.DataFrame, params: dict, last_signal_ts: int = None) -> dict:
    """
    df: columnas ['open_time','open','high','low','close','volume'], orden
        ascendente por tiempo. La ÚLTIMA fila debe ser la última vela YA
        CERRADA (el caller es responsable de descartar la vela en curso).
    params: lookback_energy, k_dominance, cooldown_bars, atr_length,
            atr_mult_sl, atr_mult_tp, bar_ms.
    last_signal_ts: open_time (ms) de la última señal disparada para este
        símbolo — aplica el cooldown en barras, igual que `can_signal` en Pine.

    Devuelve un dict con la señal de la última vela cerrada. Si no hay datos
    suficientes, is_trending/long_cond/short_cond salen en False.
    """
    if len(df) < 20:
        return {
            "timestamp": int(df["open_time"].iloc[-1]) if len(df) else None,
            "close": float(df["close"].iloc[-1]) if len(df) else None,
            "approx": None, "is_trending": False,
            "long_cond": False, "short_cond": False, "sl": None, "tp": None, "atr": None,
        }

    src = df["close"]
    h1 = _haar_detail(src, 1)
    h2 = _haar_detail(src, 2)
    h4 = _haar_detail(src, 4)
    h8 = _haar_detail(src, 8)

    lookback = params.get("lookback_energy", 40)
    e1 = (h1.fillna(0) ** 2).rolling(lookback).sum()
    e2 = (h2.fillna(0) ** 2).rolling(lookback).sum()
    e4 = (h4.fillna(0) ** 2).rolling(lookback).sum()
    e8 = (h8.fillna(0) ** 2).rolling(lookback).sum()
    fine = e1 + e2
    coarse = e4 + e8

    min_bars_ready = len(df) > (16 + lookback)
    k_dom = params.get("k_dominance", 1.5)
    fine_last, coarse_last = fine.iloc[-1], coarse.iloc[-1]
    is_trending = bool(
        min_bars_ready
        and not np.isnan(fine_last) and not np.isnan(coarse_last)
        and coarse_last > k_dom * fine_last
    )

    approx = src.rolling(8).mean()

    crossover = bool(src.iloc[-2] <= approx.iloc[-2] and src.iloc[-1] > approx.iloc[-1])
    crossunder = bool(src.iloc[-2] >= approx.iloc[-2] and src.iloc[-1] < approx.iloc[-1])

    cooldown_bars = params.get("cooldown_bars", 4)
    bar_ms = params.get("bar_ms", 5 * 60 * 1000)
    current_ts = int(df["open_time"].iloc[-1])
    can_signal = True
    if last_signal_ts is not None:
        bars_since = (current_ts - last_signal_ts) / bar_ms
        can_signal = bars_since >= cooldown_bars

    h8_last = h8.iloc[-1]
    h8_ok_long = (not np.isnan(h8_last)) and h8_last > 0
    h8_ok_short = (not np.isnan(h8_last)) and h8_last < 0

    long_cond = bool(is_trending and crossover and h8_ok_long and can_signal)
    short_cond = bool(is_trending and crossunder and h8_ok_short and can_signal)

    atr_length = params.get("atr_length", 14)
    atr_series = _atr(df, atr_length)
    atr_last = atr_series.iloc[-1]
    atr_last = float(atr_last) if not np.isnan(atr_last) else None

    close_price = float(src.iloc[-1])
    sl = tp = None
    if atr_last:
        mult_sl = params.get("atr_mult_sl", 1.5)
        mult_tp = params.get("atr_mult_tp", 2.5)
        if long_cond:
            sl = close_price - atr_last * mult_sl
            tp = close_price + atr_last * mult_tp
        elif short_cond:
            sl = close_price + atr_last * mult_sl
            tp = close_price - atr_last * mult_tp

    return {
        "timestamp": current_ts,
        "close": close_price,
        "approx": float(approx.iloc[-1]) if not np.isnan(approx.iloc[-1]) else None,
        "is_trending": is_trending,
        "long_cond": long_cond,
        "short_cond": short_cond,
        "sl": sl,
        "tp": tp,
        "atr": atr_last,
    }


def klines_to_df(rows) -> pd.DataFrame:
    """Normaliza la respuesta cruda de BingX (lista de dicts o de listas) a
    un DataFrame OHLCV ordenado ascendente por tiempo."""
    cols = ["open_time", "open", "high", "low", "close", "volume"]
    if not rows:
        return pd.DataFrame(columns=cols)

    if isinstance(rows[0], dict):
        df = pd.DataFrame(rows)
        rename = {"time": "open_time", "openTime": "open_time", "ts": "open_time"}
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"Faltan columnas en la respuesta de klines: {missing}")
        df = df[cols]
    else:
        df = pd.DataFrame(rows).iloc[:, :6]
        df.columns = cols

    for c in cols:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    return df.sort_values("open_time").reset_index(drop=True)
