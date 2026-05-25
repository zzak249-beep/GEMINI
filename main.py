"""
main.py — CVD Bot Multi-Símbolo
ORDEN DE ARRANQUE:
  1. health_server.start()  ← PRIMERO, antes de todo
  2. imports pesados (pandas, numpy, etc.)
  3. init del bot en background thread
  4. scheduler
"""
import os
import sys
import time
import logging
import threading

# ─── 1. HEALTH SERVER — debe ser lo primero ───────────────────
# Importar y arrancar ANTES que pandas/numpy/requests para que
# Railway reciba 200 en el primer healthcheck.
import health_server as hs
hs.start()

# ─── 2. Resto de imports ──────────────────────────────────────
from datetime import datetime, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config as C
from bingx_client import BingXClient
from strategy import Strategy
from risk_manager import RiskManager
from telegram_notifier import Telegram
from market_data import fetch, ok

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

_pos: dict  = {}
_symbols: list = []
_cycle_count   = 0


# ── Gestión de símbolos ───────────────────────────────────────

def refresh_symbols():
    global _symbols
    if C.SYMBOL_MODE == "scanner":
        selected = client.scan_by_volume(
            C.SCANNER_MIN_VOLUME,
            C.SCANNER_TOP_N,
            C.SCANNER_BLACKLIST,
        )
        if selected:
            _symbols = selected
            log.info(f"[Scanner] {len(_symbols)} símbolos: {_symbols}")
            tg.info("🔍 Escáner: " + " · ".join(_symbols[:10]) +
                    ("…" if len(_symbols) > 10 else ""))
        else:
            log.warning("[Scanner] Sin resultados, manteniendo lista anterior")
    else:
        _symbols = list(C.SYMBOLS_LIST)
        log.info(f"[Manual] Símbolos: {_symbols}")
    hs.update(symbols=len(_symbols))


# ── Setup y sync ──────────────────────────────────────────────

def setup(symbol: str):
    try:
        client.set_leverage(symbol, C.LEVERAGE)
        client.set_margin_type(symbol, "ISOLATED")
    except Exception as e:
        log.warning(f"Setup {symbol}: {e}")


def sync_all():
    for p in client.get_positions():
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        sym   = p.get("symbol", "")
        entry = float(p.get("avgPrice", 0))
        atr   = entry * 0.004
        _pos[sym] = {
            "direction": "LONG" if amt > 0 else "SHORT",
            "sl":   entry - atr * C.SL_ATR_MULT if amt > 0 else entry + atr * C.SL_ATR_MULT,
            "tp":   0.0, "entry": entry, "qty": abs(amt), "atr": atr,
        }
        log.info(f"Posición recuperada: {sym} {_pos[sym]['direction']} @ {entry}")


# ── Gestión posición ──────────────────────────────────────────

def manage(symbol: str, price: float, atr: float):
    state = _pos.get(symbol)
    if not state:
        return
    positions = client.get_positions(symbol)
    if not positions:
        log.info(f"{symbol} cerrada externamente")
        _on_close(symbol, price, "EXCHANGE")
        return
    new_sl = risk.trail_sl(state["direction"], price, state["sl"], atr)
    if new_sl != state["sl"]:
        log.info(f"Trailing SL {symbol}: {state['sl']:.5f} → {new_sl:.5f}")
        state["sl"] = new_sl
    d, sl, tp = state["direction"], state["sl"], state["tp"]
    sl_hit = (d == "LONG" and price <= sl) or (d == "SHORT" and price >= sl)
    tp_hit = tp > 0 and ((d == "LONG" and price >= tp) or (d == "SHORT" and price <= tp))
    if sl_hit or tp_hit:
        reason = "TP" if tp_hit else "SL"
        log.info(f"{reason} {symbol} @ {price:.5f}")
        client.cancel_all(symbol)
        client.close_position(symbol, positions[0])
        _on_close(symbol, price, reason)


def _on_close(symbol: str, price: float, reason: str):
    state = _pos.pop(symbol, None)
    if not state:
        return
    pnl = (price - state["entry"]) * state["qty"] * C.LEVERAGE
    if state["direction"] == "SHORT":
        pnl = -pnl
    risk.record(pnl)
    tg.close(symbol, state["direction"], state["entry"], price, pnl, reason)


# ── Ciclo ─────────────────────────────────────────────────────

