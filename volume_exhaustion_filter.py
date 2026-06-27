"""
Volume Exhaustion SHORT Filter — volume_exhaustion_filter.py
══════════════════════════════════════════════════════════════
Señal: precio subiendo con volumen decreciente en barras alcistas
  = fuerza compradora agotándose = probable techo y reversión → SHORT

Lógica:
  Un rally saludable tiene volumen creciente o estable.
  Cuando el precio sigue subiendo pero el volumen de las barras
  alcistas es cada vez menor → los compradores pierden convicción.
  El mercado sube por inercia, no por presión real.

  Analiza solo las barras alcistas del lookback (donde close > prev close)
  y calcula la pendiente del volumen en esas barras.

Sin llamada extra a la API — reutiliza k3m ya fetcheado.
Función stateless (no necesita historial).

Parámetros Railway:
  VOL_EXHAUST_ENABLED=true
  VOL_EXHAUST_LOOKBACK=12       (barras a analizar)
  VOL_EXHAUST_MIN_UP_BARS=3     (mínimo de barras alcistas en el período)
  VOL_EXHAUST_SLOPE_THR=-0.05   (pendiente normalizada del volumen)
  VOL_EXHAUST_BOOST=8.0
══════════════════════════════════════════════════════════════
"""
import numpy as np


def _linreg_slope_normalized(arr: np.ndarray) -> float:
    """Pendiente de regresión lineal normalizada por la media del array."""
    n = len(arr)
    if n < 2:
        return 0.0
    x      = np.arange(n, dtype=float)
    x_mean = x.mean()
    y_mean = arr.mean()
    if y_mean < 1e-12:
        return 0.0
    num = float(np.sum((x - x_mean) * (arr - y_mean)))
    den = float(np.sum((x - x_mean) ** 2))
    if den < 1e-12:
        return 0.0
    return (num / den) / y_mean   # pendiente relativa a la media


def volume_exhaustion_filter(
    klines: list,
    direction: str,
    lookback: int         = 12,
    min_up_bars: int      = 3,
    vol_slope_threshold: float = -0.05,
    boost_amount: float   = 8.0,
) -> tuple[float, str, bool]:
    """
    Retorna (boost, reason, block) — patrón estándar de filtros del bot.

    Solo actúa en SHORT. Para LONG siempre devuelve neutral —
    el agotamiento comprador no bloquea LONG por sí solo
    (podría ser solo una pausa antes de continuar).
    """
    if direction != "SHORT":
        return 0.0, "vol_exhaust_long_skip", False

    if len(klines) < lookback + 3:
        return 0.0, "vol_exhaust_insufficient_data", False

    arr = np.array(klines, dtype=float)
    c   = arr[-lookback:, 4]   # closes
    v   = arr[-lookback:, 5]   # volúmenes
    o   = arr[-lookback:, 1]   # opens

    # ── ¿El precio está en rally? ─────────────────────────────────────────
    price_return = (c[-1] - c[0]) / c[0] if c[0] > 0 else 0.0
    up_mask      = c > o           # barras alcistas (close > open)
    up_count     = int(np.sum(up_mask))

    if up_count < min_up_bars:
        return 0.0, (
            f"vol_exhaust_no_rally: "
            f"up_bars={up_count}<{min_up_bars} return={price_return*100:.1f}%"
        ), False

    if price_return <= 0:
        return 0.0, f"vol_exhaust_price_flat: return={price_return*100:.2f}%", False

    # ── Pendiente del volumen en barras alcistas ──────────────────────────
    up_vols = v[up_mask]
    if len(up_vols) < 3:
        return 0.0, "vol_exhaust_few_up_vols", False

    vol_slope = _linreg_slope_normalized(up_vols)

    # ── Ratio volumen reciente vs anterior (segunda métrica) ──────────────
    half      = max(len(v) // 2, 1)
    vol_prior  = float(np.mean(v[:half]))
    vol_recent = float(np.mean(v[half:]))
    vol_ratio  = vol_recent / vol_prior if vol_prior > 0 else 1.0

    # ── Decisión ──────────────────────────────────────────────────────────
    # Condición primaria: pendiente negativa en barras alcistas
    exhaustion = vol_slope < vol_slope_threshold

    # Condición secundaria refuerza la señal (no es requisito)
    vol_weakening = vol_ratio < 0.85   # último 50% tiene 15% menos volumen

    if exhaustion:
        strength = "fuerte" if vol_weakening else "moderada"
        reason   = (
            f"✅ VOL_exhaustion SHORT ({strength}): "
            f"precio +{price_return*100:.1f}% ({up_count} barras ↑) "
            f"vol_slope={vol_slope:.3f} ratio_reciente={vol_ratio:.2f}× "
            f"→ compradores perdiendo fuerza"
        )
        return boost_amount, reason, False

    return 0.0, (
        f"vol_exhaust_neutral: slope={vol_slope:.3f} ratio={vol_ratio:.2f}×"
    ), False
