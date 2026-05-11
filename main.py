"""
ZigZag V32 — Apex Quantum Shield
Deploy: Railway | Python 3.11+
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

import config
import telegram_notifier as tg
from bingx_client import BingXClient
from scanner import scan_explosive_pairs
from trader import Trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("main")

_shutdown = False
_last_cycle = datetime.now(timezone.utc)
_cycle_count = 0


async def _health(request):
    age = (datetime.now(timezone.utc) - _last_cycle).total_seconds()
    status = 200 if age < 300 else 503
    return web.Response(status=status, text=f"OK cycles={_cycle_count} last={age:.0f}s")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info(f"🌐 Health server puerto {config.PORT}")
    return runner


async def main():
    global _last_cycle, _cycle_count, _shutdown

    runner = await start_health_server()
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        client = BingXClient(session)
        trader = Trader(client, session)

        # Startup con retry
        balance = 0.0
        for i in range(5):
            try:
                balance = await client.get_balance()
                if balance >= 0:
                    break
            except Exception as e:
                log.error(f"get_balance intento {i+1}/5: {e}")
                await asyncio.sleep(5)

        log.info(f"💵 Balance: {balance:.2f} USDT")
        await tg.bot_start(session)

        active_pairs = []
        for i in range(3):
            try:
                active_pairs = await scan_explosive_pairs(client, session, balance)
                if active_pairs:
                    break
            except Exception as e:
                log.error(f"Scan intento {i+1}/3: {e}")
                await asyncio.sleep(10)

        last_scan_day = datetime.now(timezone.utc).day
        log.info(f"🚀 Bot activo con {len(active_pairs)} pares: {active_pairs}")

        consec_errors = 0

        while not _shutdown:
            try:
                now = datetime.now(timezone.utc)
                _last_cycle = now
                _cycle_count += 1

                # Nuevo día
                if now.day != last_scan_day:
                    try:
                        balance = await client.get_balance()
                        await tg.daily_summary(session, trader.daily_trades,
                                               trader.daily_wins, trader.daily_pnl, balance)
                        trader.reset_daily()
                        active_pairs = await scan_explosive_pairs(client, session, balance)
                        last_scan_day = now.day
                    except Exception as e:
                        log.error(f"Reset diario error: {e}")

                # Balance + posiciones
                try:
                    balance = await client.get_balance()
                except Exception as e:
                    log.warning(f"get_balance error: {e}")

                try:
                    await trader.refresh_live_positions()
                except Exception as e:
                    log.warning(f"refresh_live error: {e}")

                active_count = sum(1 for p in trader.positions.values() if not p.closed)
                log.info(
                    f"━━ CICLO #{_cycle_count} | balance={balance:.2f} USDT | "
                    f"pos_activas={active_count}/{config.MAX_POSITIONS} | "
                    f"paused={trader.paused} | pares={len(active_pairs)}"
                )

                # Procesar pares
                if active_pairs and not trader.paused:
                    tasks = [trader.process_pair(sym, balance) for sym in active_pairs]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for sym, res in zip(active_pairs, results):
                        if isinstance(res, Exception):
                            log.error(f"[{sym}] process_pair exception: {type(res).__name__}: {res}")
                elif trader.paused:
                    log.warning("⏸️  Bot pausado por límite de pérdida diaria")
                else:
                    log.warning("⚠️  Sin pares activos")

                consec_errors = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                consec_errors += 1
                log.exception(f"Main loop error #{consec_errors}: {e}")
                try:
                    await tg.error_alert(session, f"Main loop error: {e}")
                except Exception:
                    pass
                wait = min(10 * (2 ** (consec_errors - 1)), 120)
                await asyncio.sleep(wait)
                continue

            await asyncio.sleep(config.CANDLE_SLEEP)

    await runner.cleanup()


def _handle_signal(sig, frame):
    global _shutdown
    log.info(f"Signal {sig} — deteniendo")
    _shutdown = True


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Detenido.")
