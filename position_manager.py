"""
QF×JP Bot v7.2 — Position Manager TRAILING STOP DINÁMICO + PARTIAL CLOSE
═══════════════════════════════════════════════════════════════════════════════
NUEVO vs v7.1:
  ✅ partial_close(symbol, pct): cierra un % de la posición (toma de
     ganancia parcial) y mueve el SL del resto a breakeven. Usado por
     el Guardian v1.2 cuando detecta divergencia CVD fuerte en una
     posición propia que va en ganancia.
  ✅ get_pnl_pct(symbol, mark): PnL% direccional de una posición propia
     trackeada (helper para el guardian).

(Todo lo de v7.1 sin cambios: fix loop 110412, _sl_valid margen 0.5%,
 re-fetch mark antes de enviar, last_failed_sl anti-spam, trailing,
 place-then-cancel, reconcile, etc.)
═══════════════════════════════════════════════════════════════════════════════
"""
import asyncio
import logging
from dataclasses import dataclass

import config as C
from bingx_client import BingXClient
from risk_manager import RiskManager
import telegram_client as tg

log = logging.getLogger("position_mgr")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_order_id(resp: dict) -> str:
    """Extrae orderId de la respuesta de BingX (maneja varios formatos)."""
    data = resp.get("data", {})
    if isinstance(data, dict):
        oid = (data.get("order") or {}).get("orderId") or data.get("orderId", "")
        return str(oid) if oid else ""
    return ""


def _sl_valid(sl_price: float, mark: float, direction: str) -> bool:
    """
    Valida que el precio de SL sea aceptable para BingX antes de enviarlo.
    - LONG SELL STOP: sl_price debe ser < mark (se dispara cuando baja)
    - SHORT BUY STOP: sl_price debe ser > mark (se dispara cuando sube)
    Evita el error 110412 "Stop Loss price should be greater/less than current price"

    Margen 0.5% (v7.1): cubre spread/tick/latencia en pares de precio bajo.
    """
    if sl_price <= 0:
        return False
    if direction == "LONG":
        return sl_price < mark * 0.995
    else:
        return sl_price > mark * 1.005


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
    be_moved:      bool  = False   # compat legacy — True cuando trailing activo
    tp1_hit:       bool  = False
    position_side: str   = ""      # LONG/SHORT/BOTH leído de BingX

    # ── Trailing stop ─────────────────────────────────────────────────────────
    trailing_active:  bool  = False  # trailing activado
    trail_sl:         float = 0.0    # precio del SL activo en BingX
    peak_price:       float = 0.0    # mejor precio visto en dirección favorable
    trail_order_id:   str   = ""     # orderId del STOP_MARKET activo en BingX

    # ── Anti-loop de retries idénticos ───────────────────────────────────────
    last_failed_sl:   float = 0.0    # último new_sl que fue inválido/rechazado

    # ── v7.2: tracking de cierre parcial por guardian ────────────────────────
    partial_closed:   bool  = False  # True si ya se ejecutó un cierre parcial


# ── Manager ───────────────────────────────────────────────────────────────────

