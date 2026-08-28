"""
backtest.py
============
Descarga histórico real de BingX (paginando hacia atrás con endTime) y
simula la estrategia RSI+SuperTrend "Doble Dip" -exactamente con la misma
lógica de indicators.py que usa el bot en vivo- sobre varias temporalidades
a la vez, para comparar cuál rinde mejor después de comisiones.

USO
---
    python backtest.py --symbol BTC-USDT --timeframes 5m,15m,30m,1h,2h,4h,1d --days 120

Requiere BINGX_API_KEY / BINGX_API_SECRET en el entorno (o en un .env en el
mismo directorio) - son endpoints públicos de mercado, no hace falta que la
key tenga permisos de trading para esto. NO requiere las variables de
Telegram (a propósito, para poder correr este script suelto sin desplegar
el bot).

Fidelidad al bot real: la entrada se simula al CIERRE de la vela de señal
(igual que hace trading_bot.py: detecta al cierre y manda MARKET casi de
inmediato) y no a la apertura de la siguiente vela como asume Pine Script
por defecto - con datos de 15m+ la diferencia es mínima, pero en 1m/5m
puede notarse un poco. El stop-loss de seguridad se simula como una orden
STOP_MARKET real: se dispara si el "low" de una vela posterior a la entrada
toca el precio de stop, igual que en el exchange.

LIMITACIÓN CONOCIDA: es una simulación con datos históricos de un único
símbolo/periodo - no reemplaza el forward-testing en DRY_RUN. Un
"buen" resultado aquí es evidencia, no garantía.
"""

from __future__ import annotations

import argparse
import os
import time

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from bingx_client import BingXClient
from indicators import compute_rsi, compute_special_buy, compute_supertrend

TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}

DEFAULT_TIMEFRAMES = "5m,15m,30m,1h,2h,4h,1d"


