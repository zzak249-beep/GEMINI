"""
ZigZag Institutional Elite V6 — BingX Perpetual Futures Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Deploy: Railway (GitHub) | Python 3.11+
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
import aiohttp

import config
import telegram_notifier as tg
from bingx_client import BingXClient
from scanner import scan_explosive_pairs
from trader import Trader

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("main")


async def main():
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        client = BingXClient(session)
        trader = Trader(client, session)

        # ── Startup ──────────────────────────────────────────────────
        balance = await client.get_balance()
        log.info(f"💵 Balance inicial: {balance:.2f} USDT")
        await tg.bot_start(session)

        # ── Scan inicial ─────────────────────────────────────────────
        active_pairs = await scan_explosive_pairs(client, session, balance)
        last_scan_day = datetime.now(timezone.utc).day

        log.info(f"🚀 Bot activo con {len(active_pairs)} pares.")

        # ── Main loop ─────────────────────────────────────────────────
        while True:
            try:
                now = datetime.now(timezone.utc)

                # ── Nuevo día → resumen + re-scan ────────────────────
                if now.day != last_scan_day:
                    balance = await client.get_balance()
                    await tg.daily_summary(
                        session,
                        trader.daily_trades,
                        trader.daily_wins,
                        trader.daily_pnl,
                        balance
                    )
                    trader.reset_daily()
                    active_pairs = await scan_explosive_pairs(client, session, balance)
                    last_scan_day = now.day
                    log.info(f"🔄 Nuevo día — {len(active_pairs)} pares activos")

                # ── Una sola llamada a balance + posiciones por ciclo ─
                balance = await client.get_balance()
                await trader.refresh_live_positions()

                # ── Procesar pares en paralelo ───────────────────────
                if active_pairs and not trader.paused:
                    tasks = [trader.process_pair(sym, balance) for sym in active_pairs]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for sym, res in zip(active_pairs, results):
                        if isinstance(res, Exception):
                            log.error(f"[{sym}] Error no manejado: {res}")

            except Exception as e:
                log.exception(f"Error en main loop: {e}")
                await tg.error_alert(session, f"Main loop error: {e}")
                await asyncio.sleep(10)

            # ── Esperar siguiente vela ────────────────────────────────
            await asyncio.sleep(config.CANDLE_SLEEP)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot detenido manualmente.")
