"""
Configuración centralizada. Todo se lee de variables de entorno
(Railway → Variables). Ver .env.example para la lista completa.
"""
import os


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _str(name, default=""):
    """Lee una variable de entorno y le quita espacios/saltos de línea
    accidentales (Railway/copiar-pegar suele dejar un '\\n' al final,
    lo que rompe cabeceras HTTP como X-BX-APIKEY con un ValueError)."""
    return os.getenv(name, default).strip()


# --- BingX ---
BINGX_API_KEY = _str("BINGX_API_KEY")
BINGX_API_SECRET = _str("BINGX_API_SECRET")
BINGX_BASE_URL = _str("BINGX_BASE_URL", "https://open-api.bingx.com")
BINGX_DEMO = _bool("BINGX_DEMO", "true")  # usa VST (demo trading) por defecto

# --- Telegram ---
TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")

# --- Webhook ---
WEBHOOK_SECRET = _str("WEBHOOK_SECRET")  # token que añades a la URL de la alerta

# --- Modo de operación ---
# AUTO_TRADE=false  -> solo reenvía la señal a Telegram, no ejecuta nada (modo manual)
# AUTO_TRADE=true   -> ejecuta la orden en BingX y además avisa por Telegram
AUTO_TRADE = _bool("AUTO_TRADE", "false")

# --- Riesgo / cuenta ---
RISK_PCT_PER_TRADE = float(os.getenv("RISK_PCT_PER_TRADE", "2.0"))  # % del equity arriesgado (via SL)
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))
# Tope de seguridad ABSOLUTO, independiente de MAX_CONCURRENT_POSITIONS y del
# estado local (que puede resetearse si Railway no tiene un Volume montado
# para STATE_FILE). Se comprueba contra las posiciones REALES en BingX, no
# contra el JSON local, así que protege incluso si el estado se perdió.
HARD_MAX_TOTAL_POSITIONS = int(os.getenv("HARD_MAX_TOTAL_POSITIONS", "5"))

# Volumen mínimo en USDT de las últimas 24h para que un símbolo se vigile
# en modo SYMBOLS=ALL. Filtra alts ilíquidos donde el spread/slippage real
# se come cualquier ventaja del filtro antes de que se mueva el precio
# (ver RESEARCH.md sección 5). No aplica si SYMBOLS es una lista explícita.
MIN_24H_VOLUME_USDT = float(os.getenv("MIN_24H_VOLUME_USDT", "2000000"))

# --- Circuit breaker ---
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
MAX_DAILY_DRAWDOWN_PCT = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "6.0"))

# --- Persistencia ---
STATE_FILE = _str("STATE_FILE", "state.json")

# --- Origen de la señal ---
# "python": el propio bot calcula la señal wavelet leyendo velas de BingX
#           cada 5 minutos (NO necesita plan de pago de TradingView).
# "tradingview": usa el webhook de TradingView como hasta ahora (requiere
#                plan Essential o superior para alertas por webhook).
SIGNAL_SOURCE = _str("SIGNAL_SOURCE", "python")
ENABLE_SCHEDULER = _bool("ENABLE_SCHEDULER", "true")  # los tests lo ponen a false

# Símbolos a vigilar en formato BingX (BASE-QUOTE), separados por coma.
# Escribe "ALL" para que el bot descubra y vigile TODOS los perpetuos
# USDT-margin de BingX automáticamente (ver scanner.py / poller.py).
_symbols_raw = _str("SYMBOLS", "BTC-USDT")
SCAN_ALL_SYMBOLS = _symbols_raw.strip().upper() == "ALL"
SYMBOLS = [] if SCAN_ALL_SYMBOLS else [s.strip().upper() for s in _symbols_raw.split(",") if s.strip()]

# Cuántos símbolos como máximo procesa un ciclo del escaneo "ALL" (evita
# ciclos de 5 min que tarden demasiado si BingX tiene cientos de perpetuos).
SCAN_ALL_MAX_SYMBOLS = int(_str("SCAN_ALL_MAX_SYMBOLS", "150"))
# Cada cuántas horas se refresca la lista de símbolos en modo "ALL"
SCAN_ALL_REFRESH_HOURS = float(_str("SCAN_ALL_REFRESH_HOURS", "6"))

# Resumen periódico por Telegram del estado del filtro en todo el universo
# vigilado (puramente informativo, no ejecuta nada)
SCAN_REPORT_ENABLED = _bool("SCAN_REPORT_ENABLED", "false")
SCAN_REPORT_INTERVAL_HOURS = float(_str("SCAN_REPORT_INTERVAL_HOURS", "4"))

# --- Parámetros del filtro Wavelet MRA Haar (equivalentes a los inputs del Pine) ---
WAVELET_LOOKBACK_ENERGY = int(_str("WAVELET_LOOKBACK_ENERGY", "40"))
WAVELET_K_DOMINANCE = float(_str("WAVELET_K_DOMINANCE", "1.5"))
WAVELET_COOLDOWN_BARS = int(_str("WAVELET_COOLDOWN_BARS", "4"))
WAVELET_ATR_LENGTH = int(_str("WAVELET_ATR_LENGTH", "14"))
WAVELET_ATR_MULT_SL = float(_str("WAVELET_ATR_MULT_SL", "1.5"))
WAVELET_ATR_MULT_TP = float(_str("WAVELET_ATR_MULT_TP", "2.5"))

# --- Mapeo símbolo TradingView -> BingX ---
# TradingView suele mandar "BTCUSDT" o "BTCUSDT.P". BingX quiere "BTC-USDT".
def tv_symbol_to_bingx(tv_symbol: str) -> str:
    s = tv_symbol.upper().replace(".P", "").replace("PERP", "")
    if "-" in s:
        return s
    # separar el par asumiendo que termina en USDT/USDC/USD
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            base = s[: -len(quote)]
            return f"{base}-{quote}"
    return s
