"""
config.py — Configuración del bot, leída de variables de entorno (Railway).

⚠️ ESTE ARCHIVO ES UNA RECONSTRUCCIÓN, NO EL ORIGINAL. ⚠️
No tenía tu config.py real: solo el bloque final ("Mejoras medidas...").
El resto se ha reconstruido buscando cada `config.ALGO` en main.py,
poller.py, bingx_client.py, scanner.py, state_manager.py y
telegram_notifier.py, y cruzándolo con railway_vars_15m.txt.

ANTES DE DESPLEGAR ESTO EN PRODUCCIÓN, revisa en especial:

  1. tv_symbol_to_bingx() -- su lógica NO aparece en ningún archivo que
     me pasaste, solo su nombre y dos llamadas. Lo de abajo es una
     implementación razonable (quita sufijos de TradingView, añade el
     guión BASE-USDT), pero es una suposición. Pruébala con un payload
     real de tu alerta de TradingView antes de fiarte con AUTO_TRADE=true.
  2. WEBHOOK_SECRET -- no tiene default a propósito (debe venir de
     Railway). Si no lo defines, main.py ya rehúsa arrancar en real.
  3. MIN_STOP_DISTANCE_PCT, MAX_NOTIONAL_PCT_EQUITY,
     SCAN_REPORT_INTERVAL_HOURS -- no estaban en railway_vars_15m.txt.
     Los dejo en un valor que los DESACTIVA (0), que es el lado seguro,
     pero puede que tú los tuvieras con un valor real. Compruébalo.
  4. Cualquier otra variable que uses en tu propio signal_engine.py (no
     me lo subiste) y que dependa de config -- revísala aparte.

Lo más seguro sigue siendo que pegues aquí tu config.py real si lo
recuperas (git log, otro despliegue, Railway "Source" tab) en vez de
confiar en esta reconstrucción para las partes marcadas arriba.
"""
from __future__ import annotations

import os


# --------------------------------------------------------------------------- #
# Helpers de lectura de entorno
# --------------------------------------------------------------------------- #
def _str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _bool(key: str, default: str) -> bool:
    """'true'/'1'/'yes'/'si'/'sí'/'on' (sin distinguir mayúsculas) = True."""
    return _str(key, default).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _int(key: str, default: str) -> int:
    try:
        return int(float(_str(key, default)))
    except (TypeError, ValueError):
        return int(float(default))


def _float(key: str, default: str) -> float:
    try:
        return float(_str(key, default))
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------- #
# BingX
# --------------------------------------------------------------------------- #
BINGX_API_KEY = _str("BINGX_API_KEY", "")
BINGX_API_SECRET = _str("BINGX_API_SECRET", "")
BINGX_BASE_URL = _str("BINGX_BASE_URL", "https://open-api.bingx.com")
BINGX_DEMO = _bool("BINGX_DEMO", "false")

# One-Way vs Hedge. bingx_client.py ya trae su propio fallback (True) por
# si esta variable no existiera, pero se define aquí para que sea visible.
HEDGE_MODE = _bool("HEDGE_MODE", "true")

# bingx_client.py: prefijos de símbolo a excluir del universo escaneable
# (ej. tokens apalancados "NC..."). Tupla de strings en mayúsculas.
EXCLUDE_PREFIXES = tuple(
    p.strip().upper() for p in _str("EXCLUDE_PREFIXES", "").split(",") if p.strip()
)

# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID", "")

# Coste de ida y vuelta en % del nocional. telegram_notifier.py ya tiene su
# propio default (0.25) vía getattr si esto no existiera; se deja igual
# aquí para que ambos coincidan siempre.
COST_ROUNDTRIP_PCT = _float("COST_ROUNDTRIP_PCT", "0.25")

