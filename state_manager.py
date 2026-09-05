"""
state_manager.py — Persistencia JSON del estado del bot (posiciones
abiertas conocidas, racha de pérdidas, equity de referencia diaria) +
reconciliación contra BingX al arrancar + circuit breaker.

Sigue el mismo patrón que el resto de la flota: todo lo que debe
sobrevivir a un restart de Railway se guarda en disco en JSON.

ÁMBITO (cambio importante): esta cuenta de BingX la comparten varios
bots y operativa manual del usuario. Todo lo que este módulo GESTIONA
—cerrar por falta de SL, contar para los límites, calcular la racha de
pérdidas— se limita a las posiciones que ESTE bot abrió y registró en
state["positions"]. Lo que aparezca en BingX y no esté aquí se reporta,
pero NO se toca: cerrarlo sería destruir operativa ajena.
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


def _utc_today() -> str:
    """El día de referencia va en UTC, no en hora local del contenedor.

    dt.date.today() depende de la zona horaria donde Railway levante el
    contenedor, así que el 'día' del circuit breaker podía empezar a una
    hora distinta que el del resto de la flota — y cambiar sin avisar si
    el contenedor migraba de región."""
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


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

    def is_ours(self, symbol) -> bool:
        """True si este bot abrió y registró esa posición."""
        return symbol in self.state.get("positions", {})

    def count_own_live_positions(self, bingx_client) -> int:
        """Cuántas posiciones REALES en BingX son de este bot.

        Reemplaza al conteo de toda la cuenta que usaba
        HARD_MAX_TOTAL_POSITIONS: con varios bots y operativa manual
        compartiendo cuenta, contar todo bloqueaba este bot por
        posiciones que no eran suyas. Sigue consultando el exchange (no
        solo el JSON) para que el tope siga siendo real aunque el estado
        local mienta, pero filtrando por lo que es nuestro.
        """
        nuestros = set(self.state.get("positions", {}).keys())
        if not nuestros:
            return 0
        try:
            live = bingx_client.get_positions()
        except Exception:
            log.exception("No se pudieron leer posiciones para contar las propias")
            # Ante la duda, el JSON local es el límite: nunca devolver 0
            # (eso permitiría saltarse el tope justo cuando falla la API).
            return len(nuestros)
        n = 0
        for p in live:
            amt = float(p.get("positionAmt", p.get("positionSize", 0)) or 0)
            if amt != 0 and p.get("symbol") in nuestros:
                n += 1
        return n

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

        Reglas de ámbito:
          - Registrada aquí y ya no está en BingX -> se elimina del estado.
          - Registrada aquí y sigue viva SIN SL/TP -> se cierra (es nuestra,
            y una posición nuestra desprotegida es exactamente lo que este
            mecanismo existe para evitar).
          - En BingX pero NO registrada aquí -> NO SE TOCA. Se reporta y ya.
            Puede ser de otro bot de la flota o una operación manual del
            usuario; la versión anterior las importaba y, si no tenían
            SL/TP, las CERRABA. Eso llegó a intentar cerrar operativa manual
            del usuario y solo falló por un error de la API.
        """
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

        # 1. Nuestras que ya no existen -> fuera del estado.
        for sym in local_symbols - live_symbols:
            log.warning("Reconciliación: %s ya no existe en BingX, se elimina del estado local", sym)
            self.state["positions"].pop(sym, None)

        # 2. Nuestras que siguen vivas -> verificar que están protegidas.
        for sym in local_symbols & live_symbols:
            try:
                protected = bingx_client.has_stop_and_take_profit(sym)
            except Exception:
                protected = False
                log.exception("No se pudo verificar SL/TP de %s al reconciliar", sym)
            if protected:
                continue

            p = live_by_symbol[sym]
            position_side = p.get("positionSide", self.state["positions"][sym].get("positionSide", "LONG"))
            qty = abs(float(p.get("positionAmt", 0) or 0))
            log.error("%s (nuestra) sigue abierta SIN SL/TP -- cerrando por seguridad", sym)
            try:
                bingx_client.close_position(sym, position_side, qty)
                self.state["positions"].pop(sym, None)
                telegram_notifier.send(
                    f"🚨 *{sym}*: posición de ESTE bot encontrada sin SL/TP al arrancar "
                    f"(probable corte justo tras abrir la orden). Cerrada por seguridad."
                )
            except Exception as e:
                telegram_notifier.send(
                    f"🚨🚨 *{sym}*: posición de ESTE bot sin SL/TP y el cierre falló: {e}. "
                    f"REVISA BINGX A MANO AHORA."
                )

        # 3. Ajenas -> solo informar. NUNCA cerrar ni importar.
        ajenas = live_symbols - local_symbols
        if ajenas:
            sin_sl = []
            for sym in sorted(ajenas):
                try:
                    if not bingx_client.has_stop_and_take_profit(sym):
                        sin_sl.append(sym)
                except Exception:
                    log.exception("No se pudo comprobar SL/TP de la posición ajena %s", sym)
            log.warning(
                "Reconciliación: %d posiciones abiertas en BingX que NO son de este bot "
                "(otro bot de la flota u operativa manual). No se tocan: %s",
                len(ajenas), sorted(ajenas),
            )
            msg = (
                f"ℹ️ {len(ajenas)} posiciones abiertas en la cuenta que NO son de este bot; "
                f"no se van a tocar: {sorted(ajenas)}"
            )
            if sin_sl:
                msg += (
                    f"\n⚠️ De esas, sin SL/TP: {sin_sl}. Este bot NO las cierra — "
                    f"revísalas tú en BingX si te interesan."
                )
            telegram_notifier.send(msg)

        self._save()

    # -- circuit breaker ------------------------------------------------------
    def refresh_daily_anchor(self, current_equity: float):
        """Fija el equity de referencia del día y levanta el halt al cambiar
        de día. Idempotente: si ya es el día correcto no toca nada."""
        today = _utc_today()
        cambio = False
        with _lock:
            if self.state.get("daily_date") != today:
                self.state["daily_date"] = today
                self.state["daily_start_equity"] = current_equity
                self.state["trading_halted"] = False
                self.state["halt_reason"] = None
                cambio = True
            elif self.state.get("daily_start_equity") in (None, 0):
                # Ancla ausente o corrupta: sin esto el drawdown se calcula
                # contra None y el breaker se queda pillado sin motivo real.
                self.state["daily_start_equity"] = current_equity
                cambio = True
        if cambio:
            self._save()
            log.info("Ancla diaria fijada: %s equity=%.4f", today, current_equity)

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
                reason = (
                    f"drawdown diario {drawdown_pct:.2f}% (límite {config.MAX_DAILY_DRAWDOWN_PCT}%). "
                    f"OJO: el equity es de TODA la cuenta, así que incluye otros bots y operativa manual"
                )
                self._halt(reason)
                return False, reason

        return True, None

    def _halt(self, reason):
        with _lock:
            self.state["trading_halted"] = True
            self.state["halt_reason"] = reason
        self._save()
        log.error("CIRCUIT BREAKER ACTIVADO: %s", reason)

    def manual_reset_breaker(self, reanclar_equity: float = None):
        """Levanta el halt. Si se pasa reanclar_equity, además mueve el ancla
        diaria al equity actual: sin eso, un reset con la cuenta ya caída
        vuelve a disparar el breaker en la siguiente señal, porque el
        drawdown se sigue midiendo contra el equity de antes de la caída."""
        with _lock:
            self.state["trading_halted"] = False
            self.state["halt_reason"] = None
            self.state["consecutive_losses"] = 0
            if reanclar_equity is not None:
                self.state["daily_date"] = _utc_today()
                self.state["daily_start_equity"] = reanclar_equity
        self._save()
