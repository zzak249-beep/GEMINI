"""
QF×JP Bot v8.0 — Config JOYFUL-ART (COMPLEMENTO)
═══════════════════════════════════════════════════════════════════════════════
TUNING v8.0 — RAPIDEZ + GANANCIAS + LEVERAGE SEGURO:

Objetivo: entrar rápido en señales fuertes, cerrar con beneficio antes de
que el mercado revierta, gestionar riesgo real en leverage alto (10-20x).

Cambios principales vs v7.6:
  • SCAN_INTERVAL: 60→30s — detecta señales el doble de rápido
  • POSITION_CHECK_INTERVAL: 30→15s — monitorea y actúa dos veces más
  • BREAKEVEN_ATR_MULT: 1.0→0.7 — trail se activa 30% antes (más rápido
    al breakeven)
  • TRAIL_DISTANCE_ATR: 1.5→1.0 — SL más pegado al precio (más beneficio
    locked)
  • TP1_ATR_MULT: 2.0→1.5 — cierra mitad de posición antes
  • TP2_ATR_MULT: 4.0→3.0 — no espera movimientos enormes
  • MAX_HOLD_MINUTES: 60→30 — time stop más agresivo
  • EMA_EXIT_ENABLED: False→True — cierre por EMA9 activo (más rápido
    que time_stop)
  • EMA_EXIT_PERIOD: 9 (EMA estándar de scalping)
  • EMA_EXIT_MIN_HOLD_MIN: 4 — 4 min de gracia antes de evaluar EMA
  • ATR_LIVE_ENABLED: True — ATR vivo, trail usa volatilidad actual
  • ATR_REFRESH_CYCLES: 4 — refresca ATR cada 4 ciclos del monitor
  • MIN_NOTIONAL_USDT: 3→10 — sin trades simbólicos donde fees > edge
  • MAX_OPEN_TRADES: 3→5 — aprovecha top-50 símbolos
  • LEVERAGE: mantenido en 10 — seguro con SL a 1.5 ATR y liq a ~10%
═══════════════════════════════════════════════════════════════════════════════
Bot COMPLEMENTO — escanea top-50 símbolos por volumen, copia trades SUP>80
de renewed-love al 40% del size, actúa como guardián de salida anticipada,
abre hedge BTC cuando renewed-love tiene 3+ posiciones perdiendo.

Variables críticas Railway:
  MASTER_URL = URL del servicio renewed-love
  COMPLEMENT_MODE = GUARDIAN,COPY,EXCLUSIVE
"""
import os
from dotenv import load_dotenv
load_dotenv()

def _bool(k, d): return os.getenv(k, str(d)).strip().lower() in ("true","1","yes")
def _float(k, d):
    try: return float(os.getenv(k, str(d)).strip().split()[0])
    except: return d
def _int(k, d):
    try: return int(os.getenv(k, str(d)).strip().split()[0])
    except: return d
def _list(k, d):
    r = os.getenv(k, d).strip()
    return [x.strip() for x in r.split(",") if x.strip()] if r else []

# ── BingX ─────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "").strip()
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "").strip()
BINGX_BASE_URL   = "https://open-api.bingx.com"

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ── Modo ──────────────────────────────────────────────────────────────────────
MODE = os.getenv("MODE", "SIGNAL").upper()

# ── Capital y riesgo ──────────────────────────────────────────────────────────
CAPITAL          = _float("CAPITAL", 200.0)
RISK_PCT         = _float("RISK_PCT", 0.5)
LEVERAGE         = _int("LEVERAGE", 10)       # NO subir >20x — ver comentario en position_manager._calc_pnl
MAX_OPEN_TRADES  = _int("MAX_OPEN_TRADES", 5)  # 3→5: aprovecha top-50 símbolos
MAX_DAILY_TRADES = _int("MAX_DAILY_TRADES", 15)

# ── Umbrales de señal ─────────────────────────────────────────────────────────
MIN_SCORE  = _float("MIN_SCORE",  58.0)
FUEL_SCORE = _float("FUEL_SCORE", 65.0)
SUP_SCORE  = _float("SUP_SCORE",  80.0)
MIN_TIER   = os.getenv("MIN_TIER", "FUEL").upper()

# ── Entrada ───────────────────────────────────────────────────────────────────
REQUIRE_TL_BREAK = _bool("REQUIRE_TL_BREAK", True)
HTF_MIN_ALIGNED  = _int("HTF_MIN_ALIGNED", 2)

# ── Scanner ───────────────────────────────────────────────────────────────────
# TUNING v8.0: 60→30s — detecta señales el doble de rápido
SCAN_INTERVAL   = _int("SCAN_INTERVAL", 30)
TOP_N_SYMBOLS   = _int("TOP_N_SYMBOLS", 0)
BLACKLIST       = set(_list("BLACKLIST", "ESPORTS,STABLE,EURUSD,SILVER,PAXG,CUSDT,SYN,FONK,FOLK,NCS"))
MIN_VOLUME_USDT = _float("MIN_VOLUME_USDT", 5_000_000.0)

