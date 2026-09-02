"""
Ties the wavelet regime filter to live candles, decides when to fire a
signal, always notifies Telegram, and — only when the config allows live
trading (real keys + DRY_RUN=false) — sends the order to BingX.

One poll = one cycle:
  1. fetch recent candles
  2. drop the still-forming candle (mirrors Pine's barstate.isconfirmed)
  3. compute the regime filter + crossover
  4. if a signal fired: compute SL/TP, notify Telegram, place the order
     if live trading is enabled and the daily kill switch isn't tripped
  5. if a position is already open and trailing is enabled, maybe tighten
     the stop
"""
import logging

from .config import Config
from .exchange import BingXExchange, ExchangeError
from .risk import DailyKillSwitch, compute_sl_tp
from .telegram_notify import TelegramNotifier
from .wavelet import WaveletRegime

log = logging.getLogger(__name__)


class WaveletStrategy:
    def __init__(self, config: Config, exchange: BingXExchange, notifier: TelegramNotifier):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.regime = WaveletRegime(config.lookback_energy, config.k_dominance)
        self.kill_switch = DailyKillSwitch(config.max_daily_loss_pct)

        self._last_signal_ts = None
        self._trail_stop_price = None

        # Enough history for the slowest rolling window (energy lookback)
        # plus warm-up, with headroom.
        self._fetch_limit = max(300, config.lookback_energy + 80)

    def run_once(self) -> None:
        df = self.exchange.fetch_ohlcv_df(self.config.symbol, self.config.timeframe, limit=self._fetch_limit)
        if len(df) < 3:
            log.warning("Not enough candles returned (%s) — skipping this cycle.", len(df))
            return

        closed = df.iloc[:-1]  # drop the candle still forming
        sig = self.regime.compute(closed, atr_length=self.config.atr_length)
        merged = closed.join(sig)

        last = merged.iloc[-1]
        last_ts = merged.index[-1]

        if self.config.use_vol_filter:
            vol_sma = closed["volume"].rolling(self.config.vol_len).mean()
            vol_ok = bool(closed["volume"].iloc[-1] > vol_sma.iloc[-1] * self.config.vol_mult)
        else:
            vol_ok = True

        cooldown_ok = self._cooldown_ok(merged, last_ts)

        long_cond = bool(last["is_trending"] and vol_ok and last["cross_up"] and last["h8"] > 0 and cooldown_ok)
        short_cond = bool(last["is_trending"] and vol_ok and last["cross_down"] and last["h8"] < 0 and cooldown_ok)

        equity = self.exchange.fetch_equity_usdt() if self.exchange.has_keys else 0.0
        halted = self.kill_switch.update(equity) if self.exchange.has_keys else False

        if self.config.use_trail and self.exchange.has_keys:
            self._maybe_update_trailing(last)

        if not (long_cond or short_cond):
            return

        self._last_signal_ts = last_ts
        side = "long" if long_cond else "short"
        price = float(last["close"])
        atr_val = float(last["atr"])

        sl, tp = compute_sl_tp(
            side,
            price,
            atr_val,
            self.config.use_atr_sl,
            self.config.atr_mult_sl,
            self.config.atr_mult_tp,
            self.config.sl_percent,
            self.config.tp_percent,
        )

        live_order_sent = False
        if halted:
            log.warning("Kill switch is active for today — signal detected but no order will be sent.")
        elif self.config.can_trade_live:
            try:
                notional = max(equity, 0.0) * (self.config.qty_pct / 100)
                self.exchange.enter_position(self.config.symbol, side, notional, price, sl, tp)
                live_order_sent = True
                self._trail_stop_price = sl
            except ExchangeError:
                pass  # already logged and telegrammed inside exchange.py
            except Exception as e:
                log.exception("Unexpected error entering position")
                self.notifier.send_error(f"Unexpected error entering {side} on {self.config.symbol}: {e}")

        self.notifier.send_signal(
            side=side,
            symbol=self.config.symbol,
            price=price,
            sl=sl,
            tp=tp,
            timeframe=self.config.timeframe,
            mode_label=self.config.mode_label,
            live_order_sent=live_order_sent,
        )

    def _cooldown_ok(self, merged, last_ts) -> bool:
        if self._last_signal_ts is None:
            return True
        try:
            bars_since = merged.index.get_loc(last_ts) - merged.index.get_loc(self._last_signal_ts)
        except KeyError:
            # Previous signal timestamp fell outside the current window
            # (bot restarted, or a data gap) — treat cooldown as satisfied.
            return True
        return bars_since >= self.config.cooldown_bars

    def _maybe_update_trailing(self, last) -> None:
        pos = self.exchange.fetch_open_position(self.config.symbol)
        if not pos:
            self._trail_stop_price = None
            return

        side = pos.get("side")
        entry = float(pos.get("entryPrice") or 0)
        price = float(last["close"])
        atr_val = float(last["atr"]) if last["atr"] == last["atr"] else 0.0  # NaN check
        if not entry or not atr_val:
            return

        trigger = self.config.trail_trigger_atr * atr_val
        offset = self.config.trail_offset_atr * atr_val

        if side == "long":
            in_profit_enough = price >= entry + trigger
            candidate_stop = price - offset
            improves = self._trail_stop_price is None or candidate_stop > self._trail_stop_price
        else:
            in_profit_enough = price <= entry - trigger
            candidate_stop = price + offset
            improves = self._trail_stop_price is None or candidate_stop < self._trail_stop_price

        if in_profit_enough and improves:
            self._trail_stop_price = candidate_stop
            self.exchange.update_trailing_stop(self.config.symbol, side, candidate_stop)
