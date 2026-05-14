import asyncio
import logging
from typing import List
import aiohttp

import config
import telegram_notifier as tg
from bingx_client import BingXClient
from strategy import ExplosionScorer

log = logging.getLogger("scanner")


async def scan_explosive_pairs(
    client: BingXClient,
    session: aiohttp.ClientSession,
    balance: float
) -> List[str]:

    log.info("🔭 Iniciando scan de pares...")

    if config.WHITELIST_ONLY:
        try:
            tickers    = await client.get_tickers()
            ticker_map = {t["symbol"]: t for t in tickers if "symbol" in t}
            active = [
                sym for sym in config.PAIR_WHITELIST
                if sym in ticker_map
                and float(ticker_map[sym].get("quoteVolume", 0)) >= config.MIN_QUOTE_VOL
            ]
        except Exception as e:
            log.error(f"scan whitelist error: {e}")
            active = config.PAIR_WHITELIST[:]

        if not active:
            log.warning("Whitelist vacía tras filtro de volumen — usando lista completa")
            active = config.PAIR_WHITELIST[:]

        log.info(f"   ✅ Whitelist: {len(active)} pares")
        await tg.scanner_result(session, active, balance)
        return active

    # Modo dinámico
    scorer    = ExplosionScorer()
    contracts = await client.get_contracts()
    usdt_pairs = [c["symbol"] for c in contracts
                  if c.get("symbol","").endswith("-USDT")]

    tickers    = await client.get_tickers()
    ticker_map = {t["symbol"]: t for t in tickers if "symbol" in t}

    candidates = [
        sym for sym in usdt_pairs
        if float(ticker_map.get(sym,{}).get("quoteVolume",0)) >= config.MIN_QUOTE_VOL
        and float(ticker_map.get(sym,{}).get("lastPrice",  0)) >= config.MIN_PRICE_USDT
    ]

    scored = []
    for i in range(0, len(candidates), 20):
        batch   = candidates[i:i+20]
        tasks   = [client.get_24h_volume_history(sym) for sym in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, res in zip(batch, results):
            if isinstance(res, Exception) or not res:
                continue
            score = scorer.score(ticker_map.get(sym,{}), res)
            scored.append((sym, score))
        await asyncio.sleep(0.2)

    scored.sort(key=lambda x: x[1], reverse=True)
    top = [sym for sym, _ in scored[:config.TOP_PAIRS]]
    log.info(f"   ✅ Top {len(top)} pares dinámicos")
    await tg.scanner_result(session, top, balance)
    return top