# ── Timeframes ────────────────────────────────────────────────────────────────
TIMEFRAME      = os.getenv("TIMEFRAME",      "3m")
HTF_TIMEFRAME  = os.getenv("HTF_TIMEFRAME",  "15m")
HTF2_TIMEFRAME = os.getenv("HTF2_TIMEFRAME", "1h")
HTF5_TIMEFRAME = os.getenv("HTF5_TIMEFRAME", "4h")

# ── ATR / SL / TP ─────────────────────────────────────────────────────────────
# TUNING v8.0: TP1 más cercano (1.5 vs 2.0), TP2 también (3.0 vs 4.0)
# Objetivo: cerrar beneficio antes de que revierta. A 10x leverage,
# 1.5 ATR de movimiento = beneficio real significativo sobre el margen.
ATR_LEN      = _int("ATR_LEN",       10)
SL_ATR_MULT  = _float("SL_ATR_MULT",  1.5)   # 2.0→1.5: SL más ajustado (mejor R:R)
TP1_ATR_MULT = _float("TP1_ATR_MULT", 1.5)   # 2.0→1.5: cierra antes
TP2_ATR_MULT = _float("TP2_ATR_MULT", 3.0)   # 4.0→3.0: TP2 más realista

# ── ADX ───────────────────────────────────────────────────────────────────────
ADX_LEN     = _int("ADX_LEN", 14)
ADX_TREND   = _float("ADX_TREND",   25.0)
ADX_LATERAL = _float("ADX_LATERAL", 20.0)

# ── Kelly ─────────────────────────────────────────────────────────────────────
KELLY_WIN_RATE = _float("KELLY_WIN_RATE", 0.55)
KELLY_RR       = _float("KELLY_RR",       1.5)
KELLY_FRACTION = _float("KELLY_FRACTION", 0.15)

# ── Circuit Breaker ───────────────────────────────────────────────────────────
CB_ENABLED  = _bool("CB_ENABLED",   False)
CB_ATR_MULT = _float("CB_ATR_MULT", 3.0)
CB_BARS     = _int("CB_BARS",       10)

# ── Monitor de posiciones ─────────────────────────────────────────────────────
# TUNING v8.0: 30→15s — reacciona el doble de rápido a cambios de precio,
# activa trail antes y ejecuta time_stop/EMA exit más puntualmente.
POSITION_CHECK_INTERVAL = _int("POSITION_CHECK_INTERVAL", 15)

# ── Trailing Stop ─────────────────────────────────────────────────────────────
# TUNING v8.0:
# BREAKEVEN_ATR_MULT 1.0→0.7: trail se activa cuando el precio avanza solo
#   0.7 ATR a favor (antes 1.0). Llega al breakeven más rápido = menos
#   pérdidas en trades que revierten después de un movimiento inicial.
# TRAIL_DISTANCE_ATR 1.5→1.0: SL más pegado al precio, más beneficio locked.
BREAKEVEN_ATR_MULT = _float("BREAKEVEN_ATR_MULT", 0.7)
TRAIL_DISTANCE_ATR = _float("TRAIL_DISTANCE_ATR", 1.0)

# ── Time Stop ─────────────────────────────────────────────────────────────────
# TUNING v8.0: 60→30 min. Con TIMEFRAME=3m y trailing activo, 30 min
# (~10 velas de 3m) es más que suficiente para que un trade con edge real
# progrese. Si no avanzó en 30 min, probablemente la señal fue falsa.
MAX_HOLD_MINUTES           = _int("MAX_HOLD_MINUTES", 30)
TIME_STOP_MIN_PROGRESS_ATR = _float("TIME_STOP_MIN_PROGRESS_ATR", 0.3)

# ── EMA Exit ─────────────────────────────────────────────────────────────────
# TUNING v8.0: activado (era False). Cierra trades MUCHO más rápido que
# time_stop cuando la EMA9 detecta muerte de tendencia. Complementa el
# time_stop — normalmente actúa antes que él.
# EMA_EXIT_MIN_HOLD_MIN=4 → 4 min (~1.3 velas de 3m) de gracia antes de
# evaluar, para no salir en el primer tick de ruido tras la entrada.
EMA_EXIT_ENABLED      = _bool("EMA_EXIT_ENABLED", True)
EMA_EXIT_PERIOD       = _int("EMA_EXIT_PERIOD", 9)
EMA_EXIT_MIN_HOLD_MIN = _int("EMA_EXIT_MIN_HOLD_MIN", 4)

# ── ATR Vivo (position_manager.py) ───────────────────────────────────────────
# NUEVO v8.0: ATR se refresca cada ATR_REFRESH_CYCLES ciclos del monitor
# desde klines reales. Con POSITION_CHECK_INTERVAL=15s y CYCLES=4 →
# refresco cada 60s. El trail usa la volatilidad actual, no la del momento
# de entrada (que puede haber cambiado drásticamente en un spike).
ATR_LIVE_ENABLED    = _bool("ATR_LIVE_ENABLED", True)
ATR_REFRESH_CYCLES  = _int("ATR_REFRESH_CYCLES", 4)

