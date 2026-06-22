"""
QF×JP Bot v7.7 — Position Manager (FIX _calc_pnl × LEVERAGE)
═══════════════════════════════════════════════════════════════════════════════
NUEVO en v7.7:
  ✅ FIX CRÍTICO _calc_pnl(): quitado * C.LEVERAGE.
     En BingX USDT-margined perps, positionAmt está en unidades de activo
     base y el PnL es (exit - entry) * qty USDT — punto. El leverage
     afecta el margen bloqueado (notional / leverage), no el PnL nominal.
     Antes: todo el stack de riesgo recibía PnL × 10 (o × leverage):
       • risk.on_trade_closed(pnl=inflado) → circuit breaker de pérdida
         diaria disparaba 10x antes de lo previsto (con LEVERAGE=10 y
         DAILY_LOSS_PCT=5%, el límite real efectivo era 0.5% — 10x menos
         que la intención).
       • get_unrealized_pnl() también usa _calc_pnl → can_trade() bloqueaba
         operaciones con drawdown 10x ficticio.
       • Telegram notificaba PnL falso 10x mayor.
       • journal.on_close(pnl=inflado) calculaba win-rate y offset
         adaptativo con datos incorrectos.

Sin más cambios funcionales vs v7.6.
═══════════════════════════════════════════════════════════════════════════════
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import config as C
from bingx_client import BingXClient
from risk_manager import RiskManager
import telegram_client as tg

log = logging.getLogger("position_mgr")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_order_id(resp: dict) -> str:
    data = resp.get("data", {})
    if isinstance(data, dict):
        oid = (data.get("order") or {}).get("orderId") or data.get("orderId", "")
        return str(oid) if oid else ""
    return ""


def _is_position_closed_error(resp: dict) -> bool:
    code = resp.get("code", 0) if isinstance(resp, dict) else 0
    return code in (109420, 110025)


def _sl_valid(sl_price: float, mark: float, direction: str) -> bool:
    """
    Valida SL antes de enviarlo a BingX (evita error 110412).
    Margen 0.5% — cubre spread + latencia en pares de precio bajo.
    """
    if sl_price <= 0:
        return False
    if direction == "LONG":
        return sl_price < mark * 0.995
    else:
        return sl_price > mark * 1.005


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class OpenTrade:
    symbol:        str
    direction:     str
    entry:         float
    sl:            float
    tp1:           float
    tp2:           float
    qty:           float
    atr:           float
    order_id:      str
    be_moved:      bool  = False
    tp1_hit:       bool  = False
    position_side: str   = ""

    trailing_active:     bool  = False
    trail_sl:            float = 0.0
    peak_price:          float = 0.0
    trail_order_id:      str   = ""

    last_failed_sl:      float = 0.0
    activation_attempts: int   = 0
    opened_at:           float = 0.0


# ── Manager ───────────────────────────────────────────────────────────────────

class PositionManager:
    def __init__(self, client: BingXClient, risk: RiskManager, journal=None):
        self.client   = client
        self.risk     = risk
        self._journal = journal
        self._trades: dict[str, OpenTrade] = {}
        self._lock   = asyncio.Lock()
        self._trail_last_notify: dict[str, float] = {}

    # ── Reconciliar al arrancar ───────────────────────────────────────────────

    async def reconcile_on_startup(self):
        try:
            positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("reconcile_on_startup error: %s", e)
            return

        if not positions:
            log.info("reconcile: sin posiciones abiertas")
            return

        count = 0
        for pos in positions:
            sym = pos.get("symbol", "")
            amt = float(pos.get("positionAmt", 0) or 0)
            if not sym or amt == 0:
                continue
            direction_from_amt = "LONG" if amt > 0 else "SHORT"
            pos_side = pos.get("positionSide", "BOTH")
            if pos_side not in ("LONG", "SHORT", "BOTH"):
                pos_side = "BOTH"

            if pos_side in ("LONG", "SHORT"):
                direction = pos_side
                if direction != direction_from_amt:
                    log.warning(
                        "[%s] Discrepancia dirección: amt=%s positionSide=%s — usando positionSide",
                        sym, direction_from_amt, pos_side,
                    )
            else:
                direction = direction_from_amt

            entry = float(pos.get("avgPrice", pos.get("entryPrice", 0)) or 0)
            qty   = abs(amt)
            sl    = entry * (0.99 if direction == "LONG" else 1.01)
            tp1   = entry * (1.02 if direction == "LONG" else 0.98)
            tp2   = entry * (1.04 if direction == "LONG" else 0.96)
            async with self._lock:
                self._trades[sym] = OpenTrade(
                    symbol=sym, direction=direction, entry=entry,
                    sl=sl, tp1=tp1, tp2=tp2, qty=qty,
                    atr=entry * 0.005,
                    order_id="reconciled",
                    position_side=pos_side,
                    trail_sl=sl,
                    peak_price=entry,
                    # FIX v7.5: asumir que ya se gastó la mitad del presupuesto
                    # de tiempo — evita ventana completa y fresca en cada redeploy.
                    opened_at=time.time() - (getattr(C, 'MAX_HOLD_MINUTES', 60) * 60 * 0.5),
                )
            count += 1
            log.info("[%s] Reconciliado: %s qty=%.4f @ %.6f", sym, direction, qty, entry)

        if count:
            log.info("reconcile: %d posición(es) — colocando SL emergencia...", count)
            await self._place_emergency_sl_all()

    async def _place_emergency_sl_all(self):
        """
        Coloca SL inmediato en todas las posiciones reconciliadas.
        FIX: cancela órdenes previas antes de colocar la nueva (evita
        acumulación de decenas de SL huérfanos por redeploys frecuentes).
        """
        async with self._lock:
            trades = dict(self._trades)
        for sym, trade in trades.items():
            try:
                ticker = await self.client.get_ticker(sym)
                mark   = float(ticker.get("lastPrice", trade.entry) or trade.entry)
                if mark <= 0:
                    mark = trade.entry

                side_close = "SELL" if trade.direction == "LONG" else "BUY"
                sl_price   = mark * 0.98 if trade.direction == "LONG" else mark * 1.02

                try:
                    await self.client.cancel_all_orders(sym)
                    await asyncio.sleep(0.3)
                except Exception as ce:
                    log.debug("[%s] cancel_all_orders: %s", sym, ce)

                log.info("[%s] SL emergencia: mark=%.6f sl=%.6f", sym, mark, sl_price)
                resp = await self.client.place_stop_market_order(
                    sym, side_close, trade.qty, sl_price,
                    trade.direction, order_type="STOP_MARKET",
                )
                if resp.get("code", -1) == 0:
                    oid = _extract_order_id(resp)
                    trade.sl             = sl_price
                    trade.trail_sl       = sl_price
                    trade.trail_order_id = oid
                    log.info("[%s] SL emergencia OK @ %.6f (oid=%s)", sym, sl_price, oid)
                else:
                    log.error("[%s] SL emergencia FALLIDO: %s", sym, resp)
            except Exception as e:
                log.error("[%s] _place_emergency_sl_all: %s", sym, e)
            await asyncio.sleep(0.4)

    # ── Registro ──────────────────────────────────────────────────────────────

    async def register_trade(self, trade: OpenTrade):
        if trade.opened_at == 0.0:
            trade.opened_at = time.time()
        async with self._lock:
            self._trades[trade.symbol] = trade
        await self.risk.on_trade_opened(symbol=trade.symbol, direction=trade.direction)
        log.info("[%s] Trade registrado %s @ %.6f", trade.symbol, trade.direction, trade.entry)

    async def remove_trade(self, symbol: str, pnl: float = 0.0):
        existed = False
        async with self._lock:
            if symbol in self._trades:
                del self._trades[symbol]
                existed = True
        if existed:
            self._trail_last_notify.pop(symbol, None)
            await self.risk.on_trade_closed(pnl=pnl, symbol=symbol)
            if self._journal is not None:
                await self._journal.on_close(symbol, pnl)

    # ── Monitor loop ──────────────────────────────────────────────────────────

    async def monitor_loop(self):
        log.info("Position monitor v7.7 — trailing + EMA exit + autocorrección dirección | intervalo=%ds",
                 C.POSITION_CHECK_INTERVAL)
        while True:
            try:
                await self._check_all_positions()
            except Exception as e:
                log.error("monitor_loop error: %s", e)
                await tg.notify_error("position_monitor", str(e))
            await asyncio.sleep(C.POSITION_CHECK_INTERVAL)

    async def _check_all_positions(self):
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("get_open_positions failed: %s", e)
            return

        real_map: dict[str, dict] = {
            p["symbol"]: p for p in real_positions
            if p.get("symbol") and float(p.get("positionAmt", 0)) != 0
        }

        # FIX v7.2: solo contar posiciones que ESTE bot trackea
        async with self._lock:
            own_symbols = set(self._trades.keys())
        own_open_count = len(own_symbols & set(real_map.keys()))
        await self.risk.update_open_count(own_open_count)

        async with self._lock:
            tracked = dict(self._trades)

        for symbol, trade in tracked.items():

            # ── Posición cerrada externamente ──────────────────────────────────
            if symbol not in real_map:
                try:
                    ticker      = await self.client.get_ticker(symbol)
                    close_price = float(ticker.get("lastPrice", trade.entry))
                except Exception:
                    close_price = trade.entry
                pnl = self._calc_pnl(trade, close_price)
                log.info("[%s] Cerrada externamente. PnL≈%.4f USDT", symbol, pnl)
                await tg.notify_trade_closed(
                    symbol, trade.direction, trade.entry,
                    close_price, trade.qty, "sl_tp_auto", pnl,
                )
                await self.remove_trade(symbol, pnl)
                continue

            pos = real_map[symbol]

            # ── FIX v7.6: auto-corregir dirección contra BingX ────────────────
            real_amt = float(pos.get("positionAmt", 0) or 0)
            real_ps  = pos.get("positionSide", "")
            real_direction = (
                real_ps if real_ps in ("LONG", "SHORT")
                else ("LONG" if real_amt > 0 else "SHORT")
            )
            if real_direction != trade.direction:
                log.warning(
                    "[%s] DIRECCIÓN CORREGIDA: tracker tenía %s, BingX confirma %s",
                    symbol, trade.direction, real_direction,
                )
                trade.direction = real_direction

            # ── Mark price ────────────────────────────────────────────────────
            try:
                mark = float(pos.get("markPrice", 0) or 0)
                if mark <= 0:
                    ticker = await self.client.get_ticker(symbol)
                    mark   = float(ticker.get("lastPrice", trade.entry))
            except Exception:
                continue
            if mark <= 0:
                continue

            # ── Sync qty real ─────────────────────────────────────────────────
            real_qty = abs(float(pos.get("positionAmt", trade.qty) or trade.qty))
            if real_qty > 0:
                drift = abs(real_qty - trade.qty) / max(trade.qty, 1e-12)
                if drift > 0.05:
                    log.info("[%s] qty sync: %.6f → %.6f", symbol, trade.qty, real_qty)
                    trade.qty = real_qty

            # ── TP1 tracking ──────────────────────────────────────────────────
            if not trade.tp1_hit:
                tp1_hit = (
                    (trade.direction == "LONG"  and mark >= trade.tp1) or
                    (trade.direction == "SHORT" and mark <= trade.tp1)
                )
                if tp1_hit:
                    trade.tp1_hit = True
                    log.info("[%s] TP1 alcanzado @ %.6f", symbol, mark)

            # ── TIME STOP ─────────────────────────────────────────────────────
            if await self._check_time_stop(trade, mark, symbol):
                continue

            # ── EMA EXIT ─────────────────────────────────────────────────────
            if await self._check_ema_exit(trade, symbol):
                continue

            # ── Trailing Stop ─────────────────────────────────────────────────
            if not trade.trailing_active:
                activate_at = (
                    trade.entry + trade.atr * C.BREAKEVEN_ATR_MULT
                    if trade.direction == "LONG"
                    else trade.entry - trade.atr * C.BREAKEVEN_ATR_MULT
                )
                should_activate = (
                    (trade.direction == "LONG"  and mark >= activate_at) or
                    (trade.direction == "SHORT" and mark <= activate_at)
                )
                if should_activate:
                    await self._activate_trail(trade, mark)
            elif not trade.trail_order_id:
                # FIX v7.3: posición sin protección real — reintentar cada ciclo
                trade.activation_attempts += 1
                if trade.activation_attempts == 1 or trade.activation_attempts % 10 == 0:
                    log.warning(
                        "[%s] trailing_active=True pero SIN SL real (intento #%d) — reintentando",
                        symbol, trade.activation_attempts,
                    )
                await self._activate_trail(trade, mark)
            else:
                await self._update_trail(trade, mark)

    # ── Activación del trailing ───────────────────────────────────────────────

    async def _activate_trail(self, trade: OpenTrade, current_mark: float):
        symbol = trade.symbol
        log.info("[%s] Trail activation — mark=%.6f entry=%.6f atr=%.6f",
                 symbol, current_mark, trade.entry, trade.atr)

        # FIX DEFINITIVO: marcar activo AL INICIO evita loop infinito 110412
        trade.trailing_active = True
        trade.be_moved        = True
        trade.peak_price      = current_mark

        already_cancelled = False

        try:
            ticker = await self.client.get_ticker(symbol)
            mark   = float(ticker.get("lastPrice", current_mark) or current_mark)
            if mark <= 0:
                mark = current_mark
            trade.peak_price = mark

            sl_be      = trade.entry
            side_close = "SELL" if trade.direction == "LONG" else "BUY"

            if _sl_valid(sl_be, mark, trade.direction):
                try:
                    await self.client.cancel_all_orders(symbol)
                    already_cancelled = True
                    await asyncio.sleep(0.3)
                except Exception as ce:
                    log.debug("[%s] cancel_all_orders: %s", symbol, ce)

                resp = await self.client.place_stop_market_order(
                    symbol, side_close, trade.qty, sl_be,
                    trade.direction, order_type="STOP_MARKET",
                )

                if resp.get("code", -1) == 0:
                    oid = _extract_order_id(resp)
                    trade.trail_sl       = sl_be
                    trade.trail_order_id = oid
                    trade.sl             = sl_be
                    self._trail_last_notify[symbol] = sl_be
                    log.info("[%s] Trail ACTIVADO — SL @ breakeven %.6f | oid=%s",
                             symbol, sl_be, oid)
                    await tg.send(
                        f"🎯 *TRAIL ACTIVADO* — `{symbol}` "
                        f"{'🟢' if trade.direction == 'LONG' else '🔴'}\n"
                        f"SL → breakeven `{sl_be:.6f}` | Mark: `{mark:.6f}`"
                    )
                    return

                if _is_position_closed_error(resp):
                    pnl = self._calc_pnl(trade, mark)
                    await tg.notify_trade_closed(symbol, trade.direction, trade.entry,
                                                 mark, trade.qty, "sl_tp_auto(trail)", pnl)
                    await self.remove_trade(symbol, pnl)
                    return

                log.warning("[%s] BE @ entry falló: %s — probando SL offset", symbol, resp)

            else:
                log.warning("[%s] Precio revertió (mark=%.6f entry=%.6f) — SL original sigue activo",
                            symbol, mark, trade.entry)

            # Fallback: SL en mark offset con re-fetch
            try:
                t2 = await self.client.get_ticker(symbol)
                m2 = float(t2.get("lastPrice", mark) or mark)
                if m2 > 0:
                    mark = m2
                    trade.peak_price = mark
            except Exception:
                pass

            em_sl = mark * 0.985 if trade.direction == "LONG" else mark * 1.015

            if _sl_valid(em_sl, mark, trade.direction):
                if not already_cancelled:
                    try:
                        await self.client.cancel_all_orders(symbol)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass

                em_resp = await self.client.place_stop_market_order(
                    symbol, side_close, trade.qty, em_sl,
                    trade.direction, order_type="STOP_MARKET",
                )
                if em_resp.get("code", -1) == 0:
                    oid = _extract_order_id(em_resp)
                    trade.trail_sl       = em_sl
                    trade.trail_order_id = oid
                    trade.sl             = em_sl
                    self._trail_last_notify[symbol] = em_sl
                    log.info("[%s] Trail ACTIVADO (SL emergencia) @ %.6f", symbol, em_sl)
                    await tg.send(f"🎯 *TRAIL ACTIVADO* (emergencia) — `{symbol}`\n"
                                  f"SL @ `{em_sl:.6f}` | Mark: `{mark:.6f}`")
                elif _is_position_closed_error(em_resp):
                    pnl = self._calc_pnl(trade, mark)
                    await tg.notify_trade_closed(symbol, trade.direction, trade.entry,
                                                 mark, trade.qty, "sl_tp_auto(trail)", pnl)
                    await self.remove_trade(symbol, pnl)
                else:
                    log.error("[%s] Trail activation: SL emergencia FALLIDO: %s", symbol, em_resp)
                    if trade.activation_attempts <= 1 or trade.activation_attempts % 10 == 0:
                        await tg.notify_error(
                            f"trail_activation({symbol})",
                            f"SL emergencia fallido (intento #{trade.activation_attempts}) — "
                            f"POSICIÓN SIN PROTECCIÓN, reintentando\n{em_resp}"
                        )

        except Exception as e:
            log.error("[%s] _activate_trail error: %s", symbol, e)

    # ── Actualización del trailing ────────────────────────────────────────────

    async def _update_trail(self, trade: OpenTrade, mark: float):
        """
        Actualiza trailing SL cuando el precio alcanza nuevo peak.
        Estrategia PLACE-THEN-CANCEL: nunca queda sin protección.
        FIX v7.1: re-fetch mark, margen 0.5%, anti-spam last_failed_sl.
        """
        symbol     = trade.symbol
        trail_dist = trade.atr * C.TRAIL_DISTANCE_ATR

        if trade.direction == "LONG":
            if mark <= trade.peak_price:
                return
            new_peak = mark
            new_sl   = new_peak - trail_dist
            if new_sl <= trade.trail_sl:
                trade.peak_price = new_peak
                return
        else:
            if trade.peak_price > 0 and mark >= trade.peak_price:
                return
            new_peak = mark
            new_sl   = new_peak + trail_dist
            if trade.trail_sl > 0 and new_sl >= trade.trail_sl:
                trade.peak_price = new_peak
                return

        if not _sl_valid(new_sl, mark, trade.direction):
            trade.peak_price = new_peak
            if trade.last_failed_sl and abs(new_sl - trade.last_failed_sl) < trade.atr * 0.05:
                log.debug("[%s] Trail: new_sl=%.6f repetido e inválido", symbol, new_sl)
            else:
                log.debug("[%s] Trail: new_sl=%.6f inválido para mark=%.6f", symbol, new_sl, mark)
            trade.last_failed_sl = new_sl
            return

        # Re-fetch mark fresco antes de enviar
        fresh_mark = mark
        try:
            t = await self.client.get_ticker(symbol)
            fm = float(t.get("lastPrice", mark) or mark)
            if fm > 0:
                fresh_mark = fm
        except Exception:
            pass

        if not _sl_valid(new_sl, fresh_mark, trade.direction):
            trade.peak_price     = new_peak
            trade.last_failed_sl = new_sl
            log.debug("[%s] Trail: new_sl=%.6f inválido tras refresh mark=%.6f", symbol, new_sl, fresh_mark)
            return

        try:
            side_close = "SELL" if trade.direction == "LONG" else "BUY"

            resp = await self.client.place_stop_market_order(
                symbol, side_close, trade.qty, new_sl,
                trade.direction, order_type="STOP_MARKET",
            )

            if resp.get("code", -1) == 0:
                new_oid       = _extract_order_id(resp)
                old_oid       = trade.trail_order_id
                old_sl        = trade.trail_sl
                profit_locked = self._calc_pnl(trade, new_sl)

                trade.peak_price     = new_peak
                trade.trail_sl       = new_sl
                trade.trail_order_id = new_oid
                trade.sl             = new_sl
                trade.last_failed_sl = 0.0

                log.info("[%s] Trail: %.6f→%.6f | peak=%.6f | PnL@SL≈%.4f USDT",
                         symbol, old_sl, new_sl, new_peak, profit_locked)

                if old_oid and old_oid != new_oid:
                    await asyncio.sleep(0.1)
                    try:
                        await self.client.cancel_order(symbol, old_oid)
                    except Exception as ce:
                        log.debug("[%s] cancel old trail %s: %s", symbol, old_oid, ce)

                last_sl = self._trail_last_notify.get(symbol, trade.entry)
                if abs(new_sl - last_sl) >= trade.atr:
                    self._trail_last_notify[symbol] = new_sl
                    pnl_icon = "💚" if profit_locked > 0 else "⚡"
                    await tg.send(
                        f"{pnl_icon} *TRAIL* — `{symbol}` "
                        f"{'🟢' if trade.direction == 'LONG' else '🔴'}\n"
                        f"SL: `{old_sl:.6f}` → `{new_sl:.6f}`\n"
                        f"Peak: `{new_peak:.6f}` | PnL@SL: `{profit_locked:+.4f} USDT`"
                    )

            else:
                if _is_position_closed_error(resp):
                    pnl = self._calc_pnl(trade, fresh_mark)
                    await tg.notify_trade_closed(symbol, trade.direction, trade.entry,
                                                 fresh_mark, trade.qty, "sl_tp_auto(trail)", pnl)
                    await self.remove_trade(symbol, pnl)
                    return
                trade.peak_price     = new_peak
                trade.last_failed_sl = new_sl
                log.warning("[%s] Trail update falló new_sl=%.6f: %s", symbol, new_sl, resp)

        except Exception as e:
            trade.peak_price     = new_peak
            trade.last_failed_sl = new_sl
            log.error("[%s] _update_trail error: %s", symbol, e)

    # ── Time Stop ─────────────────────────────────────────────────────────────

    async def _check_time_stop(self, trade: OpenTrade, mark: float, symbol: str) -> bool:
        if trade.trailing_active:
            return False
        if trade.opened_at <= 0:
            trade.opened_at = time.time()
            return False
        elapsed_min = (time.time() - trade.opened_at) / 60.0
        max_hold    = getattr(C, 'MAX_HOLD_MINUTES', 60)
        if elapsed_min < max_hold:
            return False
        atr      = trade.atr if trade.atr > 0 else mark * 0.005
        progress = (mark - trade.entry) if trade.direction == "LONG" else (trade.entry - mark)
        min_prog = atr * getattr(C, 'TIME_STOP_MIN_PROGRESS_ATR', 0.5)
        if progress >= min_prog:
            return False
        log.warning("[%s] TIME STOP — %.0fmin sin progreso. Cerrando.", symbol, elapsed_min)
        await tg.notify_time_stop(symbol, trade.direction, trade.entry, mark,
                                   int(elapsed_min), progress)
        await self.close_position_emergency(symbol, reason="time_stop")
        return True

    # ── EMA Exit ─────────────────────────────────────────────────────────────

    async def _check_ema_exit(self, trade: OpenTrade, symbol: str) -> bool:
        """
        Salida por EMA corta — detecta muerte de tendencia antes que time_stop.
        Usa klines[-2] (última vela CERRADA) para evitar señales prematuras.
        Desactivado por defecto (EMA_EXIT_ENABLED=False).
        """
        if not getattr(C, 'EMA_EXIT_ENABLED', False):
            return False
        if trade.trailing_active:
            return False
        min_hold_min = getattr(C, 'EMA_EXIT_MIN_HOLD_MIN', 6)
        if trade.opened_at > 0:
            elapsed_min = (time.time() - trade.opened_at) / 60.0
            if elapsed_min < min_hold_min:
                return False
        period = getattr(C, 'EMA_EXIT_PERIOD', 9)
        try:
            klines = await self.client.get_klines(symbol, C.TIMEFRAME, period + 30)
        except Exception as e:
            log.debug("[%s] EMA exit klines error: %s", symbol, e)
            return False
        if len(klines) < period + 2:
            return False
        closes = [c[4] for c in klines]
        ema    = _ema(closes, period)
        if len(ema) < 2:
            return False
        last_closed_close = closes[-2]
        last_closed_ema   = ema[-2]
        exit_triggered = (
            (trade.direction == "LONG"  and last_closed_close < last_closed_ema) or
            (trade.direction == "SHORT" and last_closed_close > last_closed_ema)
        )
        if not exit_triggered:
            return False
        log.warning("[%s] EMA(%d) EXIT — vela cerrada %.6f vs EMA %.6f. Cerrando.",
                    symbol, period, last_closed_close, last_closed_ema)
        await self.close_position_emergency(symbol, reason=f"ema{period}_exit")
        return True

    # ── Cierre de emergencia ──────────────────────────────────────────────────

    async def close_position_emergency(self, symbol: str, reason: str = "emergency"):
        async with self._lock:
            trade = self._trades.get(symbol)
        if not trade:
            log.warning("[%s] close_emergency: no registrado", symbol)
            return
        try:
            await self.client.cancel_all_orders(symbol)
            await asyncio.sleep(0.2)
            await self.client.close_position_market(symbol, trade.qty, trade.direction)
            ticker      = await self.client.get_ticker(symbol)
            close_price = float(ticker.get("lastPrice", trade.entry))
            pnl         = self._calc_pnl(trade, close_price)
            log.info("[%s] Cierre emergencia. PnL=%.4f USDT", symbol, pnl)
            await tg.notify_trade_closed(symbol, trade.direction, trade.entry,
                                         close_price, trade.qty, reason, pnl)
            await self.remove_trade(symbol, pnl)
        except Exception as e:
            log.error("[%s] close_emergency error: %s", symbol, e)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _calc_pnl(self, trade: OpenTrade, close_price: float) -> float:
        """
        FIX v7.7 CRÍTICO: quitado * C.LEVERAGE.

        En BingX USDT-margined perps, 1 contrato = 1 unidad de activo base.
        El PnL es (exit - entry) * qty USDT — ya está en términos nominales.
        El leverage afecta cuánto margen bloquea BingX (notional/leverage),
        no el PnL. kelly_position_size ya calcula qty = notional/entry con
        el riesgo deseado embebido — multiplicar luego por LEVERAGE contaba
        el apalancamiento DOS veces:
          Ejemplo: qty=10, entry=1.0, exit=1.02 → PnL = 0.02*10 = 0.20 USDT
          Antes:   0.20 * LEVERAGE(10) = 2.0 USDT (FALSO, 10x inflado)
          Ahora:   0.20 USDT (correcto)

        Efectos del bug:
          • circuit breaker de pérdida diaria disparaba 10x antes
          • get_unrealized_pnl() bloqueaba can_trade() con drawdown ficticio
          • Telegram mostraba PnL 10x falso
          • journal acumulaba win-rate incorrecto
        """
        if trade.direction == "LONG":
            raw = (close_price - trade.entry) * trade.qty
        else:
            raw = (trade.entry - close_price) * trade.qty
        return round(raw, 4)

    def get_tracked(self) -> dict[str, OpenTrade]:
        return dict(self._trades)

    def is_trading(self, symbol: str) -> bool:
        return symbol in self._trades

    async def get_unrealized_pnl(self) -> float:
        """
        Suma el PnL no realizado de todas las posiciones trackeadas.
        Ahora correcto (sin inflación × LEVERAGE) gracias al fix de _calc_pnl.
        """
        async with self._lock:
            tracked = dict(self._trades)
        if not tracked:
            return 0.0
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("get_unrealized_pnl: get_open_positions failed: %s", e)
            return 0.0
        real_map: dict[str, dict] = {
            p["symbol"]: p for p in real_positions
            if p.get("symbol") and float(p.get("positionAmt", 0)) != 0
        }
        total = 0.0
        for symbol, trade in tracked.items():
            pos = real_map.get(symbol)
            if not pos:
                continue
            try:
                mark = float(pos.get("markPrice", 0) or 0)
            except Exception:
                continue
            if mark <= 0:
                continue
            total += self._calc_pnl(trade, mark)
        return round(total, 4)
