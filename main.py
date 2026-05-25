"""
main.py — CVD Bot
Entra LONG/SHORT cuando Score + Decaimiento + CVD coinciden.
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

# ── Instancias ────────────────────────────────────────────────
client   = BingXClient()
risk     = RiskManager()
tg       = Telegram()
strategy = Strategy()

# Estado de posición activa en memoria
_pos = {}   # symbol → {direction, sl, tp, entry, qty, atr}


# ─────────────────────────────────────────────────────────────
def setup(symbol: str):
    client.set_leverage(symbol, C.LEVERAGE)
    client.set_margin_type(symbol, "ISOLATED")

def sync(symbol: str):
    """Recupera posiciones abiertas al arrancar"""
    for p in client.get_positions(symbol):
        amt = float(p.get("positionAmt", 0))
        if amt == 0: continue
        entry = float(p.get("avgPrice", 0))
        atr   = entry * 0.004
        _pos[symbol] = {
            "direction": "LONG" if amt > 0 else "SHORT",
            "sl":   entry - atr * C.SL_ATR_MULT if amt > 0 else entry + atr * C.SL_ATR_MULT,
            "tp":   0.0, "entry": entry, "qty": abs(amt), "atr": atr,
        }
        log.info(f"Posición recuperada: {_pos[symbol]}")


# ─────────────────────────────────────────────────────────────
def manage(symbol: str, price: float, atr: float):
    """Gestiona SL/TP trailing en posición abierta"""
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
        log.info(f"Trailing SL: {state['sl']:.5f} → {new_sl:.5f}")
        state["sl"] = new_sl

    # Check SL / TP
    d  = state["direction"]
    sl = state["sl"]
    tp = state["tp"]
    sl_hit = (d == "LONG"  and price <= sl) or (d == "SHORT" and price >= sl)
    tp_hit = tp > 0 and ((d == "LONG" and price >= tp) or (d == "SHORT" and price <= tp))

    if sl_hit or tp_hit:
        reason = "TP" if tp_hit else "SL"
        log.info(f"{reason} @ {price:.5f}")
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


# ─────────────────────────────────────────────────────────────
def cycle():
    symbol = C.SYMBOL
    log.info(f"── ciclo {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC ──")
    try:
        # Datos
        df_3m  = fetch(client, symbol, "3m",  C.LOOKBACK)
        df_15m = fetch(client, symbol, "15m", 100)

        if not ok(df_3m):
            log.warning("Datos insuficientes")
            hs.update(error=True)
            return

        price = float(df_3m["close"].iloc[-1])
        atr   = float((df_3m["high"] - df_3m["low"]).iloc[-10:].mean())

        # Gestionar posición abierta
        manage(symbol, price, atr)

        # Si ya hay posición abierta no abrir otra
        if symbol in _pos:
            log.info(f"Posición {_pos[symbol]['direction']} activa — esperando cierre")
            hs.update(last=f"{datetime.now(timezone.utc).strftime('%H:%M')} hold")
            return

        # Generar señal
        sig = strategy.compute(df_3m, df_15m)

        if sig.direction == "NONE":
            log.info(f"Sin señal | score={sig.score:+.3f} "
                     f"decay={sig.decay_pct:.0f}% cvd={'↑' if sig.cvd_rising else '↓'}")
            hs.update(last=f"{datetime.now(timezone.utc).strftime('%H:%M')} wait")
            return

        # Validaciones riesgo
        balance  = client.get_balance()
        positions= client.get_positions(symbol)
        ok_trade, reason = risk.can_trade(balance, len(positions))
        if not ok_trade:
            log.info(f"Bloqueado: {reason}")
            tg.warn(f"⛔ {symbol} bloqueado: {reason}")
            hs.update(last=f"{datetime.now(timezone.utc).strftime('%H:%M')} blocked")
            return

        ok_dir, reason2 = risk.anti_hedge(sig.direction, positions)
        if not ok_dir:
            log.info(f"Anti-hedge: {reason2}")
            # Cerrar posición contraria primero
            for p in positions:
                client.close_position(symbol, p)
            time.sleep(1)
            balance   = client.get_balance()
            positions = []

        # Sizing
        qty = risk.position_size(balance, sig)
        if qty <= 0:
            log.warning("Cantidad = 0, abortando")
            return

        # Ejecutar
        side   = "BUY" if sig.direction == "LONG" else "SELL"
        result = client.market_order(symbol, side, qty)

        if result.get("code", -1) != 0:
            log.error(f"Error orden: {result}")
            tg.warn(f"❌ Error orden {symbol}: {result.get('msg', result)}")
            return

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
        hs.update(last=f"{datetime.now(timezone.utc).strftime('%H:%M')} {sig.direction}")

    except Exception as e:
        log.exception(f"Error en ciclo: {e}")
        tg.warn(f"Error ciclo: {e}")
        hs.update(error=True)


# ─────────────────────────────────────────────────────────────
def main():
    log.info("═══════════════════════════════════")
    log.info("  CVD Bot — Score + Decay + CVD    ")
    log.info("═══════════════════════════════════")

    hs.start()

    if not C.BINGX_API_KEY or not C.BINGX_SECRET_KEY:
        log.error("Credenciales BingX no configuradas")
        sys.exit(1)

    balance = client.get_balance()
    log.info(f"Balance: {balance:.2f} USDT")

    setup(C.SYMBOL)
    sync(C.SYMBOL)
    tg.startup(C.SYMBOL, balance)

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