# ── Correlation Guard ─────────────────────────────────────────────────────────
CORRELATION_WINDOW_SEC = _int("CORRELATION_WINDOW_SEC", 900)
MAX_SAME_DIRECTION     = _int("MAX_SAME_DIRECTION", 2)

# ── Pérdida diaria ────────────────────────────────────────────────────────────
DAILY_LOSS_PCT = _float("DAILY_LOSS_PCT", 2.0)

# ── Notional ─────────────────────────────────────────────────────────────────
# TUNING v8.0: 3→10 USDT mínimo. Con fees del 0.02-0.05%, en trades de
# 3 USDT los fees consumen el edge completo. 10 USDT es el mínimo práctico.
MAX_NOTIONAL_USDT = _float("MAX_NOTIONAL_USDT", 200.0)
MIN_NOTIONAL_USDT = _float("MIN_NOTIONAL_USDT", 10.0)

# ── Sesión ────────────────────────────────────────────────────────────────────
TRADE_START_UTC = _int("TRADE_START_UTC", 0)
TRADE_END_UTC   = _int("TRADE_END_UTC",   24)

# ── Funding Rate ──────────────────────────────────────────────────────────────
FR_EXTREME_THR  = _float("FR_EXTREME_THR", 0.0005)
FR_REGIME_ENABLED = _bool("FR_REGIME_ENABLED", True)
HARVEST_ENABLED   = _bool("HARVEST_ENABLED", True)
HARVEST_FR_THR    = _float("HARVEST_FR_THR", 0.0010)

# ── Open Interest ─────────────────────────────────────────────────────────────
OI_FILTER_ENABLED = _bool("OI_FILTER_ENABLED", True)

# ── Limit Orders ─────────────────────────────────────────────────────────────
LIMIT_ORDERS_ENABLED = _bool("LIMIT_ORDERS_ENABLED", True)
LIMIT_TIMEOUT_SECS   = _int("LIMIT_TIMEOUT_SECS", 25)

# ── Volatility Regime ─────────────────────────────────────────────────────────
VOL_REGIME_ENABLED = _bool("VOL_REGIME_ENABLED", True)

# ── Candle Turn ───────────────────────────────────────────────────────────────
CANDLE_TURN_ENABLED       = _bool("CANDLE_TURN_ENABLED", False)
CANDLE_TURN_BOOST         = _float("CANDLE_TURN_BOOST", 3.0)
CANDLE_TURN_TOLERANCE_MIN = _int("CANDLE_TURN_TOLERANCE_MIN", 1)

# ── Slope Multi-TF ────────────────────────────────────────────────────────────
SLOPE_FILTER_ENABLED = _bool("SLOPE_FILTER_ENABLED", True)

# ── BTC Correlation Guard ─────────────────────────────────────────────────────
BTC_CORR_ENABLED    = _bool("BTC_CORR_ENABLED", True)
BTC_CORR_THRESHOLD  = _float("BTC_CORR_THRESHOLD", 0.5)
BTC_CORR_MAX_SAME   = _int("BTC_CORR_MAX_SAME", 3)
BTC_CORR_WINDOW_SEC = _int("BTC_CORR_WINDOW_SEC", 1800)

# ── WebSocket (desactivado por defecto) ───────────────────────────────────────
WS_ENABLED = _bool("WS_ENABLED", False)

# ── Explosion Detector ────────────────────────────────────────────────────────
EXPLOSION_ENABLED = _bool("EXPLOSION_ENABLED", True)

# ── Trend Magic + RMI (nuevo filtro — desactivado hasta validar en SIGNAL) ────
TREND_MAGIC_RMI_ENABLED = _bool("TREND_MAGIC_RMI_ENABLED", False)
TMR_CCI_LEN     = _int("TMR_CCI_LEN",     20)
TMR_ATR_LEN     = _int("TMR_ATR_LEN",     5)
TMR_ATR_MULT    = _float("TMR_ATR_MULT",  1.0)
TMR_RMI_LEN     = _int("TMR_RMI_LEN",    14)
TMR_PMOM        = _float("TMR_PMOM",      66.0)
TMR_NMOM        = _float("TMR_NMOM",      30.0)
TMR_BOOST_AMOUNT = _float("TMR_BOOST_AMOUNT", 7.0)

# ── Indicadores v3.6 ─────────────────────────────────────────────────────────
CVD_ROLL_WINDOW = _int("CVD_ROLL_WINDOW", 60)
EQL_LEN         = _int("EQL_LEN", 20)
EQL_TOL         = _float("EQL_TOL", 0.15)
OBP2_DIST       = _float("OBP2_DIST", 1.5)
PRE_SCORE       = _float("PRE_SCORE", 45.0)

# ── Puerto ────────────────────────────────────────────────────────────────────
PORT = _int("PORT", 8080)
