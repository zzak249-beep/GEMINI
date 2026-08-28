"""
config.py
=========
Carga la configuración desde variables de entorno. Parseo defensivo:
quita comillas, espacios y comentarios tipo "# nota" al final del valor
(bug ya visto en Railway: caracteres sueltos rompiendo el parseo numérico).
"""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()  # no-op si no existe un .env (p.ej. en Railway)
except ImportError:
    pass


def _clean(raw: str) -> str:
    val = raw.split("#", 1)[0].strip()
    return val.strip("'\"")


def _get_str(name: str, default: str | None = None, required: bool = False) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if required:
            raise RuntimeError(f"Falta la variable de entorno obligatoria: {name}")
        return default
    return _clean(raw)


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(_clean(raw))


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(_clean(raw))


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return _clean(raw).lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self):
        # --- Credenciales / notificaciones (obligatorias) ---
        self.BINGX_API_KEY = _get_str("BINGX_API_KEY", required=True)
        self.BINGX_API_SECRET = _get_str("BINGX_API_SECRET", required=True)
        self.TELEGRAM_BOT_TOKEN = _get_str("TELEGRAM_BOT_TOKEN", required=True)
        self.TELEGRAM_CHAT_ID = _get_str("TELEGRAM_CHAT_ID", required=True)

        # --- Mercado ---
        self.SYMBOL = _get_str("SYMBOL", default="BTC-USDT")
        self.TIMEFRAME = _get_str("TIMEFRAME", default="15m")
        self.TIMEFRAME_MINUTES = _get_int("TIMEFRAME_MINUTES", default=15)
        self.HISTORY_CANDLES = _get_int("HISTORY_CANDLES", default=500)

        # --- Parámetros de la estrategia (idénticos al Pine Script fuente) ---
        self.RSI_LENGTH = _get_int("RSI_LENGTH", default=10)
        self.SIG_LENGTH = _get_int("SIG_LENGTH", default=10)
        self.TRIGGER_LEVEL = _get_float("TRIGGER_LEVEL", default=50.0)
        self.TARGET_CROSS_COUNT = _get_int("TARGET_CROSS_COUNT", default=2)
        self.ATR_PERIOD = _get_int("ATR_PERIOD", default=10)
        self.ST_FACTOR = _get_float("ST_FACTOR", default=2.5)

        # --- Gestión de riesgo (no viene en el Pine, se añade para operar real) ---
        self.LEVERAGE = _get_int("LEVERAGE", default=3)
        self.POSITION_SIZE_PCT = _get_float("POSITION_SIZE_PCT", default=20.0)
        self.STOP_LOSS_PCT = _get_float("STOP_LOSS_PCT", default=5.0)  # 0 = desactivado

        # --- Operación ---
        self.DRY_RUN = _get_bool("DRY_RUN", default=True)
        self.POLL_BUFFER_SECONDS = _get_int("POLL_BUFFER_SECONDS", default=8)
        self.PORT = _get_int("PORT", default=8080)
        self.LOG_LEVEL = _get_str("LOG_LEVEL", default="INFO")

    def strategy_params(self) -> dict:
        return {
            "rsi_length": self.RSI_LENGTH,
            "sig_length": self.SIG_LENGTH,
            "trigger_level": self.TRIGGER_LEVEL,
            "target_cross_count": self.TARGET_CROSS_COUNT,
            "atr_period": self.ATR_PERIOD,
            "st_factor": self.ST_FACTOR,
        }

    def summary(self) -> str:
        modo = "DRY-RUN (simulado)" if self.DRY_RUN else "REAL (dinero real)"
        return (
            f"Símbolo: {self.SYMBOL} | TF: {self.TIMEFRAME}\n"
            f"RSI({self.RSI_LENGTH}) / Señal SMA({self.SIG_LENGTH}) / "
            f"Trigger {self.TRIGGER_LEVEL} / Doble cruce #{self.TARGET_CROSS_COUNT}\n"
            f"SuperTrend ATR({self.ATR_PERIOD}) x{self.ST_FACTOR}\n"
            f"Apalancamiento: {self.LEVERAGE}x | Tamaño posición: {self.POSITION_SIZE_PCT}% del balance\n"
            f"Stop-loss de seguridad: {self.STOP_LOSS_PCT}% "
            f"{'(desactivado)' if self.STOP_LOSS_PCT <= 0 else ''}\n"
            f"Modo: {modo}"
        )