# --------------------------------------------------------------------------- #
# Webhook (TradingView -> Flask)
# --------------------------------------------------------------------------- #
# SIN DEFAULT A PROPÓSITO. main.py se niega a arrancar en real
# (AUTO_TRADE=true, BINGX_DEMO=false) si esto está vacío.
WEBHOOK_SECRET = _str("WEBHOOK_SECRET", "")

# --------------------------------------------------------------------------- #
# Operativa general
# --------------------------------------------------------------------------- #
AUTO_TRADE = _bool("AUTO_TRADE", "false")
SIGNAL_SOURCE = _str("SIGNAL_SOURCE", "python")   # "python" | "tradingview"
ENABLE_SCHEDULER = _bool("ENABLE_SCHEDULER", "true")
SIGNAL_TIMEFRAME = _str("SIGNAL_TIMEFRAME", "5m")
SIGNAL_SECOND_OFFSET = _int("SIGNAL_SECOND_OFFSET", "40")

# Persistencia (posiciones, circuit breaker, cooldown). Debe ser una ruta
# ABSOLUTA sobre un Volume de Railway, o se pierde en cada redeploy.
STATE_FILE = _str("STATE_FILE", "/data/state.json")

# --------------------------------------------------------------------------- #
# Universo de símbolos
# --------------------------------------------------------------------------- #
# SYMBOLS=ALL -> escanea todo el universo de perpetuos USDT (ver poller.py /
# SCAN_ALL_*). Cualquier otro valor -> lista separada por comas, formato
# BASE-QUOTE (ej. "BTC-USDT,ETH-USDT").
_symbols_raw = _str("SYMBOLS", "").strip()
SCAN_ALL_SYMBOLS = _symbols_raw.upper() == "ALL"
SYMBOLS = (
    []
    if SCAN_ALL_SYMBOLS
    else [s.strip().upper() for s in _symbols_raw.split(",") if s.strip()]
)

SCAN_ALL_MAX_SYMBOLS = _int("SCAN_ALL_MAX_SYMBOLS", "150")
SCAN_ALL_REFRESH_HOURS = _float("SCAN_ALL_REFRESH_HOURS", "6")
MIN_24H_VOLUME_USDT = _float("MIN_24H_VOLUME_USDT", "2000000")

# Informe periódico de escaneo (poller.py). Si está activado, cada cuántas
# horas se manda -- no estaba en railway_vars_15m.txt (ahí venía
# SCAN_REPORT_ENABLED=false), así que este número no importa por defecto.
SCAN_REPORT_ENABLED = _bool("SCAN_REPORT_ENABLED", "false")
SCAN_REPORT_INTERVAL_HOURS = _float("SCAN_REPORT_INTERVAL_HOURS", "6")

# --------------------------------------------------------------------------- #
# Wavelet MRA Haar — parámetros del motor de señal
# --------------------------------------------------------------------------- #
WAVELET_LOOKBACK_ENERGY = _int("WAVELET_LOOKBACK_ENERGY", "40")
WAVELET_K_DOMINANCE = _float("WAVELET_K_DOMINANCE", "1.5")
WAVELET_COOLDOWN_BARS = _int("WAVELET_COOLDOWN_BARS", "4")
WAVELET_ATR_LENGTH = _int("WAVELET_ATR_LENGTH", "14")
WAVELET_ATR_MULT_SL = _float("WAVELET_ATR_MULT_SL", "1.5")
WAVELET_ATR_MULT_TP = _float("WAVELET_ATR_MULT_TP", "2.5")

# --------------------------------------------------------------------------- #
# Dimensionado y riesgo
# --------------------------------------------------------------------------- #
RISK_PCT_PER_TRADE = _float("RISK_PCT_PER_TRADE", "2.0")
MIN_NOTIONAL_USDT = _float("MIN_NOTIONAL_USDT", "9.0")
MAX_RISK_PCT_ABS = _float("MAX_RISK_PCT_ABS", "4.0")
MARGIN_PER_TRADE_USDT = _float("MARGIN_PER_TRADE_USDT", "0")
LEVERAGE = _int("LEVERAGE", "10")
REJECT_HIGHER_LEVERAGE = _bool("REJECT_HIGHER_LEVERAGE", "true")

