"""
Risk helpers: SL/TP price calculation (mirrors the Pine strategy's
use_atr_sl branch exactly) and a simple daily-drawdown kill switch.

The kill switch is a deliberate addition beyond the original script. An
unattended bot that keeps re-entering through a losing streak is the
single most common way these things blow up an account; a circuit
breaker that halts new entries after a bad day is cheap insurance.
"""
import datetime as dt
import logging

log = logging.getLogger(__name__)


def compute_sl_tp(
    side: str,
    entry_price: float,
    atr_value: float,
    use_atr_sl: bool,
    atr_mult_sl: float,
    atr_mult_tp: float,
    sl_percent: float,
    tp_percent: float,
):
    """Returns (stop_loss_price, take_profit_price)."""
    if side == "long":
        if use_atr_sl:
            return entry_price - atr_value * atr_mult_sl, entry_price + atr_value * atr_mult_tp
        return entry_price * (1 - sl_percent), entry_price * (1 + tp_percent)
    else:  # short
        if use_atr_sl:
            return entry_price + atr_value * atr_mult_sl, entry_price - atr_value * atr_mult_tp
        return entry_price * (1 + sl_percent), entry_price * (1 - tp_percent)


class DailyKillSwitch:
    """Halts new entries once equity drawdown from the start of the current
    UTC day exceeds max_daily_loss_pct. Existing open positions are left
    alone (their own SL/TP still governs them) — this only blocks new
    entries until the next UTC day rolls over."""

    def __init__(self, max_daily_loss_pct: float):
        self.max_daily_loss_pct = max_daily_loss_pct
        self._day: dt.date | None = None
        self._day_start_equity: float | None = None
        self.halted = False

    def update(self, equity: float, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        today = now.date()

        if self._day != today:
            self._day = today
            self._day_start_equity = equity
            if self.halted:
                log.info("New UTC day — resetting daily kill switch.")
            self.halted = False

        if self._day_start_equity and self._day_start_equity > 0:
            drawdown_pct = (self._day_start_equity - equity) / self._day_start_equity * 100
            if drawdown_pct >= self.max_daily_loss_pct and not self.halted:
                self.halted = True
                log.warning(
                    "Daily kill switch triggered: %.2f%% drawdown from "
                    "today's starting equity (limit %.2f%%). Halting new "
                    "entries until the next UTC day.",
                    drawdown_pct,
                    self.max_daily_loss_pct,
                )
        return self.halted
