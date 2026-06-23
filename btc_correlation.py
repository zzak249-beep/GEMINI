"""
QF×JP Bot — BTC Correlation Guard v2.0
═══════════════════════════════════════════════════════════════════════════════
FIX v2.0: release() con token único — mismo bug que direction_allowed() en
  risk_manager.py v7.8. La versión anterior usaba lista+pop() en release(),
  igual que el bug ya resuelto: en batches concurrentes de 20 símbolos, un
  símbolo podía liberar la reserva de OTRO (el pop() borraba el último
  elemento, no el que correspondía a esa coroutine). Con el guard activo y
  3 símbolos BTC-correlacionados abriéndose en el mismo ciclo, una liberación
  desordenada podía inflar el contador o borrar una reserva legítima.
  Fix: allowed() devuelve un token int único (tercer elemento de la tupla),
  release() lo acepta y borra exactamente esa entrada.
  El caller (scanner.py) debe actualizar btc_ok, btc_reason, btc_token =
  btc_guard.allowed(...) y pasar btc_token a btc_guard.release().

Sin cambios en la lógica de correlación vs v1.0.
═══════════════════════════════════════════════════════════════════════════════
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger("btc_guard")


def compute_correlation(symbol_klines: list, btc_klines: list, lookback: int = 60) -> float:
    """
    Correlación de Pearson entre retornos del símbolo y BTC.
    Retorna 0.0 si datos insuficientes o serie constante.
    """
    if len(symbol_klines) < lookback + 1 or len(btc_klines) < lookback + 1:
        return 0.0
    sym_closes = np.array([c[4] for c in symbol_klines[-(lookback + 1):]], dtype=float)
    btc_closes = np.array([c[4] for c in btc_klines[-(lookback + 1):]], dtype=float)
    if np.any(sym_closes <= 0) or np.any(btc_closes <= 0):
        return 0.0
    sym_ret = np.diff(sym_closes) / sym_closes[:-1]
    btc_ret = np.diff(btc_closes) / btc_closes[:-1]
    if sym_ret.std() < 1e-12 or btc_ret.std() < 1e-12:
        return 0.0
    try:
        corr = np.corrcoef(sym_ret, btc_ret)[0, 1]
    except Exception:
        return 0.0
    return float(corr) if not np.isnan(corr) else 0.0


def btc_net_direction(direction: str, correlation: float) -> str:
    """
    Dirección neta sobre BTC que realmente representa la señal.
    Correlación positiva: LONG=alcista, SHORT=bajista.
    Correlación negativa: LONG=bajista, SHORT=alcista (invertido).
    """
    if correlation >= 0:
        return direction
    return "SHORT" if direction == "LONG" else "LONG"


@dataclass
class BTCCorrelationGuard:
    """
    Guard de exposición agregada a BTC.
    v2.0: reservas con token único — ver docstring del módulo.
    """
    threshold:   float = 0.5
    window_sec:  int   = 1800
    max_same:    int   = 3

    # FIX v2.0: dict de dicts {token: timestamp} en vez de lista de timestamps.
    # Permite liberar exactamente la reserva indicada sin importar el orden.
    _btc_reservations: dict = field(default_factory=lambda: {"LONG": {}, "SHORT": {}})
    _reservation_seq:  int  = 0

    def allowed(self, direction: str, correlation: float) -> tuple[bool, str, Optional[int]]:
        """
        Chequea Y RESERVA atómicamente.
        Retorna (allowed, reason, token).
        token es None si allowed=False.
        El caller DEBE guardar el token y pasarlo a release() si el trade
        no se concreta — de lo contrario la reserva queda viva para siempre.
        """
        if abs(correlation) < self.threshold:
            return True, "", None  # riesgo idiosincrático — no cuenta

        net_dir = btc_net_direction(direction, correlation)
        now = time.time()
        reservations = self._btc_reservations.setdefault(net_dir, {})

        # Purgar expiradas
        expired = [t for t, ts in reservations.items() if now - ts >= self.window_sec]
        for t in expired:
            del reservations[t]

        if len(reservations) >= self.max_same:
            mins = int(self.window_sec / 60)
            return (False,
                    f"btc_correlation_guard(btc_dir={net_dir},"
                    f"{len(reservations)}/{self.max_same} en {mins}min,"
                    f"corr={correlation:+.2f})",
                    None)

        # Reservar
        self._reservation_seq += 1
        token = self._reservation_seq
        reservations[token] = now
        return True, "", token

    def register(self, direction: str, correlation: float):
        """Obsoleto desde v1.1 (reserva atómica en allowed). No-op."""
        pass

    def release(self, direction: str, correlation: float, token: Optional[int] = None):
        """
        Libera la reserva identificada por token.
        FIX v2.0: si no se pasa token, no hace nada (en vez de pop()
        que borraba la reserva equivocada en batches concurrentes).
        """
        if abs(correlation) < self.threshold:
            return
        if token is None:
            log.warning("btc_guard.release() sin token — no se libera nada (caller desactualizado)")
            return
        net_dir = btc_net_direction(direction, correlation)
        self._btc_reservations.get(net_dir, {}).pop(token, None)


# Singleton global
btc_guard = BTCCorrelationGuard()