def cycle():
    global _cycle_count
    _cycle_count += 1
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log.info(f"══ ciclo #{_cycle_count} {now} UTC — {len(_symbols)} símbolos ══")

    try:
        if _cycle_count == 1 or _cycle_count % C.SCANNER_REFRESH_CYCLES == 0:
            refresh_symbols()

        if not _symbols:
            log.warning("Lista de símbolos vacía")
            hs.update(error=True)
            return

        all_pos   = client.get_positions()
        open_syms = {p["symbol"]: p for p in all_pos
                     if float(p.get("positionAmt", 0)) != 0}
        balance   = client.get_balance()

        hs.update(
            last=f"{datetime.now(timezone.utc).strftime('%H:%M')} #{_cycle_count}",
            positions=len(open_syms),
            symbols=len(_symbols),
        )

        # Gestionar posiciones abiertas
        for symbol in list(_pos.keys()):
            df = fetch(client, symbol, "3m", 20)
            if not ok(df, min_rows=5):
                continue
            price = float(df["close"].iloc[-1])
            atr   = float((df["high"] - df["low"]).iloc[-10:].mean())
            manage(symbol, price, atr)

        # Buscar señales nuevas
        ok_trade, reason = risk.can_trade(balance, len(_pos))
        if not ok_trade:
            log.info(f"Sin nuevas entradas: {reason}")
            return

        for symbol in _symbols:
            if symbol in _pos:
                continue
            if len(_pos) >= C.MAX_POSITIONS:
                log.info(f"MAX_POSITIONS={C.MAX_POSITIONS} alcanzado")
                break
            try:
                df_3m  = fetch(client, symbol, "3m",  C.LOOKBACK)
                df_15m = fetch(client, symbol, "15m", 100)
                if not ok(df_3m):
                    continue

                price = float(df_3m["close"].iloc[-1])
                sig   = strategy.compute(df_3m, df_15m)

                if sig.direction == "NONE":
                    continue

                log.info(f"SEÑAL {sig.direction} {sig.quality} {symbol} score={sig.score:+.3f}")

                sym_pos = [open_syms[symbol]] if symbol in open_syms else []
                ok_dir, r2 = risk.anti_hedge(sig.direction, sym_pos)
                if not ok_dir:
                    continue

                qty = risk.position_size(balance, sig)
                if qty <= 0:
                    continue

                setup(symbol)
                time.sleep(0.3)

                side   = "BUY" if sig.direction == "LONG" else "SELL"
                result = client.market_order(symbol, side, qty)

                if result.get("code", -1) != 0:
                    log.error(f"Error orden {symbol}: {result}")
                    tg.warn(f"❌ Error {symbol}: {result.get('msg', result)}")
                    continue

                _pos[symbol] = {
                    "direction": sig.direction, "sl": sig.sl,
                    "tp": sig.tp, "entry": sig.entry,
                    "qty": qty, "atr": sig.atr_val,
                }
                tg.entry(symbol, sig, qty, balance)
                log.info(f"✅ {sig.direction} {qty:.6f} {symbol} @ {sig.entry:.5f}")
                balance = client.get_balance()

            except Exception as e:
                log.exception(f"Error {symbol}: {e}")
                hs.update(error=True)

    except Exception as e:
        log.exception(f"Error ciclo: {e}")
        tg.warn(f"⚠️ Error ciclo: {e}")
        hs.update(error=True)


# ── Bot init (en thread separado) ─────────────────────────────

def _bot_init():
    try:
        log.info("════════════════════════════════════")
        log.info("  CVD Bot Multi-Símbolo — iniciando ")
        log.info("════════════════════════════════════")

        if not C.BINGX_API_KEY or not C.BINGX_SECRET_KEY:
            log.error("❌ Credenciales BingX no configuradas")
            return

        balance = client.get_balance()
        log.info(f"Balance: {balance:.2f} USDT | Modo: {C.SYMBOL_MODE}")

        refresh_symbols()
        sync_all()
        for sym in list(_pos.keys()):
            setup(sym)

        tg.startup_multi(balance, _symbols)
        cycle()

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
        log.info("Scheduler activo — cada 3min")
        sched.start()

    except (KeyboardInterrupt, SystemExit):
        log.info("Bot detenido")
    except Exception as e:
        log.exception(f"Error fatal: {e}")
        tg.warn(f"💀 Error fatal: {e}")


# ── Main ──────────────────────────────────────────────────────

def main():
    # health server ya arrancó arriba (nivel de módulo)
    # Bot corre en thread no-daemon para que el proceso no termine
    t = threading.Thread(target=_bot_init, daemon=False, name="cvd-bot")
    t.start()
    t.join()


if __name__ == "__main__":
    main()
