"""
Configuración centralizada. Todo se lee de variables de entorno
(Railway → Variables). Ver .env.example para la lista completa.

OJO con los nombres: este bot NO lee DRY_RUN, MODE ni LIVE_CONFIRMED
(esas son de otros bots de la flota). Los únicos interruptores reales
son AUTO_TRADE y BINGX_DEMO.
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

# Modo de posición de la cuenta. HEDGE (por defecto) manda positionSide
# explícito y NUNCA el campo reduceOnly, que BingX rechaza con el error
# 109400 en ese modo. Ponlo a false solo si la cuenta está en One-Way.
HEDGE_MODE = _bool("HEDGE_MODE", "true")

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

# Suelo de tamaño: valor MÍNIMO de la posición en USDT (nocional = qty ×
# precio), NO margen. Con LEVERAGE=10, 9 USDT de nocional inmovilizan 0.9
# USDT de margen.
#
# Por qué hace falta: el sizing por riesgo da nocional = riesgo / stop%.
# En un símbolo con stop del 15% eso deja posiciones de céntimos, donde
# las comisiones se comen cualquier resultado. Ponlo a 0 para desactivarlo.
MIN_NOTIONAL_USDT = float(os.getenv("MIN_NOTIONAL_USDT", "9.0"))

# Tope duro al subir hasta el suelo anterior: forzar el mínimo en un
# símbolo de stop muy ancho puede arriesgar mucho más que
# RISK_PCT_PER_TRADE. Si para llegar al mínimo hay que superar este % del
# equity, la señal se DESCARTA en vez de operarse con un riesgo que no
# habías autorizado.
MAX_RISK_PCT_ABS = float(os.getenv("MAX_RISK_PCT_ABS", "4.0"))

# MARGEN FIJO por operación, en USDT. 0 = desactivado (comportamiento
# por defecto: dimensionar por riesgo).
#
# Este bot dimensiona por RIESGO: qty = (equity x RISK_PCT_PER_TRADE) /
# distancia_al_SL. Eso mantiene constante lo que se pierde si salta el
# stop, pero hace que el MARGEN varíe en cada operación -- en símbolos
# de stop estrecho salen posiciones grandes y al revés.
#
# Con MARGIN_PER_TRADE_USDT > 0 se invierte el criterio: el margen es
# fijo (nocional = margen x LEVERAGE) y lo que varía es el riesgo, que
# pasa a depender de dónde caiga el stop. Sigue acotado por
# MAX_RISK_PCT_ABS: si con ese tamaño la pérdida en el SL superaría ese
# % del equity, la señal se descarta.
MARGIN_PER_TRADE_USDT = float(os.getenv("MARGIN_PER_TRADE_USDT", "0"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))

# --- Frenos aprendidos de RIVER (-21.04 USDT, 7x el riesgo previsto) ---
#
# 1) Apalancamiento REAL mayor del pedido. BingX puede dejar el simbolo al
#    apalancamiento que ya tuviera si set_leverage no lo baja. Eso acerca
#    la liquidacion (a 19x cae a ~4.8% en contra, a 10x a ~9.5%) Y ADEMAS
#    relaja la comprobacion de margen de main.py, porque required_margin
#    se divide por el apalancamiento real. RIVER se liquido con un
#    movimiento del 4.40%: perdio el 101% de su margen.
#    true = no se opera ese simbolo si BingX confirma mas del pedido.
REJECT_HIGHER_LEVERAGE = _bool("REJECT_HIGHER_LEVERAGE", "true")

# 2) Tope de NOCIONAL por posicion, en % del equity. El sizing por riesgo
#    calcula qty = riesgo / distancia_al_stop, asi que con un stop muy
#    estrecho el nocional se dispara: RIVER movio 396 USDT sobre un
#    patrimonio de ~150, o sea 2.6 VECES la cuenta en un solo simbolo.
#    0 = desactivado.
#    MEDIDO antes de fijar el valor: las operaciones reales del bot corren
#    entre 1.8x y 2.8x el equity, y RIVER estaba en 2.6x -- DENTRO de ese
#    rango. Asi que este tope NO distingue RIVER de una operacion normal.
#    Se deja en 400% como guarda de cola pura: solo ataja un nocional
#    absurdo, sin tocar nada de lo que el bot opera hoy. Bajarlo a 150
#    habria bloqueado ETHFI (2.8x) y FF (1.8x), que eran legitimas.
MAX_NOTIONAL_PCT_EQUITY = float(os.getenv("MAX_NOTIONAL_PCT_EQUITY", "400"))

# 3) Distancia MINIMA al stop, en % del precio. Es el otro lado del mismo
#    problema: un stop al 0.76% (el de RIVER) produce nocionales enormes y
#    ademas deja el coste pesando demasiado. 0 = desactivado.
#    DESACTIVADO por defecto (0). Con 0.60% habria bloqueado operaciones
#    normales del bot, y el stop estrecho NO fue la causa de RIVER: la
#    causa fue el apalancamiento de 19x. Actívalo solo si mides que los
#    stops estrechos pierden mas, no por precaucion generica.
MIN_STOP_DISTANCE_PCT = float(os.getenv("MIN_STOP_DISTANCE_PCT", "0"))
# Tope de seguridad ABSOLUTO, independiente de MAX_CONCURRENT_POSITIONS y del
# estado local (que puede resetearse si Railway no tiene un Volume montado
# para STATE_FILE). Se comprueba contra las posiciones REALES en BingX que
# sean DE ESTE BOT -- no contra toda la cuenta: la comparten otros bots y
# operativa manual, y contarlo todo bloqueaba este bot por posiciones ajenas.
HARD_MAX_TOTAL_POSITIONS = int(os.getenv("HARD_MAX_TOTAL_POSITIONS", "5"))

# Volumen mínimo en USDT de las últimas 24h para que un símbolo se vigile
# en modo SYMBOLS=ALL. Filtra alts ilíquidos donde el spread/slippage real
# se come cualquier ventaja del filtro antes de que se mueva el precio
# (ver RESEARCH.md sección 5). No aplica si SYMBOLS es una lista explícita.
MIN_24H_VOLUME_USDT = float(os.getenv("MIN_24H_VOLUME_USDT", "2000000"))

# Prefijos de símbolo a excluir del universo (BASE del par, separados por
# coma). Ej. EXCLUDE_PREFIXES="NC" descarta NCCOGOLD2USD-USDT y compañía.
# Esta variable existía en Railway pero NADIE la leía en este repo, así que
# no filtraba nada: ahora la aplica bingx_client.get_all_symbols().
EXCLUDE_PREFIXES = tuple(
    p.strip().upper() for p in _str("EXCLUDE_PREFIXES", "").split(",") if p.strip()
)

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

# Temporalidad de las velas y cada cuántos minutos barre el poller. Deben
# coincidir: con velas de 5m y un barrido cada 15 se perderían señales, y
# al revés se evaluaría varias veces la misma vela.
SIGNAL_TIMEFRAME = _str("SIGNAL_TIMEFRAME", _str("TIMEFRAME", "5m"))
# OJO: SIGNAL_INTERVAL_MINUTES no lo lee NADIE. El barrido sale de BAR_MS
# en poller.py, o sea de SIGNAL_TIMEFRAME. Cambiarla no hace nada.
SIGNAL_INTERVAL_MINUTES = int(_str("SIGNAL_INTERVAL_MINUTES", "5"))

# Segundo dentro del minuto en que se evalua (antes fijo a 15 en poller.py).
# Hay picos recurrentes de volatilidad y volumen en los minutos 0, 15, 30 y
# 45 por actividad algoritmica, y los creadores de mercado ensanchan el
# spread justo antes. Esperar unos segundos deja pasar la rafaga inicial.
SIGNAL_SECOND_OFFSET = int(_str("SIGNAL_SECOND_OFFSET", "40"))

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
