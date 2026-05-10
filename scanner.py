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
    """
    WHITELIST_ONLY=true (recomendado para dinero real):
      Usa PAIR_WHITELIST — pares de alta liquidez con
      mean-reversion fiable. Ignora el scoring dinámico.

    WHITELIST_ONLY=false:
      Scan completo con MIN_QUOTE_VOL=50M para filtrar
      altcoins de baja liquidez.
    """
    log.info("🔭 Iniciando scan de pares...")

    # ── MODO WHITELIST (recomendado dinero real) ──────────────────────
    if config.WHITELIST_ONLY:
        # Verificar que los pares existen en BingX
        tickers    = await client.get_tickers()
        ticker_map = {t["symbol"]: t for t in tickers if "symbol" in t}
        active = [
            sym for sym in config.PAIR_WHITELIST
            if sym in ticker_map
            and float(ticker_map[sym].get("quoteVolume", 0)) >= config.MIN_QUOTE_VOL
        ]
        log.info(f"   ✅ Whitelist activa: {len(active)} pares verificados en BingX")
        await tg.scanner_result(session, active, balance)
        return active

    # ── MODO DINÁMICO (solo si WHITELIST_ONLY=false) ─────────────────
    log.info("   ⚠️ Modo dinámico — asegúrate de tener MIN_QUOTE_VOL alto")
    scorer    = ExplosionScorer()
    contracts = await client.get_contracts()
    usdt_pairs = [
        c["symbol"] for c in contracts
        if c.get("symbol", "").endswith("-USDT")
    ]

    tickers    = await client.get_tickers()
    ticker_map = {t["symbol"]: t for t in tickers if "symbol" in t}

    candidates = [
        sym for sym in usdt_pairs
        if float(ticker_map.get(sym, {}).get("quoteVolume", 0)) >= config.MIN_QUOTE_VOL
        and float(ticker_map.get(sym, {}).get("lastPrice",   0)) >= config.MIN_PRICE_USDT
    ]
    log.info(f"   → {len(candidates)} pares con volumen ≥ {config.MIN_QUOTE_VOL/1e6:.0f}M USDT")

    scored     = []
    batch_size = 20
    for i in range(0, len(candidates), batch_size):
        batch   = candidates[i:i + batch_size]
        tasks   = [client.get_24h_volume_history(sym) for sym in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, res in zip(batch, results):
            if isinstance(res, Exception) or not res:
                continue
            score = scorer.score(ticker_map.get(sym, {}), res)
            scored.append((sym, score))
        await asyncio.sleep(0.2)

    scored.sort(key=lambda x: x[1], reverse=True)
    top_pairs = [sym for sym, _ in scored[:config.TOP_PAIRS]]
    log.info(f"   ✅ Top {len(top_pairs)} pares: {top_pairs[:5]}...")

    await tg.scanner_result(session, top_pairs, balance)
    return top_pairs
