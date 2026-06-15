"""
QF×JP Bot v6.5 — Position Manager — TRAILING STOP DINÁMICO
Cambios v2:
  - _update_trailing_stop reemplaza _move_to_breakeven
  - _valid_sl_price valida precio ANTES de cada llamada → elimina error 110412
  - trail_high: watermark correcto (max LONG / min SHORT)
  - Solo activa BingX cuando SL mejoró > TRAIL_UPDATE_THRESHOLD * ATR
  - Notificación Telegram al activar trail y al cruzar entry (profit garantizado)
  - open_count sincronizado solo desde BingX real
  - reconcile NO toca _open_count
  - remove_trade pasa symbol para cooldown
"""
import asyncio
import logging
from dataclasses import dataclass

import config as C
from bingx_client import BingXClient
from risk_manager import RiskManager
import telegram_client as tg

log = logging.getLogger("position_mgr")


@dataclass
class OpenTrade:
    symbol:         str
    direction:      str
    entry:          float
    sl:             float
    tp1:            float
    tp2:            float
    qty:            float
    atr:            float
    order_id:       str
    be_moved:       bool  = False   # True cuando trailing se activa (compatibilidad)
    tp1_hit:        bool  = False
    position_side:  str   = ""      # LONG/SHORT/BOTH leído de BingX
    # ── Trailing Stop ─────────────────────────────────────────────────────────
    trail_active:   bool  = False   # trailing activado (precio cruzó umbral)
    trail_high:     float = 0.0     # watermark: máximo (LONG) o mínimo (SHORT)
    trail_notified: bool  = False   # ya enviamos notificación de profit garantizado


