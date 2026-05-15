import asyncio,logging,sys
from datetime import datetime,timezone
import aiohttp
from aiohttp import web
import config,telegram_notifier as tg
from bingx_client import BingXClient
from scanner import scan_explosive_pairs
from trader import Trader

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",handlers=[logging.StreamHandler(sys.stdout)])
log=logging.getLogger("main")
_shutdown=False;_last=datetime.now(timezone.utc);_cycle=0

async def _health(r):
    age=(datetime.now(timezone.utc)-_last).total_seconds()
    return web.Response(status=200 if age<300 else 503,text=f"OK cycles={_cycle} age={age:.0f}s")

async def main():
    global _last,_cycle,_shutdown
    app=web.Application()
    app.router.add_get("/",_health);app.router.add_get("/health",_health)
    runner=web.AppRunner(app);await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",config.PORT).start()
    log.info(f"🌐 :{config.PORT}")
    timeout=aiohttp.ClientTimeout(total=30,connect=10)
    connector=aiohttp.TCPConnector(limit=30,ttl_dns_cache=300,enable_cleanup_closed=True)
    async with aiohttp.ClientSession(connector=connector,timeout=timeout) as session:
        client=BingXClient(session);trader=Trader(client,session)
        balance=0.0
        for i in range(5):
            try: balance=await client.get_balance(); break
            except Exception as e: log.error(f"balance retry {i+1}: {e}"); await asyncio.sleep(5)
        log.info(f"💵 Balance: {balance:.2f} USDT")
        if balance==0: log.warning("⚠️ balance=0 — transfiere fondos de Spot a Futuros Perpetuos en BingX")
        await tg.bot_start(session)
        pairs=[]
        for i in range(3):
            try: pairs=await scan_explosive_pairs(client,session,balance); break
            except Exception as e: log.error(f"scan retry {i+1}: {e}"); await asyncio.sleep(10)
        last_scan=datetime.now(timezone.utc);last_day=last_scan.day
        log.info(f"🚀 {len(pairs)} pares: {pairs}");errs=0
        while not _shutdown:
            try:
                now=datetime.now(timezone.utc);_last=now;_cycle+=1
                if (now-last_scan).total_seconds()>=config.SCAN_INTERVAL_H*3600:
                    try:
                        balance=await client.get_balance()
                        if now.day!=last_day:
                            await tg.daily_summary(session,trader.daily_trades,trader.daily_wins,trader.daily_pnl,balance)
                            trader.reset_daily();last_day=now.day
                        pairs=await scan_explosive_pairs(client,session,balance);last_scan=now
                    except Exception as e: log.error(f"rescan: {e}")
                try: balance=await client.get_balance()
                except: pass
                try: await trader.refresh_live_positions()
                except: pass
                active=sum(1 for p in trader.positions.values() if not p.closed)
                log.info(f"━━ #{_cycle} bal={balance:.2f} pos={active}/{config.MAX_POSITIONS} "
                         f"pnl={trader.daily_pnl:+.3f} pairs={len(pairs)}")
                if pairs and not trader.paused:
                    results=await asyncio.gather(*[trader.process_pair(s,balance) for s in pairs],return_exceptions=True)
                    for s,r in zip(pairs,results):
                        if isinstance(r,Exception): log.error(f"[{s}] {r}")
                errs=0
            except asyncio.CancelledError: break
            except Exception as e:
                errs+=1;log.exception(f"loop #{errs}: {e}")
                try: await tg.error_alert(session,str(e))
                except: pass
                await asyncio.sleep(min(10*(2**(errs-1)),120));continue
            await asyncio.sleep(config.CANDLE_SLEEP)
    await runner.cleanup()

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("Detenido.")
