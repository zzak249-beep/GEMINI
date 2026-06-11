"""
QF×JP Bot v6.4 — Scanner
"""
import asyncio
import logging
import time
from typing import Optional

import config as C
from bingx_client import BingXClient
from indicators import analyze, Signal
from risk_manager import RiskManager
from position_manager import PositionManager, OpenTrade
import telegram_client as tg

log = logging.getLogger("scanner")

_cb_blacklist: dict[str, float] = {}
CB_COOLDOWN = 600


async def _fetch_klines_all(client, symbol):
    """Descarga klines + order book + funding en paralelo."""
    results = await asyncio.gather(
        client.get_klines(symbol, C.TIMEFRAME,      200),
        client.get_klines(symbol, C.HTF_TIMEFRAME,  100),
        client.get_klines(symbol, C.HTF2_TIMEFRAME, 100),
        client.get_klines(symbol, C.HTF5_TIMEFRAME, 100),
        client.get_order_book(symbol, 10),   # OBI — sin firma, ultra rápido
        client.get_funding_rate(symbol),     # Funding bias
        return_exceptions=True,
    )
    def _s(r, t=list): return r if isinstance(r, t) else ([] if t==list else (r if isinstance(r,(int,float)) else 0.0))
    k3m = _s(results[0])
    k15m = _s(results[1])
    k1h  = _s(results[2])
    k4h  = _s(results[3])
    ob   = results[4] if isinstance(results[4], dict) else {}
    fr   = results[5] if isinstance(results[5], float) else 0.0
    return k3m, k15m, k1h, k4h, ob, fr

def _calc_obi(order_book: dict) -> float:
    """
    Order Book Imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol)
    Rango: -1 (presión vendedora total) a +1 (presión compradora total)
    """
    try:
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        bid_vol = sum(float(b[1]) for b in bids[:5] if len(b)>=2)
        ask_vol = sum(float(a[1]) for a in asks[:5] if len(a)>=2)
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0
    except Exception:
        return 0.0


async def _process_symbol(symbol, client, risk, pos_mgr):
    if pos_mgr.is_trading(symbol):
        return None

    now = time.time()
    if symbol in _cb_blacklist and now - _cb_blacklist[symbol] < CB_COOLDOWN:
        return None

    try:
        k3m, k15m, k1h, k4h, ob, fr = await _fetch_klines_all(client, symbol)
    except Exception as e:
        log.debug("[%s] fetch error: %s", symbol, e)
        return None

    if len(k3m) < 60:
        return None

    obi = _calc_obi(ob)

    try:
        sig = analyze(symbol, k3m, k15m, k1h, k4h, funding_rate=fr)
        # Ajustar score con OBI directamente en el signal
        if sig.direction != "NONE" and abs(obi) > 0.1:
            from indicators import composite_score, score_to_tier
            import config as C
            boost = 0.0
            if sig.direction == "SHORT" and obi < -0.1:
                boost = abs(obi) * 5   # presión vendedora confirma SHORT
            elif sig.direction == "LONG" and obi > 0.1:
                boost = obi * 5        # presión compradora confirma LONG
            if boost > 0:
                sig.score = min(sig.score + boost, 100.0)
                sig.tier  = score_to_tier(sig.score)
                log.debug("[%s] OBI boost +%.1f → score=%.1f tier=%s", symbol, boost, sig.score, sig.tier)
    except Exception as e:
        log.warning("[%s] analyze error: %s", symbol, e)
        return None

    if sig.direction == "NONE":
        return None

    if sig.circuit_breaker:
        _cb_blacklist[symbol] = now
        await tg.notify_circuit_breaker(symbol)
        return None

    if not risk.tier_ok(sig.tier):
        return None

    log.info("[%s] Señal %s tier=%s score=%.1f", symbol, sig.direction, sig.tier, sig.score)

    if C.MODE == "SIGNAL":
        await tg.notify_signal(sig)
        return sig

    # ── LIVE ─────────────────────────────────────────────────────────────────
    can, reason = await risk.can_trade()
    if not can:
        log.info("[%s] Bloqueado por risk: %s", symbol, reason)
        return None

    try:
        balance = await client.get_balance()
    except Exception as e:
        log.error("[%s] get_balance error: %s", symbol, e)
        return None

    if balance < 5.0:
        log.warning("get_balance=%.4f — usando CAPITAL fallback=%.2f USDT", balance, C.CAPITAL)
        balance = C.CAPITAL

    log.info("Balance activo: %.4f USDT", balance)

    qty = risk.kelly_position_size(balance, sig.entry, sig.sl, sig.score, sig.tier)
    if qty <= 0:
        log.warning("[%s] qty=0, skip", symbol)
        return None

    notional = qty * sig.entry
    log.info("[%s] qty=%s notional=%.2f USDT (price=%s)", symbol, qty, notional, sig.entry)

    await tg.notify_signal(sig)

    try:
        results = await client.open_trade(
            symbol=symbol, direction=sig.direction, quantity=qty,
            sl_price=sig.sl, tp1_price=sig.tp1, tp2_price=sig.tp2,
        )
    except Exception as e:
        log.error("[%s] open_trade error: %s", symbol, e)
        await tg.notify_error(f"open_trade({symbol})", str(e))
        return None

    entry_resp = results.get("entry", {})
    if entry_resp.get("code", -1) != 0:
        log.error("[%s] Entrada rechazada: %s", symbol, entry_resp)
        await tg.notify_error(f"entrada_rechazada({symbol})", str(entry_resp))
        return None

    order_id = str(
        entry_resp.get("data", {}).get("order", {}).get("orderId", "unknown")
        or entry_resp.get("data", {}).get("orderId", "unknown")
    )

    trade = OpenTrade(
        symbol=symbol, direction=sig.direction,
        entry=sig.entry, sl=sig.sl, tp1=sig.tp1, tp2=sig.tp2,
        qty=qty, atr=sig.atr, order_id=order_id,
    )
    await pos_mgr.register_trade(trade)
    await tg.notify_trade_opened(sig, qty, order_id)
    return sig


