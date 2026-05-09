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
    Escanea TODOS los pares perpetuos USDT de BingX y retorna los
    TOP_PAIRS con mayor probabilidad de movimiento explosivo hoy.

    Para dinero real en 3m, TOP_PAIRS=15 es el punto óptimo:
      • < 10 pares → pocas señales, días sin operar
      • 15 pares   → equilibrio calidad/frecuencia (recomendado)
      • > 20 pares → pares de baja liquidez + rate limit + señales basura
    """
    log.info("🔭 Iniciando scan de pares explosivos...")

    scorer = ExplosionScorer()

    # ── 1. Contratos USDT ────────────────────────────────────────────
    contracts = await client.get_contracts()
    usdt_pairs = [
        c["symbol"] for c in contracts
        if c.get("symbol", "").endswith("-USDT")
    ]
    log.info(f"   → {len(usdt_pairs)} pares USDT encontrados")

    # ── 2. Tickers 24h ───────────────────────────────────────────────
    tickers    = await client.get_tickers()
    ticker_map = {t["symbol"]: t for t in tickers if "symbol" in t}

    # ── 3. Filtro primario: volumen mínimo 5M USDT/24h ───────────────
    MIN_QUOTE_VOL = 5_000_000
    candidates = [
        sym for sym in usdt_pairs
        if float(ticker_map.get(sym, {}).get("quoteVolume", 0)) > MIN_QUOTE_VOL
        and float(ticker_map.get(sym, {}).get("lastPrice",   0)) > config.MIN_PRICE_USDT
    ]
    log.info(f"   → {len(candidates)} pares con volumen suficiente")

    # ── 4. Scoring con klines diarias (batches de 20) ────────────────
    scored     = []
    batch_size = 20

    for i in range(0, len(candidates), batch_size):
        batch   = candidates[i:i + batch_size]
        tasks   = [client.get_24h_volume_history(sym) for sym in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, res in zip(batch, results):
            if isinstance(res, Exception) or not res:
                continue
            ticker = ticker_map.get(sym, {})
            score  = scorer.score(ticker, res)
            scored.append((sym, score))

        await asyncio.sleep(0.2)

    # ── 5. Top N ─────────────────────────────────────────────────────
    scored.sort(key=lambda x: x[1], reverse=True)
    top_pairs = [sym for sym, _ in scored[:config.TOP_PAIRS]]

    log.info(f"   ✅ Top {len(top_pairs)} pares: {top_pairs[:5]}...")

    await tg.scanner_result(session, top_pairs, balance)
    return top_pairs
