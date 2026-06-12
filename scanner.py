"""
QF×JP Bot v6.7 — Scanner
MEJORAS v6.7:
  SPEED 1: SCAN_INTERVAL efectivo 20s (era 60s) — 3x más reacciones por hora
  SPEED 2: Batches de 30 sin sleep entre ellos (rate-limit safe con semáforo)
  SPEED 3: Priority queue — los símbolos con señal reciente escanean PRIMERO
  SPEED 4: Entry paralelo SL+TP en el mismo gather (≈800ms menos de slippage)
  SPEED 5: Símbolos sin volumen skippeados en RAM — sin llamada a API
  FIX:     notify_partial_close añadido en telegram_client v6.7
"""
import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

import config as C
from bingx_client import BingXClient
from indicators import analyze, Signal
from risk_manager import RiskManager
from position_manager import PositionManager, OpenTrade
import telegram_client as tg

log = logging.getLogger("scanner")

# ── Circuit breaker blacklist ─────────────────────────────────────────────────
_cb_blacklist: dict[str, float] = {}
CB_COOLDOWN = 600

# ── Priority tracking — símbolos con señal reciente suben en la cola ──────────
_symbol_priority: dict[str, float] = defaultdict(float)   # symbol → last_signal_ts
_PRIORITY_WINDOW = 300   # 5 min de ventana de prioridad

# ── Semáforo global de API — evita 429 con batches grandes ───────────────────
_api_sem = asyncio.Semaphore(25)   # máx 25 calls concurrentes


async def _fetch_klines_all(client, symbol):
    """Descarga klines + order book + funding en paralelo con semáforo."""
    async with _api_sem:
        results = await asyncio.gather(
            client.get_klines(symbol, C.TIMEFRAME,      200),
            client.get_klines(symbol, C.HTF_TIMEFRAME,  100),
            client.get_klines(symbol, C.HTF2_TIMEFRAME, 100),
            client.get_klines(symbol, C.HTF5_TIMEFRAME, 100),
            client.get_order_book(symbol, 10),
            client.get_funding_rate(symbol),
            return_exceptions=True,
        )
    def _s(r, t=list): return r if isinstance(r, t) else ([] if t == list else 0.0)
    return (
        _s(results[0]),
        _s(results[1]),
        _s(results[2]),
        _s(results[3]),
        results[4] if isinstance(results[4], dict) else {},
        results[5] if isinstance(results[5], float) else 0.0,
    )


def _calc_obi(order_book: dict) -> float:
    try:
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        bid_vol = sum(float(b[1]) for b in bids[:5] if len(b) >= 2)
        ask_vol = sum(float(a[1]) for a in asks[:5] if len(a) >= 2)
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0
    except Exception:
        return 0.0


def _sort_by_priority(symbols: list[str]) -> list[str]:
    """
    Símbolos con señal reciente (< 5 min) van primero.
    El resto mantiene orden original (ya filtrado por volumen).
    """
    now = time.time()
    hot  = [s for s in symbols if now - _symbol_priority.get(s, 0) < _PRIORITY_WINDOW]
    cold = [s for s in symbols if s not in set(hot)]
    return hot + cold


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
        if sig.direction != "NONE" and abs(obi) > 0.1:
            from indicators import score_to_tier
            boost = 0.0
            if sig.direction == "SHORT" and obi < -0.1:
                boost = abs(obi) * 5
            elif sig.direction == "LONG" and obi > 0.1:
                boost = obi * 5
            if boost > 0:
                sig.score = min(sig.score + boost, 100.0)
                sig.tier  = score_to_tier(sig.score)
                log.debug("[%s] OBI boost +%.1f → score=%.1f tier=%s",
                          symbol, boost, sig.score, sig.tier)
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

    # Marcar como activo para prioridad futura
    _symbol_priority[symbol] = now

    log.info("[%s] Señal %s tier=%s score=%.1f", symbol, sig.direction, sig.tier, sig.score)

    if C.MODE == "SIGNAL":
        await tg.notify_signal(sig)
        return sig

    # ── LIVE ──────────────────────────────────────────────────────────────────
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
        log.warning("[%s] balance=%.4f — fallback CAPITAL=%.2f", symbol, balance, C.CAPITAL)
        balance = C.CAPITAL

    qty = risk.kelly_position_size(balance, sig.entry, sig.sl, sig.score, sig.tier)
    if qty <= 0:
        log.warning("[%s] qty=0, skip", symbol)
        return None

    log.info("[%s] qty=%s notional=%.2f USDT", symbol, qty, qty * sig.entry)

    await tg.notify_signal(sig)

    # SPEED 4: entrada + SL en paralelo donde sea posible
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
    log.info("Scanner v6.7 iniciado. Modo: %s | Interval: %ds | TOP_N: %s",
             C.MODE, C.SCAN_INTERVAL,
             C.TOP_N_SYMBOLS if C.TOP_N_SYMBOLS > 0 else "TODAS")

    symbols: list[str] = []
    iteration   = 0
    # SPEED 1: intervalo efectivo reducido — ignorar SCAN_INTERVAL si > 20s en LIVE
    _eff_interval = min(C.SCAN_INTERVAL, 20) if C.MODE == "LIVE" else C.SCAN_INTERVAL

    while True:
        start = time.time()
        iteration += 1

        # Refrescar lista de símbolos cada 10 iteraciones
        if iteration == 1 or iteration % 10 == 0 or not symbols:
            try:
                new = await client.get_all_symbols()
                if new:
                    symbols = new
                    log.info("Símbolos activos: %d", len(symbols))
                else:
                    log.warning("get_all_symbols vacío (iter=%d)", iteration)
            except Exception as e:
                log.error("get_all_symbols error: %s", e)
                if not symbols:
                    await asyncio.sleep(30)
                    continue

        if not symbols:
            await asyncio.sleep(10)
            continue

        # Status + WinRate report cada hora
        iters_per_hour = max(1, 3600 // max(_eff_interval, 1))
        if iteration % iters_per_hour == 0:
            try:
                balance = await client.get_balance()
                await tg.notify_status(risk.status(), balance, len(symbols))
                await tg.notify_winrate_report()   # reporte WR horario
            except Exception as e:
                log.warning("status notify error: %s", e)

        # SPEED 3: priorizar símbolos con señal reciente
        ordered = _sort_by_priority(symbols)

        # SPEED 2: batches de 30, sin sleep interno (semáforo controla rate)
        BATCH = 30
        signals_found = 0
        for i in range(0, len(ordered), BATCH):
            batch = ordered[i : i + BATCH]
            results = await asyncio.gather(
                *[_process_symbol(s, client, risk, pos_mgr) for s in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Signal) and r.direction != "NONE":
                    signals_found += 1

        elapsed = time.time() - start
        log.info("Iteración %d | %d símbolos | %d señales | %.1fs",
                 iteration, len(symbols), signals_found, elapsed)

        if signals_found == 0 and iteration <= 3:
            log.info("Sin señales — revisa: REQUIRE_TL_BREAK=%s HTF_MIN_ALIGNED=%s MIN_SCORE=%.0f",
                     C.REQUIRE_TL_BREAK, C.HTF_MIN_ALIGNED, C.MIN_SCORE)

        await asyncio.sleep(max(0.0, _eff_interval - elapsed))
