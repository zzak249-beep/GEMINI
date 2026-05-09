import os

# ==========================================
# BINGX API
# ==========================================
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL         = "https://open-api.bingx.com"

# ==========================================
# TELEGRAM
# ==========================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==========================================
# ZIGZAG CORE
# ==========================================
PIVOT_LEN = int(os.getenv("PIVOT_LEN", "5"))
ATR_LEN   = int(os.getenv("ATR_LEN", "14"))
TIMEFRAME = os.getenv("TIMEFRAME", "3m")

# ==========================================
# ESTRATEGIA: CHANNEL FADE V32
# Línea verde = último pivot HIGH
# Línea roja  = último pivot LOW
# ==========================================
SHORT_PIPS  = float(os.getenv("SHORT_PIPS", "45"))
LONG_PIPS   = float(os.getenv("LONG_PIPS",  "30"))
PIP_SIZE    = float(os.getenv("PIP_SIZE",   "0.001"))
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.5"))  # Ajustado a V32 (era 2.0)

# ==========================================
# FILTROS V32 — APEX QUANTUM SHIELD
# ==========================================
# ADX: evita mercados laterales donde ZigZag falla
ADX_MIN = int(os.getenv("ADX_MIN", "25"))
ADX_LEN = int(os.getenv("ADX_LEN", "14"))

# EMAs de confirmación de giro
EMA_FAST = int(os.getenv("EMA_FAST", "7"))   # Cruce bajista (SHORT) o alcista (LONG)
EMA_MED  = int(os.getenv("EMA_MED",  "17"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))  # Media base (TP alternativo)

# Volumen institucional — SIEMPRE activo en V32
VOL_FILTER = os.getenv("VOL_FILTER", "true").lower() == "true"
VOL_MULT   = float(os.getenv("VOL_MULT", "1.5"))  # Volume > 1.5× MA20

# Time-Stop: máximo N velas abiertas antes de cerrar forzosamente
# 15 velas × 3m = 45 min máximo por operación
TIME_STOP_MINUTES = int(os.getenv("TIME_STOP_MINUTES", "45"))

# ==========================================
# GESTIÓN DE RIESGO
# ==========================================
LEVERAGE       = int(os.getenv("LEVERAGE", "10"))
RISK_PCT       = float(os.getenv("RISK_PCT", "1.5"))
MAX_POSITIONS  = int(os.getenv("MAX_POSITIONS", "3"))   # Reducido a 3 para dinero real
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5.0"))

# ==========================================
# SCANNER
# TOP_PAIRS=15: punto óptimo para dinero real en 3m
# Menos de 10 = pocas señales; más de 20 = pares basura + rate limit
# ==========================================
TOP_PAIRS      = int(os.getenv("TOP_PAIRS", "15"))
MIN_PRICE_USDT = float(os.getenv("MIN_PRICE_USDT", "0.001"))

# ==========================================
# TIMING
# ==========================================
CANDLE_SLEEP = int(os.getenv("CANDLE_SLEEP", "60"))
KLINE_LIMIT  = int(os.getenv("KLINE_LIMIT",  "120"))
PORT         = int(os.getenv("PORT", "8080"))