def _env(name: str, default=None, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return cast(raw.split("#", 1)[0].strip().strip("'\""))


# ---------------------------------------------------------------------
# Descarga de histórico con paginación
# ---------------------------------------------------------------------
def fetch_history(client: BingXClient, symbol: str, interval: str, days: int, max_candles: int = 20000) -> pd.DataFrame:
    minutes = TIMEFRAME_MINUTES[interval]
    target = min(int(days * 24 * 60 / minutes) + 5, max_candles)

    frames = []
    end_time = None
    fetched = 0
    oldest_seen = None

    while fetched < target:
        page_limit = min(1000, target - fetched)
        df = client.get_klines(symbol, interval, limit=page_limit, end_time=end_time)
        if df.empty:
            break
        frames.append(df)
        fetched += len(df)
        oldest = df.index[0]
        if oldest_seen is not None and oldest >= oldest_seen:
            break  # sin progreso -> ya no hay más historia, evita bucle infinito
        oldest_seen = oldest
        end_time = int(oldest.timestamp() * 1000) - 1
        time.sleep(0.25)  # amable con el rate limit al paginar muchas páginas

    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    full = pd.concat(frames)
    full = full[~full.index.duplicated(keep="first")].sort_index()

    # Igual que en vivo: si la última vela aún no ha cerrado, se descarta.
    now = pd.Timestamp.now(tz="UTC")
    tf = pd.Timedelta(minutes=minutes)
    if len(full) and (full.index[-1] + tf) > now:
        full = full.iloc[:-1]

    return full


# ---------------------------------------------------------------------
# Simulación de operaciones
# ---------------------------------------------------------------------
def simulate_trades(
    df: pd.DataFrame,
    params: dict,
    fee_rate: float,
    position_size_pct: float,
    leverage: float,
    stop_loss_pct: float,
    starting_equity: float = 1000.0,
) -> tuple[list[dict], list[float]]:
    """Simula la estrategia barra a barra, con el mismo dimensionamiento
    (position_size_pct % de equity * leverage) y comisiones del bot real.
    fee_rate: fracción por lado (0.0005 = 0.05%), se cobra en entrada Y salida.
    """
    rsi = compute_rsi(df["close"], params["rsi_length"])
    rsi_signal = rsi.rolling(params["sig_length"]).mean()
    special_buy, _ = compute_special_buy(
        rsi, rsi_signal, params["trigger_level"], params["target_cross_count"]
    )
    supertrend, direction = compute_supertrend(
        df["high"], df["low"], df["close"], params["atr_period"], params["st_factor"]
    )
    st_sell = direction.diff() > 0

    closes = df["close"].to_numpy()
    lows = df["low"].to_numpy()
    times = df.index

    equity = starting_equity
    equity_curve = []
    trades = []

    in_position = False
    entry_price = entry_time = entry_index = stop_price = None

    n = len(df)
    for i in range(n):
        if not in_position:
            equity_curve.append(equity)
            if bool(special_buy.iloc[i]):
                in_position = True
                entry_price = closes[i]
                entry_time = times[i]
                entry_index = i
                stop_price = entry_price * (1 - stop_loss_pct / 100.0) if stop_loss_pct > 0 else None
            continue

        exit_price = None
        exit_reason = None
        if i > entry_index:
            if stop_price is not None and lows[i] <= stop_price:
                exit_price = stop_price
                exit_reason = "stop-loss"
            elif bool(st_sell.iloc[i]):
                exit_price = closes[i]
                exit_reason = "SuperTrend"
            elif i == n - 1:
                # Fin de los datos con posición todavía abierta: se marca a
                # mercado para que la curva de equity refleje el estado real.
                exit_price = closes[i]
                exit_reason = "fin_de_datos (abierta)"

        if exit_price is not None:
            notional = equity * (position_size_pct / 100.0) * leverage
            gross_pct = (exit_price / entry_price) - 1.0
            gross_pnl = notional * gross_pct
            fee = notional * fee_rate * 2  # entrada + salida
            net_pnl = gross_pnl - fee
            equity += net_pnl
            trades.append(
                {
                    "entry_time": entry_time,
                    "exit_time": times[i],
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "bars_held": i - entry_index,
                    "gross_pct": gross_pct * 100,
                    "fee": fee,
                    "net_pnl": net_pnl,
                    "exit_reason": exit_reason,
                }
            )
            in_position = False
            entry_price = entry_time = entry_index = stop_price = None

        equity_curve.append(equity)

    return trades, equity_curve


def compute_metrics(trades: list[dict], equity_curve: list[float], starting_equity: float) -> dict:
    if not trades:
        return {
            "num_trades": 0, "win_rate_pct": float("nan"), "profit_factor": float("nan"),
            "total_return_pct": 0.0, "max_drawdown_pct": 0.0, "avg_bars_held": float("nan"),
            "final_equity": starting_equity,
        }

    t = pd.DataFrame(trades)
    wins = t[t["net_pnl"] > 0]
    losses = t[t["net_pnl"] <= 0]

    gross_win = wins["net_pnl"].sum()
    gross_loss = -losses["net_pnl"].sum()
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else float("nan")

    eq = pd.Series(equity_curve)
    running_max = eq.cummax()
    drawdown = (eq - running_max) / running_max * 100
    final_equity = equity_curve[-1]

    return {
        "num_trades": len(t),
        "win_rate_pct": len(wins) / len(t) * 100,
        "profit_factor": profit_factor,
        "total_return_pct": (final_equity / starting_equity - 1) * 100,
        "max_drawdown_pct": drawdown.min(),
        "avg_bars_held": t["bars_held"].mean(),
        "final_equity": final_equity,
    }


# ---------------------------------------------------------------------
# Barrido multi-temporalidad
# ---------------------------------------------------------------------
def run_sweep(
    client: BingXClient,
    symbol: str,
    timeframes: list[str],
    days: int,
    params: dict,
    fee_rate: float,
    position_size_pct: float,
    leverage: float,
    stop_loss_pct: float,
    save_trades_dir: str | None = None,
) -> pd.DataFrame:
    min_needed = max(params["rsi_length"], params["atr_period"]) + params["sig_length"] + 30
    rows = []

    for tf in timeframes:
        tf = tf.strip()
        if tf not in TIMEFRAME_MINUTES:
            print(f"[{tf}] temporalidad no reconocida, se omite (usa: {', '.join(TIMEFRAME_MINUTES)})")
            continue
        print(f"[{tf}] descargando histórico ({days} días)...")
        try:
            df = fetch_history(client, symbol, tf, days)
        except Exception as exc:
            print(f"[{tf}] error al descargar: {exc}")
            continue

        if len(df) < min_needed:
            print(f"[{tf}] histórico insuficiente ({len(df)} velas, se necesitan >= {min_needed}), se omite")
            continue

        trades, equity_curve = simulate_trades(
            df, params, fee_rate, position_size_pct, leverage, stop_loss_pct
        )
        metrics = compute_metrics(trades, equity_curve, starting_equity=1000.0)
        metrics.update(
            {
                "timeframe": tf,
                "velas": len(df),
                "periodo": f"{df.index[0].date()} -> {df.index[-1].date()}",
            }
        )
        rows.append(metrics)
        print(f"[{tf}] {metrics['num_trades']} operaciones | retorno {metrics['total_return_pct']:.1f}% | "
              f"drawdown máx {metrics['max_drawdown_pct']:.1f}%")

        if save_trades_dir and trades:
            os.makedirs(save_trades_dir, exist_ok=True)
            pd.DataFrame(trades).to_csv(os.path.join(save_trades_dir, f"trades_{tf}.csv"), index=False)

    if not rows:
        return pd.DataFrame()

    cols = [
        "timeframe", "velas", "periodo", "num_trades", "win_rate_pct",
        "profit_factor", "total_return_pct", "max_drawdown_pct",
        "avg_bars_held", "final_equity",
    ]
    return pd.DataFrame(rows)[cols].sort_values("total_return_pct", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Backtest RSI+SuperTrend Doble Dip en varias temporalidades (BingX)")
    parser.add_argument("--symbol", default=_env("SYMBOL", "BTC-USDT"))
    parser.add_argument("--timeframes", default=DEFAULT_TIMEFRAMES, help="lista separada por comas, ej: 15m,1h,4h")
    parser.add_argument("--days", type=int, default=120, help="días de histórico por temporalidad (por defecto 120)")
    parser.add_argument("--fee", type=float, default=0.05, help="comisión taker por lado en %% (por defecto 0.05, tarifa estándar BingX)")
    parser.add_argument("--leverage", type=float, default=_env("LEVERAGE", 3, float))
    parser.add_argument("--position-size", type=float, default=_env("POSITION_SIZE_PCT", 20, float))
    parser.add_argument("--stop-loss", type=float, default=_env("STOP_LOSS_PCT", 5, float))
    parser.add_argument("--rsi-length", type=int, default=_env("RSI_LENGTH", 10, int))
    parser.add_argument("--sig-length", type=int, default=_env("SIG_LENGTH", 10, int))
    parser.add_argument("--trigger-level", type=float, default=_env("TRIGGER_LEVEL", 50, float))
    parser.add_argument("--target-cross-count", type=int, default=_env("TARGET_CROSS_COUNT", 2, int))
    parser.add_argument("--atr-period", type=int, default=_env("ATR_PERIOD", 10, int))
    parser.add_argument("--st-factor", type=float, default=_env("ST_FACTOR", 2.5, float))
    parser.add_argument("--out", default="backtest_resultados.csv")
    parser.add_argument("--save-trades", action="store_true", help="guarda el detalle de cada operación en trades_<tf>.csv")
    args = parser.parse_args()

    api_key = _env("BINGX_API_KEY")
    api_secret = _env("BINGX_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit(
            "Falta BINGX_API_KEY / BINGX_API_SECRET en el entorno (o en un .env en este directorio). "
            "Solo se usan endpoints públicos de klines, no hace falta permiso de trading en la key."
        )

    client = BingXClient(api_key, api_secret)
    params = {
        "rsi_length": args.rsi_length,
        "sig_length": args.sig_length,
        "trigger_level": args.trigger_level,
        "target_cross_count": args.target_cross_count,
        "atr_period": args.atr_period,
        "st_factor": args.st_factor,
    }
    timeframes = [t for t in args.timeframes.split(",") if t.strip()]

    print(
        f"Backtest {args.symbol} | {args.days} días | comisión {args.fee}%/lado (round-trip {args.fee*2}%) | "
        f"leverage {args.leverage}x | tamaño {args.position_size}% equity | stop-loss {args.stop_loss}%\n"
        f"Estrategia: RSI({args.rsi_length}) / SMA({args.sig_length}) / trigger {args.trigger_level} / "
        f"doble cruce #{args.target_cross_count} | SuperTrend ATR({args.atr_period}) x{args.st_factor}\n"
    )

    save_dir = "backtest_trades" if args.save_trades else None
    results = run_sweep(
        client, args.symbol, timeframes, args.days, params,
        args.fee / 100.0, args.position_size, args.leverage, args.stop_loss,
        save_trades_dir=save_dir,
    )

    print()
    if results.empty:
        print("Sin resultados (revisa símbolo / temporalidades / histórico disponible).")
        return

    with pd.option_context("display.width", 160, "display.float_format", "{:.2f}".format):
        print(results.to_string(index=False))

    results.to_csv(args.out, index=False)
    print(f"\nGuardado en {args.out}" + (f" y detalle de operaciones en {save_dir}/" if save_dir else ""))
    print(
        "\nOjo: profit_factor = ganancia bruta / pérdida bruta (>1 = rentable en el periodo probado). "
        "num_trades bajo (<20-30) no es una muestra fiable, aunque el retorno parezca bueno."
    )


if __name__ == "__main__":
    main()
