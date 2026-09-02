"""
Entry point. Run locally with:  python main.py
On Railway, this is what the Procfile / start command runs.
"""
import signal
import time

from bot.config import load_config
from bot.exchange import BingXExchange
from bot.logger import get_logger, setup_logging
from bot.strategy import WaveletStrategy
from bot.telegram_notify import TelegramNotifier

log = get_logger("main")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %s — will shut down after the current cycle.", signum)
    _shutdown = True


def main() -> None:
    setup_logging()
    config = load_config()

    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id, config.telegram_enabled)

    exchange = BingXExchange(
        api_key=config.bingx_api_key,
        api_secret=config.bingx_api_secret,
        market_type=config.market_type,
        leverage=config.leverage,
        on_critical=notifier.send_error,
    )

    strategy = WaveletStrategy(config, exchange, notifier)

    log.info("Starting — symbol=%s timeframe=%s mode=%s", config.symbol, config.timeframe, config.mode_label)
    notifier.send(
        f"🤖 <b>Bot iniciado</b>\n"
        f"Símbolo: {config.symbol}\n"
        f"Timeframe: {config.timeframe}\n"
        f"Modo: {config.mode_label}"
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    consecutive_errors = 0
    while not _shutdown:
        try:
            strategy.run_once()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.exception("Error in main loop (consecutive_errors=%s)", consecutive_errors)
            if consecutive_errors in (1, 5) or consecutive_errors % 20 == 0:
                # Don't spam Telegram on every transient network blip, but
                # surface it early and then periodically if it keeps failing.
                notifier.send_error(f"{e}\n(errores consecutivos: {consecutive_errors})")
            backoff = min(config.poll_seconds * min(consecutive_errors, 10), 300)
            _sleep_interruptible(backoff)
            continue

        _sleep_interruptible(config.poll_seconds)

    notifier.send("🛑 Bot detenido.")
    log.info("Shutdown complete.")


def _sleep_interruptible(seconds: int) -> None:
    """Sleeps in 1-second steps so SIGTERM (Railway redeploys/restarts)
    is honoured quickly instead of blocking for the full poll interval."""
    for _ in range(max(int(seconds), 1)):
        if _shutdown:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
