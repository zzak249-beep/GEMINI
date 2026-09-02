"""
Thin wrapper around ccxt's `bingx` exchange class.

A few BingX-specific things that are easy to get wrong — collected from
ccxt's issue tracker and official examples, not guesswork, because this
handles real orders:

- Your API key needs "Perpetual Futures Trading" permission explicitly
  enabled on BingX's API management page. A key with only spot permissions
  authenticates fine for reads and then fails on create_order with an
  authorization error (code 100004). If you hit that error, this is why.
- SL/TP are placed as separate reduce-only STOP_MARKET / TAKE_PROFIT_MARKET
  orders *after* the entry order confirms — mirroring ccxt's own documented
  pattern for Binance-style futures APIs (which BingX's swap API closely
  follows) — rather than bundling them into the entry call, which has been
  unreliable on this endpoint historically.
- This targets *one-way* position mode (the default for most accounts),
  not hedge mode. If your BingX account is in hedge mode, switch it to
  one-way in the position settings first, or the flip-on-reverse-signal
  logic below will not behave the way you expect.

Public market data (fetch_ohlcv) works without API keys — so this class
can run keyless in signal-only mode.
"""
import logging
import time
from typing import Callable, Optional

import ccxt
import pandas as pd

log = logging.getLogger(__name__)


class ExchangeError(Exception):
    pass


