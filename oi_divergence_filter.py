"""
OI Divergence SHORT Filter — oi_divergence_filter.py
══════════════════════════════════════════════════════════════
Señal: precio subiendo pero Open Interest bajando
  = posiciones cerrándose durante el rally
  = rally sin respaldo institucional = trampa alcista → SHORT

Lógica:
  Cuando el precio sube con OI también subiendo → tendencia real
  Cuando el precio sube pero OI baja → los que tenían LONG están
  saliendo, no entrando nuevos — el rally no tiene combustible.

Motor con estado (clase): mantiene historial por símbolo para
comparar OI y precio entre iteraciones del scanner (cada 60s).

Requiere mínimo 2-3 actualizaciones antes de dar señal.
update() se llama en scanner.py cada vez que se procesa un símbolo.

Parámetros Railway:
  OI_DIV_ENABLED=true
  OI_DIV_MIN_PRICE_RISE_PCT=1.0   (precio debe haber subido ≥1%)
  OI_DIV_MIN_OI_FALL_PCT=2.0      (OI debe haber bajado ≥2%)
  OI_DIV_LOOKBACK=5               (periodos de historia a comparar)
  OI_DIV_BOOST=9.0
  OI_DIV_VETO_LONG=true
══════════════════════════════════════════════════════════════
"""
import time
import logging

log = logging.getLogger("oi_divergence")


class OIDivergenceEngine:
    """
    Motor con estado — una instancia global compartida entre todos los símbolos.
    Mantiene historial de (timestamp, precio, OI) por símbolo.
    """

    def __init__(self, max_history: int = 10, min_interval_sec: float = 55.0):
        self._history: dict[str, list] = {}
        self.max_history  = max_history
        self.min_interval = min_interval_sec

    def update(self, symbol: str, price: float, oi: float) -> None:
        """Llamar en scanner.py cada vez que se procesa el símbolo."""
        if price <= 0 or oi <= 0:
            return
        now  = time.time()
        hist = self._history.setdefault(symbol, [])
        if hist and now - hist[-1][0] < self.min_interval:
            return  # demasiado pronto, esperar
        hist.append((now, price, oi))
        if len(hist) > self.max_history:
            hist.pop(0)

    def signal(
        self,
        symbol: str,
        direction: str,
        min_price_rise_pct: float = 1.0,
        min_oi_fall_pct: float    = 2.0,
        lookback: int             = 5,
        boost_amount: float       = 9.0,
        veto_long: bool           = True,
    ) -> tuple[float, str, bool]:
        """
        Retorna (boost, reason, block) — patrón estándar de filtros del bot.
        """
        hist = self._history.get(symbol, [])
        if len(hist) < 2:
            return 0.0, "oi_div_no_history", False

        n = min(lookback, len(hist) - 1)
        _, prev_price, prev_oi = hist[-(n + 1)]
        _, curr_price, curr_oi = hist[-1]

        if prev_price <= 0 or prev_oi <= 0 or curr_oi <= 0:
            return 0.0, "oi_div_invalid_data", False

        price_chg_pct = (curr_price - prev_price) / prev_price * 100
        oi_chg_pct    = (curr_oi    - prev_oi)    / prev_oi    * 100

        divergence = (
            price_chg_pct >= min_price_rise_pct and
            oi_chg_pct    <= -min_oi_fall_pct
        )

        if divergence:
            reason_base = (
                f"precio +{price_chg_pct:.1f}% pero OI {oi_chg_pct:.1f}% "
                f"({n} ciclos) = rally sin respaldo de posiciones"
            )
            if direction == "SHORT":
                return boost_amount, f"✅ OI_divergence: {reason_base}", False
            elif direction == "LONG" and veto_long:
                return 0.0, f"🚫 OI_div veto LONG: {reason_base}", True

        return 0.0, (
            f"oi_div_neutral: dP={price_chg_pct:+.1f}% dOI={oi_chg_pct:+.1f}%"
        ), False


# Instancia global — importar y usar en scanner.py
oi_div_engine = OIDivergenceEngine()
