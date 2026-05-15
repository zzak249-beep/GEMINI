import asyncio,logging
from typing import List
import aiohttp
import config,telegram_notifier as tg
from bingx_client import BingXClient
from strategy import ExplosionScorer

log=logging.getLogger("scanner")

async def scan_explosive_pairs(client:BingXClient,session:aiohttp.ClientSession,balance:float)->List[str]:
    log.info("🔭 Scan...")
    if config.WHITELIST_ONLY:
        tickers=await client.get_tickers()
        tm={t["symbol"]:t for t in tickers if "symbol" in t}
        active=[s for s in config.PAIR_WHITELIST
                if s in tm and float(tm[s].get("quoteVolume",0))>=config.MIN_QUOTE_VOL]
        log.info(f"   ✅ {len(active)} pares whitelist")
        await tg.scanner_result(session,active,balance)
        return active
    scorer=ExplosionScorer()
    contracts=await client.get_contracts()
    usdt_pairs=[c["symbol"] for c in contracts if c.get("symbol","").endswith("-USDT")]
    tickers=await client.get_tickers()
    tm={t["symbol"]:t for t in tickers if "symbol" in t}
    candidates=[s for s in usdt_pairs
                if float(tm.get(s,{}).get("quoteVolume",0))>=config.MIN_QUOTE_VOL
                and float(tm.get(s,{}).get("lastPrice",0))>=config.MIN_PRICE_USDT]
    scored=[]
    for i in range(0,len(candidates),20):
        batch=candidates[i:i+20]
        results=await asyncio.gather(*[client.get_24h_volume_history(s) for s in batch],return_exceptions=True)
        for s,r in zip(batch,results):
            if not isinstance(r,Exception) and r:
                scored.append((s,scorer.score(tm.get(s,{}),r)))
        await asyncio.sleep(0.2)
    scored.sort(key=lambda x:x[1],reverse=True)
    top=[s for s,_ in scored[:config.TOP_PAIRS]]
    log.info(f"   ✅ {len(top)} pares dinámico")
    await tg.scanner_result(session,top,balance)
    return top
