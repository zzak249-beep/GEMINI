"""
config.py
---------
Carga toda la configuración del bot desde variables de entorno.

En local, puedes crear un archivo `.env` (copia `.env.example`) y se cargará
automáticamente. En Railway, estas variables se configuran en:
Project -> Service -> Variables.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # No hace nada si no existe .env (p. ej. en Railway), es seguro.


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


@dataclass
class Settings:
    # --- Credenciales BingX ---
    bingx_api_key: str
    bingx_api_secret: str
    bingx_demo: bool  # True = opera en la red de pruebas VST (dinero ficticio) de BingX

    # --- Credenciales Telegram ---
    telegram_bot_token: str
    telegram_chat_id: str

    # --- Mercado ---
    symbol: str  # p. ej. "BTC/USDT"
    timeframe: str  # p. ej. "15m"
    candles_lookback: int  # nº de velas históricas a pedir en cada ciclo

    # --- Gestión de capital ---
    trade_amount_usdt: float  # USDT a gastar en cada compra
    min_position_value_usdt: float  # por debajo de esto, se considera que no hay posición abierta

    # --- Parámetros de la estrategia (idénticos al Pine Script original) ---
    rsi_length: int
    signal_length: int
    trigger_level: float
    target_cross_count: int
    atr_period: int
    st_factor: float

    # --- Operativa ---
    dry_run: bool  # True = solo analiza y avisa por Telegram, NO envía órdenes reales
    poll_buffer_seconds: int  # segundos de margen tras el cierre de vela antes de pedir datos
    log_level: str

    # --- Fiabilidad ---
    max_retries: int  # reintentos ante errores de red transitorios al hablar con BingX
    retry_backoff_seconds: float  # espera inicial entre reintentos (crece exponencialmente)
    error_notify_cooldown_minutes: int  # no repetir el mismo aviso de error en Telegram antes de este tiempo

    # --- Gestión de riesgo (opcional, desactivado por defecto = comportamiento idéntico al Pine original) ---
    stop_loss_pct: float  # 0 = desactivado. Si >0, cierra la posición si el precio cae este % desde la entrada

    # --- Observabilidad ---
    heartbeat_every_hours: float  # 0 = desactivado. Envía un resumen de "sigo vivo" cada N horas

    # --- Comandos remotos por Telegram ---
    telegram_commands_enabled: bool  # /status, /pause, /resume, /close


def load_settings() -> Settings:
    missing = []

    def require(name: str) -> str:
        val = os.getenv(name)
        if not val:
            missing.append(name)
        return val or ""

    bingx_api_key = require("BINGX_API_KEY")
    bingx_api_secret = require("BINGX_API_SECRET")
    telegram_bot_token = require("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = require("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Faltan variables de entorno obligatorias: "
            + ", ".join(missing)
            + ". Revisa tu archivo .env (local) o las Variables del servicio en Railway."
        )

    return Settings(
        bingx_api_key=bingx_api_key,
        bingx_api_secret=bingx_api_secret,
        bingx_demo=_get_bool("BINGX_DEMO", True),
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        symbol=os.getenv("SYMBOL", "BTC/USDT"),
        timeframe=os.getenv("TIMEFRAME", "15m"),
        candles_lookback=_get_int("CANDLES_LOOKBACK", 300),
        trade_amount_usdt=_get_float("TRADE_AMOUNT_USDT", 100.0),
        min_position_value_usdt=_get_float("MIN_POSITION_VALUE_USDT", 5.0),
        rsi_length=_get_int("RSI_LENGTH", 10),
        signal_length=_get_int("SIGNAL_LENGTH", 10),
        trigger_level=_get_float("TRIGGER_LEVEL", 50.0),
        target_cross_count=_get_int("TARGET_CROSS_COUNT", 2),
        atr_period=_get_int("ATR_PERIOD", 10),
        st_factor=_get_float("ST_FACTOR", 2.5),
        dry_run=_get_bool("DRY_RUN", True),
        poll_buffer_seconds=_get_int("POLL_BUFFER_SECONDS", 20),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        max_retries=_get_int("MAX_RETRIES", 3),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 2.0),
        error_notify_cooldown_minutes=_get_int("ERROR_NOTIFY_COOLDOWN_MINUTES", 15),
        stop_loss_pct=_get_float("STOP_LOSS_PCT", 0.0),
        heartbeat_every_hours=_get_float("HEARTBEAT_EVERY_HOURS", 24.0),
        telegram_commands_enabled=_get_bool("TELEGRAM_COMMANDS_ENABLED", True),
    )
