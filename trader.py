import asyncio, logging, math, time
from typing import Dict
import aiohttp
import config
import telegram_notifier as tg
from bingx_client import BingXClient
from strategy import ChannelFadeSignal, parse_klines

log = logging.getLogger("trader")

class Position:
    def __init__(self, symbol, side, entry, sl, tp, qty, green, red, rr=0.0, atr=0.0):
        self.symbol=symbol; self.side=side; self.entry=entry
        self.sl=sl; self.tp=tp; self.qty=qty; self.green=green; self.red=red
        self.rr=rr; self.atr=atr; self.open_time=time.time(); self.closed=False
        self.best_price=entry; self.trail_sl=sl; self.breakeven_active=False

class Trader:
    def __init__(self, client: BingXClient, session: aiohttp.ClientSession):
        self.client=client; self.session=session
        self.strategy=ChannelFadeSignal()
        self.positions: Dict[str, Position]={}
        self.daily_pnl=0.0; self.daily_trades=0; self.daily_wins=0
        self.paused=False; self._live_pos_cache: set=set()

    async def refresh_live_positions(self):
        try:
            live = await self.client.get_positions()
            self._live_pos_cache = {p.get("symbol","") for p in live if self._pos_amt(p)!=0}
        except Exception as e:
            log.error(f"refresh_live: {e}")

    @staticmethod
    def _pos_amt(p):
        for k in ("positionAmt","posAmt","availableAmt"):
            try:
                v = float(p.get(k,0))
                if v != 0: return v
            except: pass
        return 0.0

    async def process_pair(self, symbol: str, balance: float):
        if self.paused: return
        if balance > 0 and (self.daily_pnl/balance)*100 <= -config.MAX_DAILY_LOSS:
            self.paused = True
            await tg.daily_loss_limit(self.session, self.daily_pnl, config.MAX_DAILY_LOSS, balance)
            return
        pos = self.positions.get(symbol)
        if pos and not pos.closed:
            await self._monitor_position(symbol); return
        if sum(1 for p in self.positions.values() if not p.closed) >= config.MAX_POSITIONS:
            return
        raw = await self.client.get_klines(symbol, config.TIMEFRAME, config.KLINE_LIMIT)
        if not raw or len(raw) < 50: return
        opens,highs,lows,closes,volumes = parse_klines(raw)
        if len(closes) < 50: return
        sig = self.strategy.compute(opens,highs,lows,closes,volumes,symbol=symbol)
        if sig is None: return
        await tg.signal_channel_fade(self.session, symbol, sig["side"],
            sig["green"], sig["red"], sig["entry"], sig["trigger"],
            sig["canal_width"], sig["vol_ratio"], sig["adx"],
            sig["rr"], sig.get("rsi", 50.0))
        await self._enter_trade(symbol, sig, balance)

    async def _enter_trade(self, symbol: str, sig: dict, balance: float):
        try:
            entry=sig["entry"]; sl=sig["sl"]; tp=sig["tp"]
            side=sig["side"]; atr=sig["atr"]; rr=sig.get("rr",0.0)
            if abs(entry-sl)==0 or entry==0: return
            risk_usdt = balance*(config.RISK_PCT/100)
            qty = math.floor((risk_usdt*config.LEVERAGE/entry)*1000)/1000
            # Verificar notional mínimo (BingX requiere > 5 USDT)
            if qty*entry < config.MIN_NOTIONAL:
                log.warning(f"[{symbol}] notional={qty*entry:.2f} < {config.MIN_NOTIONAL} USDT mínimo")
                return
            if qty <= 0:
                log.warning(f"[{symbol}] qty=0 — balance insuficiente")
                return
            await self.client.set_leverage(symbol, config.LEVERAGE)
            await asyncio.sleep(0.15)
            resp = await self.client.place_market_order(symbol, side, qty, sl, tp)
            if resp.get("code",-1) != 0:
                err = resp.get("msg", str(resp))
                log.error(f"[{symbol}] rejected: {err}")
                await tg.error_alert(self.session, f"[{symbol}] {err}")
                return
            pos = Position(symbol,side,entry,sl,tp,qty,sig["green"],sig["red"],rr,atr)
            self.positions[symbol]=pos; self._live_pos_cache.add(symbol)
            self.daily_trades+=1
            await tg.trade_entry(self.session,symbol,side,entry,sl,tp,qty,
                                 balance,rr,atr,sig["adx"],sig["vol_ratio"],
                                 sig.get("rsi",50.0))
            log.info(f"✅ [{symbol}] {side} @ {entry:.5g} SL={sl:.5g} TP={tp:.5g} qty={qty} RR=1:{rr:.2f}")
        except Exception as e:
            log.exception(f"[{symbol}] _enter_trade: {e}")
            await tg.error_alert(self.session, f"[{symbol}] {e}")

    async def _monitor_position(self, symbol: str):
        try:
            pos = self.positions.get(symbol)
            if not pos or pos.closed: return
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, 4)
            _,_,_,C,_ = parse_klines(raw)
            if len(C) < 2: return
            current = float(C[-2])
            elapsed = (time.time()-pos.open_time)/60.0

            if elapsed >= config.TIME_STOP_MINUTES:
                log.info(f"[{symbol}] ⏱ TIME-STOP {elapsed:.0f}m")
                await self.client.close_position_market(symbol, pos.side, pos.qty)
                pos.closed = True
                await self._record_exit(pos, symbol, "⏱ TIME-STOP", current)
                return

            if await self._check_trail(pos, symbol, current):
                return

            if symbol not in self._live_pos_cache:
                pos.closed = True
                reason = ("TAKE PROFIT ✅"
                          if abs(current-pos.tp) < abs(current-pos.sl)
                          else "STOP LOSS ❌")
                if "TAKE" in reason: self.daily_wins += 1
                await self._record_exit(pos, symbol, reason, current)
        except Exception as e:
            log.error(f"[{symbol}] monitor: {e}")

    async def _check_trail(self, pos: Position, symbol: str, current: float) -> bool:
        if pos.atr == 0: return False
        be  = config.BREAKEVEN_ATR * pos.atr
        tri = config.TRAIL_ATR     * pos.atr
        trd = config.TRAIL_DIST    * pos.atr

        if pos.side == "BUY":
            fav = current - pos.entry
            if current > pos.best_price: pos.best_price = current
            if fav >= be and not pos.breakeven_active:
                pos.breakeven_active = True; pos.trail_sl = pos.entry
                log.info(f"[{symbol}] 🔒 BE activado")
                await tg.send(self.session,
                    f"🔒 <b>BREAKEVEN</b> — <code>{symbol}</code>\n"
                    f"Precio: <code>{current:.5g}</code> → SL a entrada <code>{pos.entry:.5g}</code>")
            if fav >= tri:
                nt = pos.best_price - trd
                if nt > pos.trail_sl: pos.trail_sl = nt
            if pos.breakeven_active and current <= pos.trail_sl:
                await self.client.close_position_market(symbol, pos.side, pos.qty)
                pos.closed = True
                pnl_pts = current - pos.entry
                r = "🔒 BREAKEVEN" if abs(pnl_pts) < pos.atr*0.1 else "📈 TRAILING ✅"
                if pnl_pts > 0: self.daily_wins += 1
                await self._record_exit(pos, symbol, r, current)
                return True
        else:
            fav = pos.entry - current
            if current < pos.best_price: pos.best_price = current
            if fav >= be and not pos.breakeven_active:
                pos.breakeven_active = True; pos.trail_sl = pos.entry
                log.info(f"[{symbol}] 🔒 BE activado")
                await tg.send(self.session,
                    f"🔒 <b>BREAKEVEN</b> — <code>{symbol}</code>\n"
                    f"Precio: <code>{current:.5g}</code> → SL a entrada <code>{pos.entry:.5g}</code>")
            if fav >= tri:
                nt = pos.best_price + trd
                if nt < pos.trail_sl: pos.trail_sl = nt
            if pos.breakeven_active and current >= pos.trail_sl:
                await self.client.close_position_market(symbol, pos.side, pos.qty)
                pos.closed = True
                pnl_pts = pos.entry - current
                r = "🔒 BREAKEVEN" if abs(pnl_pts) < pos.atr*0.1 else "📉 TRAILING ✅"
                if pnl_pts > 0: self.daily_wins += 1
                await self._record_exit(pos, symbol, r, current)
                return True
        return False

    async def _record_exit(self, pos: Position, symbol: str, reason: str, exit_price: float=0.0):
        if exit_price == 0.0:
            raw = await self.client.get_klines(symbol, config.TIMEFRAME, 3)
            _,_,_,C,_ = parse_klines(raw)
            exit_price = float(C[-2]) if len(C)>=2 else pos.entry
        pnl_pts = (exit_price-pos.entry) if pos.side=="BUY" else (pos.entry-exit_price)
        pnl     = pnl_pts*pos.qty*config.LEVERAGE
        pnl_pct = (pnl_pts/pos.entry)*100*config.LEVERAGE
        self.daily_pnl += pnl
        log.info(f"[{symbol}] Cerrada PnL={pnl:+.4f} | {reason}")
        await tg.trade_exit(self.session, symbol, pos.side,
                            pos.entry, exit_price, pnl, pnl_pct, reason)

    def reset_daily(self):
        self.daily_pnl=0.0; self.daily_trades=0; self.daily_wins=0; self.paused=False
        log.info("🔄 Reset diario")
