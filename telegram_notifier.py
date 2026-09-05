"""Envío de mensajes a Telegram (señales manuales + avisos de ejecución/errores)."""
import logging

import requests

import config

log = logging.getLogger("telegram")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram corta en 4096 caracteres y rechaza el mensaje entero si se pasa.
# Con listas largas de símbolos (escaneo de todo el universo) se alcanza.
MAX_LEN = 3900


def send(text: str, parse_mode: str = "Markdown") -> bool:
    """Manda un mensaje. Devuelve True si Telegram lo aceptó.

    Antes se ignoraba la respuesta HTTP: el except solo captura fallos de
    red, no rechazos de la API. Un 400 por Markdown mal formado -- que es
    lo que pasa en cuanto un '_' o un '*' aparece en un nombre de símbolo
    o en el texto de una excepción -- hacía desaparecer el mensaje sin
    dejar rastro. Y por ahí se van precisamente los avisos que importan
    ("REVISA BINGX A MANO AHORA").

    Ahora se comprueba, y si el rechazo viene del formato se reintenta en
    texto plano: perder el formato es mejor que perder el aviso.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado; mensaje omitido: %s", text)
        return False

    text = str(text)
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "\n… (mensaje truncado)"

    url = API_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    for modo in (parse_mode, None):
        data = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        if modo:
            data["parse_mode"] = modo
        try:
            resp = requests.post(url, data=data, timeout=10)
        except Exception:
            log.exception("Fallo de red enviando mensaje a Telegram")
            return False
        if resp.ok:
            return True
        log.warning("Telegram rechazó el mensaje (parse_mode=%s, HTTP %s): %s",
                    modo, resp.status_code, resp.text[:200])
        if modo is None:
            break
    return False


def _num(v) -> str:
    """%.6g mantiene legibles tanto BTC (79380) como tokens de precio muy
    bajo (0.000012). Con .4f estos últimos salían como 0.0000."""
    try:
        return f"{float(v):.6g}"
    except (TypeError, ValueError):
        return str(v)


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
