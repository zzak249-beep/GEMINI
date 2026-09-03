"""
Persistencia JSON del estado del bot (posiciones abiertas conocidas,
racha de pérdidas, equity de referencia diaria) + reconciliación contra
BingX al arrancar + circuit breaker.

Sigue el mismo patrón que el resto de la flota: todo lo que debe
sobrevivir a un restart de Railway se guarda en disco en JSON.
"""
import datetime as dt
import json
import logging
import os
import threading

import config
import telegram_notifier

log = logging.getLogger("state")

_lock = threading.Lock()

_DEFAULT_STATE = {
    "positions": {},          # symbol -> {"positionSide", "qty", "entry_price", "sl", "tp"}
    "consecutive_losses": 0,
    "daily_start_equity": None,
    "daily_date": None,
    "trading_halted": False,
    "halt_reason": None,
    "last_signal_ts": {},      # symbol -> open_time (ms) de la última señal disparada
    "last_income_check_ts": {},  # symbol -> desde cuándo mirar PnL realizado
}


def _load_raw():
    if not os.path.exists(config.STATE_FILE):
        return dict(_DEFAULT_STATE)
    try:
        with open(config.STATE_FILE, "r") as f:
            data = json.load(f)
        merged = dict(_DEFAULT_STATE)
        merged.update(data)
        return merged
    except Exception:
        log.exception("Estado corrupto, se reinicia desde defaults")
        return dict(_DEFAULT_STATE)


def _save_raw(state):
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, config.STATE_FILE)


