"""
Loads and validates all bot settings from environment variables (via a
local .env file when running locally, or Railway's injected env vars in
production). Nothing in here is a secret by itself — see .env.example for
the full list of variables — but this module is the single source of truth
for defaults, so change values there or in your Railway service settings,
not in code.
"""
import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # no-op in Railway, useful for local runs


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


def _str(name: str, default: str = "") -> str:
    val = os.getenv(name)
    return val if val not in (None, "") else default


@dataclass
class Config:
    # Exchange
    bingx_api_key: str
    bingx_api_secret: str
    market_type: str      # "swap" (USDT-M perpetual futures) or "spot"
    symbol: str            # ccxt unified symbol, e.g. "BTC/USDT:USDT"
    timeframe: str          # e.g. "5m"
    leverage: int
    dry_run: bool           # True = never place real orders

    # Strategy (mirrors the Pine script inputs 1:1)
    lookback_energy: int
    k_dominance: float
    cooldown_bars: int
    use_vol_filter: bool
    vol_len: int
    vol_mult: float

    # Risk management
    qty_pct: float           # % of equity risked as position notional
    use_atr_sl: bool
    atr_length: int
    atr_mult_sl: float
    atr_mult_tp: float
    sl_percent: float        # fraction, e.g. 0.01 = 1%
    tp_percent: float
    use_trail: bool
    trail_trigger_atr: float
    trail_offset_atr: float
    max_daily_loss_pct: float  # kill switch: halt new entries for the day

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # Loop
    poll_seconds: int
    log_level: str

    @property
    def can_trade_live(self) -> bool:
        """
        Whether the bot is actually allowed to send real orders to BingX.
        Requires BOTH real API keys AND DRY_RUN=false. Missing either one
        keeps the bot in read-only / signal-only mode, on purpose.
        """
        has_keys = bool(self.bingx_api_key and self.bingx_api_secret)
        return has_keys and not self.dry_run

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def mode_label(self) -> str:
        if self.can_trade_live:
            return "🔴 LIVE — real orders will be sent to BingX"
        if self.bingx_api_key and self.bingx_api_secret:
            return "🟡 PAPER (DRY_RUN) — real data, simulated orders only"
        return "⚪ SIGNAL-ONLY — no BingX keys, Telegram alerts only"


def load_config() -> Config:
    cfg = Config(
        bingx_api_key=_str("BINGX_API_KEY"),
        bingx_api_secret=_str("BINGX_API_SECRET"),
        market_type=_str("MARKET_TYPE", "swap"),
        symbol=_str("SYMBOL", "BTC/USDT:USDT"),
        timeframe=_str("TIMEFRAME", "5m"),
        leverage=_int("LEVERAGE", 3),
        dry_run=_bool("DRY_RUN", True),
        lookback_energy=_int("LOOKBACK_ENERGY", 40),
        k_dominance=_float("K_DOMINANCE", 1.5),
        cooldown_bars=_int("COOLDOWN_BARS", 4),
        use_vol_filter=_bool("USE_VOL_FILTER", False),
        vol_len=_int("VOL_LEN", 20),
        vol_mult=_float("VOL_MULT", 1.2),
        qty_pct=_float("QTY_PCT", 10.0),
        use_atr_sl=_bool("USE_ATR_SL", True),
        atr_length=_int("ATR_LENGTH", 14),
        atr_mult_sl=_float("ATR_MULT_SL", 1.5),
        atr_mult_tp=_float("ATR_MULT_TP", 2.5),
        sl_percent=_float("SL_PERCENT", 1.0) / 100,
        tp_percent=_float("TP_PERCENT", 2.0) / 100,
        use_trail=_bool("USE_TRAIL", False),
        trail_trigger_atr=_float("TRAIL_TRIGGER_ATR", 1.0),
        trail_offset_atr=_float("TRAIL_OFFSET_ATR", 1.0),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 5.0),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        poll_seconds=_int("POLL_SECONDS", 30),
        log_level=_str("LOG_LEVEL", "INFO"),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    errors = []
    if not (0 < cfg.qty_pct <= 100):
        errors.append("QTY_PCT must be between 0 and 100")
    if not (1 <= cfg.leverage <= 125):
        errors.append("LEVERAGE looks out of range (expected 1-125)")
    if cfg.market_type not in ("swap", "spot"):
        errors.append("MARKET_TYPE must be 'swap' or 'spot'")
    if not cfg.dry_run and not (cfg.bingx_api_key and cfg.bingx_api_secret):
        errors.append(
            "DRY_RUN=false but BINGX_API_KEY/BINGX_API_SECRET are missing — "
            "refusing to start in a half-configured live-trading state. "
            "Set both keys, or set DRY_RUN=true."
        )

    if errors:
        raise SystemExit("Configuration error(s):\n- " + "\n- ".join(errors))

    if not cfg.telegram_enabled:
        logging.getLogger(__name__).warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — the bot will run "
            "and log signals, but will NOT push anything to Telegram."
        )
    if cfg.can_trade_live:
        logging.getLogger(__name__).warning(
            "Starting in LIVE mode — real orders will be sent to BingX with "
            "real funds. Ctrl+C now if that's not what you intended."
        )
