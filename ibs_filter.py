"""
IBS Pullback SHORT Filter — ibs_filter.py
══════════════════════════════════════════════════════════════
Portado del Pine Script "[SHORT ONLY] 10 Bar Low Pullback" (Botnet101).

Internal Bar Strength (IBS) = (close - low) / (high - low)
  ≥ 0.85 → cierre cerca del HIGH de la vela

Lógica SHORT:
  1. low[-1] < min(low[-N:-1])  → nuevo mínimo de N barras
  2. ibs > umbral (0.85)        → el cierre recuperó hacia el HIGH
     = vela de "ruptura bajista falsa" o "sell the rip intrabarra"
  3. close < EMA(period)        → contexto bajista macro (opcional)

Exit natural: trailing stop dinámico del position_manager del bot.
La condición Pine original (close < low[1]) queda cubierta por él.

⚠️ Nota de timeframe:
  El Pine original está calibrado para velas DIARIAS en índices/ETFs.
  En 3m crypto, los parámetros por defecto son más agresivos:
    - IBS_LOOKBACK=10  → mínimo de los últimos 30 minutos
    - IBS_EMA_PERIOD=50 → ~2.5h de tendencia, más relevante que EMA200
  Activar primero en MODE=SIGNAL (IBS_PULLBACK_ENABLED=true) y revisar
  cuántas señales lanza antes de poner IBS_REQUIRE=true.

Integración: paso 5h en scanner.py, misma arquitectura que
  price_action_framework.py, trend_magic_rmi.py, stc_asymmetry.py.
══════════════════════════════════════════════════════════════
"""
import numpy as np


def _ema_ibs(arr: np.ndarray, period: int) -> np.ndarray:
    k = 2.0 / (period + 1)
    out = np.empty_like(arr, dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def ibs_pullback_filter(
    klines: list,
    direction: str,
    lookback: int     = 10,
    ibs_threshold: float = 0.85,
    ema_period: int   = 50,
    use_ema_filter: bool = True,
    boost_amount: float  = 8.0,
) -> tuple[float, str, bool]:
    """
    Retorna (boost, reason, block) — mismo patrón que todos los filtros del bot.

      boost > 0  → setup IBS confirmado, suma al score
      block=True → señal vetada (encima de EMA en contexto alcista) → return None
      (0, reason, False) → neutral, sin efecto

    Solo actúa en señales SHORT.
    Para LONG devuelve siempre (0, "ibs_long_skip", False) — no veta LONGs,
    solo añade señal cuando hay SHORT confirmado por IBS.
    """
    if direction != "SHORT":
        return 0.0, "ibs_long_skip", False

    min_bars = max(lookback + 5, ema_period + 5 if use_ema_filter else lookback + 5)
    if len(klines) < min_bars:
        return 0.0, "ibs_insufficient_data", False

    arr = np.array(klines, dtype=float)
    h = arr[:, 2]   # high
    l = arr[:, 3]   # low
    c = arr[:, 4]   # close

    # ── IBS de la barra actual ─────────────────────────────────────────────
    hl_range = h[-1] - l[-1]
    if hl_range < 1e-12:
        return 0.0, "ibs_zero_range", False
    ibs = (c[-1] - l[-1]) / hl_range

    # ── Nuevo mínimo de N barras (excluye barra actual, igual que Pine [1]) ──
    lowest_low = float(np.min(l[-(lookback + 1):-1]))
    new_low = bool(l[-1] < lowest_low)

    # ── EMA trend filter ──────────────────────────────────────────────────
    below_ema = True
    ema_val   = 0.0
    if use_ema_filter:
        ema_arr  = _ema_ibs(c, ema_period)
        ema_val  = float(ema_arr[-1])
        below_ema = bool(c[-1] < ema_val)

    # ── Evaluación ────────────────────────────────────────────────────────
    if not new_low:
        # Sin nuevo mínimo → no hay setup IBS (neutral, no veta)
        return 0.0, f"ibs_no_new_low(ibs={ibs:.2f})", False

    if use_ema_filter and not below_ema:
        # Encima de EMA → contexto alcista → vetar SHORT en joyful-art
        return 0.0, (
            f"ibs_above_ema{ema_period}(ibs={ibs:.2f} ema={ema_val:.6f})"
        ), True

    if not (ibs >= ibs_threshold):
        # Nuevo mínimo pero cierre cerca del LOW → ruptura real bajista,
        # NO un pullback — sin boost IBS (el scanner puede seguir si score OK)
        return 0.0, f"ibs_low_close(ibs={ibs:.2f}<{ibs_threshold})", False

    # ── Setup completo: nuevo mínimo + cierre recuperado al HIGH ─────────
    # En contexto bajista (below EMA) esto es un "sell the rip" intra-barra
    reason = (
        f"✅ IBS_pullback SHORT: new_{lookback}bar_low={lowest_low:.6f} "
        f"ibs={ibs:.2f}>={ibs_threshold} "
        f"{'below_ema' + str(ema_period) + '=' + f'{ema_val:.6f}' if use_ema_filter else 'ema_off'}"
    )
    return boost_amount, reason, False
