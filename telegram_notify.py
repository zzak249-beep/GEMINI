"""
Minimal Telegram notifier. This intentionally does NOT build a full
interactive Telegram bot (no /pause, /status commands) — the request this
was built for is "push me the signal so I can trade it by hand", which is
a one-way sendMessage call. It's a small, well-contained piece to extend
later if you want two-way control.

To set this up:
1. Talk to @BotFather on Telegram, run /newbot, copy the token it gives you
   into TELEGRAM_BOT_TOKEN.
2. Send any message to your new bot, then open in a browser:
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   and copy the "chat":{"id": ...} value into TELEGRAM_CHAT_ID.
   (For a channel instead of a DM, add the bot as admin and use the
   channel's negative chat id.)
"""
import logging

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, enabled: bool):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled

    def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram disabled] %s", text.replace("\n", " | "))
            return
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=self.bot_token),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                log.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
        except requests.RequestException:
            log.exception("Telegram send raised an exception")

    def send_signal(
        self,
        side: str,
        symbol: str,
        price: float,
        sl: float,
        tp: float,
        timeframe: str,
        mode_label: str,
        live_order_sent: bool,
    ) -> None:
        arrow = "🟢 LONG" if side == "long" else "🔴 SHORT"
        status = "✅ orden enviada a BingX" if live_order_sent else "✋ señal solo — sin orden enviada"
        text = (
            f"<b>{arrow}</b>  {symbol} ({timeframe})\n"
            f"Precio: <code>{price:.6f}</code>\n"
            f"SL: <code>{sl:.6f}</code>   TP: <code>{tp:.6f}</code>\n"
            f"{status}\n"
            f"<i>{mode_label}</i>"
        )
        self.send(text)

    def send_exit(self, symbol: str, side: str, price: float, reason: str) -> None:
        text = (
            f"⚪ Posición cerrada — {symbol} ({side})\n"
            f"Precio de cierre: <code>{price:.6f}</code>\n"
            f"Motivo: {reason}"
        )
        self.send(text)

    def send_error(self, text: str) -> None:
        self.send(f"⚠️ <b>Error del bot</b>\n{text}")
