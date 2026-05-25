import os
from dotenv import load_dotenv
load_dotenv()

# ── BingX ─────────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BINGX_BASE_URL   = "https://open-api.bingx.com"

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Multi-símbolo ─────────────────────────────────────────────
# Modo de selección de símbolos:
#   "manual"  → usa SYMBOLS_LIST (lista fija separada por comas)
#   "scanner" → escanea TODO BingX y filtra por criterios
SYMBOL_MODE      = os.getenv("SYMBOL_MODE", "scanner")

# Lista manual de símbolos (solo si SYMBOL_MODE=manual)
# Ejemplo: "BTC-USDT,ETH-USDT,SOL-USDT"
SYMBOLS_LIST     = [s.strip() for s in os.getenv("SYMBOLS_LIST", "BTC-USDT,ETH-USDT,SOL-USDT").split(",") if s.strip()]

# Compatibilidad hacia atrás — si solo se define SYMBOL, úsalo en modo manual
_single = os.getenv("SYMBOL", "")
if _single and SYMBOL_MODE == "manual" and len(SYMBOLS_LIST) == 1:
    SYMBOLS_LIST = [_single]

# ── Filtros del escáner ───────────────────────────────────────
# Volumen mínimo 24h en USDT para incluir un símbolo
SCANNER_MIN_VOLUME   = float(os.getenv("SCANNER_MIN_VOLUME",   "5000000"))   # 5M USDT
# Número máximo de símbolos que el escáner selecciona (por volumen desc.)
SCANNER_TOP_N        = int(os.getenv("SCANNER_TOP_N",          "20"))
# Excluir estos símbolos aunque pasen el filtro (separados por comas)
SCANNER_BLACKLIST    = [s.strip() for s in os.getenv("SCANNER_BLACKLIST", "").split(",") if s.strip()]
# Cada cuántos ciclos refrescar la lista del escáner (1 ciclo = 3min)
SCANNER_REFRESH_CYCLES = int(os.getenv("SCANNER_REFRESH_CYCLES", "20"))  # ~60min

# ── Operativa ─────────────────────────────────────────────────
LEVERAGE         = int(os.getenv("LEVERAGE",         "10"))
RISK_PER_TRADE   = float(os.getenv("RISK_PER_TRADE", "0.015"))  # 1.5% por trade
MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS",    "3"))       # máximo total simultáneo
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "0.06")) # 6% balance

# ── SL / TP ───────────────────────────────────────────────────
SL_ATR_MULT      = float(os.getenv("SL_ATR_MULT",   "1.5"))
TP_RR            = float(os.getenv("TP_RR",          "2.5"))
TRAIL_ATR_MULT   = float(os.getenv("TRAIL_ATR_MULT", "1.0"))

# ── Estrategia — umbrales ─────────────────────────────────────
SCORE_THR        = float(os.getenv("SCORE_THR",   "0.15"))
DECAY_THR        = float(os.getenv("DECAY_THR",   "0.50"))
CVD_DIV_BARS     = int(os.getenv("CVD_DIV_BARS",  "5"))
REQUIRE_HTF      = os.getenv("REQUIRE_HTF", "true").lower() == "true"
COOLDOWN_CANDLES = int(os.getenv("COOLDOWN_CANDLES", "3"))

# ── Parámetros indicadores ────────────────────────────────────
ATR_PERIOD   = 10
MOM_LOOKBACK = 20
REV_LOOKBACK = 8
VOL_LOOKBACK = 14
W_MOM        = 0.40
W_REV        = 0.30
W_VOL        = 0.30
SMOOTH       = 3
DECAY_LEN    = 40
CVD_EMA_LEN  = 20
HTF_FAST     = 9
HTF_SLOW     = 21
LOOKBACK     = 250

# ── Railway ───────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8080"))
