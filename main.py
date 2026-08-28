"""
main.py
=======
Punto de entrada del bot. Levanta un mini servidor Flask de estado/salud
(para verificar desde fuera que el proceso sigue vivo en Railway) en un
hilo en segundo plano, y ejecuta el bucle principal de trading en el
hilo principal.
"""

from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify

from config import Config
from trading_bot import TradingBot

config = Config()

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

bot = TradingBot(config)
app = Flask(__name__)


@app.route("/")
def root():
    return jsonify(
        {
            "status": "ok",
            "bot": "rsi-supertrend-dip-bot",
            "symbol": config.SYMBOL,
            "timeframe": config.TIMEFRAME,
            "dry_run": config.DRY_RUN,
        }
    )


@app.route("/health")
def health():
    return jsonify(bot.last_status)


def _run_bot():
    try:
        bot.run()
    except Exception:
        logger.exception("El bucle principal del bot terminó con un error fatal.")
        raise


if __name__ == "__main__":
    worker = threading.Thread(target=_run_bot, daemon=True, name="trading-loop")
    worker.start()
    logger.info("Servidor de estado escuchando en el puerto %s", config.PORT)
    app.run(host="0.0.0.0", port=config.PORT)