# No estaban en railway_vars_15m.txt. 0 = desactivado (comportamiento
# seguro por defecto: main.py solo los aplica si son "truthy").
MIN_STOP_DISTANCE_PCT = _float("MIN_STOP_DISTANCE_PCT", "0")
MAX_NOTIONAL_PCT_EQUITY = _float("MAX_NOTIONAL_PCT_EQUITY", "0")

# --------------------------------------------------------------------------- #
# Límites de posiciones y circuit breaker
# --------------------------------------------------------------------------- #
MAX_CONCURRENT_POSITIONS = _int("MAX_CONCURRENT_POSITIONS", "1")
HARD_MAX_TOTAL_POSITIONS = _int("HARD_MAX_TOTAL_POSITIONS", "3")
MAX_CONSECUTIVE_LOSSES = _int("MAX_CONSECUTIVE_LOSSES", "4")
MAX_DAILY_DRAWDOWN_PCT = _float("MAX_DAILY_DRAWDOWN_PCT", "6.0")


# --------------------------------------------------------------------------- #
# Conversión de símbolo TradingView -> BingX
# --------------------------------------------------------------------------- #
# ⚠️ RECONSTRUIDA, NO ORIGINAL. Solo tenía el nombre de la función y sus
# dos llamadas (main.py:317, main.py:697); nunca vi su cuerpo real.
# Asume payloads tipo "BTCUSDT", "BTCUSDT.P" o "BINANCE:BTCUSDT.P" y los
# convierte al formato BASE-USDT que usa el resto del bot. Si tu alerta de
# TradingView manda algo distinto, esto dará un símbolo que BingX no
# reconoce -- lo verás como "Símbolos en SYMBOLS que BingX no reconoce"
# o como un rechazo silencioso al ejecutar. PRUÉBALA ANTES DE CONFIAR EN
# ELLA CON AUTO_TRADE=true.
_TV_SUFFIXES = (".P", "PERP", "-PERP", "_PERP", ".PS")


def tv_symbol_to_bingx(tv_symbol: str) -> str:
    s = (tv_symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for suf in _TV_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    if "-" in s:
        return s
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}-{quote}"
    return s


# --------------------------------------------------------------------------- #
# Mejoras medidas sobre el histórico de 128 operaciones
# --------------------------------------------------------------------------- #
# 1) SIN TP NO SE OPERA. El ratio realizado fue 0,54 frente al 1,67 de
#    diseño (SL 1.5 ATR / TP 2.5 ATR). Con 46% de aciertos, 1,67 da
#    +0,23 R por operación y 0,54 da -0,29: la diferencia entre ganar y
#    perder. Una posición con stop y sin objetivo tiene pago asimétrico
#    —la pérdida se corta, la ganancia no se cobra— y antes solo se
#    avisaba por Telegram dejándola abierta.
CERRAR_SIN_TP = _bool("CERRAR_SIN_TP", "true")

# 2) TOPE DIRECCIONAL. El 09/09 salieron nueve señales LONG seguidas. En
#    un desplome las alts se mueven juntas: nueve largos no son nueve
#    apuestas, son una repetida nueve veces. 0 = desactivado.
MAX_MISMA_DIRECCION = int(_str("MAX_MISMA_DIRECCION", "2"))

# 3) PATRIMONIO MÍNIMO. Con equity 0 el dimensionado por riesgo da
#    cantidad 0 y la señal moría con un "qty tras redondeo es 0" que
#    parece un problema de precisión y no lo es. Peor:
#    check_circuit_breaker(0) no puede medir drawdown, así que el límite
#    diario quedaba desactivado sin avisar.
EQUITY_MINIMO = float(_str("EQUITY_MINIMO", "5"))
