"""
QF×JP Bot v6.4 — Risk Manager
Kelly Criterion, límites diarios, daily drawdown.
"""
import asyncio
import logging
import math
from datetime import date

import config as C

log = logging.getLogger("risk")


class RiskManager:
    def __init__(self):
        self._today             = date.today()
        self._daily_trades      = 0
        self._daily_pnl         = 0.0
        self._daily_loss_limit  = C.CAPITAL * 0.05
        self._open_count        = 0
        self._lock              = asyncio.Lock()

    def _check_reset(self):
        today = date.today()
        if today != self._today:
            self._today        = today
            self._daily_trades = 0
            self._daily_pnl    = 0.0
            log.info("Daily stats reset for %s", today)

    # ── Kelly sizing ──────────────────────────────────────────────────────────

    def kelly_position_size(self, balance, entry, sl, score, tier):
        self._check_reset()
        if balance <= 0 or entry <= 0:
            return 0.0

        risk_per_unit = abs(entry - sl)
        if risk_per_unit < 1e-12:
            return 0.0

        tier_mult  = {"STD": 1.0, "FUEL": 1.1, "SUP": 1.25}.get(tier, 1.0)
        score_mult = 0.7 + 0.3 * (score / 100.0)
        p  = min(C.KELLY_WIN_RATE * tier_mult * score_mult, 0.9)
        rr = C.KELLY_RR
        q  = 1.0 - p

        kelly_f  = max(0.0, (p * rr - q) / rr) * C.KELLY_FRACTION
        risk_usdt = min(balance * kelly_f * (C.RISK_PCT / 100.0), balance * 0.03)

        qty = (risk_usdt * C.LEVERAGE) / risk_per_unit

        # Notional cap dinámico: STD=500, FUEL=750, SUP=1000 USDT
        notional = qty * entry
        cap_map  = {"STD": 500.0, "FUEL": 750.0, "SUP": 1000.0}
        cap      = cap_map.get(tier, 500.0)
        if notional > cap:
            log.info("[sizing] %s notional clampeado %.2f→%.2f USDT (qty %s, entry=%s SL=%s)",
                     tier, notional, cap, qty, entry, sl)
            qty = cap / entry

        log.info("[sizing] %s score=%.1f ks=%.2f risk=%.2f USDT qty=%s notional=%.2f USDT (entry=%s SL=%s)",
                 tier, score, kelly_f, risk_usdt, round(qty, 6), qty * entry, entry, sl)

        return round(qty, 6)

    # ── Límites diarios ───────────────────────────────────────────────────────

    async def can_trade(self):
        async with self._lock:
            self._check_reset()
            if self._daily_trades >= C.MAX_DAILY_TRADES:
                return False, f"daily_trades_limit({self._daily_trades}/{C.MAX_DAILY_TRADES})"
            if self._open_count >= C.MAX_OPEN_TRADES:
                return False, f"max_open_trades({self._open_count}/{C.MAX_OPEN_TRADES})"
            if self._daily_pnl <= -self._daily_loss_limit:
                return False, f"daily_drawdown_limit(pnl={self._daily_pnl:.2f})"
            return True, "ok"

    async def on_trade_opened(self):
        async with self._lock:
            self._daily_trades += 1
            self._open_count   += 1

    async def on_trade_closed(self, pnl: float):
        async with self._lock:
            self._open_count = max(0, self._open_count - 1)
            self._daily_pnl += pnl

    async def update_open_count(self, n: int):
        async with self._lock:
            self._open_count = n

    def tier_ok(self, tier: str) -> bool:
        hierarchy = {"NONE": -1, "STD": 0, "FUEL": 1, "SUP": 2}
        return hierarchy.get(tier, -1) >= hierarchy.get(C.MIN_TIER, 0)

    def status(self) -> dict:
        self._check_reset()
        return {
            "date":             str(self._today),
            "daily_trades":     self._daily_trades,
            "max_daily_trades": C.MAX_DAILY_TRADES,
            "open_positions":   self._open_count,
            "max_open_trades":  C.MAX_OPEN_TRADES,
            "daily_pnl":        round(self._daily_pnl, 2),
            "daily_loss_limit": round(self._daily_loss_limit, 2),
        }
