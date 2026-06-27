"""
BB Short Filter — bb_short_filter.py
══════════════════════════════════════════════════════════════
Portado de Pine Script "BB Short DCA Strategy".

Señal: close > upper_BB * (1 + signal_above_pct/100)
  = precio cierra más de X% por encima de la banda superior BB
  = sobrecompra extrema → mean-reversion SHORT

Del Pine original se porta:
  ✅ Condición de entrada (BB + % encima)
  ✅ Cálculo de BB (SMA + stdev)
  ❌ DCA/pyramiding → no implementado (requiere reescribir position_manager)
     El bot abre 1 posición por símbolo. Para DCA real, añadir en el futuro.

⚠️ STOP LOSS: el Pine original NO tiene SL explícito.
  En crypto esto es peligroso — el bot aplica su SL normal (SL_ATR_MULT).
  Para esta estrategia se recomienda un SL más ajustado: 2-3% del precio
  de entrada (configurar SL_ATR_MULT más conservador o BB_SHORT_SL_PCT).

Parámetros Railway:
  BB_SHORT_ENABLED=true
  BB_SHORT_ABOVE_PCT=1.0   (señal cuando close > upper_BB × 1.01)
  BB_SHORT_BOOST=10.0      (puntos sumados al score SHORT)
  BB_SHORT_VETO_LONG=true  (veta LONGs cuando hay sobrecompra extrema)
  BB_SHORT_LENGTH=20
  BB_SHORT_STD=2.0

Integración: paso 5i en scanner.py, mismo patrón que ibs_filter.py.
══════════════════════════════════════════════════════════════
"""
import math
import numpy as np


def _sma_bb(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        out[i] = arr[i - period + 1 : i + 1].mean()
    return out


def _stdev_bb(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        out[i] = arr[i - period + 1 : i + 1].std()
    return out


def bb_short_filter(
    klines: list,
    direction: str,
    bb_length: int       = 20,
    bb_std: float        = 2.0,
    signal_above_pct: float = 1.0,   # % encima de upper BB para señal
    boost_amount: float  = 10.0,
    veto_long: bool      = True,     # veta LONGs cuando precio >BB+%
) -> tuple[float, str, bool]:
    """
    Retorna (boost, reason, block) — patrón estándar de filtros del bot.

      SHORT + close > upper_BB * (1 + pct/100):
        → boost_amount añadido al score (sobrecompra extrema confirma SHORT)

      LONG + close > upper_BB * (1 + pct/100) + veto_long=True:
        → block=True (precio extremadamente sobrecomprado, malo para LONG)

      Cualquier otro caso: (0, reason, False) — neutral
    """
    if len(klines) < bb_length + 5:
        return 0.0, "bb_insufficient_data", False

    arr = np.array(klines, dtype=float)
    c = arr[:, 4]  # close

    basis_arr = _sma_bb(c, bb_length)
    std_arr   = _stdev_bb(c, bb_length)

    basis = basis_arr[-1]
    std   = std_arr[-1]

    if not (math.isfinite(basis) and math.isfinite(std) and std > 0):
        return 0.0, "bb_invalid_calc", False

    upper = basis + bb_std * std
    lower = basis - bb_std * std
    curr  = float(c[-1])

    # Umbral de señal: precio > upper * (1 + pct/100)
    signal_level = upper * (1.0 + signal_above_pct / 100.0)
    above_signal = curr > signal_level

    # Posición relativa al BB para diagnóstico
    bb_range = upper - lower
    pct_above_upper = (curr - upper) / upper * 100 if upper > 0 else 0.0

    if above_signal:
        reason_base = (
            f"BB_short: close={curr:.6f} > upper×{1+signal_above_pct/100:.2f}={signal_level:.6f} "
            f"({pct_above_upper:.2f}% encima BB) basis={basis:.6f}"
        )
        if direction == "SHORT":
            return boost_amount, f"✅ {reason_base}", False
        elif direction == "LONG" and veto_long:
            # Sobrecompra extrema → mala entrada LONG
            return 0.0, f"🚫 BB veto LONG (sobrecompra extrema): {reason_base}", True
        else:
            return 0.0, f"bb_long_no_veto: {reason_base}", False

    # Precio dentro de las bandas o por debajo → sin efecto
    pct_from_upper = (upper - curr) / upper * 100
    return 0.0, f"bb_neutral: {pct_from_upper:.2f}% bajo upper_BB", False