class BingXExchange:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        market_type: str,
        leverage: int,
        on_critical: Optional[Callable[[str], None]] = None,
    ):
        """on_critical: optional callback (e.g. notifier.send_error) fired
        the moment something happens that could leave a live position
        unprotected. Kept decoupled from any specific notifier class."""
        self.market_type = market_type
        self.leverage = leverage
        self.has_keys = bool(api_key and api_secret)
        self.on_critical = on_critical or (lambda msg: None)

        self.client = ccxt.bingx(
            {
                "apiKey": api_key or None,
                "secret": api_secret or None,
                "enableRateLimit": True,
                "options": {"defaultType": market_type},
            }
        )

        self.client.load_markets()
        self._leverage_set_for: set = set()

    # ------------------------------------------------------------ market data
    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        raw = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("ts")

    # ------------------------------------------------------------ account
    def fetch_equity_usdt(self) -> float:
        if not self.has_keys:
            return 0.0
        balance = self.client.fetch_balance()
        usdt = balance.get("USDT", {})
        total = usdt.get("total")
        if total is None:
            total = (balance.get("total") or {}).get("USDT", 0.0)
        return float(total or 0.0)

    def fetch_open_position(self, symbol: str) -> Optional[dict]:
        if not self.has_keys:
            return None
        positions = self.client.fetch_positions([symbol])
        for p in positions:
            contracts = p.get("contracts") or 0
            if float(contracts) != 0:
                return p
        return None

    def _ensure_leverage(self, symbol: str) -> None:
        if symbol in self._leverage_set_for:
            return
        try:
            self.client.set_leverage(self.leverage, symbol)
        except Exception as e:
            log.warning(
                "Could not set leverage to %sx for %s (%s) — continuing "
                "with whatever leverage is already configured on the "
                "account for this symbol.",
                self.leverage,
                symbol,
                e,
            )
        self._leverage_set_for.add(symbol)

    # ------------------------------------------------------------ trading
    def close_position(self, symbol: str, reason: str = "manual close") -> None:
        pos = self.fetch_open_position(symbol)
        if not pos:
            return
        side = "sell" if pos.get("side") == "long" else "buy"
        amount = abs(float(pos["contracts"]))
        log.info("Closing %s position on %s (%s contracts) — %s", pos.get("side"), symbol, amount, reason)
        self.client.create_order(symbol, "market", side, amount, params={"reduceOnly": True})
        self._cancel_open_orders(symbol)

    def _cancel_open_orders(self, symbol: str) -> None:
        try:
            self.client.cancel_all_orders(symbol)
        except Exception:
            log.exception(
                "Failed to cancel leftover conditional orders for %s — "
                "check the exchange manually for stray SL/TP orders.",
                symbol,
            )

    def enter_position(
        self, symbol: str, side: str, notional_usdt: float, price: float, sl: float, tp: float
    ) -> Optional[dict]:
        """
        side: "long" or "short". Flattens any opposite open position first
        (mirrors the original Pine strategy's netting behaviour with
        pyramiding=0 — it doesn't hold long and short at once).
        """
        self._ensure_leverage(symbol)

        existing = self.fetch_open_position(symbol)
        if existing:
            if existing.get("side") == side:
                log.info("Already in a %s position on %s — skipping duplicate entry.", side, symbol)
                return None
            self.close_position(symbol, reason="flipping direction on new signal")

        amount = float(self.client.amount_to_precision(symbol, notional_usdt * self.leverage / price))
        if amount <= 0:
            raise ExchangeError(f"Computed order amount is {amount} (<=0) — check QTY_PCT and account equity.")

        entry_side = "buy" if side == "long" else "sell"
        log.info("Entering %s %s — amount=%s notional=%.2f USDT @ %.6f", side, symbol, amount, notional_usdt, price)
        order = self.client.create_order(symbol, "market", entry_side, amount)

        # Give the fill a moment to land before we try to protect it.
        time.sleep(1.5)
        if not self.fetch_open_position(symbol):
            msg = (
                f"Entry order sent for {symbol} {side} but no open position was found "
                f"a moment later — the fill may be delayed or the order may not have "
                f"executed. NOT placing SL/TP yet; check the exchange manually."
            )
            log.error(msg)
            self.on_critical(msg)
            return order

        self._place_protective_orders(symbol, side, amount, sl, tp)
        return order

    def _place_protective_orders(self, symbol: str, side: str, amount: float, sl: float, tp: float) -> None:
        exit_side = "sell" if side == "long" else "buy"
        sl = float(self.client.price_to_precision(symbol, sl))
        tp = float(self.client.price_to_precision(symbol, tp))

        try:
            self.client.create_order(
                symbol,
                "STOP_MARKET",
                exit_side,
                amount,
                None,
                {"stopPrice": sl, "reduceOnly": True, "workingType": "MARK_PRICE"},
            )
        except Exception as e:
            msg = (
                f"FAILED TO PLACE STOP LOSS for {symbol} {side} at {sl}: {e}. "
                f"The position is OPEN and UNPROTECTED — close it or set a stop manually now."
            )
            log.error(msg)
            self.on_critical(msg)
            raise ExchangeError(msg) from e

        try:
            self.client.create_order(
                symbol,
                "TAKE_PROFIT_MARKET",
                exit_side,
                amount,
                None,
                {"stopPrice": tp, "reduceOnly": True, "workingType": "MARK_PRICE"},
            )
        except Exception as e:
            msg = (
                f"Stop-loss is active, but failed to place take-profit for {symbol} {side} "
                f"at {tp}: {e}. Position is protected on the downside only."
            )
            log.warning(msg)
            self.on_critical(msg)

    def update_trailing_stop(self, symbol: str, side: str, new_stop: float) -> None:
        """Cancels existing conditional orders and re-places the stop at a
        tighter level. Used only when USE_TRAIL is enabled. Take-profit,
        if one existed, is intentionally not re-created — once trailing
        takes over, the trail *is* the exit plan."""
        exit_side = "sell" if side == "long" else "buy"
        pos = self.fetch_open_position(symbol)
        if not pos:
            return
        amount = abs(float(pos["contracts"]))
        self._cancel_open_orders(symbol)
        new_stop = float(self.client.price_to_precision(symbol, new_stop))
        try:
            self.client.create_order(
                symbol,
                "STOP_MARKET",
                exit_side,
                amount,
                None,
                {"stopPrice": new_stop, "reduceOnly": True, "workingType": "MARK_PRICE"},
            )
            log.info("Trailing stop moved to %.6f for %s", new_stop, symbol)
        except Exception as e:
            msg = f"Failed to move trailing stop for {symbol} to {new_stop}: {e}"
            log.error(msg)
            self.on_critical(msg)
