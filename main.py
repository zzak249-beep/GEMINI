"""
main.py — CVD Bot Multi-Símbolo
Escanea TODO BingX (o lista manual) y opera en los mejores símbolos por volumen.
Ciclos cada 3min alineados al cierre de vela.
"""
import logging, sys, time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config as C
from bingx_client import BingXClient
from strategy import Strategy
from risk_manager import RiskManager
from telegram_notifier import Telegram
from market_data import fetch, ok
import health_server as hs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")

# ── Instancias globales ───────────────────────────────────────
client   = BingXClient()
risk     = RiskManager()
tg       = Telegram()
strategy = Strategy()

# Estado de posiciones activas en memoria: symbol → {direction, sl, tp, entry, qty, atr}
_pos: dict = {}

# Lista activa de símbolos a escanear (se refresca cada SCANNER_REFRESH_CYCLES ciclos)
_symbols: list  = []
_cycle_count    = 0


# ── Gestión de símbolos ───────────────────────────────────────

def refresh_symbols():
    """Actualiza la lista de símbolos según el modo configurado."""
    global _symbols
    if C.SYMBOL_MODE == "scanner":
        selected = client.scan_by_volume(
            C.SCANNER_MIN_VOLUME,
            C.SCANNER_TOP_N,
            C.SCANNER_BLACKLIST,
        )
        if selected:
            _symbols = selected
            log.info(f"[Scanner] {len(_symbols)} símbolos activos: {_symbols}")
            tg.info(f"🔍 Escáner: {len(_symbols)} pares seleccionados\n" +
                    " · ".join(_symbols[:10]) + ("…" if len(_symbols) > 10 else ""))
        else:
            log.warning("[Scanner] Sin resultados, manteniendo lista anterior")
    else:
        _symbols = list(C.SYMBOLS_LIST)
        log.info(f"[Manual] Símbolos: {_symbols}")


# ── Setup y sincronización ────────────────────────────────────

def setup(symbol: str):
    try:
        client.set_leverage(symbol, C.LEVERAGE)
        client.set_margin_type(symbol, "ISOLATED")
    except Exception as e:
        log.warning(f"Setup {symbol}: {e}")

def sync_all():
    """Recupera todas las posiciones abiertas al arrancar."""
    for p in client.get_positions():
        amt = float(p.get("positionAmt", 0))
        if amt == 0: continue
        sym   = p.get("symbol", "")
        entry = float(p.get("avgPrice", 0))
        atr   = entry * 0.004
        _pos[sym] = {
            "direction": "LONG" if amt > 0 else "SHORT",
            "sl":   entry - atr * C.SL_ATR_MULT if amt > 0 else entry + atr * C.SL_ATR_MULT,
            "tp":   0.0, "entry": entry, "qty": abs(amt), "atr": atr,
        }
        log.info(f"Posición recuperada: {sym} {_pos[sym]['direction']} @ {entry}")


# ── Gestión de posición abierta ───────────────────────────────

def manage(symbol: str, price: float, atr: float):
    """Gestiona SL/TP trailing en posición abierta."""
    state = _pos.get(symbol)
    if not state: return

    positions = client.get_positions(symbol)
    if not positions:
        log.info(f"Posición {symbol} cerrada externamente")
        _on_close(symbol, price, "EXCHANGE")
        return

    # Trailing SL
    new_sl = risk.trail_sl(state["direction"], price, state["sl"], atr)
    if new_sl != state["sl"]:
        log.info(f"Trailing SL {symbol}: {state['sl']:.5f} → {new_sl:.5f}")
        state["sl"] = new_sl

    # Check SL / TP
    d, sl, tp = state["direction"], state["sl"], state["tp"]
    sl_hit = (d == "LONG"  and price <= sl) or (d == "SHORT" and price >= sl)
    tp_hit = tp > 0 and ((d == "LONG" and price >= tp) or (d == "SHORT" and price <= tp))

    if sl_hit or tp_hit:
        reason = "TP" if tp_hit else "SL"
        log.info(f"{reason} {symbol} @ {price:.5f}")
        client.cancel_all(symbol)
        client.close_position(symbol, positions[0])
        _on_close(symbol, price, reason)


def _on_close(symbol: str, price: float, reason: str):
    state = _pos.pop(symbol, None)
    if not state: return
    entry = state["entry"]
    qty   = state["qty"]
    pnl   = (price - entry) * qty * C.LEVERAGE
    if state["direction"] == "SHORT": pnl = -pnl
    risk.record(pnl)
    tg.close(symbol, state["direction"], entry, price, pnl, reason)


# ── Ciclo principal ───────────────────────────────────────────

