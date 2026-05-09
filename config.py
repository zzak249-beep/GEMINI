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
PIVOT_LEN = int(os.getenv("PIVOT_LEN", "5"))   # Profundidad ZigZag
ATR_LEN   = int(os.getenv("ATR_LEN", "14"))    # Periodo ATR
TIMEFRAME = os.getenv("TIMEFRAME", "3m")        # 3 minutos

# ==========================================
# ESTRATEGIA: CHANNEL FADE
# Línea verde = último pivot HIGH
# Línea roja  = último pivot LOW
# ==========================================
#   SHORT: precio sube SHORT_PIPS encima de línea verde → TP en línea roja
#   LONG : precio baja LONG_PIPS debajo de línea roja  → TP en línea verde
SHORT_PIPS     = float(os.getenv("SHORT_PIPS", "45"))   # pips encima de green → SHORT
LONG_PIPS      = float(os.getenv("LONG_PIPS",  "30"))   # pips debajo de red   → LONG
PIP_SIZE       = float(os.getenv("PIP_SIZE",   "1.0"))  # valor de 1 pip en USDT
                                                          # BTC=1.0, ETH=0.1, alts=0.001
SL_ATR_MULT    = float(os.getenv("SL_ATR_MULT", "2.0")) # multiplicador ATR para SL
VOL_FILTER     = os.getenv("VOL_FILTER", "false").lower() == "true"  # filtro vol institucional
VOL_MULT       = float(os.getenv("VOL_MULT", "1.5"))    # threshold vol (si VOL_FILTER=true)

# ==========================================
# GESTIÓN DE RIESGO
# ==========================================
LEVERAGE       = int(os.getenv("LEVERAGE", "10"))
RISK_PCT       = float(os.getenv("RISK_PCT", "1.5"))     # % balance por trade
MAX_POSITIONS  = int(os.getenv("MAX_POSITIONS", "5"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5.0"))

# ==========================================
# SCANNER
# ==========================================
TOP_PAIRS      = int(os.getenv("TOP_PAIRS", "20"))
VOL_SURGE_MULT = float(os.getenv("VOL_SURGE_MULT", "2.0"))
MIN_PRICE_USDT = float(os.getenv("MIN_PRICE_USDT", "0.001"))

# ==========================================
# TIMING
# ==========================================
CANDLE_SLEEP = int(os.getenv("CANDLE_SLEEP", "60"))   # 60s para velas 3m
KLINE_LIMIT  = int(os.getenv("KLINE_LIMIT",  "120"))  # velas a descargar