class PositionManager:
    def __init__(self, client: BingXClient, risk: RiskManager):
        self.client = client
        self.risk   = risk
        self._trades: dict[str, OpenTrade] = {}
        self._lock  = asyncio.Lock()

    # ── Helper: validar precio antes de enviar a BingX ────────────────────────

    @staticmethod
    def _valid_sl_price(direction: str, sl_price: float, mark: float) -> float:
        """
        BingX error 110412: el stop price debe estar en el lado correcto del precio actual.

        LONG (SELL STOP_MARKET): stop_price DEBE ser < mark  (trigger cuando cae)
        SHORT (BUY  STOP_MARKET): stop_price DEBE ser > mark  (trigger cuando sube)

        Si el precio calculado es inválido, lo ajusta con un buffer del 0.15%.
        Nunca enviar a BingX sin pasar por esta función.
        """
        buf = 0.0015  # 0.15%
        if direction == "LONG":
            limit = mark * (1.0 - buf)
            if sl_price >= mark:
                log.debug("[valid_sl] LONG sl=%.6f≥mark=%.6f → %.6f", sl_price, mark, limit)
                return limit
        else:  # SHORT
            limit = mark * (1.0 + buf)
            if sl_price <= mark:
                log.debug("[valid_sl] SHORT sl=%.6f≤mark=%.6f → %.6f", sl_price, mark, limit)
                return limit
        return sl_price

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
                    atr=entry * 0.005, order_id="reconciled",
                    position_side=pos_side,
                )
            count += 1
            log.info("[%s] Reconciliado: %s qty=%.4f @ %.6f", sym, direction, qty, entry)

        if count:
            log.info("reconcile: %d posición(es) — colocando SL emergencia...", count)
            await self._place_emergency_sl_all()

    async def _place_emergency_sl_all(self):
        """
        Coloca SL inmediato en todas las posiciones reconciliadas.
        SL a 2% del mark price actual → siempre válido para BingX.
        Pasa por _valid_sl_price como doble seguridad.
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
                # 2% desde mark → siempre del lado correcto
                raw_sl   = mark * 0.98 if trade.direction == "LONG" else mark * 1.02
                sl_price = self._valid_sl_price(trade.direction, raw_sl, mark)

                log.info("[%s] SL emergencia: mark=%.6f sl=%.6f %s",
                         sym, mark, sl_price, trade.direction)

                resp = await self.client.place_stop_market_order(
                    sym, side_close, trade.qty, sl_price,
                    trade.direction, order_type="STOP_MARKET",
                )
                if resp.get("code", -1) == 0:
                    trade.sl = sl_price
                    log.info("[%s] SL emergencia OK @ %.6f", sym, sl_price)
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
            await self.risk.on_trade_closed(pnl=pnl, symbol=symbol)

    # ── Monitor loop ──────────────────────────────────────────────────────────

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
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("get_open_positions failed: %s", e)
            return

        # Mapa real de BingX
        real_map: dict[str, dict] = {
            p["symbol"]: p for p in real_positions
            if p.get("symbol") and float(p.get("positionAmt", 0)) != 0
        }

        # Sincronizar open_count con BingX real
        await self.risk.update_open_count(len(real_map))

        async with self._lock:
            tracked = dict(self._trades)

        for symbol, trade in tracked.items():

            # Posición cerrada externamente (SL/TP tocado en BingX)
            if symbol not in real_map:
                try:
                    ticker      = await self.client.get_ticker(symbol)
                    close_price = float(ticker.get("lastPrice", trade.entry))
                except Exception:
                    close_price = trade.entry
                pnl = self._calc_pnl(trade, close_price)
                log.info("[%s] Cerrada externamente. PnL≈%.2f", symbol, pnl)
                await tg.notify_trade_closed(
                    symbol, trade.direction, trade.entry,
                    close_price, trade.qty, "sl_tp_auto", pnl,
                )
                await self.remove_trade(symbol, pnl)
                continue

            # Posición abierta — obtener mark price
            pos = real_map[symbol]
            try:
                mark = float(pos.get("markPrice", 0) or 0)
                if mark <= 0:
                    ticker = await self.client.get_ticker(symbol)
                    mark   = float(ticker.get("lastPrice", trade.entry))
            except Exception:
                continue
            if mark <= 0:
                continue

            # TP1 tracking
            if not trade.tp1_hit:
                tp1_hit = (
                    (trade.direction == "LONG"  and mark >= trade.tp1) or
                    (trade.direction == "SHORT" and mark <= trade.tp1)
                )
                if tp1_hit:
                    trade.tp1_hit = True
                    log.info("[%s] TP1 alcanzado @ %.6f", symbol, mark)

            # ── Trailing Stop Dinámico ────────────────────────────────────────
            # Reemplaza el BE fijo. Gestiona activación, watermark y envío a BingX.
            await self._update_trailing_stop(trade, mark, real_map)

    # ── Trailing Stop ─────────────────────────────────────────────────────────

    async def _update_trailing_stop(self, trade: OpenTrade, mark: float, real_map: dict):
        """
        Trailing Stop Dinámico — lógica completa:

        1. Watermark: rastrea el máximo (LONG) o mínimo (SHORT) de mark visto.
        2. Activación: solo cuando precio va TRAIL_ACTIVATION_MULT * ATR a favor.
        3. new_sl = watermark ± TRAIL_ATR_MULT * ATR
        4. SL solo se mueve en dirección favorable (nunca retrocede).
        5. _valid_sl_price garantiza que BingX no rechace la orden (error 110412).
        6. Solo llama a BingX si la mejora es ≥ TRAIL_UPDATE_THRESHOLD * ATR.
        7. Notifica Telegram: activación + profit garantizado (trail_sl cruza entry).
        """
        if trade.symbol not in real_map:
            return

        atr = trade.atr if trade.atr > 0 else mark * 0.005

        # Inicializar watermark la primera vez (al precio de entrada)
        if trade.trail_high == 0.0:
            trade.trail_high = trade.entry

        # ── Actualizar watermark (solo en dirección favorable) ────────────────
        if trade.direction == "LONG":
            if mark > trade.trail_high:
                trade.trail_high = mark
            new_sl = trade.trail_high - atr * C.TRAIL_ATR_MULT
        else:  # SHORT
            if mark < trade.trail_high:
                trade.trail_high = mark
            new_sl = trade.trail_high + atr * C.TRAIL_ATR_MULT

        # ── Activación ────────────────────────────────────────────────────────
        if not trade.trail_active:
            activation_px = (
                trade.entry + atr * C.TRAIL_ACTIVATION_MULT
                if trade.direction == "LONG"
                else trade.entry - atr * C.TRAIL_ACTIVATION_MULT
            )
            activated = (
                (trade.direction == "LONG"  and mark >= activation_px) or
                (trade.direction == "SHORT" and mark <= activation_px)
            )
            if not activated:
                # Precio todavía no llegó al umbral — no hacer nada
                return

            trade.trail_active = True
            trade.be_moved     = True  # compatibilidad con campo existente
            log.info("[%s] 🎯 Trail ACTIVADO mark=%.6f activation=%.6f trail_sl=%.6f",
                     trade.symbol, mark, activation_px, new_sl)
            await tg.notify_trail_activated(trade.symbol, trade.direction, mark, new_sl)

        # ── El SL solo puede mejorar ──────────────────────────────────────────
        # LONG: SL debe subir (nunca bajar). SHORT: SL debe bajar (nunca subir).
        if trade.direction == "LONG"  and new_sl <= trade.sl:
            return
        if trade.direction == "SHORT" and new_sl >= trade.sl:
            return

        # ── Umbral mínimo para evitar spam de API ─────────────────────────────
        if abs(new_sl - trade.sl) < atr * C.TRAIL_UPDATE_THRESHOLD:
            return

        # ── Validar precio (previene error 110412 definitivamente) ────────────
        validated_sl = self._valid_sl_price(trade.direction, new_sl, mark)

        # Re-verificar mejora tras ajuste de validación
        if trade.direction == "LONG"  and validated_sl <= trade.sl:
            return
        if trade.direction == "SHORT" and validated_sl >= trade.sl:
            return

        # ── Notificar profit garantizado (primera vez que SL cruza entry) ─────
        guaranteed = (
            (trade.direction == "LONG"  and validated_sl >= trade.entry) or
            (trade.direction == "SHORT" and validated_sl <= trade.entry)
        )
        if guaranteed and not trade.trail_notified:
            trade.trail_notified = True
            await tg.notify_trail_be_locked(
                trade.symbol, trade.direction, validated_sl, trade.entry
            )

        # ── Enviar a BingX ────────────────────────────────────────────────────
        old_sl     = trade.sl
        side_close = "SELL" if trade.direction == "LONG" else "BUY"

        try:
            # Cancelar SL anterior (ignora error si no hay órdenes abiertas)
            try:
                await self.client.cancel_all_orders(trade.symbol)
            except Exception as ce:
                log.debug("[%s] cancel_all_orders ignorado: %s", trade.symbol, ce)
            await asyncio.sleep(0.3)

            resp = await self.client.place_stop_market_order(
                trade.symbol, side_close, trade.qty, validated_sl,
                trade.direction, order_type="STOP_MARKET",
            )

            if resp.get("code", -1) == 0:
                trade.sl = validated_sl
                log.info(
                    "[%s] ✅ Trail SL %.6f → %.6f  (mark=%.6f high=%.6f)",
                    trade.symbol, old_sl, validated_sl, mark, trade.trail_high,
                )
            else:
                log.warning("[%s] Trail SL rechazado por BingX: %s", trade.symbol, resp)

        except Exception as e:
            log.error("[%s] _update_trailing_stop error: %s", trade.symbol, e)

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
