"""
QF×JP Bot v6.5 — Position Manager
FIX 1: BE move verifica posición antes de actuar ('position not exist')
FIX 2: open_count sincronizado SOLO desde BingX real (no contadores manuales)
FIX 3: reconcile_on_startup NO toca _open_count (lo hace el monitor)
"""
import asyncio
import logging
from dataclasses import dataclass
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
    be_moved: bool = False
    tp1_hit: bool  = False


class PositionManager:
    def __init__(self, client: BingXClient, risk: RiskManager):
        self.client = client
        self.risk   = risk
        self._trades: dict[str, OpenTrade] = {}
        self._lock  = asyncio.Lock()

    # ── Startup: reconciliar posiciones ya abiertas en BingX ─────────────────

    async def reconcile_on_startup(self):
        """
        Lee posiciones reales de BingX al arrancar y las registra localmente.
        NO toca _open_count — el primer ciclo de monitor_loop lo sincroniza.
        """
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("reconcile_on_startup: error: %s", e)
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
                )
            count += 1
            log.info("[%s] Reconciliado: %s qty=%.4f entry=%.6f", sym, direction, qty, entry)

        if count:
            log.info("reconcile_on_startup: %d posiciones reconciliadas", count)
            # open_count lo actualizará el primer _check_all_positions con el número real

    # ── Registro y baja de trades ─────────────────────────────────────────────

    async def register_trade(self, trade: OpenTrade):
        async with self._lock:
            self._trades[trade.symbol] = trade
        await self.risk.on_trade_opened()
        log.info("[%s] Trade registrado %s entry=%.6f", trade.symbol, trade.direction, trade.entry)

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
        log.info("Position monitor iniciado (intervalo=%ds)", C.POSITION_CHECK_INTERVAL)
        while True:
            try:
                await self._check_all_positions()
            except Exception as e:
                log.error("monitor_loop error: %s", e)
                await tg.notify_error("position_monitor", str(e))
            await asyncio.sleep(C.POSITION_CHECK_INTERVAL)

    async def _check_all_positions(self):
        # Posiciones reales en BingX
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

        # ── FIX 2: sincronizar open_count con BingX real ──────────────────────
        await self.risk.update_open_count(len(real_map))

        async with self._lock:
            tracked = dict(self._trades)

        for symbol, trade in tracked.items():
            if symbol not in real_map:
                # Posición cerrada externamente (SL/TP hit)
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

            # Posición abierta — comprobar precio actual
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

            # TP1 detectado
            if not trade.tp1_hit:
                tp1_hit = (
                    (trade.direction == "LONG"  and mark >= trade.tp1) or
                    (trade.direction == "SHORT" and mark <= trade.tp1)
                )
                if tp1_hit:
                    trade.tp1_hit = True
                    log.info("[%s] TP1 alcanzado @ %.6f", symbol, mark)

            # ── FIX 1: Breakeven — verificar posición existe ANTES de actuar ──
            if not trade.be_moved:
                be_trigger = (
                    trade.entry + trade.atr * C.BREAKEVEN_ATR_MULT
                    if trade.direction == "LONG"
                    else trade.entry - trade.atr * C.BREAKEVEN_ATR_MULT
                )
                be_reached = (
                    (trade.direction == "LONG"  and mark >= be_trigger) or
                    (trade.direction == "SHORT" and mark <= be_trigger)
                )
                if be_reached:
                    # Confirmamos que sigue en real_map antes de actuar
                    if symbol in real_map:
                        await self._move_to_breakeven(trade, mark)
                    else:
                        log.info("[%s] BE skip — ya no está en BingX", symbol)

    async def _move_to_breakeven(self, trade: OpenTrade, current_price: float):
        """Mueve SL a entry. Sólo se llama si la posición está confirmada en BingX."""
        try:
            await self.client.cancel_all_orders(trade.symbol)
            await asyncio.sleep(0.3)

            side_close = "SELL" if trade.direction == "LONG" else "BUY"
            resp = await self.client.place_stop_market_order(
                trade.symbol, side_close, trade.qty, trade.entry,
                trade.direction, close_position=True, order_type="STOP_MARKET",
            )
            if resp.get("code", -1) == 0:
                trade.be_moved = True
                log.info("[%s] SL → breakeven @ %.6f", trade.symbol, trade.entry)
            else:
                log.warning("[%s] BE fallo: %s", trade.symbol, resp)
        except Exception as e:
            log.error("[%s] _move_to_breakeven error: %s", trade.symbol, e)

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
            log.info("[%s] Cierre emergencia. PnL=%.2f USDT", symbol, pnl)
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

    def get_tracked(self) -> dict[str, OpenTrade]:
        return dict(self._trades)

    def is_trading(self, symbol: str) -> bool:
        return symbol in self._trades
