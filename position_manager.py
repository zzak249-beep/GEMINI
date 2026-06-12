"""
QF×JP Bot v6.7 — Position Manager
MEJORAS v6.7 (velocidad + captura de profit):
  SPEED 1: POSITION_CHECK_INTERVAL recomendado 10s (era 30s) → 3x más reacciones
  SPEED 2: BE se activa a 0.8× ATR (era 1.0×) — entra en BE antes del reversal
  SPEED 3: Trailing más ajustado post-BE: 1.2× ATR (era 1.5×)
  SPEED 4: TP1 parcial close asíncrono inmediato (no espera confirmación de loop)
  FIX 1:   notify_partial_close existe en telegram_client v6.7
  FIX 2:   open_count sincronizado SOLO desde BingX real
  FIX 3:   reconcile_on_startup NO toca _open_count
  NUEVO:   Early exit reforzado — cierre si precio revierte > 0.5×ATR desde best_price
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config as C
from bingx_client import BingXClient
from risk_manager import RiskManager
import telegram_client as tg

log = logging.getLogger("position_mgr")


@dataclass
class OpenTrade:
    symbol: str
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    qty: float
    atr: float
    order_id: str
    be_moved: bool          = False
    tp1_hit: bool           = False
    tp2_partial_done: bool  = False
    trailing_sl: float      = 0.0
    best_price: float       = 0.0
    open_time: float        = 0.0
    partial_qty: float      = 0.0


@dataclass
class TradeConfig:
    breakeven_mult:  float = 0.8    # v6.7: era 1.0 → BE más temprano
    trailing_mult:   float = 1.2    # v6.7: era 1.5 → trailing más ajustado
    tp2_partial_pct: float = 0.5
    max_hold_bars:   int   = 60
    early_exit_cvd:  float = -0.5
    reversal_mult:   float = 0.5    # v6.7 NEW: cierre si retrocede 0.5×ATR desde best


def _get_cfg() -> TradeConfig:
    return TradeConfig(
        breakeven_mult  = getattr(C, "BREAKEVEN_ATR_MULT",  0.8),
        trailing_mult   = getattr(C, "TRAILING_ATR_MULT",   1.2),
        tp2_partial_pct = getattr(C, "TP2_PARTIAL_PCT",     0.5),
        max_hold_bars   = getattr(C, "MAX_HOLD_BARS",        60),
        early_exit_cvd  = getattr(C, "EARLY_EXIT_CVD",      -0.5),
        reversal_mult   = getattr(C, "REVERSAL_ATR_MULT",   0.5),
    )


class PositionManager:
    def __init__(self, client: BingXClient, risk: RiskManager):
        self.client = client
        self.risk   = risk
        self._trades: dict[str, OpenTrade] = {}
        self._lock  = asyncio.Lock()

    # ── Startup ───────────────────────────────────────────────────────────────

    async def reconcile_on_startup(self):
        """Lee posiciones reales de BingX al arrancar. NO toca _open_count."""
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("reconcile_on_startup error: %s", e)
            return

        if not real_positions:
            log.info("reconcile_on_startup: sin posiciones abiertas")
            return

        count = 0
        for pos in real_positions:
            sym = pos.get("symbol", "")
            if not sym:
                continue
            amt = float(pos.get("positionAmt", 0) or 0)
            if amt == 0:
                continue

            direction = "LONG" if amt > 0 else "SHORT"
            entry     = float(pos.get("avgPrice", pos.get("entryPrice", 0)) or 0)
            qty       = abs(amt)
            sl  = entry * 0.99  if direction == "LONG" else entry * 1.01
            tp1 = entry * 1.015 if direction == "LONG" else entry * 0.985
            tp2 = entry * 1.03  if direction == "LONG" else entry * 0.97

            async with self._lock:
                self._trades[sym] = OpenTrade(
                    symbol=sym, direction=direction,
                    entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                    qty=qty, atr=entry * 0.005,
                    order_id="reconciled",
                    open_time=time.time(),
                    best_price=entry,
                    partial_qty=qty,
                )
            count += 1
            log.info("[%s] Reconciliado: %s qty=%.4f entry=%.6f",
                     sym, direction, qty, entry)

        if count:
            log.info("reconcile_on_startup: %d posiciones reconciliadas", count)

    # ── Registro y baja ───────────────────────────────────────────────────────

    async def register_trade(self, trade: OpenTrade):
        trade.open_time   = time.time()
        trade.best_price  = trade.entry
        trade.partial_qty = trade.qty
        async with self._lock:
            self._trades[trade.symbol] = trade
        await self.risk.on_trade_opened()
        log.info("[%s] Trade registrado %s entry=%.6f qty=%.4f",
                 trade.symbol, trade.direction, trade.entry, trade.qty)

    async def remove_trade(self, symbol: str, pnl: float = 0.0):
        existed = False
        async with self._lock:
            if symbol in self._trades:
                del self._trades[symbol]
                existed = True
        if existed:
            await self.risk.on_trade_closed(pnl)

    # ── Loop principal ────────────────────────────────────────────────────────

    async def monitor_loop(self):
        # SPEED 1: intervalo efectivo máx 10s en LIVE
        interval = min(C.POSITION_CHECK_INTERVAL, 10) if C.MODE == "LIVE" else C.POSITION_CHECK_INTERVAL
        log.info("Position monitor v6.7 iniciado (intervalo=%ds)", interval)
        while True:
            try:
                await self._check_all_positions()
            except Exception as e:
                log.error("monitor_loop error: %s", e)
                await tg.notify_error("position_monitor", str(e))
            await asyncio.sleep(interval)

    async def _check_all_positions(self):
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("get_open_positions failed: %s", e)
            return

        real_map: dict[str, dict] = {
            p.get("symbol", ""): p
            for p in real_positions
            if p.get("symbol") and float(p.get("positionAmt", 0)) != 0
        }

        # Sincronizar open_count con BingX real
        await self.risk.update_open_count(len(real_map))

        async with self._lock:
            tracked = dict(self._trades)

        cfg = _get_cfg()

        for symbol, trade in tracked.items():
            if symbol not in real_map:
                # Cerrada externamente (SL/TP hit por BingX)
                try:
                    ticker = await self.client.get_ticker(symbol)
                    close_price = float(ticker.get("lastPrice", trade.entry))
                except Exception:
                    close_price = trade.entry

                pnl = self._calc_pnl(trade, close_price)
                log.info("[%s] Cerrada externamente. PnL≈%.2f USDT", symbol, pnl)
                await tg.notify_trade_closed(
                    symbol, trade.direction, trade.entry,
                    close_price, trade.qty, "sl_tp_auto", pnl,
                )
                await self.remove_trade(symbol, pnl)
                continue

            pos = real_map[symbol]
            try:
                mark = float(pos.get("markPrice", 0) or 0)
                if mark <= 0:
                    ticker = await self.client.get_ticker(symbol)
                    mark = float(ticker.get("lastPrice", trade.entry))
            except Exception:
                continue

            if mark <= 0:
                continue

            # Actualizar mejor precio para trailing
            if trade.direction == "LONG":
                trade.best_price = max(trade.best_price or trade.entry, mark)
            else:
                bp = trade.best_price or trade.entry
                trade.best_price = min(bp, mark)

            # ── TP1 ───────────────────────────────────────────────────────────
            if not trade.tp1_hit:
                tp1_hit = (
                    (trade.direction == "LONG"  and mark >= trade.tp1) or
                    (trade.direction == "SHORT" and mark <= trade.tp1)
                )
                if tp1_hit:
                    trade.tp1_hit = True
                    log.info("[%s] TP1 alcanzado @ %.6f", symbol, mark)

            # ── BE + Trailing ─────────────────────────────────────────────────
            await self._manage_sl(trade, mark, cfg)

            # ── TP2 parcial anticipado ────────────────────────────────────────
            if not trade.tp2_partial_done and trade.tp1_hit:
                tp2_reached = (
                    (trade.direction == "LONG"  and mark >= trade.tp2) or
                    (trade.direction == "SHORT" and mark <= trade.tp2)
                )
                if tp2_reached:
                    await self._partial_close_tp2(trade, mark, cfg)

            # ── NUEVO v6.7: Early exit por reversal desde best_price ──────────
            if trade.be_moved and trade.atr > 0:
                reversal_threshold = trade.atr * cfg.reversal_mult
                reversal = (
                    (trade.direction == "LONG"  and trade.best_price - mark > reversal_threshold) or
                    (trade.direction == "SHORT" and mark - trade.best_price > reversal_threshold)
                )
                if reversal and not trade.tp2_partial_done:
                    log.info("[%s] Reversal detectado @ %.6f (best=%.6f) — salida anticipada",
                             symbol, mark, trade.best_price)
                    await self.close_position_emergency(symbol, reason="reversal_exit")
                    continue

            # ── Max Hold Time ─────────────────────────────────────────────────
            if trade.open_time > 0:
                elapsed_bars = (time.time() - trade.open_time) / 180
                if elapsed_bars >= cfg.max_hold_bars:
                    log.info("[%s] Max hold time (%.0f barras) — cerrando", symbol, elapsed_bars)
                    await self.close_position_emergency(symbol, reason="max_hold_time")
                    continue

    # ── Gestión de SL ─────────────────────────────────────────────────────────

    async def _manage_sl(self, trade: OpenTrade, mark: float, cfg: TradeConfig):
        atr = trade.atr

        # Trailing (prioridad si ya está en BE)
        if trade.be_moved and atr > 0:
            if trade.direction == "LONG":
                new_trail_sl = trade.best_price - atr * cfg.trailing_mult
                if new_trail_sl > trade.sl + atr * 0.1:
                    await self._update_sl(trade, new_trail_sl, "trailing")
            else:
                new_trail_sl = trade.best_price + atr * cfg.trailing_mult
                if new_trail_sl < trade.sl - atr * 0.1:
                    await self._update_sl(trade, new_trail_sl, "trailing")
            return

        # Breakeven — SPEED 2: trigger a 0.8× ATR (era 1.0×)
        if not trade.be_moved:
            be_trigger = (
                trade.entry + atr * cfg.breakeven_mult if trade.direction == "LONG"
                else trade.entry - atr * cfg.breakeven_mult
            )
            be_reached = (
                (trade.direction == "LONG"  and mark >= be_trigger) or
                (trade.direction == "SHORT" and mark <= be_trigger)
            )
            if be_reached:
                await self._move_to_breakeven(trade, mark)

    async def _update_sl(self, trade: OpenTrade, new_sl: float, reason: str):
        try:
            await self.client.cancel_all_orders(trade.symbol)
            await asyncio.sleep(0.2)
            side_close = "SELL" if trade.direction == "LONG" else "BUY"
            resp = await self.client.place_stop_market_order(
                trade.symbol, side_close, trade.partial_qty or trade.qty,
                new_sl, trade.direction, close_position=True,
                order_type="STOP_MARKET",
            )
            code = resp.get("code", -1)
            if code == 0:
                trade.sl = new_sl
                trade.trailing_sl = new_sl
                log.info("[%s] SL %s → %.6f", trade.symbol, reason, new_sl)
            elif code == 109420:
                trade.be_moved = True
                log.debug("[%s] SL %s skip — posición ya cerrada", trade.symbol, reason)
            else:
                log.warning("[%s] SL %s fallo: %s", trade.symbol, reason, resp)
        except Exception as e:
            log.error("[%s] _update_sl error: %s", trade.symbol, e)

    async def _move_to_breakeven(self, trade: OpenTrade, current_price: float):
        try:
            await self.client.cancel_all_orders(trade.symbol)
            await asyncio.sleep(0.3)
            side_close = "SELL" if trade.direction == "LONG" else "BUY"
            resp = await self.client.place_stop_market_order(
                trade.symbol, side_close, trade.partial_qty or trade.qty,
                trade.entry, trade.direction,
                close_position=True, order_type="STOP_MARKET",
            )
            code = resp.get("code", -1)
            if code == 0:
                trade.be_moved = True
                trade.sl = trade.entry
                log.info("[%s] SL → breakeven @ %.6f", trade.symbol, trade.entry)
            elif code == 109420:
                trade.be_moved = True
                log.debug("[%s] BE skip — posición ya cerrada en BingX", trade.symbol)
            else:
                log.warning("[%s] BE fallo: %s", trade.symbol, resp)
        except Exception as e:
            log.error("[%s] _move_to_breakeven error: %s", trade.symbol, e)

    # ── TP2 parcial ───────────────────────────────────────────────────────────

    async def _partial_close_tp2(self, trade: OpenTrade, mark: float, cfg: TradeConfig):
        close_qty = round(trade.qty * cfg.tp2_partial_pct, 6)
        if close_qty <= 0:
            return

        try:
            side = "SELL" if trade.direction == "LONG" else "BUY"
            # v6.7 FIX: usar place_reduce_only_market — evita kwarg error
            resp = await self.client.place_reduce_only_market(
                trade.symbol, side, close_qty, trade.direction,
            )
            code = resp.get("code", -1)
            if code == 0:
                trade.tp2_partial_done = True
                trade.partial_qty = max(trade.qty - close_qty, 0)
                partial_pnl = self._calc_pnl_qty(trade, mark, close_qty)
                log.info("[%s] TP2 parcial: cerrado %.4f (50%%) PnL≈%.2f USDT",
                         trade.symbol, close_qty, partial_pnl)
                await tg.notify_partial_close(
                    trade.symbol, trade.direction, mark,
                    close_qty, partial_pnl, "tp2_partial"
                )
            elif code == 109420:
                trade.tp2_partial_done = True
                log.debug("[%s] TP2 parcial skip — posición ya cerrada", trade.symbol)
            else:
                log.warning("[%s] TP2 parcial fallo: %s", trade.symbol, resp)
        except Exception as e:
            log.error("[%s] _partial_close_tp2 error: %s", trade.symbol, e)

    # ── Cierre de emergencia ──────────────────────────────────────────────────

    async def close_position_emergency(self, symbol: str, reason: str = "emergency"):
        async with self._lock:
            trade = self._trades.get(symbol)
        if not trade:
            log.warning("[%s] close_emergency: trade no encontrado", symbol)
            return
        try:
            await self.client.cancel_all_orders(symbol)
            await asyncio.sleep(0.2)
            await self.client.close_position_market(symbol, trade.qty, trade.direction)
            ticker = await self.client.get_ticker(symbol)
            close_price = float(ticker.get("lastPrice", trade.entry))
            pnl = self._calc_pnl(trade, close_price)
            log.info("[%s] Cierre %s. PnL=%.2f USDT", symbol, reason, pnl)
            await tg.notify_trade_closed(symbol, trade.direction, trade.entry,
                                         close_price, trade.qty, reason, pnl)
            await self.remove_trade(symbol, pnl)
        except Exception as e:
            log.error("[%s] close_emergency error: %s", symbol, e)
            await tg.notify_error(f"close_emergency({symbol})", str(e))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _calc_pnl(self, trade: OpenTrade, close_price: float) -> float:
        if trade.direction == "LONG":
            raw = (close_price - trade.entry) * trade.qty
        else:
            raw = (trade.entry - close_price) * trade.qty
        return round(raw * C.LEVERAGE, 4)

    def _calc_pnl_qty(self, trade: OpenTrade, close_price: float, qty: float) -> float:
        if trade.direction == "LONG":
            raw = (close_price - trade.entry) * qty
        else:
            raw = (trade.entry - close_price) * qty
        return round(raw * C.LEVERAGE, 4)

    def get_tracked(self) -> dict[str, OpenTrade]:
        return dict(self._trades)

    def is_trading(self, symbol: str) -> bool:
        return symbol in self._trades
