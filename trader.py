import asyncio
import logging
import math
import time
from typing import Dict, Optional
import aiohttp

import config
import telegram_notifier as tg
from bingx_client import BingXClient
from strategy import ChannelFadeSignal, parse_klines

log = logging.getLogger("trader")


class Position:
    def __init__(self, symbol: str, side: str, entry: float,
                 sl: float, tp: float, qty: float,
                 green: float, red: float, rr: float = 0.0):
        self.symbol    = symbol
        self.side      = side
        self.entry     = entry
        self.sl        = sl
        self.tp        = tp
        self.qty       = qty
        self.green     = green
        self.red       = red
        self.rr        = rr
        self.open_time = time.time()
        self.closed    = False


class Trader:
    def __init__(self, client: BingXClient, session: aiohttp.ClientSession):
        self.client       = client
        self.session      = session
        self.strategy     = ChannelFadeSignal()
        self.positions: Dict[str, Position] = {}
        self.daily_pnl    = 0.0
        self.daily_trades = 0
        self.daily_wins   = 0
        self.paused       = False
        self._live_pos_cache: set = set()

    # ──────────────────────────────────────────────────────────────────
    # Una llamada por ciclo desde main
    # ──────────────────────────────────────────────────────────────────
    async def refresh_live_positions(self):
        try:
            live = await self.client.get_positions()
            self._live_pos_cache = {
                p.get("symbol", "")
                for p in live
                if self._pos_amt(p) != 0
            }
            log.debug(f"Live positions: {self._live_pos_cache}")
        except Exception as e:
            log.error(f"refresh_live_positions: {e}")

    @staticmethod
    def _pos_amt(p: dict) -> float:
        for key in ("positionAmt", "posAmt", "availableAmt"):
            try:
                v = float(p.get(key, 0))
                if v != 0:
                    return v
            except (TypeError, ValueError):
                continue
        return 0.0

    # ──────────────────────────────────────────────────────────────────
    # LOOP POR PAR
    # ──────────────────────────────────────────────────────────────────
    async def process_pair(self, symbol: str, balance: float):
        if self.paused:
            return

        # Límite de pérdida diaria
        if balance > 0:
            loss_pct = (self.daily_pnl / balance) * 100
            if loss_pct <= -config.MAX_DAILY_LOSS:
                if not self.paused:
                    self.paused = True
                    await tg.daily_loss_limit(
                        self.session, self.daily_pnl, config.MAX_DAILY_LOSS, balance
                    )
                return

        # Monitor si ya estamos en posición
        pos = self.positions.get(symbol)
        if pos and not pos.closed:
            await self._monitor_position(symbol)
            return

        # Limpiar posiciones cerradas del dict para no bloquear re-entradas
        if pos and pos.closed:
            del self.positions[symbol]

        # Límite de posiciones simultáneas
        active = sum(1 for p in self.positions.values() if not p.closed)
        if active >= config.MAX_POSITIONS:
            return

        # Fetch klines
        try:
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, config.KLINE_LIMIT)
        except Exception as e:
            log.error(f"[{symbol}] get_klines error: {e}")
            return

        if not raw or len(raw) < 50:
            log.debug(f"[{symbol}] Pocas velas: {len(raw) if raw else 0}")
            return

        opens, highs, lows, closes, volumes = parse_klines(raw)
        if len(closes) < 50:
            return

        # Señal V32
        sig = self.strategy.compute(opens, highs, lows, closes, volumes)
        if sig is None:
            return

        # ── GUARD: no abrir si ya hay posición viva en exchange ──────
        if symbol in self._live_pos_cache:
            log.info(f"[{symbol}] Señal OK pero ya hay posición viva en exchange, ignorando")
            return

        log.info(
            f"[{symbol}] ✨ SEÑAL {sig['side']} | ADX={sig['adx']:.1f} "
            f"Vol={sig['vol_ratio']:.2f}x RR=1:{sig['rr']:.2f}"
        )

        await tg.signal_channel_fade(
            self.session, symbol, sig["side"],
            sig["green"], sig["red"], sig["entry"],
            sig["trigger"], sig["canal_width"], sig["vol_ratio"],
            sig["adx"], sig["rr"]
        )
        await self._enter_trade(symbol, sig, balance)

    # ──────────────────────────────────────────────────────────────────
    # ENTRADA
    # ──────────────────────────────────────────────────────────────────
    async def _enter_trade(self, symbol: str, sig: dict, balance: float):
        try:
            entry = sig["entry"]
            sl    = sig["sl"]
            tp    = sig["tp"]
            side  = sig["side"]
            atr   = sig["atr"]
            rr    = sig.get("rr", 0.0)

            sl_dist = abs(entry - sl)
            if sl_dist == 0 or entry == 0:
                log.warning(f"[{symbol}] sl_dist=0 o entry=0, abortando")
                return

            # Sizing por riesgo fijo
            risk_usdt = balance * (config.RISK_PCT / 100)
            qty_raw   = (risk_usdt * config.LEVERAGE) / entry
            qty       = math.floor(qty_raw * 1000) / 1000  # floor a 3 decimales

            if qty <= 0:
                log.warning(f"[{symbol}] qty={qty:.6f} ≤ 0 (balance={balance:.2f}, risk={risk_usdt:.4f}), ignorando")
                return

            log.info(f"[{symbol}] Intentando {side} qty={qty:.4f} entry≈{entry:.4f} SL={sl:.4f} TP={tp:.4f}")

            # Leverage + one-way mode
            try:
                await self.client.set_leverage(symbol, config.LEVERAGE)
                await asyncio.sleep(0.2)
            except Exception as e:
                log.warning(f"[{symbol}] set_leverage error (continuando): {e}")

            # Orden market con SL/TP nativos
            resp = await self.client.place_market_order(symbol, side, qty, sl, tp)
            code = resp.get("code", -1)

            if code != 0:
                err = resp.get("msg", str(resp))
                log.error(f"[{symbol}] Order RECHAZADA (code={code}): {err}")
                await tg.error_alert(self.session, f"[{symbol}] Order rechazada: {err}")
                return

            self.positions[symbol] = Position(
                symbol, side, entry, sl, tp, qty, sig["green"], sig["red"], rr
            )
            self._live_pos_cache.add(symbol)
            self.daily_trades += 1

            await tg.trade_entry(
                self.session, symbol, side, entry, sl, tp, qty, balance, rr, atr,
                sig["adx"], sig["vol_ratio"]
            )
            log.info(
                f"✅ [{symbol}] ABIERTO {side} @ {entry:.6g} | SL={sl:.6g} TP={tp:.6g} "
                f"qty={qty:.4f} RR=1:{rr:.2f} ADX={sig['adx']:.1f}"
            )

        except Exception as e:
            log.exception(f"[{symbol}] _enter_trade error: {e}")
            await tg.error_alert(self.session, f"[{symbol}] Entry error: {e}")

    # ──────────────────────────────────────────────────────────────────
    # MONITOREO + TIME-STOP
    # ──────────────────────────────────────────────────────────────────
    async def _monitor_position(self, symbol: str):
        try:
            pos = self.positions.get(symbol)
            if not pos or pos.closed:
                return

            elapsed_min = (time.time() - pos.open_time) / 60.0

            # ── Time-Stop ─────────────────────────────────────────────
            if elapsed_min >= config.TIME_STOP_MINUTES:
                log.info(f"[{symbol}] ⏱ TIME-STOP ({elapsed_min:.0f} min) → cerrando")
                try:
                    await self.client.close_position_market(symbol, pos.side, pos.qty)
                except Exception as e:
                    log.error(f"[{symbol}] Error cerrando TIME-STOP: {e}")

                pos.closed = True
                self._live_pos_cache.discard(symbol)

                exit_price = await self._get_last_price(symbol, pos.entry)
                pnl, pnl_pct = self._calc_pnl(pos, exit_price)

                if pnl > 0:
                    self.daily_wins += 1
                self.daily_pnl += pnl

                await tg.trade_exit(
                    self.session, symbol, pos.side,
                    pos.entry, exit_price, pnl, pnl_pct,
                    f"⏱ TIME-STOP ({config.TIME_STOP_MINUTES} min)"
                )
                return

            # ── Cierre por SL/TP (posición ya no existe en exchange) ──
            if symbol not in self._live_pos_cache:
                log.info(f"[{symbol}] Posición cerrada por SL/TP en exchange")
                pos.closed = True

                exit_price = await self._get_last_price(symbol, pos.entry)
                pnl, pnl_pct = self._calc_pnl(pos, exit_price)

                # Determinar razón comparando distancia a SL vs TP
                dist_tp = abs(exit_price - pos.tp)
                dist_sl = abs(exit_price - pos.sl)
                reason  = "TAKE PROFIT ✅" if dist_tp < dist_sl else "STOP LOSS ❌"

                if "TAKE" in reason:
                    self.daily_wins += 1
                self.daily_pnl += pnl

                await tg.trade_exit(
                    self.session, symbol, pos.side,
                    pos.entry, exit_price, pnl, pnl_pct, reason
                )
                log.info(f"[{symbol}] Cerrada | PnL={pnl:+.4f} USDT | {reason}")

        except Exception as e:
            log.error(f"[{symbol}] _monitor_position error: {e}")

    # ──────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────
    async def _get_last_price(self, symbol: str, fallback: float) -> float:
        """Obtiene el último precio cerrado del exchange."""
        try:
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, 3)
            _, _, _, C, _ = parse_klines(raw)
            if len(C) >= 2:
                return float(C[-2])
        except Exception as e:
            log.debug(f"[{symbol}] _get_last_price error: {e}")
        return fallback

    @staticmethod
    def _calc_pnl(pos: Position, exit_price: float):
        """Calcula PnL en USDT y porcentaje."""
        if pos.side == "BUY":
            pnl_pts = exit_price - pos.entry
        else:
            pnl_pts = pos.entry - exit_price
        pnl     = pnl_pts * pos.qty * config.LEVERAGE
        pnl_pct = (pnl_pts / pos.entry) * 100 * config.LEVERAGE if pos.entry else 0.0
        return pnl, pnl_pct

    def reset_daily(self):
        self.daily_pnl    = 0.0
        self.daily_trades = 0
        self.daily_wins   = 0
        self.paused       = False
        # Limpiar posiciones cerradas al resetear el día
        self.positions = {k: v for k, v in self.positions.items() if not v.closed}
        log.info("🔄 Contadores diarios reseteados")
