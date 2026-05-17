"""
bot/risk_manager.py
Gestión de riesgo profesional para NEXUS Bot:
  - Kelly fraccional (1/4 Kelly) para sizing óptimo
  - Triple Barrera: TP / SL (gestionados por BingX) + tiempo
  - Protección de drawdown diario
  - Cooldown post-pérdida por símbolo
  - Límite de posiciones simultáneas
"""
import logging
from datetime import date, datetime, timezone
from dataclasses import dataclass, field

from config import Config
from bot.strategy import SignalResult

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    symbol:      str
    side:        str         # LONG | SHORT
    entry_price: float
    quantity:    float
    tp_price:    float
    sl_price:    float
    entry_bar:   int
    open_time:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RiskManager:

    def __init__(self, config: Config):
        self.cfg              = config
        self._daily_loss      = 0.0
        self._last_reset_day  = date.today()
        self._cooldown_until: dict[str, datetime] = {}
        self._open_count      = 0

    # ─────────────────────────────────────────────────────────
    # GUARDS DE ENTRADA
    # ─────────────────────────────────────────────────────────

    def can_trade(self, symbol: str) -> bool:
        self._reset_daily_if_needed()

        if self._open_count >= self.cfg.MAX_OPEN_POSITIONS:
            logger.debug(f"{symbol}: límite de posiciones simultáneas")
            return False

        if self._is_cooldown(symbol):
            logger.debug(f"{symbol}: en cooldown post-pérdida")
            return False

        if self._daily_loss >= self.cfg.MAX_DAILY_LOSS_PCT:
            logger.warning(f"Daily loss {self._daily_loss:.2f}% ≥ {self.cfg.MAX_DAILY_LOSS_PCT}% — pausando")
            return False

        return True

    def register_open(self, symbol: str) -> None:
        self._open_count = max(0, self._open_count + 1)

    def register_close(self, symbol: str, pnl_pct: float) -> None:
        self._open_count = max(0, self._open_count - 1)
        self._reset_daily_if_needed()

        if pnl_pct < 0:
            self._daily_loss += abs(pnl_pct)
            cool_secs = self.cfg.LOOP_INTERVAL * 2
            self._cooldown_until[symbol] = datetime.now(timezone.utc).replace(
                second=datetime.now(timezone.utc).second
            ).__class__.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + cool_secs, tz=timezone.utc
            )
            logger.info(f"{symbol}: cooldown {cool_secs}s tras pérdida {pnl_pct:.2f}%")

    # ─────────────────────────────────────────────────────────
    # SIZING — KELLY FRACCIONAL
    # ─────────────────────────────────────────────────────────

    def calculate_position_size(self, signal: SignalResult,
                                balance_usdt: float) -> float:
        """
        1/4 Kelly con probability de Markov + límite RISK_PER_TRADE%.
        qty = (notional con leverage) / entry_price
        """
        rr     = self.cfg.ATR_MULT_TP / self.cfg.ATR_MULT_SL   # ej: 2.2/1.2 = 1.83

        raw_p  = signal.prob_bull if signal.long else signal.prob_bear
        p_win  = max(0.45, min(0.75, raw_p / 100.0))           # clip conservador

        kelly_full = (p_win * rr - (1 - p_win)) / rr
        kelly_frac = max(0.0, kelly_full / 4)                   # 1/4 Kelly

        # Bonus de confianza si score alto
        score_bonus = min(0.3, (signal.score - 55) / 100)       # +0 a +0.3 según score
        kelly_frac  = min(kelly_frac * (1 + score_bonus), self.cfg.RISK_PER_TRADE / 100)

        risk_pct   = min(kelly_frac * 100, self.cfg.RISK_PER_TRADE)
        risk_usdt  = balance_usdt * (risk_pct / 100.0)
        notional   = risk_usdt * self.cfg.LEVERAGE
        qty        = notional / signal.entry_price if signal.entry_price > 0 else 0.0

        logger.info(
            f"Kelly sizing [{signal.symbol}]: p_win={p_win:.2%} rr={rr:.2f} "
            f"kelly_frac={kelly_frac:.3f} risk={risk_pct:.2f}% qty={qty:.6f}"
        )
        return qty

    # ─────────────────────────────────────────────────────────
    # TRIPLE BARRERA
    # ─────────────────────────────────────────────────────────

    def compute_barriers(self, entry_price: float, atr14: float,
                         side: str) -> tuple[float, float]:
        tp_dist = atr14 * self.cfg.ATR_MULT_TP
        sl_dist = atr14 * self.cfg.ATR_MULT_SL

        if side == "LONG":
            tp = entry_price + tp_dist
            sl = entry_price - sl_dist
        else:
            tp = entry_price - tp_dist
            sl = entry_price + sl_dist

        return round(tp, 6), round(sl, 6)

    def check_time_exit(self, state: PositionState, current_bar: int) -> bool:
        return (current_bar - state.entry_bar) >= self.cfg.MAX_BARS_HOLD

    # ─────────────────────────────────────────────────────────
    # INTERNOS
    # ─────────────────────────────────────────────────────────

    def _reset_daily_if_needed(self) -> None:
        if date.today() != self._last_reset_day:
            self._daily_loss    = 0.0
            self._last_reset_day = date.today()
            logger.info("Daily PnL counter reseteado")

    def _is_cooldown(self, symbol: str) -> bool:
        until = self._cooldown_until.get(symbol)
        return until is not None and datetime.now(timezone.utc) < until

    @property
    def daily_loss_pct(self) -> float:
        return self._daily_loss

    @property
    def open_positions(self) -> int:
        return self._open_count
