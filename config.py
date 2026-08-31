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
        # SYMBOLS acepta "ALL" (escanea todos los perpetuos USDT-M de BingX)
        # o una lista separada por comas, ej: "BTC-USDT,ETH-USDT,SOL-USDT".
        # Si no está definida, cae en el antiguo SYMBOL (compatibilidad).
        self.SYMBOLS = _get_str("SYMBOLS", default=None)
        if self.SYMBOLS is None:
            self.SYMBOLS = _get_str("SYMBOL", default="BTC-USDT")
        self.SCAN_ALL_SYMBOLS = self.SYMBOLS.strip().upper() == "ALL"
        self.QUOTE_ASSET_FILTER = _get_str("QUOTE_ASSET_FILTER", default="USDT")
        self.EXCLUDED_SYMBOLS = [
            s.strip() for s in _get_str("EXCLUDED_SYMBOLS", default="").split(",") if s.strip()
        ]
        self.SYMBOL_REFRESH_HOURS = _get_float("SYMBOL_REFRESH_HOURS", default=24.0)
        self.SCAN_CONCURRENCY = _get_int("SCAN_CONCURRENCY", default=8)

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

        # --- Filtro de tendencia (de "Higher-Low tras Ruptura + EMA50") ---
        # No toma el Doble Dip si el precio sigue en una base sin confirmar:
        # exige EMA de tendencia subiendo Y ruptura ya confirmada de la
        # directriz bajista de la base previa. Actívalo/desactívalo para
        # comparar con backtest.py --no-trend-filter.
        self.TREND_FILTER_ENABLED = _get_bool("TREND_FILTER_ENABLED", default=True)
        self.TREND_EMA_LENGTH = _get_int("TREND_EMA_LENGTH", default=50)
        self.TREND_EMA_SLOPE_LOOKBACK = _get_int("TREND_EMA_SLOPE_LOOKBACK", default=5)
        self.TREND_PIVOT_LEFT = _get_int("TREND_PIVOT_LEFT", default=5)
        self.TREND_PIVOT_RIGHT = _get_int("TREND_PIVOT_RIGHT", default=5)
        self.TREND_MAX_BARS_AFTER_BREAK = _get_int("TREND_MAX_BARS_AFTER_BREAK", default=30)

        # --- Gestión de riesgo ---
        self.LEVERAGE = _get_int("LEVERAGE", default=3)
        # OJO: con varios símbolos abiertos a la vez esto se APILA. 5% x 5
        # posiciones simultáneas = 25% del equity en margen. Con "ALL"
        # activo, el valor por defecto baja de 20% (versión single-symbol)
        # a 5% a propósito - súbelo con conocimiento de causa.
        self.POSITION_SIZE_PCT = _get_float("POSITION_SIZE_PCT", default=5.0)
        self.STOP_LOSS_PCT = _get_float("STOP_LOSS_PCT", default=5.0)  # 0 = desactivado

        # Techo duro de posiciones abiertas simultáneas (across todos los
        # símbolos). Es lo único que evita comprometer >100% del equity si
        # muchos símbolos dan señal el mismo ciclo.
        self.MAX_CONCURRENT_POSITIONS = _get_int("MAX_CONCURRENT_POSITIONS", default=5)
        # Tras cerrar una posición en un símbolo, minutos de espera antes de
        # poder volver a entrar en ese mismo símbolo (evita re-entradas
        # inmediatas en un mercado lateral/picado).
        self.SYMBOL_COOLDOWN_MINUTES = _get_float("SYMBOL_COOLDOWN_MINUTES", default=60.0)
        # Circuit breaker global: tras N cierres en pérdida SEGUIDOS (en
        # cualquier símbolo), se pausan nuevas entradas un rato. Las salidas
        # de posiciones ya abiertas NUNCA se pausan por esto.
        self.MAX_CONSECUTIVE_LOSSES = _get_int("MAX_CONSECUTIVE_LOSSES", default=5)
        self.CIRCUIT_BREAKER_COOLDOWN_MINUTES = _get_float("CIRCUIT_BREAKER_COOLDOWN_MINUTES", default=120.0)

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
            "trend_filter_enabled": self.TREND_FILTER_ENABLED,
            "trend_ema_length": self.TREND_EMA_LENGTH,
            "trend_ema_slope_lookback": self.TREND_EMA_SLOPE_LOOKBACK,
            "trend_pivot_left": self.TREND_PIVOT_LEFT,
            "trend_pivot_right": self.TREND_PIVOT_RIGHT,
            "trend_max_bars_after_break": self.TREND_MAX_BARS_AFTER_BREAK,
        }

    def summary(self) -> str:
        modo = "DRY-RUN (simulado)" if self.DRY_RUN else "REAL (dinero real)"
        universo = (
            f"TODAS las monedas {self.QUOTE_ASSET_FILTER}-M"
            if self.SCAN_ALL_SYMBOLS
            else self.SYMBOLS
        )
        excl = f" | excluidos: {', '.join(self.EXCLUDED_SYMBOLS)}" if self.EXCLUDED_SYMBOLS else ""
        filtro = (
            f"Filtro de tendencia: ACTIVO (EMA{self.TREND_EMA_LENGTH} subiendo + ruptura de base confirmada)"
            if self.TREND_FILTER_ENABLED
            else "Filtro de tendencia: desactivado (Doble Dip puro, como el Pine original)"
        )
        return (
            f"Universo: {universo}{excl} | TF: {self.TIMEFRAME}\n"
            f"RSI({self.RSI_LENGTH}) / Señal SMA({self.SIG_LENGTH}) / "
            f"Trigger {self.TRIGGER_LEVEL} / Doble cruce #{self.TARGET_CROSS_COUNT}\n"
            f"SuperTrend ATR({self.ATR_PERIOD}) x{self.ST_FACTOR}\n"
            f"{filtro}\n"
            f"Apalancamiento: {self.LEVERAGE}x | Tamaño por operación: {self.POSITION_SIZE_PCT}% del balance\n"
            f"Máx. posiciones simultáneas: {self.MAX_CONCURRENT_POSITIONS} "
            f"(exposición máx. teórica en margen: {self.MAX_CONCURRENT_POSITIONS * self.POSITION_SIZE_PCT:.0f}% del balance)\n"
            f"Stop-loss de seguridad: {self.STOP_LOSS_PCT}% "
            f"{'(desactivado)' if self.STOP_LOSS_PCT <= 0 else ''}\n"
            f"Cooldown por símbolo: {self.SYMBOL_COOLDOWN_MINUTES:.0f} min | "
            f"Circuit breaker: {self.MAX_CONSECUTIVE_LOSSES} pérdidas seguidas -> "
            f"pausa {self.CIRCUIT_BREAKER_COOLDOWN_MINUTES:.0f} min\n"
            f"Modo: {modo}"
        )
