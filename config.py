"""
config.py — Carga y valida todas las variables de entorno del NEXUS Bot.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Variable requerida no encontrada: {key}")
    return val


class Config:
    # ── BingX ──────────────────────────────────────────────
    BINGX_API_KEY:    str   = _require("BINGX_API_KEY")
    BINGX_SECRET_KEY: str   = _require("BINGX_SECRET_KEY")

    # ── Telegram ───────────────────────────────────────────
    TELEGRAM_TOKEN:   str   = _require("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID: str   = _require("TELEGRAM_CHAT_ID")

    # ── Trading general ────────────────────────────────────
    SYMBOLS:          list  = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT").split(",")]
    TIMEFRAME:        str   = os.getenv("TIMEFRAME", "15m")
    LEVERAGE:         int   = int(os.getenv("LEVERAGE", "5"))
    RISK_PER_TRADE:   float = float(os.getenv("RISK_PER_TRADE", "1.5"))

    # ── SAMA ───────────────────────────────────────────────
    SAMA_LENGTH:      int   = int(os.getenv("SAMA_LENGTH", "200"))
    SAMA_MAJ_LENGTH:  int   = int(os.getenv("SAMA_MAJ_LENGTH", "14"))
    SAMA_MIN_LENGTH:  int   = int(os.getenv("SAMA_MIN_LENGTH", "6"))
    SLOPE_PERIOD:     int   = int(os.getenv("SLOPE_PERIOD", "34"))
    SLOPE_RANGE:      int   = int(os.getenv("SLOPE_RANGE", "25"))
    SLOPE_FLAT:       int   = int(os.getenv("SLOPE_FLAT", "17"))

    # ── Markov ─────────────────────────────────────────────
    SLOPE_MIN:        float = float(os.getenv("SLOPE_MIN", "30.0"))
    LOOKBACK_MARKOV:  int   = int(os.getenv("LOOKBACK_MARKOV", "200"))
    PROB_THRESHOLD:   float = float(os.getenv("PROB_THRESHOLD", "40.0"))

    # ── ADX Adaptativo ─────────────────────────────────────
    ADX_LEN:          int   = int(os.getenv("ADX_LEN", "14"))
    ADX_TREND:        int   = int(os.getenv("ADX_TREND", "25"))
    ADX_RANGE:        int   = int(os.getenv("ADX_RANGE", "20"))

    # ── Filtros institucionales ────────────────────────────
    RVOL_MIN:         float = float(os.getenv("RVOL_MIN", "1.5"))
    POC_LOOKBACK:     int   = int(os.getenv("POC_LOOKBACK", "50"))
    PIVOT_LEN:        int   = int(os.getenv("PIVOT_LEN", "4"))
    LIQUIDITY_LOOKBACK: int = int(os.getenv("LIQUIDITY_LOOKBACK", "20"))

    # ── CVD ────────────────────────────────────────────────
    CVD_SLOPE_PERIOD:       int = int(os.getenv("CVD_SLOPE_PERIOD", "8"))
    CVD_DIVERGENCE_LOOKBACK: int = int(os.getenv("CVD_DIVERGENCE_LOOKBACK", "10"))

    # ── Funding Rate ───────────────────────────────────────
    FUNDING_BULL_THRESHOLD: float = float(os.getenv("FUNDING_BULL_THRESHOLD", "-0.0001"))
    FUNDING_BEAR_THRESHOLD: float = float(os.getenv("FUNDING_BEAR_THRESHOLD", "0.0005"))

    # ── Kotegawa ───────────────────────────────────────────
    DIP_PCT:          float = float(os.getenv("DIP_PCT", "20.0"))
    MA_LEN:           int   = int(os.getenv("MA_LEN", "25"))
    RSI_LEN:          int   = int(os.getenv("RSI_LEN", "14"))
    RSI_OVERSOLD:     float = float(os.getenv("RSI_OVERSOLD", "28.0"))
    BB_LEN:           int   = int(os.getenv("BB_LEN", "20"))
    BB_MULT:          float = float(os.getenv("BB_MULT", "2.0"))

    # ── Triple barrera ─────────────────────────────────────
    ATR_MULT_TP:      float = float(os.getenv("ATR_MULT_TP", "2.2"))
    ATR_MULT_SL:      float = float(os.getenv("ATR_MULT_SL", "1.2"))
    MAX_BARS_HOLD:    int   = int(os.getenv("MAX_BARS_HOLD", "24"))

    # ── Scoring ────────────────────────────────────────────
    MIN_SCORE:        float = float(os.getenv("MIN_SCORE", "55.0"))

    # ── Riesgo global ──────────────────────────────────────
    MAX_DAILY_LOSS_PCT:  float = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    MAX_OPEN_POSITIONS:  int   = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
    LOOP_INTERVAL:       int   = int(os.getenv("LOOP_INTERVAL", "60"))
    HEALTH_PORT:         int   = int(os.getenv("HEALTH_PORT", "8080"))

    def __repr__(self) -> str:
        return (
            f"<Config symbols={self.SYMBOLS} tf={self.TIMEFRAME} "
            f"lev={self.LEVERAGE}x minScore={self.MIN_SCORE}>"
        )