class StateManager:
    """Wrapper con locking simple para uso desde un webhook con varios workers."""

    def __init__(self):
        with _lock:
            self.state = _load_raw()

    def _save(self):
        with _lock:
            _save_raw(self.state)

    # -- posiciones conocidas localmente -----------------------------------
    def record_open(self, symbol, position_side, qty, entry_price, sl, tp):
        with _lock:
            self.state["positions"][symbol] = {
                "positionSide": position_side,
                "qty": qty,
                "entry_price": entry_price,
                "sl": sl,
                "tp": tp,
                "opened_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        self._save()

    def record_close(self, symbol, pnl: float = None):
        with _lock:
            self.state["positions"].pop(symbol, None)
            if pnl is not None:
                if pnl < 0:
                    self.state["consecutive_losses"] += 1
                else:
                    self.state["consecutive_losses"] = 0
        self._save()

    def get_open(self, symbol):
        return self.state["positions"].get(symbol)

    def open_count(self):
        return len(self.state["positions"])

    # -- cooldown de señales (equivalente a can_signal / last_signal_bar de Pine) --
    def get_last_signal_ts(self, symbol):
        return self.state.get("last_signal_ts", {}).get(symbol)

    def set_last_signal_ts(self, symbol, ts):
        with _lock:
            self.state.setdefault("last_signal_ts", {})[symbol] = ts
        self._save()

    # -- desde cuándo mirar PnL realizado al reconciliar un cierre por SL/TP --
    def get_last_income_check_ts(self, symbol):
        return self.state.get("last_income_check_ts", {}).get(symbol)

    def set_last_income_check_ts(self, symbol, ts):
        with _lock:
            self.state.setdefault("last_income_check_ts", {})[symbol] = ts
        self._save()

    # -- reconciliación con el exchange al arrancar --------------------------
    def reconcile(self, bingx_client):
        """Compara el estado local con las posiciones reales en BingX.
        Prioriza siempre lo que diga el exchange (fuente de verdad)."""
        try:
            live_positions = bingx_client.get_positions()
        except Exception:
            log.exception("No se pudo reconciliar con BingX al arrancar")
            return

        live_by_symbol = {}
        for p in live_positions:
            amt = float(p.get("positionAmt", p.get("positionSize", 0)) or 0)
            if amt == 0:
                continue
            live_by_symbol[p["symbol"]] = p

        local_symbols = set(self.state["positions"].keys())
        live_symbols = set(live_by_symbol.keys())

        for sym in local_symbols - live_symbols:
            log.warning("Reconciliación: %s ya no existe en BingX, se elimina del estado local", sym)
            self.state["positions"].pop(sym, None)

        for sym in live_symbols - local_symbols:
            log.warning("Reconciliación: %s abierta en BingX pero no en estado local, se importa", sym)
            p = live_by_symbol[sym]
            position_side = p.get("positionSide", "LONG")
            qty = abs(float(p.get("positionAmt", 0)))

            # Antes de darla por buena, confirma que de verdad tiene SL/TP
            # activo en BingX -- mismo hueco que cubre _handle_entry justo
            # tras abrir una orden. Si el proceso murió entre abrir la orden
            # y esa verificación, y luego reinicia, reconcile() la importaría
            # como "todo en orden" sin comprobar nada -- dejando una posición
            # real desprotegida marcada como si estuviera bien.
            try:
                protected = bingx_client.has_stop_and_take_profit(sym)
            except Exception:
                protected = False
                log.exception("No se pudo verificar SL/TP de %s al importarla en reconciliación", sym)

            if not protected:
                log.error(
                    "%s encontrada al arrancar SIN SL/TP confirmado -- cerrando inmediatamente por seguridad",
                    sym,
                )
                try:
                    bingx_client.close_position(sym, position_side, qty)
                    telegram_notifier.send(
                        f"🚨 *{sym}*: encontrada abierta al arrancar el bot SIN SL/TP confirmado "
                        f"(probable corte justo tras abrir la orden en el arranque anterior). "
                        f"Se cerró de inmediato por seguridad."
                    )
                except Exception as e:
                    telegram_notifier.send(
                        f"🚨🚨 *{sym}*: encontrada sin SL/TP al arrancar y el cierre de emergencia "
                        f"también falló: {e}. REVISA BINGX A MANO AHORA."
                    )
                continue  # no se añade al estado local, ya se cerró (o falló y se avisó)

            self.state["positions"][sym] = {
                "positionSide": position_side,
                "qty": qty,
                "entry_price": float(p.get("avgPrice", 0)),
                "sl": None,
                "tp": None,
                "opened_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "imported_on_reconcile": True,
            }
        self._save()

    # -- circuit breaker ------------------------------------------------------
    def refresh_daily_anchor(self, current_equity: float):
        today = dt.date.today().isoformat()
        with _lock:
            if self.state.get("daily_date") != today:
                self.state["daily_date"] = today
                self.state["daily_start_equity"] = current_equity
                self.state["trading_halted"] = False
                self.state["halt_reason"] = None
        self._save()

    def check_circuit_breaker(self, current_equity: float) -> (bool, str):
        """Devuelve (permitido, motivo_si_no_permitido)."""
        self.refresh_daily_anchor(current_equity)

        if self.state.get("trading_halted"):
            return False, self.state.get("halt_reason", "circuit breaker activo")

        if self.state["consecutive_losses"] >= config.MAX_CONSECUTIVE_LOSSES:
            reason = f"{self.state['consecutive_losses']} pérdidas consecutivas (límite {config.MAX_CONSECUTIVE_LOSSES})"
            self._halt(reason)
            return False, reason

        start_equity = self.state.get("daily_start_equity") or current_equity
        if start_equity > 0:
            drawdown_pct = (start_equity - current_equity) / start_equity * 100
            if drawdown_pct >= config.MAX_DAILY_DRAWDOWN_PCT:
                reason = f"drawdown diario {drawdown_pct:.2f}% (límite {config.MAX_DAILY_DRAWDOWN_PCT}%)"
                self._halt(reason)
                return False, reason

        return True, None

    def _halt(self, reason):
        with _lock:
            self.state["trading_halted"] = True
            self.state["halt_reason"] = reason
        self._save()
        log.error("CIRCUIT BREAKER ACTIVADO: %s", reason)

    def manual_reset_breaker(self):
        with _lock:
            self.state["trading_halted"] = False
            self.state["halt_reason"] = None
            self.state["consecutive_losses"] = 0
        self._save()
