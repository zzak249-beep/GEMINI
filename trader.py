"""
Trader — ZigZag V32
"""
import asyncio
import logging
import math
import time
from typing import Dict
import aiohttp

import config
import telegram_notifier as tg
from bingx_client import BingXClient
from strategy import ChannelFadeSignal, parse_klines

log = logging.getLogger("trader")


class Position:
    def __init__(self, symbol, side, entry, sl, tp, qty, green, red, rr=0.0, atr=0.0):
        self.symbol      = symbol
        self.side        = side
        self.entry       = entry
        self.sl          = sl
        self.sl_initial  = sl
        self.tp          = tp
        self.qty         = qty
        self.green       = green
        self.red         = red
        self.rr          = rr
        self.atr         = atr
        self.open_time   = time.time()
        self.closed      = False

    @property
    def elapsed_min(self):
        return (time.time() - self.open_time) / 60.0


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
        self._last_prices: Dict[str, float] = {}

    async def refresh_live_positions(self):
        try:
            live = await self.client.get_positions()
            self._live_pos_cache = set()
            for p in live:
                amt = self._pos_amt(p)
                sym = p.get("symbol", "")
                if amt != 0 and sym:
                    self._live_pos_cache.add(sym)
                    try:
                        mark = float(p.get("markPrice", 0))
                        if mark > 0:
                            self._last_prices[sym] = mark
                    except (ValueError, TypeError):
                        pass
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

    async def process_pair(self, symbol: str, balance: float):
        if self.paused:
            return

        if balance > 0:
            loss_pct = (self.daily_pnl / balance) * 100
            if loss_pct <= -config.MAX_DAILY_LOSS:
                if not self.paused:
                    self.paused = True
                    await tg.daily_loss_limit(
                        self.session, self.daily_pnl, config.MAX_DAILY_LOSS, balance)
                return

        pos = self.positions.get(symbol)
        if pos and pos.closed:
            del self.positions[symbol]
            pos = None

        if pos:
            await self._monitor_position(symbol)
            return

        active_count = sum(1 for p in self.positions.values() if not p.closed)
        if active_count >= config.MAX_POSITIONS:
            log.info(f"  [{symbol}] ✗ Max posiciones alcanzadas ({active_count}/{config.MAX_POSITIONS})")
            return

        if symbol in self._live_pos_cache:
            log.info(f"  [{symbol}] ✗ Ya hay posición abierta en exchange")
            return

        try:
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, config.KLINE_LIMIT)
        except Exception as e:
            log.warning(f"  [{symbol}] get_klines error: {e}")
            return

        if not raw or len(raw) < 50:
            log.warning(f"  [{symbol}] Pocas velas recibidas: {len(raw) if raw else 0}")
            return

        opens, highs, lows, closes, volumes = parse_klines(raw)
        if len(closes) < 50:
            log.warning(f"  [{symbol}] Pocas velas parseadas: {len(closes)}")
            return

        # PASA symbol para logging detallado en strategy
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
                log.warning(f"[{symbol}] SL==entry, abortando")
                return

            risk_usdt = balance * (config.RISK_PCT / 100)
            qty_raw   = (risk_usdt * config.LEVERAGE) / entry
            qty       = math.floor(qty_raw * 10000) / 10000

            if qty <= 0:
                log.warning(f"[{symbol}] qty={qty:.8f} ≤ 0 | balance={balance:.2f} risk={risk_usdt:.4f} entry={entry:.6g}")
                return

            log.info(f"[{symbol}] 🚀 Intentando orden {side} qty={qty:.4f} entry={entry:.6g} SL={sl:.6g} TP={tp:.6g}")

            await self.client.set_leverage(symbol, config.LEVERAGE)
            await asyncio.sleep(0.3)

            resp = await self.client.place_market_order(symbol, side, qty, sl, tp)
            code = resp.get("code", -1)

            if code != 0:
                err = resp.get("msg", str(resp))
                log.error(f"[{symbol}] ❌ Orden RECHAZADA code={code}: {err}")
                await tg.error_alert(self.session, f"[{symbol}] Orden rechazada (code={code}): {err}")
                return

            # Intentar leer qty ejecutada real
            order_data = resp.get("data", {})
            if isinstance(order_data, dict):
                for k in ("executedQty", "origQty"):
                    v = order_data.get(k)
                    if v:
                        try:
                            qty = float(v); break
                        except (ValueError, TypeError):
                            pass

            self.positions[symbol] = Position(symbol, side, entry, sl, tp, qty,
                                              sig["green"], sig["red"], rr, atr)
            self._live_pos_cache.add(symbol)
            self.daily_trades += 1

            await tg.trade_entry(self.session, symbol, side, entry, sl, tp, qty,
                                 balance, rr, atr, sig["adx"], sig["vol_ratio"])
            log.info(f"✅ [{symbol}] ABIERTO {side}@{entry:.6g} SL={sl:.6g} TP={tp:.6g} qty={qty:.4f} RR=1:{rr:.2f}")

        except Exception as e:
            log.exception(f"[{symbol}] _enter_trade: {e}")
            await tg.error_alert(self.session, f"[{symbol}] Entry error: {e}")

    async def _monitor_position(self, symbol: str):
        try:
            pos = self.positions.get(symbol)
            if not pos or pos.closed:
                return

            if pos.elapsed_min >= config.TIME_STOP_MINUTES:
                log.info(f"[{symbol}] ⏱ TIME-STOP ({pos.elapsed_min:.0f} min)")
                try:
                    await self.client.close_position_market(symbol, pos.side, pos.qty)
                except Exception as e:
                    log.error(f"[{symbol}] close error: {e}")
                pos.closed = True
                self._live_pos_cache.discard(symbol)
                exit_price = await self._get_exit_price(symbol, pos.entry)
                pnl, pnl_pct = self._pnl(pos, exit_price)
                if pnl > 0: self.daily_wins += 1
                self.daily_pnl += pnl
                await tg.trade_exit(self.session, symbol, pos.side, pos.entry,
                                    exit_price, pnl, pnl_pct, f"⏱ TIME-STOP")
                return

            if symbol not in self._live_pos_cache:
                pos.closed = True
                self._live_pos_cache.discard(symbol)
                exit_price = await self._get_exit_price(symbol, pos.entry)
                pnl, pnl_pct = self._pnl(pos, exit_price)
                dist_tp = abs(exit_price - pos.tp)
                dist_sl = abs(exit_price - pos.sl_initial)
                reason  = "TAKE PROFIT ✅" if dist_tp < dist_sl else "STOP LOSS ❌"
                if "TAKE" in reason: self.daily_wins += 1
                self.daily_pnl += pnl
                await tg.trade_exit(self.session, symbol, pos.side, pos.entry,
                                    exit_price, pnl, pnl_pct, reason)
                log.info(f"[{symbol}] {reason} PnL={pnl:+.4f} USDT")
        except Exception as e:
            log.error(f"[{symbol}] _monitor_position: {e}")

    async def _get_exit_price(self, symbol: str, fallback: float) -> float:
        cached = self._last_prices.get(symbol, 0)
        if cached > 0:
            return cached
        try:
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, 3)
            _, _, _, C, _ = parse_klines(raw)
            if len(C) >= 2:
                return float(C[-2])
        except Exception:
            pass
        return fallback

    @staticmethod
    def _pnl(pos: Position, exit_price: float):
        pts = (exit_price - pos.entry) if pos.side == "BUY" else (pos.entry - exit_price)
        pnl = pts * pos.qty * config.LEVERAGE
        pct = (pts / pos.entry * 100 * config.LEVERAGE) if pos.entry else 0.0
        return pnl, pct

    def reset_daily(self):
        self.daily_pnl    = 0.0
        self.daily_trades = 0
        self.daily_wins   = 0
        self.paused       = False
        self.positions    = {k: v for k, v in self.positions.items() if not v.closed}
        log.info(f"🔄 Día reseteado | pos conservadas: {list(self.positions.keys())}")