def cycle():
    global _cycle_count
    _cycle_count += 1
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log.info(f"══ ciclo #{_cycle_count} {now} UTC — {len(_symbols)} símbolos ══")

    try:
        # Refrescar lista de símbolos periódicamente
        if _cycle_count == 1 or _cycle_count % C.SCANNER_REFRESH_CYCLES == 0:
            refresh_symbols()

        if not _symbols:
            log.warning("Lista de símbolos vacía")
            hs.update(error=True)
            return

        # Obtener posiciones abiertas globales (una sola llamada API)
        all_positions = client.get_positions()
        open_by_sym   = {p["symbol"]: p for p in all_positions if float(p.get("positionAmt", 0)) != 0}
        n_open        = len(open_by_sym)

        balance = client.get_balance()
        hs.update(
            last=f"{datetime.now(timezone.utc).strftime('%H:%M')} c#{_cycle_count}",
            positions=n_open,
            symbols=len(_symbols),
        )

        # ── Gestionar posiciones activas ──────────────────────
        for symbol, state in list(_pos.items()):
            df_3m = fetch(client, symbol, "3m", 20)
            if not ok(df_3m, min_rows=5):
                continue
            price = float(df_3m["close"].iloc[-1])
            atr   = float((df_3m["high"] - df_3m["low"]).iloc[-10:].mean())
            manage(symbol, price, atr)

        # ── Buscar nuevas señales ─────────────────────────────
        ok_trade, reason = risk.can_trade(balance, len(_pos))
        if not ok_trade:
            log.info(f"No se buscan nuevas entradas: {reason}")
            return

        for symbol in _symbols:
            # Ya tenemos posición en este símbolo
            if symbol in _pos:
                continue

            # Límite global alcanzado
            if len(_pos) >= C.MAX_POSITIONS:
                log.info(f"MAX_POSITIONS={C.MAX_POSITIONS} alcanzado, no más entradas")
                break

            try:
                df_3m  = fetch(client, symbol, "3m",  C.LOOKBACK)
                df_15m = fetch(client, symbol, "15m", 100)

                if not ok(df_3m):
                    continue

                price = float(df_3m["close"].iloc[-1])
                atr   = float((df_3m["high"] - df_3m["low"]).iloc[-10:].mean())

                sig = strategy.compute(df_3m, df_15m)

                if sig.direction == "NONE":
                    log.debug(f"{symbol} sin señal | score={sig.score:+.3f}")
                    continue

                log.info(f"SEÑAL {sig.direction} {sig.quality} en {symbol} | score={sig.score:+.3f}")

                # Validar riesgo para este símbolo
                sym_positions = [open_by_sym[symbol]] if symbol in open_by_sym else []
                ok_dir, reason2 = risk.anti_hedge(sig.direction, sym_positions)
                if not ok_dir:
                    log.info(f"Anti-hedge {symbol}: {reason2}")
                    continue

                # Sizing
                qty = risk.position_size(balance, sig)
                if qty <= 0:
                    log.warning(f"{symbol}: qty=0, omitiendo")
                    continue

                # Setup del símbolo (leverage + margin type)
                setup(symbol)
                time.sleep(0.3)  # pequeña pausa tras configurar

                # Ejecutar orden
                side   = "BUY" if sig.direction == "LONG" else "SELL"
                result = client.market_order(symbol, side, qty)

                if result.get("code", -1) != 0:
                    log.error(f"Error orden {symbol}: {result}")
                    tg.warn(f"❌ Error orden {symbol}: {result.get('msg', result)}")
                    continue

                # Guardar estado
                _pos[symbol] = {
                    "direction": sig.direction,
                    "sl":        sig.sl,
                    "tp":        sig.tp,
                    "entry":     sig.entry,
                    "qty":       qty,
                    "atr":       sig.atr_val,
                }

                tg.entry(symbol, sig, qty, balance)
                log.info(f"✅ {sig.direction} {qty:.6f} {symbol} @ {sig.entry:.5f}")

                # Refrescar balance tras operar
                balance = client.get_balance()

            except Exception as e:
                log.exception(f"Error procesando {symbol}: {e}")
                hs.update(error=True)

    except Exception as e:
        log.exception(f"Error en ciclo: {e}")
        tg.warn(f"⚠️ Error ciclo: {e}")
        hs.update(error=True)


# ── Main ──────────────────────────────────────────────────────

def main():
    log.info("═══════════════════════════════════════════")
    log.info("  CVD Bot Multi-Símbolo — BingX Scanner   ")
    log.info("═══════════════════════════════════════════")

    hs.start()

    if not C.BINGX_API_KEY or not C.BINGX_SECRET_KEY:
        log.error("Credenciales BingX no configuradas")
        sys.exit(1)

    balance = client.get_balance()
    log.info(f"Balance: {balance:.2f} USDT")
    log.info(f"Modo: {C.SYMBOL_MODE.upper()} | MAX_POSITIONS: {C.MAX_POSITIONS}")

    # Cargar lista inicial de símbolos
    refresh_symbols()

    # Sincronizar posiciones existentes
    sync_all()

    # Setup de símbolos con posición ya abierta
    for sym in list(_pos.keys()):
        setup(sym)

    tg.startup_multi(balance, _symbols)

    # Ciclo inmediato al arrancar
    cycle()

    # Scheduler: cada 3min, 10s después del cierre de vela
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(
        cycle,
        CronTrigger(
            minute="0,3,6,9,12,15,18,21,24,27,30,33,36,39,42,45,48,51,54,57",
            second=10,
        ),
        max_instances=1,
        misfire_grace_time=30,
    )
    log.info("Scheduler listo — cada 3min")
    try:
        sched.start()
    except KeyboardInterrupt:
        log.info("Bot detenido")


if __name__ == "__main__":
    main()