class PositionManager:
    def __init__(self, client: BingXClient, risk: RiskManager):
        self.client  = client
        self.risk    = risk
        self._trades: dict[str, OpenTrade] = {}
        self._lock   = asyncio.Lock()
        # Throttle Telegram: notificar trail solo cada 1 ATR de mejora por símbolo
        self._trail_last_notify: dict[str, float] = {}

    # ── Reconciliar al arrancar ───────────────────────────────────────────────

    async def reconcile_on_startup(self):
        """Lee posiciones reales de BingX. NO toca _open_count."""
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
            direction = "LONG" if amt > 0 else "SHORT"
            pos_side  = pos.get("positionSide", "BOTH")
            if pos_side not in ("LONG", "SHORT", "BOTH"):
                pos_side = "BOTH"
            entry = float(pos.get("avgPrice", pos.get("entryPrice", 0)) or 0)
            qty   = abs(amt)
            sl    = entry * (0.99 if direction == "LONG" else 1.01)
            tp1   = entry * (1.02 if direction == "LONG" else 0.98)
            tp2   = entry * (1.04 if direction == "LONG" else 0.96)
            async with self._lock:
                self._trades[sym] = OpenTrade(
                    symbol=sym, direction=direction, entry=entry,
                    sl=sl, tp1=tp1, tp2=tp2, qty=qty,
                    atr=entry * 0.005,    # estimación conservadora para reconcile
                    order_id="reconciled",
                    position_side=pos_side,
                    trail_sl=sl,          # SL inicial = SL de emergencia
                    peak_price=entry,     # peak inicial = entry
                )
            count += 1
            log.info("[%s] Reconciliado: %s qty=%.4f @ %.6f", sym, direction, qty, entry)

        if count:
            log.info("reconcile: %d posición(es) — colocando SL emergencia...", count)
            await self._place_emergency_sl_all()

    async def _place_emergency_sl_all(self):
        """
        Coloca SL inmediato en todas las posiciones reconciliadas.
        SL calculado desde mark price actual con 2% offset → siempre válido.
        Guarda el orderId para el sistema de trailing.
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
        async with self._lock:
            self._trades[trade.symbol] = trade
        await self.risk.on_trade_opened(symbol=trade.symbol)
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

    # ── Monitor loop ──────────────────────────────────────────────────────────

    async def monitor_loop(self):
        log.info("Position monitor v7.2 — trailing stop + partial close | intervalo=%ds",
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

        # Mapa real de BingX (fuente de verdad)
        real_map: dict[str, dict] = {
            p["symbol"]: p for p in real_positions
            if p.get("symbol") and float(p.get("positionAmt", 0)) != 0
        }
        await self.risk.update_open_count(len(real_map))

        async with self._lock:
            tracked = dict(self._trades)

        for symbol, trade in tracked.items():

            # ── Posición cerrada externamente (SL/TP disparado por BingX) ─────
            if symbol not in real_map:
                try:
                    ticker      = await self.client.get_ticker(symbol)
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

            # ── Sync qty real (TP parciales ejecutados por BingX) ────────────
            real_qty = abs(float(pos.get("positionAmt", trade.qty) or trade.qty))
            if real_qty > 0:
                drift = abs(real_qty - trade.qty) / max(trade.qty, 1e-12)
                if drift > 0.05:   # >5% de diferencia = parcial ejecutado
                    log.info("[%s] qty sync: %.6f → %.6f (parcial TP?)",
                             symbol, trade.qty, real_qty)
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

            # ── Trailing Stop ─────────────────────────────────────────────────
            if not trade.trailing_active:
                # Umbral de activación = BREAKEVEN_ATR_MULT ATR favorable
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
            else:
                await self._update_trail(trade, mark)

    # ── Activación del trailing ───────────────────────────────────────────────

    async def _activate_trail(self, trade: OpenTrade, current_mark: float):
        """
        Activa el trailing stop por primera vez:
        1. Re-fetch precio fresco (fix race condition)
        2. Marca trailing_active=True ANTES de cualquier operación
           → ESTO es el fix definitivo del loop infinito 110412
        3. Valida el precio SL antes de cancelar nada
        4. Solo si precio válido: cancel_all → place BE SL
        5. Si falla: coloca SL de emergencia desde mark actual
        """
        symbol = trade.symbol
        log.info("[%s] Trail activation — mark=%.6f entry=%.6f atr=%.6f",
                 symbol, current_mark, trade.entry, trade.atr)

        # ── FIX DEFINITIVO: marcar activo AL INICIO, no al final ─────────────
        trade.trailing_active = True
        trade.be_moved        = True    # compat con código legacy
        trade.peak_price      = current_mark

        already_cancelled = False   # evitar doble cancel_all_orders

        try:
            # Re-fetch precio fresco para detectar reversiones rápidas
            ticker = await self.client.get_ticker(symbol)
            mark   = float(ticker.get("lastPrice", current_mark) or current_mark)
            if mark <= 0:
                mark = current_mark
            trade.peak_price = mark     # usar precio más fresco

            # Precio de breakeven
            sl_be = trade.entry
            side_close = "SELL" if trade.direction == "LONG" else "BUY"

            if _sl_valid(sl_be, mark, trade.direction):
                # ── Caso normal: precio sigue favorable ──────────────────────
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
                    log.info("[%s] 🎯 Trail ACTIVADO — SL @ breakeven %.6f | peak=%.6f | oid=%s",
                             symbol, sl_be, mark, oid)
                    await tg.send(
                        f"🎯 *TRAIL ACTIVADO* — `{symbol}` "
                        f"{'🟢' if trade.direction == 'LONG' else '🔴'}\n"
                        f"SL → breakeven `{sl_be:.6f}` | Mark: `{mark:.6f}`\n"
                        f"ATR: `{trade.atr:.6f}` | Peak: `{mark:.6f}`"
                    )
                    return

                log.warning("[%s] BE @ entry falló: %s — probando SL offset", symbol, resp)

            else:
                log.warning("[%s] Precio revertió (mark=%.6f, entry=%.6f, dir=%s) "
                            "— SL original sigue activo, usando offset de mark",
                            symbol, mark, trade.entry, trade.direction)

            # ── Fallback universal: SL en mark offset ─────────────────────────
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
                    log.info("[%s] 🎯 Trail ACTIVADO (SL emergencia) @ %.6f | mark=%.6f",
                             symbol, em_sl, mark)
                    await tg.send(
                        f"🎯 *TRAIL ACTIVADO* (emergencia) — `{symbol}`\n"
                        f"SL @ `{em_sl:.6f}` | Mark: `{mark:.6f}`"
                    )
                else:
                    log.error("[%s] Trail activation: SL emergencia FALLIDO: %s — "
                              "posición sin protección, monitorizar manual", symbol, em_resp)
                    await tg.notify_error(
                        f"trail_activation({symbol})",
                        f"SL emergencia fallido — POSICIÓN SIN PROTECCIÓN\n{em_resp}"
                    )
            else:
                log.error("[%s] Trail activation: no se puede calcular SL válido "
                          "para mark=%.6f dir=%s", symbol, mark, trade.direction)

        except Exception as e:
            log.error("[%s] _activate_trail error: %s", symbol, e)

    # ── Actualización del trailing ────────────────────────────────────────────

    async def _update_trail(self, trade: OpenTrade, mark: float):
        """
        Actualiza el trailing SL cuando el precio alcanza un nuevo peak.
        Estrategia PLACE-THEN-CANCEL (ver v7.1 para detalle completo).
        """
        symbol     = trade.symbol
        trail_dist = trade.atr * C.TRAIL_DISTANCE_ATR

        # ── Calcular nuevo peak ───────────────────────────────────────────────
        if trade.direction == "LONG":
            if mark <= trade.peak_price:
                return
            new_peak = mark
            new_sl   = new_peak - trail_dist
            if new_sl <= trade.trail_sl:
                trade.peak_price = new_peak
                return
        else:  # SHORT
            if trade.peak_price > 0 and mark >= trade.peak_price:
                return
            new_peak = mark
            new_sl   = new_peak + trail_dist
            if trade.trail_sl > 0 and new_sl >= trade.trail_sl:
                trade.peak_price = new_peak
                return

        # ── Validar precio SL para BingX (con mark actual) ───────────────────
        if not _sl_valid(new_sl, mark, trade.direction):
            trade.peak_price = new_peak
            if trade.last_failed_sl and abs(new_sl - trade.last_failed_sl) < trade.atr * 0.05:
                log.debug("[%s] Trail: new_sl=%.6f repetido e inválido (mark=%.6f) — esperando nuevo peak",
                          symbol, new_sl, mark)
            else:
                log.debug("[%s] Trail: new_sl=%.6f inválido para mark=%.6f dir=%s",
                          symbol, new_sl, mark, trade.direction)
            trade.last_failed_sl = new_sl
            return

        # ── Re-fetch mark fresco justo antes de enviar ───────────────────────
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
            log.debug("[%s] Trail: new_sl=%.6f inválido tras refresh (mark fresco=%.6f, dir=%s) — "
                      "se reintentará con próximo peak",
                      symbol, new_sl, fresh_mark, trade.direction)
            return

        # ── PLACE-THEN-CANCEL ─────────────────────────────────────────────────
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

                log.info("[%s] 📈 Trail: %.6f→%.6f | peak=%.6f | mark=%.6f | PnL@SL≈%.2f USDT",
                         symbol, old_sl, new_sl, new_peak, fresh_mark, profit_locked)

                if old_oid and old_oid != new_oid:
                    await asyncio.sleep(0.1)
                    try:
                        await self.client.cancel_order(symbol, old_oid)
                        log.debug("[%s] Old trail SL %s cancelado", symbol, old_oid)
                    except Exception as ce:
                        log.debug("[%s] cancel_order viejo %s: %s", symbol, old_oid, ce)

                last_sl = self._trail_last_notify.get(symbol, trade.entry)
                if abs(new_sl - last_sl) >= trade.atr:
                    self._trail_last_notify[symbol] = new_sl
                    pnl_icon = "💚" if profit_locked > 0 else "⚡"
                    await tg.send(
                        f"{pnl_icon} *TRAIL* — `{symbol}` "
                        f"{'🟢' if trade.direction == 'LONG' else '🔴'}\n"
                        f"SL: `{old_sl:.6f}` → `{new_sl:.6f}`\n"
                        f"Peak: `{new_peak:.6f}` | PnL@SL: `{profit_locked:+.2f} USDT`"
                    )

            else:
                trade.peak_price     = new_peak
                trade.last_failed_sl = new_sl
                log.warning("[%s] Trail update falló new_sl=%.6f: %s",
                            symbol, new_sl, resp)

        except Exception as e:
            trade.peak_price     = new_peak
            trade.last_failed_sl = new_sl
            log.error("[%s] _update_trail error: %s", symbol, e)

    # ── v7.2: Cierre parcial (toma de ganancia + BE en el resto) ─────────────

    async def partial_close(self, symbol: str, pct: float, reason: str = "partial_close") -> bool:
        """
        Cierra `pct` (0.0-1.0) de la posición trackeada `symbol` a mercado,
        y mueve el SL del remanente a breakeven (si la posición sigue
        favorable tras el cierre parcial).

        Usado por el Guardian v1.2 cuando detecta divergencia CVD fuerte
        en una posición propia que va en ganancia: asegura parte del
        beneficio y protege el resto sin cerrar todo.

        Retorna True si el cierre parcial se ejecutó correctamente.
        """
        if not (0.0 < pct < 1.0):
            log.warning("[%s] partial_close: pct fuera de rango (%.2f)", symbol, pct)
            return False

        async with self._lock:
            trade = self._trades.get(symbol)
        if not trade:
            log.warning("[%s] partial_close: no registrado", symbol)
            return False

        if trade.partial_closed:
            log.debug("[%s] partial_close: ya ejecutado previamente — skip", symbol)
            return False

        close_qty = trade.qty * pct
        if close_qty <= 0:
            return False

        try:
            # 1. Cerrar pct de la posición a mercado
            close_resp = await self.client.close_position_market(
                symbol, close_qty, trade.direction
            )
            if isinstance(close_resp, dict) and close_resp.get("code", -1) not in (0, None):
                log.error("[%s] partial_close: cierre parcial falló: %s", symbol, close_resp)
                return False

            try:
                ticker = await self.client.get_ticker(symbol)
                close_price = float(ticker.get("lastPrice", trade.entry) or trade.entry)
            except Exception:
                close_price = trade.entry

            realized_pnl = self._calc_pnl(trade, close_price) * pct

            # 2. Actualizar qty restante
            remaining_qty = trade.qty - close_qty
            trade.qty = remaining_qty
            trade.partial_closed = True

            log.info("[%s] 💰 Partial close (%.0f%%) @ %.6f — qty %.6f→%.6f | PnL parcial≈%.2f USDT (%s)",
                     symbol, pct * 100, close_price, close_qty + remaining_qty, remaining_qty,
                     realized_pnl, reason)

            # 3. Mover SL del remanente a breakeven (si sigue siendo válido)
            sl_be = trade.entry
            if _sl_valid(sl_be, close_price, trade.direction) and remaining_qty > 0:
                side_close = "SELL" if trade.direction == "LONG" else "BUY"
                try:
                    if trade.trail_order_id:
                        await self.client.cancel_order(symbol, trade.trail_order_id)
                        await asyncio.sleep(0.2)
                    else:
                        await self.client.cancel_all_orders(symbol)
                        await asyncio.sleep(0.2)
                except Exception as ce:
                    log.debug("[%s] partial_close: cancel SL viejo: %s", symbol, ce)

                resp = await self.client.place_stop_market_order(
                    symbol, side_close, remaining_qty, sl_be,
                    trade.direction, order_type="STOP_MARKET",
                )
                if resp.get("code", -1) == 0:
                    trade.trail_sl       = sl_be
                    trade.trail_order_id = _extract_order_id(resp)
                    trade.sl             = sl_be
                    trade.trailing_active = True
                    trade.be_moved       = True
                    log.info("[%s] partial_close: SL remanente → breakeven %.6f", symbol, sl_be)
                else:
                    log.warning("[%s] partial_close: SL breakeven remanente falló: %s", symbol, resp)

            await tg.send(
                f"💰 *CIERRE PARCIAL* ({pct*100:.0f}%) — `{symbol}` "
                f"{'🟢' if trade.direction == 'LONG' else '🔴'}\n"
                f"Precio: `{close_price:.6f}` | PnL parcial: `{realized_pnl:+.2f} USDT`\n"
                f"Remanente: `{remaining_qty:.6f}` con SL → breakeven\n"
                f"_Motivo: {reason}_"
            )

            # Registrar el PnL realizado del tramo cerrado en risk_manager,
            # sin afectar open_count (la posición sigue abierta con qty restante)
            # ni el cooldown del símbolo.
            await self.risk.add_realized_pnl(realized_pnl)

            return True

        except Exception as e:
            log.error("[%s] partial_close error: %s", symbol, e)
            return False

    # ── v7.2: PnL% direccional de una posición propia trackeada ──────────────

    def get_pnl_pct(self, symbol: str, mark: float) -> float | None:
        """
        PnL% direccional de la posición propia `symbol` dado el mark actual.
        Positivo = a favor, negativo = en contra. None si no está trackeada
        o faltan datos.
        """
        trade = self._trades.get(symbol)
        if not trade or trade.entry <= 0 or mark <= 0:
            return None
        raw_pct = (mark - trade.entry) / trade.entry * 100.0
        if trade.direction == "SHORT":
            raw_pct = -raw_pct
        return raw_pct

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
            log.info("[%s] Cierre emergencia. PnL=%.2f", symbol, pnl)
            await tg.notify_trade_closed(symbol, trade.direction, trade.entry,
                                         close_price, trade.qty, reason, pnl)
            await self.remove_trade(symbol, pnl)
        except Exception as e:
            log.error("[%s] close_emergency error: %s", symbol, e)

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
