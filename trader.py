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
        self.open_time = time.time()   # Para time-stop
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
        except Exception as e:
            log.error(f"refresh_live_positions: {e}")

    @staticmethod
    def _pos_amt(p: dict) -> float:
        for key in ("positionAmt", "posAmt", "availableAmt"):
            try:
                return float(p.get(key, 0))
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
            if (self.daily_pnl / balance) * 100 <= -config.MAX_DAILY_LOSS:
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

        # Límite de posiciones simultáneas
        active = sum(1 for p in self.positions.values() if not p.closed)
        if active >= config.MAX_POSITIONS:
            return

        # Fetch klines
        raw = await self.client.get_klines(symbol, config.TIMEFRAME, config.KLINE_LIMIT)
        if not raw or len(raw) < 50:
            return

        opens, highs, lows, closes, volumes = parse_klines(raw)
        if len(closes) < 50:
            return

        # Señal V32
        sig = self.strategy.compute(opens, highs, lows, closes, volumes)
        if sig is None:
            return

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
                return

            # Sizing por riesgo fijo (floor a 3 decimales)
            risk_usdt = balance * (config.RISK_PCT / 100)
            qty = math.floor(
                (risk_usdt * config.LEVERAGE / entry) * 1000
            ) / 1000
            if qty <= 0:
                log.warning(f"[{symbol}] qty<=0, ignorando")
                return

            # Leverage + one-way mode
            await self.client.set_leverage(symbol, config.LEVERAGE)
            await asyncio.sleep(0.15)

            # Orden market con SL/TP nativos
            resp = await self.client.place_market_order(symbol, side, qty, sl, tp)
            code = resp.get("code", -1)
            if code != 0:
                err = resp.get("msg", str(resp))
                log.error(f"[{symbol}] Order rejected ({code}): {err}")
                await tg.error_alert(self.session, f"[{symbol}] {err}")
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
                f"✅ [{symbol}] {side} @ {entry:.4f} | SL={sl:.4f} TP={tp:.4f} "
                f"qty={qty:.4f} RR=1:{rr:.1f} ADX={sig['adx']:.1f}"
            )

        except Exception as e:
            log.exception(f"[{symbol}] _enter_trade: {e}")
            await tg.error_alert(self.session, f"[{symbol}] Entry error: {e}")

    # ──────────────────────────────────────────────────────────────────
    # MONITOREO DE POSICIÓN + TIME-STOP V32
    # ──────────────────────────────────────────────────────────────────
    async def _monitor_position(self, symbol: str):
        try:
            pos = self.positions.get(symbol)
            if not pos or pos.closed:
                return

            # ── Time-Stop V32 ─────────────────────────────────────────
            elapsed_min = (time.time() - pos.open_time) / 60.0
            if elapsed_min >= config.TIME_STOP_MINUTES:
                log.info(
                    f"[{symbol}] ⏱ TIME-STOP ({elapsed_min:.0f} min ≥ {config.TIME_STOP_MINUTES} min) → cerrando"
                )
                await self.client.close_position_market(symbol, pos.side, pos.qty)
                pos.closed = True

                raw = await self.client.get_klines(symbol, config.TIMEFRAME, 3)
                _, _, _, C, _ = parse_klines(raw)
                exit_price = float(C[-2]) if len(C) >= 2 else pos.entry

                pnl_pts = (exit_price - pos.entry) if pos.side == "BUY" else (pos.entry - exit_price)
                pnl     = pnl_pts * pos.qty * config.LEVERAGE
                pnl_pct = (pnl_pts / pos.entry) * 100 * config.LEVERAGE

                if pnl > 0:
                    self.daily_wins += 1
                self.daily_pnl += pnl

                await tg.trade_exit(
                    self.session, symbol, pos.side,
                    pos.entry, exit_price, pnl, pnl_pct, "⏱ TIME-STOP (45 min)"
                )
                return

            # ── Cierre por SL/TP detectado (posición ya no existe en exchange) ──
            if symbol not in self._live_pos_cache:
                pos.closed = True

                raw = await self.client.get_klines(symbol, config.TIMEFRAME, 3)
                _, _, _, C, _ = parse_klines(raw)
                exit_price = float(C[-2]) if len(C) >= 2 else pos.entry

                pnl_pts = (exit_price - pos.entry) if pos.side == "BUY" else (pos.entry - exit_price)
                pnl     = pnl_pts * pos.qty * config.LEVERAGE
                pnl_pct = (pnl_pts / pos.entry) * 100 * config.LEVERAGE

                reason = "TAKE PROFIT ✅" if abs(exit_price - pos.tp) < abs(exit_price - pos.sl) else "STOP LOSS ❌"
                if "TAKE" in reason:
                    self.daily_wins += 1
                self.daily_pnl += pnl

                await tg.trade_exit(
                    self.session, symbol, pos.side,
                    pos.entry, exit_price, pnl, pnl_pct, reason
                )
                log.info(f"[{symbol}] Cerrada | PnL={pnl:+.4f} | {reason}")

        except Exception as e:
            log.error(f"[{symbol}] _monitor_position: {e}")

    def reset_daily(self):
        self.daily_pnl    = 0.0
        self.daily_trades = 0
        self.daily_wins   = 0
        self.paused       = False
        log.info("🔄 Contadores diarios reseteados")