async def scan_loop(client: BingXClient, risk: RiskManager, pos_mgr: PositionManager):
    log.info("Scanner iniciado. Modo: %s | Interval: %ds | TOP_N: %s",
             C.MODE, C.SCAN_INTERVAL,
             C.TOP_N_SYMBOLS if C.TOP_N_SYMBOLS > 0 else "TODAS")

    symbols   = []
    iteration = 0

    while True:
        start = time.time()
        iteration += 1

        # Refrescar lista de símbolos
        if iteration == 1 or iteration % 10 == 0 or not symbols:
            try:
                new = await client.get_all_symbols()
                if new:
                    symbols = new
                    log.info("Símbolos activos: %d", len(symbols))
                else:
                    log.warning("get_all_symbols devolvió lista vacía (iter=%d)", iteration)
            except Exception as e:
                log.error("get_all_symbols error: %s", e)
                if not symbols:
                    await asyncio.sleep(30)
                    continue

        if not symbols:
            await asyncio.sleep(10)
            continue

        # Status periódico
        if iteration % 20 == 0:
            try:
                balance = await client.get_balance()
                await tg.notify_status(risk.status(), balance, len(symbols))
            except Exception as e:
                log.warning("status notify error: %s", e)

        # Procesar en batches
        BATCH = 20   # era 10 — más símbolos en paralelo
        signals_found = 0
        for i in range(0, len(symbols), BATCH):
            batch = symbols[i : i + BATCH]
            results = await asyncio.gather(
                *[_process_symbol(s, client, risk, pos_mgr) for s in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Signal) and r.direction != "NONE":
                    signals_found += 1
            await asyncio.sleep(0.2)   # era 0.5 — 2.5x más rápido

        elapsed = time.time() - start
        log.info("Iteración %d | %d símbolos | %d señales | %.1fs",
                 iteration, len(symbols), signals_found, elapsed)
        if signals_found == 0 and iteration <= 3:
            log.info("Sin señales — revisa: REQUIRE_TL_BREAK=%s HTF_MIN_ALIGNED=%s MIN_SCORE=%.0f",
                     C.REQUIRE_TL_BREAK, C.HTF_MIN_ALIGNED, C.MIN_SCORE)

        await asyncio.sleep(max(0.0, C.SCAN_INTERVAL - elapsed))
