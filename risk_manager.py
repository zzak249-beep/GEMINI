"""
risk_manager.py — Sizing · Cooldown · Límite diario
"""
import json, logging, os, time
from datetime import datetime, timezone
import config as C
from strategy import Signal

log = logging.getLogger(__name__)
STATE_FILE = "state.json"


class RiskManager:
    def __init__(self):
        self.daily_pnl        = 0.0
        self.daily_date       = ""
        self.cooldown_until   = 0
        self.consecutive_loss = 0
        self._load()

    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                s = json.load(open(STATE_FILE))
                self.daily_pnl        = s.get("daily_pnl", 0.0)
                self.daily_date       = s.get("daily_date", "")
                self.cooldown_until   = s.get("cooldown_until", 0)
                self.consecutive_loss = s.get("consecutive_loss", 0)
                log.info(f"Estado: pnl={self.daily_pnl:.2f} cooldown={self.cooldown_until}")
            except Exception as e:
                log.warning(f"state.json: {e}")
        self._reset_daily()

    def _save(self):
        json.dump({
            "daily_pnl":        self.daily_pnl,
            "daily_date":       self.daily_date,
            "cooldown_until":   self.cooldown_until,
            "consecutive_loss": self.consecutive_loss,
        }, open(STATE_FILE, "w"))

    def _reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.daily_date != today:
            self.daily_pnl  = 0.0
            self.daily_date = today
            self._save()

    # ── Sizing dinámico ────────────────────────────────────────

    def position_size(self, balance: float, signal: Signal) -> float:
        if balance <= 0 or signal.atr_val == 0:
            return 0.0
        risk_usdt = balance * C.RISK_PER_TRADE
        # FUERTE → 100% del riesgo, NORMAL → 70%
        quality_mult = 1.0 if signal.quality == "FUERTE" else 0.7
        # Reducción por pérdidas consecutivas
        loss_mult = max(0.3, 1.0 - self.consecutive_loss * 0.2)
        risk_usdt *= quality_mult * loss_mult
        sl_dist = abs(signal.entry - signal.sl)
        if sl_dist == 0:
            return 0.0
        qty = (risk_usdt / sl_dist) / C.LEVERAGE
        log.info(f"Sizing: balance={balance:.2f} risk={risk_usdt:.2f} "
                 f"sl_dist={sl_dist:.5f} qty={qty:.6f}")
        return max(qty, 0.0)

    # ── Validaciones ───────────────────────────────────────────

    def can_trade(self, balance: float, n_positions: int) -> tuple:
        self._reset_daily()
        if time.time() < self.cooldown_until:
            mins = int((self.cooldown_until - time.time()) / 60)
            return False, f"Cooldown activo — {mins}min restantes"
        if n_positions >= C.MAX_POSITIONS:
            return False, f"Máximo posiciones ({C.MAX_POSITIONS}) alcanzado"
        if balance <= 0:
            return False, "Balance cero"
        if self.daily_pnl < 0 and abs(self.daily_pnl) / max(balance, 1) >= C.DAILY_LOSS_LIMIT:
            return False, f"Límite diario: {self.daily_pnl:.2f} USDT"
        return True, "OK"

    def anti_hedge(self, direction: str, positions: list) -> tuple:
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if direction == "LONG"  and amt < 0: return False, "SHORT abierto"
            if direction == "SHORT" and amt > 0: return False, "LONG abierto"
        return True, "OK"

    # ── Registro resultado ─────────────────────────────────────

    def record(self, pnl: float):
        self._reset_daily()
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_loss += 1
            self.cooldown_until = time.time() + C.COOLDOWN_CANDLES * 3 * 60
            log.warning(f"Pérdida {pnl:.2f} | consecutivas={self.consecutive_loss}")
        else:
            self.consecutive_loss = 0
            self.cooldown_until   = 0
            log.info(f"Ganancia {pnl:.2f}")
        self._save()

    # ── Trailing stop ──────────────────────────────────────────

    def trail_sl(self, direction: str, price: float, sl: float, atr: float) -> float:
        trail = atr * C.TRAIL_ATR_MULT
        if direction == "LONG":
            return max(price - trail, sl)
        return min(price + trail, sl)
