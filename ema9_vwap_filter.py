"""
EMA9 × VWAP Crossunder SHORT Filter — ema9_vwap_filter.py
══════════════════════════════════════════════════════════════
Portado de Pine v5 "EMA 9 + VWAP Strategy with ATR Trailing Stop".

Señal SHORT: EMA9 cruza hacia abajo la VWAP
  = momentum bajista cruzando el nivel institucional de referencia

Solo usamos la mitad SHORT del Pine original (joyful-art = SHORT-only).
El ATR trailing stop ya está en position_manager.py del bot.

Diferencia vs Pine:
  Pine VWAP reinicia cada día (sesión). Aquí usamos VWAP acumulada
  sobre las klines disponibles (~200 barras de 3m = 10h). En crypto
  perpetuos sin sesión fija es el equivalente más correcto.

Modos de detección:
  STRICT:  crossunder exacto en barra actual (EMA9[-1] < VWAP[-1] y EMA9[-2] >= VWAP[-2])
  RECENT:  crossunder en las últimas N barras (más útil para scanner con polling 60s)

Parámetros Railway:
  EMA9_VWAP_ENABLED=true
  EMA9_VWAP_LOOKBACK=5      (barras atrás para detectar crossunder reciente)
  EMA9_VWAP_BOOST=9.0       (puntos sumados al score SHORT)
  EMA9_VWAP_STRICT=false    (true = solo barra exacta, false = últimas N barras)
  EMA9_VWAP_VETO_LONG=true  (veta LONGs cuando EMA9 < VWAP = contexto bajista)

Integración: paso 5j en scanner.py, mismo patrón que ibs_filter.py.
══════════════════════════════════════════════════════════════
"""
import numpy as np


def _ema_ev(arr: np.ndarray, period: int) -> np.ndarray:
    k = 2.0 / (period + 1)
    out = np.empty_like(arr, dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _vwap_cumulative(klines_arr: np.ndarray) -> np.ndarray:
    """
    VWAP acumulada = sum(hlc3 × vol) / sum(vol).
    Aproxima el VWAP de Pine para sesión única (crypto = sin reset diario).
    """
    h   = klines_arr[:, 2]
    l   = klines_arr[:, 3]
    c   = klines_arr[:, 4]
    v   = klines_arr[:, 5]
    hlc3 = (h + l + c) / 3.0
    cum_pv  = np.cumsum(hlc3 * v)
    cum_vol = np.cumsum(v)
    return np.divide(cum_pv, cum_vol + 1e-12,
                     out=np.zeros_like(cum_pv), where=cum_vol > 0)


def ema9_vwap_filter(
    klines: list,
    direction: str,
    lookback: int    = 5,
    boost_amount: float = 9.0,
    strict: bool     = False,
    veto_long: bool  = True,
) -> tuple[float, str, bool]:
    """
    Retorna (boost, reason, block) — patrón estándar de filtros del bot.

    SHORT + crossunder reciente (últimas `lookback` barras):
        → boost_amount al score

    LONG + EMA9 < VWAP (contexto bajista) + veto_long=True:
        → block=True (EMA9 bajo VWAP = momento bajista, mala entrada LONG)

    Cualquier otro caso: (0, reason, False) — neutral
    """
    min_bars = max(lookback + 5, 12)
    if len(klines) < min_bars:
        return 0.0, "ema9_vwap_insufficient_data", False

    arr  = np.array(klines, dtype=float)
    c    = arr[:, 4]

    ema9_arr = _ema_ev(c, 9)
    vwap_arr = _vwap_cumulative(arr)

    curr_ema9  = float(ema9_arr[-1])
    curr_vwap  = float(vwap_arr[-1])
    curr_below = curr_ema9 < curr_vwap   # EMA9 actualmente bajo VWAP

    # ── Detección de crossunder ───────────────────────────────────────────
    crossunder_found  = False
    crossunder_bars_ago = 0

    if strict:
        # Solo barra actual: EMA9[-1] < VWAP[-1] y EMA9[-2] >= VWAP[-2]
        if (len(ema9_arr) >= 2 and
                ema9_arr[-1] < vwap_arr[-1] and
                ema9_arr[-2] >= vwap_arr[-2]):
            crossunder_found    = True
            crossunder_bars_ago = 0
    else:
        # Crossunder en las últimas N barras
        n = min(lookback + 1, len(ema9_arr) - 1)
        for i in range(1, n + 1):
            if ema9_arr[-i] < vwap_arr[-i] and ema9_arr[-(i+1)] >= vwap_arr[-(i+1)]:
                crossunder_found    = True
                crossunder_bars_ago = i - 1
                break

    # ── Distancia EMA9 vs VWAP (para diagnóstico) ─────────────────────────
    gap_pct = (curr_vwap - curr_ema9) / curr_vwap * 100 if curr_vwap > 0 else 0.0

    # ── Evaluación SHORT ──────────────────────────────────────────────────
    if direction == "SHORT":
        if crossunder_found:
            ago_str = "barra_actual" if crossunder_bars_ago == 0 else f"hace_{crossunder_bars_ago}b"
            reason  = (
                f"✅ EMA9×VWAP crossunder SHORT ({ago_str}) "
                f"ema9={curr_ema9:.6f} vwap={curr_vwap:.6f} "
                f"gap={gap_pct:.2f}%"
            )
            return boost_amount, reason, False

        if curr_below:
            # EMA9 bajo VWAP pero sin crossunder reciente → contexto bajista
            # No boosteamos (ya pasó el momento óptimo) pero tampoco bloqueamos
            return 0.0, (
                f"ema9_below_vwap_no_cross: ema9={curr_ema9:.6f} "
                f"vwap={curr_vwap:.6f} gap={gap_pct:.2f}%"
            ), False

        # EMA9 encima de VWAP → contexto alcista → SHORT menos favorable
        # Penalización suave: no bloqueamos (otros filtros pueden confirmar)
        return 0.0, (
            f"ema9_above_vwap(bullish_context): "
            f"ema9={curr_ema9:.6f} vwap={curr_vwap:.6f}"
        ), False

    # ── Evaluación LONG ───────────────────────────────────────────────────
    if direction == "LONG":
        if curr_below and veto_long:
            # EMA9 bajo VWAP = contexto bajista → mala entrada LONG
            return 0.0, (
                f"🚫 EMA9_vwap veto LONG: ema9={curr_ema9:.6f} < "
                f"vwap={curr_vwap:.6f} (contexto bajista)"
            ), True
        return 0.0, "ema9_vwap_long_ok", False

    return 0.0, "ema9_vwap_direction_unknown", False
