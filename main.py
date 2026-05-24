"""
QF Machine × JP Fusion Bot v3.1 — Orquestador Principal
Loop de trading + Scanner completo de mercado
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from exchange     import BingXClient
from signals      import QFSignalEngine
from risk         import RiskManager
from positions    import Position, PositionTracker
from telegram_bot import TelegramNotifier
from scanner      import QFScanner
from config       import SIGNAL_CFG, RISK_CFG, SYMBOLS, HTF

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("QFBot")

BOT_STATE = {
    "paused": False,
    "paper":  os.getenv("PAPER_MODE", "true").lower() != "false",
}

# ── Config Scanner ────────────────────────────────────────────
SCAN_CFG = {
    "min_volume_usdt":    float(os.getenv("MIN_VOLUME", "200000")),
    "scan_concurrency":   int(os.getenv("SCAN_CONCURRENCY", "20")),
    "scan_cooldown_min":  float(os.getenv("SCAN_COOLDOWN_MIN", "15")),
    "scan_min_conviction":int(os.getenv("SCAN_MIN_CONV", "1")),
    # Intervalo entre scans completos (segundos)
    "scan_interval_s":    int(os.getenv("SCAN_INTERVAL_S", "180")),
    # Máx señales a notificar por scan
    "scan_max_notify":    int(os.getenv("SCAN_MAX_NOTIFY", "3")),
    # Resumen cada N scans
    "summary_every":      int(os.getenv("SUMMARY_EVERY", "10")),
}


class QFBot:
    def __init__(self):
        paper = BOT_STATE["paper"]

        self.exchange = BingXClient(
            api_key=os.environ["BINGX_API_KEY"],
            secret=os.environ["BINGX_SECRET"],
            paper=paper,
        )
        self.engine    = QFSignalEngine(SIGNAL_CFG)
        self.risk      = RiskManager(RISK_CFG)
        self.positions = PositionTracker()
        self.tg        = TelegramNotifier(
            token=os.environ["TELEGRAM_TOKEN"],
            chat_id=os.environ["TELEGRAM_CHAT_ID"],
            risk_manager=self.risk,
            bot_state=BOT_STATE,
        )
        self.scanner = QFScanner(
            exchange=self.exchange,
            signal_engine=self.engine,
            risk_manager=self.risk,
            notifier=self.tg,
            cfg=SCAN_CFG,
        )
        self._scan_count   = 0
        self._last_scan_t  = 0.0

    # ─────────────────────────────────────────────────────────
    #  ARRANQUE
    # ─────────────────────────────────────────────────────────
    async def run(self):
        await self.tg.start_polling()

        mode = "📋 PAPER MODE" if BOT_STATE["paper"] else "💵 LIVE MODE"
        min_vol = SCAN_CFG['min_volume_usdt']
        await self.tg._send(
            f"🤖 *QF Machine × JP Fusion Bot v3.1*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Modo: *{mode}*\n"
            f"🔭 Scanner: todos los pares BingX (vol≥{min_vol/1000:.0f}K USDT)\n"
            f"⚡ Concurrencia: `{SCAN_CFG['scan_concurrency']}`\n"
            f"🔄 Ciclo scan: `{SCAN_CFG['scan_interval_s']}s`\n"
            f"🎯 HUNT mode: Score≥`{SIGNAL_CFG.get('hunt_score_thr',0.08)*100:.0f}` "
            f"Decay≥`{SIGNAL_CFG.get('hunt_decay_thr',0.35)*100:.0f}%`\n\n"
            f"{'⚠️ DINERO REAL — cuida el riesgo' if not BOT_STATE['paper'] else '✅ Paper trading activo'}"
        )

        try:
            while True:
                await self._tick()
                await asyncio.sleep(int(os.getenv("LOOP_SECONDS", "30")))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Error crítico: {e}")
            await self.tg.send_alert(f"❌ Error crítico: {e}")
        finally:
            await self.tg.stop_polling()
            await self.exchange.close()

    # ─────────────────────────────────────────────────────────
    #  TICK PRINCIPAL
    # ─────────────────────────────────────────────────────────
    async def _tick(self):
        if BOT_STATE.get("paused"):
            return

        now = time.time()

        # ── 1. Scanner de mercado ─────────────────────────────
        if now - self._last_scan_t >= SCAN_CFG['scan_interval_s']:
            await self._run_scanner()
            self._last_scan_t = now

        # ── 2. Gestionar posiciones abiertas ─────────────────
        for symbol in list(self.positions.positions.keys()):
            try:
                await self._manage_open(symbol)
            except Exception as e:
                logger.error(f"Error gestionando {symbol}: {e}")

        # ── 3. Abrir señales en SYMBOLS fijos (opcional) ─────
        for symbol in SYMBOLS:
            if not self.positions.has(symbol):
                try:
                    await self._process_symbol(symbol)
                except Exception as e:
                    logger.error(f"Error en {symbol}: {e}")

    # ─────────────────────────────────────────────────────────
    #  SCANNER — ciclo completo
    # ─────────────────────────────────────────────────────────
    async def _run_scanner(self):
        self._scan_count += 1
        can, reason = self.risk.check_circuit()

        try:
            hits = await self.scanner.run_scan()
        except Exception as e:
            logger.error(f"Error en scanner: {e}")
            return

        n_scanned = len(self.scanner._sent) + len(hits) + 1  # aprox

        # Notificar top señales
        if can and not BOT_STATE.get("paused"):
            await self.scanner.notify_top_signals(
                hits, max_notify=SCAN_CFG['scan_max_notify']
            )
            # Intentar operar las mejores señales del scanner
            await self._execute_scanner_hits(hits)

        # Resumen periódico
        if self._scan_count % SCAN_CFG['summary_every'] == 1:
            try:
                symbols_scanned = await self.exchange.get_all_symbols()
                n = len(symbols_scanned)
            except Exception:
                n = len(hits) * 10
            await self.scanner.send_summary(hits, n, BOT_STATE["paper"])

    # ─────────────────────────────────────────────────────────
    #  EJECUTAR HITS DEL SCANNER
    # ─────────────────────────────────────────────────────────
    async def _execute_scanner_hits(self, hits: list):
        """
        De las mejores señales del scanner, intenta abrir posición
        en las que no tienen posición abierta ya.
        Máximo MAX_OPEN_POSITIONS simultáneas.
        """
        max_pos = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
        current = len(self.positions.positions)

        if current >= max_pos:
            return

        min_tier_to_trade = os.getenv("MIN_TIER_TO_TRADE", "HUNT_LONG")
        tier_rank = {
            "SUPREMA": 5, "FUEL": 4, "STD": 3,
            "HUNT_LONG": 2, "HUNT_SHORT": 2
        }
        min_rank = tier_rank.get(min_tier_to_trade, 2)

        for r in hits:
            if current >= max_pos:
                break
            if self.positions.has(r.symbol):
                continue
            if tier_rank.get(r.tier, 0) < min_rank:
                continue

            can, reason = self.risk.check_circuit()
            if not can:
                break

            try:
                await self._open_position_from_scan(r)
                current += 1
            except Exception as e:
                logger.error(f"Error abriendo {r.symbol}: {e}")

    async def _open_position_from_scan(self, r):
        qty = self.risk.calc_position_size(
            entry=r.entry, sl=r.sl,
            tier=r.tier if r.tier not in ("HUNT_LONG","HUNT_SHORT") else "STD",
            conviction=r.conviction,
        )
        if not qty or qty <= 0:
            return

        await self.exchange.set_leverage(r.symbol, RISK_CFG['leverage'])

        side     = "BUY" if r.direction == "LONG" else "SELL"
        pos_side = r.direction

        order = await self.exchange.place_order(r.symbol, side, pos_side, qty)
        await self.exchange.set_sl_tp(r.symbol, pos_side, r.sl, r.tp, qty)

        pos = Position(
            symbol=r.symbol, direction=r.direction, tier=r.tier,
            entry=r.entry, sl=r.sl, tp=r.tp, qty=qty,
            open_time=datetime.utcnow().isoformat(),
            order_id=str(order.get("orderId","?")),
            paper=BOT_STATE["paper"], trailing_sl=r.sl,
        )
        self.positions.open(pos)

        # Notificar apertura
        mode = "📋 PAPER" if BOT_STATE["paper"] else "💵 REAL"
        rr   = abs(r.tp-r.entry)/abs(r.entry-r.sl) if abs(r.entry-r.sl)>0 else 0
        await self.tg._send(
            f"{'🎯' if 'HUNT' in r.tier else '🚀'} *APERTURA {r.tier}* — {mode}\n"
            f"{'🟢' if r.direction=='LONG' else '🔴'} *{r.direction}* `{r.symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Entrada: `{r.entry:.6f}`\n"
            f"🛑 SL:      `{r.sl:.6f}`\n"
            f"🎯 TP:      `{r.tp:.6f}`\n"
            f"⚖️  R:R:     `{rr:.1f}x`\n"
            f"📦 Qty:     `{qty:.6f}`\n"
            f"🧠 Conv: {r.conviction}/10 | Score: {r.score*100:+.0f}"
        )
        logger.info(f"✅ {r.direction} {r.symbol} {r.tier} entry={r.entry:.6f} sl={r.sl:.6f} tp={r.tp:.6f}")

    # ─────────────────────────────────────────────────────────
    #  PROCESS SYMBOL (SYMBOLS fijos)
    # ─────────────────────────────────────────────────────────
    async def _process_symbol(self, symbol: str):
        df_3m  = await self.exchange.get_klines(symbol, "3m",  limit=250)
        df_htf = await self.exchange.get_klines(symbol, HTF,   limit=100)
        if len(df_3m) < 100:
            return

        sig = self.engine.compute(df_3m, df_htf)
        if sig.direction == "FLAT" or sig.tier == "NONE":
            return

        min_conv  = int(os.getenv("MIN_CONVICTION", "6"))
        hunt_min  = int(os.getenv("HUNT_MIN_CONVICTION", "1"))
        is_hunt   = sig.tier in ("HUNT_LONG","HUNT_SHORT")
        threshold = hunt_min if is_hunt else min_conv
        if sig.conviction < threshold:
            return

        qty = self.risk.calc_position_size(
            entry=sig.entry_price, sl=sig.sl_price,
            tier=sig.tier if not is_hunt else "STD",
            conviction=sig.conviction,
        )
        if not qty or qty <= 0:
            return

        tp   = self.risk.calc_tp(sig.entry_price, sig.sl_price, sig.direction, sig.tier)
        side = "BUY" if sig.direction == "LONG" else "SELL"

        await self.exchange.set_leverage(symbol, RISK_CFG['leverage'])
        order = await self.exchange.place_order(symbol, side, sig.direction, qty)
        await self.exchange.set_sl_tp(symbol, sig.direction, sig.sl_price, tp, qty)

        pos = Position(
            symbol=symbol, direction=sig.direction, tier=sig.tier,
            entry=sig.entry_price, sl=sig.sl_price, tp=tp, qty=qty,
            open_time=datetime.utcnow().isoformat(),
            order_id=str(order.get("orderId","?")),
            paper=BOT_STATE["paper"], trailing_sl=sig.sl_price,
        )
        self.positions.open(pos)
        await self.tg.send_signal(sig, symbol, qty, tp, BOT_STATE["paper"])

    # ─────────────────────────────────────────────────────────
    #  GESTIÓN POSICIÓN ABIERTA
    # ─────────────────────────────────────────────────────────
    async def _manage_open(self, symbol: str):
        pos = self.positions.get(symbol)
        if not pos:
            return

        try:
            df = await self.exchange.get_klines(symbol, "3m", limit=50)
        except Exception:
            return

        price = float(df['close'].iloc[-1])
        atr   = float((df['high']-df['low']).rolling(10).mean().iloc[-1])

        # Trailing stop
        new_sl = self.positions.calc_trailing_sl(
            pos, price, atr,
            trail_atr_mult=float(os.getenv("TRAIL_ATR","1.5"))
        )
        if new_sl != pos.trailing_sl:
            self.positions.update_trailing_sl(symbol, new_sl)

        exit_reason = self.positions.check_exit(symbol, price)
        if not exit_reason:
            return

        pnl = self.positions.calc_pnl(symbol, price)
        if not BOT_STATE["paper"]:
            await self.exchange.close_position(symbol, pos.direction, pos.qty)

        self.risk.record_trade(pnl)

        reason_txt = {"stop_loss": "Stop Loss 🛑", "take_profit": "Take Profit ✨"}.get(exit_reason, exit_reason)
        await self.tg.send_trade_close(
            symbol=symbol, direction=pos.direction, pnl=pnl,
            entry=pos.entry, exit_price=price, reason=reason_txt,
            paper=BOT_STATE["paper"],
        )
        self.positions.close(symbol)

        can, reason = self.risk.check_circuit()
        if not can:
            await self.tg.send_circuit_breaker(reason)


if __name__ == "__main__":
    bot = QFBot()
    asyncio.run(bot.run())
