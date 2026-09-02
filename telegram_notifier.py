"""Envío de mensajes a Telegram (señales manuales + avisos de ejecución/errores)."""
import logging

import requests

import config

log = logging.getLogger("telegram")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, parse_mode: str = "Markdown"):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado; mensaje omitido: %s", text)
        return
    try:
        requests.post(
            API_URL.format(token=config.TELEGRAM_BOT_TOKEN),
            data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception:
        log.exception("Fallo enviando mensaje a Telegram")


def format_entry_signal(alert: dict, executed: bool, qty: float = None, error: str = None):
    side = alert.get("positionSide", "?")
    icon = "🟢" if side == "LONG" else "🔴"
    lines = [
        f"{icon} *{side} {alert.get('symbol')}* — Wavelet MRA 5m",
        f"Precio señal: `{alert.get('price')}`",
        f"SL: `{alert.get('sl')}`   TP: `{alert.get('tp')}`",
    ]
    if executed:
        lines.append(f"✅ Ejecutado en BingX (qty: {qty})")
    elif error:
        lines.append(f"⚠️ NO ejecutado — {error}")
    else:
        lines.append("ℹ️ Modo manual: abre la posición tú mismo si te convence.")
    return "\n".join(lines)


def format_exit_signal(alert: dict, executed: bool, error: str = None):
    side = alert.get("positionSide", "?")
    lines = [
        f"⚪ *Cierre señal {side} {alert.get('symbol')}*",
        f"Precio: `{alert.get('price')}`",
    ]
    if executed:
        lines.append("✅ Posición cerrada en BingX")
    elif error:
        lines.append(f"⚠️ NO se pudo cerrar automáticamente — {error}")
    else:
        lines.append("ℹ️ Modo manual: cierra tú la posición si la tienes abierta.")
    return "\n".join(lines)
