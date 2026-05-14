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
                 green: float, red: float, rr: float = 0.0,
                 atr: float = 0.0):
        self.symbol           = symbol
        self.side             = side
        self.entry            = entry
        self.sl               = sl
        self.tp               = tp
        self.qty              = qty
        self.green            = green
        self.red              = red
        self.rr               = rr
        self.atr              = atr          # ATR al momento de entrada
        self.open_time        = time.time()
        self.closed           = False
        # Trailing / Breakeven
        self.best_price       = entry        # mejor precio visto (max para LONG, min para SHORT)
        self.trail_sl         = sl           # SL dinámico (software)
        self.breakeven_active = False
        self.trail_active     = False


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

    async def process_pair(self, symbol: str, balance: float):
        if self.paused:
            return
        if balance > 0 and (self.daily_pnl / balance) * 100 <= -config.MAX_DAILY_LOSS:
            self.paused = True
            await tg.daily_loss_limit(self.session, self.daily_pnl, config.MAX_DAILY_LOSS, balance)
            return

        pos = self.positions.get(symbol)
        if pos and not pos.closed:
            await self._monitor_position(symbol)
            return

        active = sum(1 for p in self.positions.values() if not p.closed)
        if active >= config.MAX_POSITIONS:
            return

        raw = await self.client.get_klines(symbol, config.TIMEFRAME, config.KLINE_LIMIT)
        if not raw or len(raw) < 50:
            return
        opens, highs, lows, closes, volumes = parse_klines(raw)
        if len(closes) < 50:
            return

        sig = self.strategy.compute(opens, highs, lows, closes, volumes, symbol=symbol)
        if sig is None:
            return

        await tg.signal_channel_fade(
            self.session, symbol, sig["side"],
            sig["green"], sig["red"], sig["entry"],
            sig["trigger"], sig["canal_width"], sig["vol_ratio"],
            sig["adx"], sig["rr"]
        )
        await self._enter_trade(symbol, sig, balance)

    async def _enter_trade(self, symbol: str, sig: dict, balance: float):
        try:
            entry = sig["entry"]
            sl    = sig["sl"]
            tp    = sig["tp"]
            side  = sig["side"]
            atr   = sig["atr"]
            rr    = sig.get("rr", 0.0)

            if abs(entry - sl) == 0 or entry == 0:
                return

            risk_usdt = balance * (config.RISK_PCT / 100)
            qty = math.floor((risk_usdt * config.LEVERAGE / entry) * 1000) / 1000
            if qty <= 0:
                log.warning(f"[{symbol}] qty=0 — balance insuficiente")
                return

            await self.client.set_leverage(symbol, config.LEVERAGE)
            await asyncio.sleep(0.15)

            resp = await self.client.place_market_order(symbol, side, qty, sl, tp)
            code = resp.get("code", -1)
            if code != 0:
                err = resp.get("msg", str(resp))
                log.error(f"[{symbol}] Order rejected ({code}): {err}")
                await tg.error_alert(self.session, f"[{symbol}] {err}")
                return

            pos = Position(symbol, side, entry, sl, tp, qty,
                           sig["green"], sig["red"], rr, atr)
            self.positions[symbol] = pos
            self._live_pos_cache.add(symbol)
            self.daily_trades += 1

            await tg.trade_entry(
                self.session, symbol, side, entry, sl, tp,
                qty, balance, rr, atr, sig["adx"], sig["vol_ratio"]
            )
            log.info(
                f"✅ [{symbol}] {side} @ {entry:.5g} SL={sl:.5g} TP={tp:.5g} "
                f"qty={qty} RR=1:{rr:.2f} ATR={atr:.5g}"
            )
        except Exception as e:
            log.exception(f"[{symbol}] _enter_trade: {e}")
            await tg.error_alert(self.session, f"[{symbol}] Entry error: {e}")

    # ──────────────────────────────────────────────────────────────────
    # MONITOR CON TRAILING STOP + BREAKEVEN
    # ──────────────────────────────────────────────────────────────────
    async def _monitor_position(self, symbol: str):
        try:
            pos = self.positions.get(symbol)
            if not pos or pos.closed:
                return

            # Precio actual (penúltima vela cerrada)
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, 4)
            _, _, _, C, _ = parse_klines(raw)
            if len(C) < 2:
                return
            current = float(C[-2])

            elapsed_min = (time.time() - pos.open_time) / 60.0

            # ── Time-Stop ─────────────────────────────────────────────
            if elapsed_min >= config.TIME_STOP_MINUTES:
                log.info(f"[{symbol}] ⏱ TIME-STOP {elapsed_min:.0f} min")
                await self.client.close_position_market(symbol, pos.side, pos.qty)
                pos.closed = True
                await self._record_exit(pos, symbol, "⏱ TIME-STOP", current)
                return

            # ── Trailing Stop / Breakeven (software) ──────────────────
            closed_by_trail = await self._update_trail(pos, symbol, current)
            if closed_by_trail:
                return

            # ── SL/TP nativo alcanzado (posición desaparece del exchange) ──
            if symbol not in self._live_pos_cache:
                pos.closed = True
                reason = (
                    "TAKE PROFIT ✅"
                    if abs(current - pos.tp) < abs(current - pos.sl)
                    else "STOP LOSS ❌"
                )
                if "TAKE" in reason:
                    self.daily_wins += 1
                await self._record_exit(pos, symbol, reason, current)

        except Exception as e:
            log.error(f"[{symbol}] _monitor_position: {e}")

    async def _update_trail(self, pos: Position, symbol: str, current: float) -> bool:
        """
        Trailing stop + breakeven en software.
        Retorna True si cerró la posición.

        Lógica:
          LONG:
            best_price = max(best_price, current)
            favorable  = current - entry
            • Breakeven: si favorable >= BREAKEVEN_ATR × atr → trail_sl = entry
            • Trailing:  si favorable >= TRAIL_ATR × atr
                           → trail_sl = max(trail_sl, best_price - TRAIL_DIST × atr)
            • Cierre:    si breakeven_active y current <= trail_sl → cerrar

          SHORT: simétrico
        """
        if pos.atr == 0:
            return False

        be_thresh    = config.BREAKEVEN_ATR * pos.atr
        trail_thresh = config.TRAIL_ATR * pos.atr
        trail_dist   = config.TRAIL_DIST * pos.atr

        if pos.side == "BUY":
            favorable = current - pos.entry
            if current > pos.best_price:
                pos.best_price = current

            # Activar breakeven
            if favorable >= be_thresh and not pos.breakeven_active:
                pos.breakeven_active = True
                pos.trail_sl = pos.entry
                log.info(f"[{symbol}] 🔒 Breakeven activado (fav={favorable:.5g} >= {be_thresh:.5g})")
                await tg.send(self.session,
                    f"🔒 <b>BREAKEVEN</b> — <code>{symbol}</code>\n"
                    f"Precio: <code>{current:.5g}</code> | "
                    f"SL movido a entrada: <code>{pos.entry:.5g}</code>"
                )

            # Activar trailing
            if favorable >= trail_thresh:
                new_trail = pos.best_price - trail_dist
                if new_trail > pos.trail_sl:
                    pos.trail_sl = new_trail
                    pos.trail_active = True
                    log.info(f"[{symbol}] 📈 Trail SL → {pos.trail_sl:.5g}")

            # Cerrar si precio cae bajo trail_sl
            if pos.breakeven_active and current <= pos.trail_sl:
                log.info(f"[{symbol}] 🛑 Trail/BE hit @ {current:.5g} (trail={pos.trail_sl:.5g})")
                await self.client.close_position_market(symbol, pos.side, pos.qty)
                pos.closed = True
                pnl_pts = current - pos.entry
                reason = "🔒 BREAKEVEN" if abs(pnl_pts) < pos.atr * 0.15 else "📈 TRAILING STOP ✅"
                if pnl_pts > 0:
                    self.daily_wins += 1
                await self._record_exit(pos, symbol, reason, current)
                return True

        else:  # SHORT
            favorable = pos.entry - current
            if current < pos.best_price:
                pos.best_price = current

            if favorable >= be_thresh and not pos.breakeven_active:
                pos.breakeven_active = True
                pos.trail_sl = pos.entry
                log.info(f"[{symbol}] 🔒 Breakeven activado (fav={favorable:.5g})")
                await tg.send(self.session,
                    f"🔒 <b>BREAKEVEN</b> — <code>{symbol}</code>\n"
                    f"Precio: <code>{current:.5g}</code> | "
                    f"SL movido a entrada: <code>{pos.entry:.5g}</code>"
                )

            if favorable >= trail_thresh:
                new_trail = pos.best_price + trail_dist
                if new_trail < pos.trail_sl:
                    pos.trail_sl = new_trail
                    pos.trail_active = True
                    log.info(f"[{symbol}] 📉 Trail SL → {pos.trail_sl:.5g}")

            if pos.breakeven_active and current >= pos.trail_sl:
                log.info(f"[{symbol}] 🛑 Trail/BE hit @ {current:.5g} (trail={pos.trail_sl:.5g})")
                await self.client.close_position_market(symbol, pos.side, pos.qty)
                pos.closed = True
                pnl_pts = pos.entry - current
                reason = "🔒 BREAKEVEN" if abs(pnl_pts) < pos.atr * 0.15 else "📉 TRAILING STOP ✅"
                if pnl_pts > 0:
                    self.daily_wins += 1
                await self._record_exit(pos, symbol, reason, current)
                return True

        return False

    async def _record_exit(self, pos: Position, symbol: str,
                           reason: str, exit_price: float = 0.0):
        if exit_price == 0.0:
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, 3)
            _, _, _, C, _ = parse_klines(raw)
            exit_price = float(C[-2]) if len(C) >= 2 else pos.entry

        pnl_pts = (exit_price - pos.entry) if pos.side == "BUY" else (pos.entry - exit_price)
        pnl     = pnl_pts * pos.qty * config.LEVERAGE
        pnl_pct = (pnl_pts / pos.entry) * 100 * config.LEVERAGE
        self.daily_pnl += pnl
        log.info(f"[{symbol}] Cerrada PnL={pnl:+.4f} USDT | {reason}")
        await tg.trade_exit(self.session, symbol, pos.side,
                            pos.entry, exit_price, pnl, pnl_pct, reason)

    def reset_daily(self):
        self.daily_pnl = 0.0; self.daily_trades = 0
        self.daily_wins = 0;  self.paused = False
        log.info("🔄 Contadores diarios reseteados")
