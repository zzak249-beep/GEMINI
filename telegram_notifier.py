"""
telegram_notifier.py
=====================
Envío de notificaciones a Telegram vía la Bot API estándar
(https://api.telegram.org/bot<token>/sendMessage).
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("telegram")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10):
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logger.warning("Telegram respondió %s: %s", resp.status_code, resp.text)
        except requests.RequestException as exc:
            logger.warning("No se pudo enviar mensaje a Telegram: %s", exc)

    # ------------------------------------------------------------------
    def startup(self, config_summary: str) -> None:
        self.send(f"🤖 <b>Bot RSI+SuperTrend Doble Dip iniciado</b>\n\n{config_summary}")

    def entry(self, symbol: str, qty: float, price: float, leverage: int, rsi: float) -> None:
        self.send(
            "🟢 <b>ENTRADA LONG</b>\n"
            f"Símbolo: <b>{symbol}</b>\n"
            f"Cantidad: <code>{qty}</code>\n"
            f"Precio aprox.: <code>{price:.4f}</code>\n"
            f"Apalancamiento: {leverage}x\n"
            f"RSI en señal: {rsi:.2f}\n"
            "Motivo: Doble Dip (2º cruce alcista de RSI bajo 50)"
        )

    def exit(self, symbol: str, qty: float, price: float, reason: str, pnl: float | None = None) -> None:
        pnl_txt = f"\nPnL no realizado al cierre: <code>{pnl:.4f}</code> USDT" if pnl is not None else ""
        self.send(
            "🔴 <b>SALIDA LONG</b>\n"
            f"Símbolo: <b>{symbol}</b>\n"
            f"Cantidad: <code>{qty}</code>\n"
            f"Precio aprox.: <code>{price:.4f}</code>\n"
            f"Motivo: {reason}"
            f"{pnl_txt}"
        )

    def error(self, message: str) -> None:
        self.send(f"⚠️ <b>Error en el bot</b>\n<code>{message}</code>")

    def info(self, message: str) -> None:
        self.send(message)
