"""
Funding Rate Spike SHORT Filter — fr_spike_filter.py
══════════════════════════════════════════════════════════════
Señal: FR acelera hacia arriba repentinamente
  = longs acumulándose más rápido de lo normal
  = sobreextensión inminente → SHORT

Diferencia vs FR_REGIME_ENABLED (ya en el bot):
  FR_REGIME_ENABLED detecta FR ALTO en ventana pre-funding.
  Este detecta ACELERACIÓN (spike): FR actual >> promedio reciente.
  
  FR alto + estable = mercado cargado pero equilibrado.
  FR alto + acelerando = desequilibrio creciente = señal más fuerte.

Motor con estado: mantiene historial de FR por símbolo.
update() se llama en scanner.py por símbolo y ciclo.

Parámetros Railway:
  FR_SPIKE_ENABLED=true
  FR_SPIKE_MULT=2.5        (spike si FR_actual > avg × 2.5)
  FR_SPIKE_MIN_ABS=0.0003  (FR mínimo para activar, evita ruido en FR~0)
  FR_SPIKE_LOOKBACK=8      (periodos para calcular el promedio base)
  FR_SPIKE_BOOST=8.0
══════════════════════════════════════════════════════════════
"""
import time
import logging

log = logging.getLogger("fr_spike")


class FRSpikeEngine:
    """Motor con estado — instancia global compartida."""

    def __init__(self, max_history: int = 20, min_interval_sec: float = 55.0):
        self._history: dict[str, list] = {}
        self.max_history  = max_history
        self.min_interval = min_interval_sec

    def update(self, symbol: str, fr: float) -> None:
        """Llamar en scanner.py cada ciclo, después de obtener el FR."""
        now  = time.time()
        hist = self._history.setdefault(symbol, [])
        if hist and now - hist[-1][0] < self.min_interval:
            return
        hist.append((now, fr))
        if len(hist) > self.max_history:
            hist.pop(0)

    def signal(
        self,
        symbol: str,
        direction: str,
        fr_current: float,
        spike_mult: float   = 2.5,
        min_fr_abs: float   = 0.0003,
        lookback: int       = 8,
        boost_amount: float = 8.0,
    ) -> tuple[float, str, bool]:
        """
        Retorna (boost, reason, block).

        Spike positivo (FR alto acelerando) → SHORT boost / LONG veto.
        Spike negativo (FR muy negativo acelerando) → LONG boost (opcional).
        """
        hist = self._history.get(symbol, [])

        # Con poco historial solo chequeo nivel absoluto
        if len(hist) < 3:
            if fr_current > min_fr_abs * 3:
                if direction == "SHORT":
                    return boost_amount * 0.5, (
                        f"fr_high_no_history: fr={fr_current:.5f}"
                    ), False
            return 0.0, "fr_spike_no_history", False

        n           = min(lookback, len(hist))
        recent_frs  = [h[1] for h in hist[-n:]]
        avg_fr      = sum(recent_frs) / len(recent_frs)

        # ── Spike positivo (longs sobreacumulados) ────────────────────────
        if fr_current > 0 and avg_fr > 0:
            spike_ratio = fr_current / avg_fr
            fr_trending = (len(recent_frs) >= 3 and
                           recent_frs[-1] > recent_frs[-2] > recent_frs[-3])
            is_spike    = spike_ratio >= spike_mult and fr_current >= min_fr_abs

            if is_spike:
                trend_str = " ↑acelerando" if fr_trending else ""
                reason    = (
                    f"✅ FR_spike +SHORT: fr={fr_current:.5f} = "
                    f"{spike_ratio:.1f}× avg={avg_fr:.5f}{trend_str} "
                    f"→ longs sobreacumulados"
                )
                if direction == "SHORT":
                    return boost_amount, reason, False
                elif direction == "LONG":
                    return 0.0, f"🚫 FR_spike veto LONG: {reason}", True

        # ── Spike negativo (shorts sobreacumulados) ───────────────────────
        elif fr_current < 0 and avg_fr < 0:
            spike_ratio = abs(fr_current) / abs(avg_fr) if avg_fr != 0 else 0.0
            if spike_ratio >= spike_mult and abs(fr_current) >= min_fr_abs:
                if direction == "LONG":
                    return boost_amount * 0.5, (
                        f"FR_neg_spike LONG: fr={fr_current:.5f} "
                        f"= {spike_ratio:.1f}× avg → shorts sobreacumulados"
                    ), False

        return 0.0, (
            f"fr_spike_normal: fr={fr_current:.5f} "
            f"avg={avg_fr:.5f} ratio={fr_current/avg_fr:.1f}×" if avg_fr != 0
            else f"fr_spike_normal: fr={fr_current:.5f}"
        ), False


# Instancia global
fr_spike_engine = FRSpikeEngine()
