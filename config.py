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

# ── Operativa ─────────────────────────────────────────────────
SYMBOL           = os.getenv("SYMBOL", "BTC-USDT")
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))
RISK_PER_TRADE   = float(os.getenv("RISK_PER_TRADE", "0.015"))  # 1.5%
MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS", "1"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "0.06")) # 6%

# ── SL / TP ───────────────────────────────────────────────────
SL_ATR_MULT      = float(os.getenv("SL_ATR_MULT",   "1.5"))
TP_RR            = float(os.getenv("TP_RR",          "2.5"))
TRAIL_ATR_MULT   = float(os.getenv("TRAIL_ATR_MULT", "1.0"))

# ── Estrategia — umbrales ─────────────────────────────────────
SCORE_THR        = float(os.getenv("SCORE_THR",   "0.15"))  # score mínimo para señal
DECAY_THR        = float(os.getenv("DECAY_THR",   "0.50"))  # decaimiento mínimo
CVD_DIV_BARS     = int(os.getenv("CVD_DIV_BARS",  "5"))     # ventana divergencia CVD
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
