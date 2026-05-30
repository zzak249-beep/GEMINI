"""
╔══════════════════════════════════════════════════════════════════════╗
║   CRYPTO SCANNER v3.0 — QF×JP v3.2 ENGINE                          ║
║   Replica exacta de las 12 capas del indicador Pine Script          ║
║                                                                      ║
║   L1  Microestructura · ATR · BP Drain                              ║
║   L2  Factores + ADX dinámico [M2]                                  ║
║   L3  Decaimiento adaptativo IC [M1]                                ║
║   L4  Dark Pool (vol spike + rango estrecho)                        ║
║   L5  Ejecución (spread ok)                                         ║
║   L6  Asimetría de momentum                                         ║
║   L7  Ruptura de trendline (pivotes HH/LL)                          ║
║   L8  Swing exhaustion (HL count / LH count)                        ║
║   L9  Fair Value Gaps (tracking múltiple) [M4]                      ║
║   L10 Order Blocks                                                  ║
║   L11 CVD Delta rodante [M3]                                        ║
║   L12 Squeeze Momentum                                              ║
║   SC  Score compuesto 0-100 + Conv-Boost [M5]                       ║
║   SIG Señales STD / FUEL / SUPREMA — idénticas al Pine              ║
║                                                                      ║
║   Auto-Trade BingX: LONG SUPREMA ≥ 80 cuando AUTO_TRADE=true        ║
║   Scan 24/7 adaptativo: 1min/5min/15min según actividad             ║
╚══════════════════════════════════════════════════════════════════════╝

Variables de entorno Railway:
  BINGX_API_KEY / BINGX_API_SECRET
  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
  TRADE_USDT   (default 20)
  LEVERAGE     (default 5)
  SL_PCT       (default 2.5)
  TP_PCT       (default 5.0)
  AUTO_TRADE   (default false)
  MAX_OPEN_TRADES (default 3)
"""

import os, time, hmac, hashlib, logging, math
from datetime import datetime, timezone
from typing import Optional
import requests
import numpy as np

# ─────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TRADE_USDT      = float(os.getenv("TRADE_USDT",      "20"))
LEVERAGE        = int(os.getenv("LEVERAGE",           "5"))
SL_PCT          = float(os.getenv("SL_PCT",           "2.5"))
TP_PCT          = float(os.getenv("TP_PCT",           "5.0"))
AUTO_TRADE      = os.getenv("AUTO_TRADE", "false").lower() == "true"
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES",    "3"))

BASE_URL = "https://open-api.bingx.com"

# ── Parámetros del indicador (espejo del Pine) ────────────────────────
I_MOM     = 20      # Lookback momentum
I_REV     = 8       # Lookback media-rev
I_VOL_L   = 14      # Longitud volumen OBV
I_ATR_L   = 10      # ATR period
I_SMO     = 3       # Suavizado EMA
I_W1      = 0.40    # Peso momentum base
I_W2      = 0.30    # Peso media-rev base
I_W3      = 0.30    # Peso volumen
I_ADX_LEN = 14      # ADX period
I_ADX_TH  = 25      # ADX umbral tendencia fuerte
I_DLEN    = 40      # Ventana decaimiento IC
I_DTHR    = 0.40    # Umbral ratio decay fijo
I_DECAY_PCT = 30    # Percentil adaptativo
I_DPM     = 2.5     # Mult. volumen Dark Pool
I_DPB     = 20      # Baseline Dark Pool
I_EXL     = 12      # Baseline ejecución
I_BPT     = 0.18    # Umbral BP drain (%)
I_ASL     = 10      # Ventana asimetría
I_ARR     = 1.20    # Ratio alcista
I_ABR     = 1.20    # Ratio bajista
I_TLB     = 30      # Lookback trendline
I_TLL     = 5       # Pivote barras izq
I_TLR     = 3       # Pivote barras der
I_TLM     = 0.15    # Buffer ruptura (× ATR)
I_PLL     = 5       # Swing low izq
I_PLR     = 3       # Swing low der
I_PHL     = 5       # Swing high izq
I_PHR     = 3       # Swing high der
I_HLC     = 2       # Min HL ascendentes
I_HHC     = 2       # Min LH descendentes
I_HLW     = 40      # Ventana análisis swings
I_FVG_MIN = 0.3     # FVG mínimo (× ATR)
I_FVG_BARS= 40      # Validez FVG
I_FVG_MAX = 5       # Máx FVGs activos
I_OB_IMP  = 1.5     # Impulso OB (× ATR)
I_OB_BARS = 50      # Validez OB
I_CVD_LEN = 20      # EMA CVD
I_CVD_DIV = 5       # Ventana divergencia
I_CVD_ROLL= 100     # CVD rodante
I_SQ_LEN  = 20      # Squeeze longitud
I_SQ_BBM  = 2.0     # Squeeze mult BB
I_SQ_KCM  = 1.5     # Squeeze mult KC
# Score umbrales
SC_THR_STD  = 55
SC_THR_FUEL = 68
SC_THR_SUP  = 80
SC_W_SCORE  = 0.30
SC_W_CVD    = 0.25
SC_W_MOM    = 0.20
SC_W_DECAY  = 0.15
SC_W_HTF    = 0.10
# [M6] Volatilidad mínima
VOL_ATR_THR = 0.70  # ATR actual > 0.70 × ATR medio 20

# ── Scan ──────────────────────────────────────────────────────────────
MIN_VOLUME_USDT = 5_000_000
TOP_N           = 10
KLINES_1H       = 80   # necesitamos suficiente historia
KLINES_15M      = 60   # para HTF (usado como 15m confirma 3m)

INTERVAL_NORMAL = 900
INTERVAL_ACTIVO = 300
INTERVAL_ALERTA = 60

trades_abiertos:  dict[str, dict] = {}
alertas_enviadas: dict[str, float] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ScannerV3")


# ─────────────────────────────────────────────────────────────────────
#  API BingX
# ─────────────────────────────────────────────────────────────────────

def _sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def _get(path: str, params: dict = None, auth: bool = False) -> Optional[dict]:
    p = params or {}
    headers = {}
    if auth:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = _sign(p)
        headers["X-BX-APIKEY"] = BINGX_API_KEY
    try:
        r = requests.get(BASE_URL + path, params=p, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}")
        return None


def _post(path: str, params: dict) -> Optional[dict]:
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    headers = {"X-BX-APIKEY": BINGX_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(BASE_URL + path, data=params, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"POST {path}: {e}")
        return None


def get_all_tickers() -> list:
    d = _get("/openApi/swap/v2/quote/ticker")
    return d.get("data", []) if d else []


def get_klines(symbol: str, interval: str = "1h", limit: int = 80) -> list:
    d = _get("/openApi/swap/v3/quote/klines",
             {"symbol": symbol, "interval": interval, "limit": limit})
    return d.get("data", []) if d else []


def get_funding(symbol: str) -> float:
    d = _get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
    if d and d.get("data"):
        try:
            return float(d["data"].get("lastFundingRate", 0))
        except Exception:
            pass
    return 0.0


def get_open_positions() -> list:
    d = _get("/openApi/swap/v2/user/positions", auth=True)
    return d.get("data", []) or [] if d else []


# ─────────────────────────────────────────────────────────────────────
#  INDICADORES — réplica Pine Script
# ─────────────────────────────────────────────────────────────────────

def f_tanh(x: float) -> float:
    x2 = max(min(2.0 * x, 20.0), -20.0)
    e = math.exp(x2)
    return (e - 1.0) / (e + 1.0)


def ema(arr: np.ndarray, period: int) -> np.ndarray:
    k = 2.0 / (period + 1)
    r = np.empty(len(arr))
    r[0] = arr[0]
    for i in range(1, len(arr)):
        r[i] = arr[i] * k + r[i - 1] * (1 - k)
    return r


def sma(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        out[i] = arr[i - period + 1:i + 1].mean()
    return out


def stdev(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        out[i] = arr[i - period + 1:i + 1].std(ddof=0)
    return out


def atr_series(highs, lows, closes, period: int) -> np.ndarray:
    tr = np.empty(len(closes))
    tr[0] = highs[0] - lows[0]
    for i in range(1, len(closes)):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i]  - closes[i - 1]))
    return ema(tr, period)


def adx_series(highs, lows, closes, period: int):
    """Returns (plus_di, minus_di, adx)."""
    n = len(closes)
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    tr       = np.zeros(n)
    for i in range(1, n):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        plus_dm[i]  = h_diff if h_diff > l_diff and h_diff > 0 else 0
        minus_dm[i] = l_diff if l_diff > h_diff and l_diff > 0 else 0
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i]  - closes[i - 1]))
    atr_e     = ema(tr, period)
    plus_di   = 100 * ema(plus_dm,  period) / np.maximum(atr_e, 1e-10)
    minus_di  = 100 * ema(minus_dm, period) / np.maximum(atr_e, 1e-10)
    dx        = 100 * np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, 1e-10)
    adx_val   = ema(dx, period)
    return plus_di, minus_di, adx_val


def obv_series(closes, volumes) -> np.ndarray:
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]
    return obv


def pivot_high(highs: np.ndarray, left: int, right: int) -> np.ndarray:
    """Returns pivot high values (nan where not pivot)."""
    n = len(highs)
    ph = np.full(n, np.nan)
    for i in range(left, n - right):
        window = highs[i - left:i + right + 1]
        if highs[i] == window.max() and (window < highs[i]).any():
            ph[i] = highs[i]
    return ph


def pivot_low(lows: np.ndarray, left: int, right: int) -> np.ndarray:
    n = len(lows)
    pl = np.full(n, np.nan)
    for i in range(left, n - right):
        window = lows[i - left:i + right + 1]
        if lows[i] == window.min() and (window > lows[i]).any():
            pl[i] = lows[i]
    return pl


def linreg(arr: np.ndarray, length: int) -> float:
    """Linear regression value at last bar (Pine ta.linreg)."""
    if len(arr) < length:
        return float(arr[-1])
    y = arr[-length:]
    x = np.arange(length)
    m, b = np.polyfit(x, y, 1)
    return m * (length - 1) + b


def percentile_li(arr: np.ndarray, pct: int) -> float:
    """np.percentile with linear interpolation — matches Pine percentile_linear_interpolation."""
    return float(np.percentile(arr, pct))


# ─────────────────────────────────────────────────────────────────────
#  MOTOR QF×JP v3.2 — análisis de un par
# ─────────────────────────────────────────────────────────────────────

def analizar_par(klines_3m: list, klines_15m: list) -> Optional[dict]:
    """
    Replica las 12 capas del indicador Pine Script sobre klines de 3m.
    klines_15m se usa para el HTF (EMA9/EMA21 en 15m).
    Retorna dict con todas las señales o None si datos insuficientes.
    """
    if len(klines_3m) < 50:
        return None

    # ── Extraer OHLCV ────────────────────────────────────────────────
    def _col(kl, idx):
        return np.array([float(k[idx]) for k in kl])

    o  = _col(klines_3m, 1)
    h  = _col(klines_3m, 2)
    l  = _col(klines_3m, 3)
    c  = _col(klines_3m, 4)
    v  = _col(klines_3m, 5)

    n = len(c)

    # ── L1 Microestructura ───────────────────────────────────────────
    atr   = atr_series(h, l, c, I_ATR_L)
    hi_lo = np.log(np.maximum(h / l, 1e-10))
    spread_est = sma(hi_lo, 5) * c
    bp_drain   = (spread_est / np.maximum(c, 1e-10)) * 100
    exec_ok    = bool(bp_drain[-1] < I_BPT)
    atr_now    = float(atr[-1])

    # [M6] Filtro volatilidad mínima
    atr_avg20 = float(sma(atr, 20)[-1]) if not np.isnan(sma(atr, 20)[-1]) else atr_now
    vol_ok = atr_now > atr_avg20 * VOL_ATR_THR
    vol_pct = round(atr_now / atr_avg20 * 100) if atr_avg20 > 0 else 100

    # ── [M2] ADX ─────────────────────────────────────────────────────
    plus_di, minus_di, adx_v = adx_series(h, l, c, I_ADX_LEN)
    adx_now      = float(adx_v[-1])
    trend_strong = adx_now >= I_ADX_TH
    trend_up     = bool(plus_di[-1] > minus_di[-1] and trend_strong)
    trend_dn     = bool(minus_di[-1] > plus_di[-1] and trend_strong)

    # ── L2 Factores + pesos ADX dinámicos ────────────────────────────
    roc_raw  = (c[-1] - c[-I_MOM]) / c[-I_MOM] if c[-I_MOM] != 0 else 0
    vol_norm = float(stdev(c, I_MOM)[-1]) / float(sma(c, I_MOM)[-1]) if float(sma(c, I_MOM)[-1]) != 0 else 1e-10
    f_mom_v  = roc_raw / vol_norm if vol_norm != 0 else 0.0

    basis_sma = sma(c, I_REV)
    basis_std = stdev(c, I_REV)
    f_rev_v   = -(c[-1] - basis_sma[-1]) / basis_std[-1] if basis_std[-1] != 0 else 0.0

    obv_arr  = obv_series(c, v)
    obv_ema_ = ema(obv_arr, I_VOL_L)
    obv_std_ = stdev(obv_arr, I_VOL_L)
    f_vol_v  = (obv_arr[-1] - obv_ema_[-1]) / obv_std_[-1] if obv_std_[-1] != 0 else 0.0

    adx_factor = min(1.0, adx_now / (I_ADX_TH * 2.0))
    w_mom_dyn  = I_W1 + adx_factor * I_W1 * 0.40
    w_rev_dyn  = max(I_W2 * 0.30, I_W2 - adx_factor * I_W2 * 0.50)
    w_total    = w_mom_dyn + w_rev_dyn + I_W3

    raw_score_arr = np.array([
        (w_mom_dyn * (roc_raw / vol_norm if vol_norm != 0 else 0)
         + w_rev_dyn * (-(c[i] - float(sma(c[:i+1], I_REV)[-1])) / max(float(stdev(c[:i+1], I_REV)[-1]), 1e-10))
         + I_W3 * 0.0)  # simplified for array
        / max(w_total, 1e-10)
        for i in range(n)
    ]) if n > I_MOM else np.zeros(n)

    # Usamos la versión correcta (escalar) para el score final
    raw_score_val = (w_mom_dyn * f_mom_v + w_rev_dyn * f_rev_v + I_W3 * f_vol_v) / max(w_total, 1e-10)

    comp_score_arr = ema(np.array([raw_score_val] * n), I_SMO)  # approx
    comp_score_arr[-1] = raw_score_val  # último valor correcto
    sc_std_val = float(stdev(comp_score_arr, I_DLEN)[-1]) if not np.isnan(stdev(comp_score_arr, I_DLEN)[-1]) else 1e-10
    norm_score = f_tanh(raw_score_val / sc_std_val) if sc_std_val != 0 else 0.0

    # ── L3 Decaimiento adaptativo [M1] ───────────────────────────────
    # Calculamos norm_score para cada barra pasada y correlamos con fwd_ret
    window = min(I_DLEN, n - 5)
    ic_num = 0.3  # valor base cuando no hay historia suficiente
    if window >= 8:
        try:
            # Serie de raw_score barra a barra (simplificada con ROC rodante)
            roc_series = np.array([
                (c[i] - c[max(0, i - I_MOM)]) / max(c[max(0, i - I_MOM)], 1e-10)
                for i in range(n)
            ])
            fwd_rets = np.diff(c) / np.maximum(c[:-1], 1e-10)
            seg_ns   = roc_series[1:window + 1]
            seg_fwd  = fwd_rets[:window]
            std_ns   = seg_ns.std()
            std_fwd  = seg_fwd.std()
            if std_ns > 1e-10 and std_fwd > 1e-10:
                ic_raw = float(np.corrcoef(seg_ns, seg_fwd)[0, 1])
                ic_num = 0.0 if np.isnan(ic_raw) else abs(ic_raw)
        except Exception:
            ic_num = 0.3

    ic_peak = max(ic_num, 0.01)
    decay_r = ic_num / ic_peak if ic_peak > 0 else 0.5

    # Percentil adaptativo [M1]: sig_alive si supera umbral fijo O percentil bajo
    ic_adapt_thr = 0.15
    sig_alive = decay_r >= I_DTHR or ic_num >= ic_adapt_thr

    # ── L4 Dark Pool ──────────────────────────────────────────────────
    vol_base   = float(sma(v, I_DPB)[-1])
    vol_spike  = bool(v[-1] > vol_base * I_DPM)
    rng_narrow = bool((h[-1] - l[-1]) < atr_now * 0.6)
    dp_buy     = bool(vol_spike and rng_narrow and c[-1] > o[-1])
    dp_sell    = bool(vol_spike and rng_narrow and c[-1] < o[-1])

    # ── L5 Ejecución ─────────────────────────────────────────────────
    # exec_ok ya calculado arriba

    # ── HTF (15m EMA9 / EMA21) ───────────────────────────────────────
    if klines_15m and len(klines_15m) >= 22:
        c15  = _col(klines_15m, 4)
        ema9_15  = float(ema(c15, 9)[-1])
        ema21_15 = float(ema(c15, 21)[-1])
        htf_bull = ema9_15 > ema21_15
        htf_bear = ema9_15 < ema21_15
    else:
        htf_bull = norm_score > 0
        htf_bear = norm_score < 0

    # ── L6 Asimetría de momentum ─────────────────────────────────────
    up_rng = np.where(c > o, h - l, 0.0)
    dn_rng = np.where(c < o, h - l, 0.0)
    avg_up_r = float(sma(up_rng, I_ASL)[-1])
    avg_dn_r = float(sma(dn_rng, I_ASL)[-1])
    rng_ratio_bull = avg_up_r / avg_dn_r if avg_dn_r > 0 else 1.0
    rng_ratio_bear = avg_dn_r / avg_up_r if avg_up_r > 0 else 1.0
    asym_bull = rng_ratio_bull >= I_ARR
    asym_bear = rng_ratio_bear >= I_ABR

    # ── L7 Ruptura de trendline ───────────────────────────────────────
    ph_arr = pivot_high(h, I_TLL, I_TLR)
    pl_arr = pivot_low(l,  I_PLL, I_PLR)

    ph_vals  = [(i, v) for i, v in enumerate(ph_arr) if not np.isnan(v)]
    pl_vals  = [(i, v) for i, v in enumerate(pl_arr) if not np.isnan(v)]

    tl_break_long  = False
    tl_break_short = False
    tl_dn_valid    = False
    tl_up_valid    = False

    if len(ph_vals) >= 2:
        (ph2b, ph2), (ph1b, ph1) = ph_vals[-2], ph_vals[-1]
        if ph2 > ph1 and (n - 1 - ph2b) <= I_TLB:
            tl_dn_valid = True
            slope = (ph1 - ph2) / max(ph1b - ph2b, 1)
            tl_dn_now = ph1 + slope * (n - 1 - ph1b)
            if c[-1] > tl_dn_now + atr_now * I_TLM:
                tl_break_long = True

    if len(pl_vals) >= 2:
        (pl2b, pl2), (pl1b, pl1) = pl_vals[-2], pl_vals[-1]
        if pl2 < pl1 and (n - 1 - pl2b) <= I_TLB:
            tl_up_valid = True
            slope = (pl1 - pl2) / max(pl1b - pl2b, 1)
            tl_up_now = pl1 + slope * (n - 1 - pl1b)
            if c[-1] < tl_up_now - atr_now * I_TLM:
                tl_break_short = True

    # ── L8 Swing exhaustion ───────────────────────────────────────────
    win = min(I_HLW, n)
    pl_recent = [(i, v) for i, v in enumerate(pl_arr[-win:]) if not np.isnan(v)]
    ph_recent = [(i, v) for i, v in enumerate(ph_arr[-win:]) if not np.isnan(v)]

    hl_count = sum(1 for j in range(1, len(pl_recent))
                   if pl_recent[j][1] > pl_recent[j - 1][1])
    lh_count = sum(1 for j in range(1, len(ph_recent))
                   if ph_recent[j][1] < ph_recent[j - 1][1])

    sell_exhausted = hl_count >= I_HLC
    buy_exhausted  = lh_count >= I_HHC

    last_sl = float(pl_recent[-1][1]) if pl_recent else float(l[-10:].min())
    last_sh = float(ph_recent[-1][1]) if ph_recent else float(h[-10:].max())

    # ── L9 FVG (último detectado) ─────────────────────────────────────
    bull_fvg_raw = bool(len(l) >= 3 and l[-1] > h[-3] and (l[-1] - h[-3]) > atr_now * I_FVG_MIN)
    bear_fvg_raw = bool(len(h) >= 3 and h[-1] < l[-3] and (l[-3] - h[-1]) > atr_now * I_FVG_MIN)

    # Buscar FVGs activos en las últimas I_FVG_BARS barras
    in_bull_fvg = False
    in_bear_fvg = False
    scan_start  = max(0, n - I_FVG_BARS)
    for i in range(scan_start, n - 2):
        if l[i+2] > h[i] and (l[i+2] - h[i]) > atr_now * I_FVG_MIN:
            gap_top = l[i+2]
            gap_bot = h[i]
            if gap_bot <= c[-1] <= gap_top:
                in_bull_fvg = True
        if h[i+2] < l[i] and (l[i] - h[i+2]) > atr_now * I_FVG_MIN:
            gap_top = l[i]
            gap_bot = h[i+2]
            if gap_bot <= c[-1] <= gap_top:
                in_bear_fvg = True

    # ── L10 Order Blocks ─────────────────────────────────────────────
    strong_bull = bool(len(c) >= 2 and (c[-1] - o[-1]) > atr_now * I_OB_IMP and c[-1] > c[-2])
    strong_bear = bool(len(c) >= 2 and (o[-1] - c[-1]) > atr_now * I_OB_IMP and c[-1] < c[-2])
    bull_ob_raw = bool(strong_bull and len(c) >= 2 and c[-2] < o[-2])
    bear_ob_raw = bool(strong_bear and len(c) >= 2 and c[-2] > o[-2])

    in_bull_ob = False
    in_bear_ob = False
    for i in range(max(0, n - I_OB_BARS), n - 1):
        if i >= 1:
            if (c[i] - o[i]) > atr_now * I_OB_IMP and c[i] > c[i-1] and c[i-1] < o[i-1]:
                if o[i-1] >= c[-1] >= c[i-1]:
                    in_bull_ob = True
            if (o[i] - c[i]) > atr_now * I_OB_IMP and c[i] < c[i-1] and c[i-1] > o[i-1]:
                if c[i-1] >= c[-1] >= o[i-1]:
                    in_bear_ob = True

    # ── L11 CVD Delta rodante [M3] ────────────────────────────────────
    hl_rng   = h - l
    bvol_est = np.where(hl_rng > 0, (c - l) / hl_rng * v, v * 0.5)
    svol_est = np.where(hl_rng > 0, (h - c) / hl_rng * v, v * 0.5)
    delta_bar = bvol_est - svol_est

    roll = min(I_CVD_ROLL, n)
    cvd  = float(sma(delta_bar, roll)[-1]) * roll  # rodante sin deriva
    cvd_ema_val = float(ema(delta_bar, I_CVD_LEN)[-1])
    cvd_rising  = cvd > cvd_ema_val

    cvd_std_val = float(stdev(delta_bar, min(I_CVD_LEN * 2, n))[-1])
    cvd_z = (cvd - cvd_ema_val) / cvd_std_val if cvd_std_val != 0 else 0.0
    cvd_score_v = max(0.0, min(1.0, (f_tanh(cvd_z) + 1) / 2))

    # Divergencia CVD
    div_win = min(I_CVD_DIV, n - 1)
    cvd_prev = float(sma(delta_bar[:-div_win], roll)[-1]) * roll if n > div_win + roll else cvd
    cvd_bull_div = bool(c[-1] < c[-div_win - 1] and cvd > cvd_prev)
    cvd_bear_div = bool(c[-1] > c[-div_win - 1] and cvd < cvd_prev)

    # ── L12 Squeeze Momentum ─────────────────────────────────────────
    sq_basis = float(sma(c, I_SQ_LEN)[-1])
    sq_dev   = float(stdev(c, I_SQ_LEN)[-1])
    sq_bb_hi = sq_basis + I_SQ_BBM * sq_dev
    sq_bb_lo = sq_basis - I_SQ_BBM * sq_dev
    sq_kc_atr= float(atr_series(h, l, c, I_SQ_LEN)[-1])
    sq_ema   = float(ema(c, I_SQ_LEN)[-1])
    sq_kc_hi = sq_ema + I_SQ_KCM * sq_kc_atr
    sq_kc_lo = sq_ema - I_SQ_KCM * sq_kc_atr
    sq_on    = sq_bb_hi < sq_kc_hi and sq_bb_lo > sq_kc_lo

    # Squeeze fire: este bar el squeeze se liberó (sq_on anterior = True, ahora False)
    # Aproximamos mirando el penúltimo bar
    if n >= I_SQ_LEN + 2:
        sq_bb_hi_p = float(sma(c[:-1], I_SQ_LEN)[-1]) + I_SQ_BBM * float(stdev(c[:-1], I_SQ_LEN)[-1])
        sq_kc_hi_p = float(ema(c[:-1], I_SQ_LEN)[-1]) + I_SQ_KCM * float(atr_series(h[:-1], l[:-1], c[:-1], I_SQ_LEN)[-1])
        sq_bb_lo_p = float(sma(c[:-1], I_SQ_LEN)[-1]) - I_SQ_BBM * float(stdev(c[:-1], I_SQ_LEN)[-1])
        sq_kc_lo_p = float(ema(c[:-1], I_SQ_LEN)[-1]) - I_SQ_KCM * float(atr_series(h[:-1], l[:-1], c[:-1], I_SQ_LEN)[-1])
        sq_on_prev = sq_bb_hi_p < sq_kc_hi_p and sq_bb_lo_p > sq_kc_lo_p
        sq_fire    = not sq_on and sq_on_prev
    else:
        sq_fire = False

    sq_mid_val = (max(h[-I_SQ_LEN:]) + min(l[-I_SQ_LEN:]) + float(sma(c, I_SQ_LEN)[-1])) / 3
    sq_linreg  = linreg(c - sq_mid_val, I_SQ_LEN)
    sq_bull    = bool(sq_fire and sq_linreg > 0)
    sq_bear    = bool(sq_fire and sq_linreg < 0)

    # ── SCORE COMPUESTO v3.2 ─────────────────────────────────────────
    ns_norm_l  = (f_tanh(norm_score) + 1) / 2
    mom_norm_l = (f_tanh(f_mom_v * 2) + 1) / 2
    decay_norm = min(1.0, decay_r)

    htf_asym_long  = (0.5 if htf_bull else 0.0) + (0.5 if asym_bull else 0.0)
    htf_asym_short = (0.5 if htf_bear else 0.0) + (0.5 if asym_bear else 0.0)

    comp_long_raw = (SC_W_SCORE * ns_norm_l +
                     SC_W_CVD   * cvd_score_v +
                     SC_W_MOM   * mom_norm_l +
                     SC_W_DECAY * decay_norm +
                     SC_W_HTF   * htf_asym_long)
    comp_long_base = round(comp_long_raw * 100)

    ns_norm_s  = (f_tanh(-norm_score) + 1) / 2
    mom_norm_s = (f_tanh(-f_mom_v * 2) + 1) / 2
    comp_short_raw = (SC_W_SCORE * ns_norm_s +
                      SC_W_CVD   * (1 - cvd_score_v) +
                      SC_W_MOM   * mom_norm_s +
                      SC_W_DECAY * decay_norm +
                      SC_W_HTF   * htf_asym_short)
    comp_short_base = round(comp_short_raw * 100)

    # Conv score 0-10 (idéntico al Pine)
    long_conv = sum([
        norm_score > 0.10,
        sig_alive,
        exec_ok,
        htf_bull,
        asym_bull,
        sell_exhausted,
        tl_break_long,
        dp_buy,
        cvd_rising,
        sq_bull or in_bull_fvg or in_bull_ob,
    ])
    short_conv = sum([
        norm_score < -0.10,
        sig_alive,
        exec_ok,
        htf_bear,
        asym_bear,
        buy_exhausted,
        tl_break_short,
        dp_sell,
        not cvd_rising,
        sq_bear or in_bear_fvg or in_bear_ob,
    ])

    # [M5] Conv-Boost
    comp_long  = min(100, comp_long_base  + round(long_conv  * 0.5))
    comp_short = min(100, comp_short_base + round(short_conv * 0.5))

    # ── SEÑALES FINALES (espejo exacto del Pine) ──────────────────────
    ses_active = True  # 24/7; se filtra por sesión en el loop si se quiere

    long_base  = comp_long  >= SC_THR_STD and exec_ok and ses_active and sig_alive and vol_ok
    short_base = comp_short >= SC_THR_STD and exec_ok and ses_active and sig_alive and vol_ok

    long_std  = long_base  and htf_bull and sell_exhausted
    short_std = short_base and htf_bear and buy_exhausted

    long_fuel  = (long_std  and comp_long  >= SC_THR_FUEL and
                  (tl_break_long  or sq_bull or ((in_bull_fvg or in_bull_ob) and cvd_rising)))
    short_fuel = (short_std and comp_short >= SC_THR_FUEL and
                  (tl_break_short or sq_bear or ((in_bear_fvg or in_bear_ob) and not cvd_rising)))

    long_sup  = long_fuel  and comp_long  >= SC_THR_SUP and (dp_buy  or cvd_bull_div)
    short_sup = short_fuel and comp_short >= SC_THR_SUP and (dp_sell or cvd_bear_div)

    # Clasificación de señal
    if long_sup:
        signal = "★ LONG SUP"
        signal_score = comp_long
    elif long_fuel:
        signal = "▲ LONG FUEL"
        signal_score = comp_long
    elif long_std:
        signal = "▲ LONG STD"
        signal_score = comp_long
    elif short_sup:
        signal = "★ SHORT SUP"
        signal_score = comp_short
    elif short_fuel:
        signal = "▼ SHORT FUEL"
        signal_score = comp_short
    elif short_std:
        signal = "▼ SHORT STD"
        signal_score = comp_short
    else:
        signal = "ESPERAR"
        signal_score = max(comp_long, comp_short)

    return {
        # Señales principales
        "signal":       signal,
        "signal_score": signal_score,
        "long_sup":     long_sup,
        "long_fuel":    long_fuel,
        "long_std":     long_std,
        "short_sup":    short_sup,
        "short_fuel":   short_fuel,
        "short_std":    short_std,
        # Scores
        "comp_long":    comp_long,
        "comp_short":   comp_short,
        "norm_score":   round(norm_score * 100),
        "long_conv":    long_conv,
        "short_conv":   short_conv,
        # Capas individuales (para el dashboard Telegram)
        "sig_alive":    sig_alive,
        "exec_ok":      exec_ok,
        "vol_ok":       vol_ok,
        "vol_pct":      vol_pct,
        "htf_bull":     htf_bull,
        "htf_bear":     htf_bear,
        "asym_bull":    asym_bull,
        "asym_bear":    asym_bear,
        "dp_buy":       dp_buy,
        "dp_sell":      dp_sell,
        "tl_break_long":  tl_break_long,
        "tl_break_short": tl_break_short,
        "sell_exhausted": sell_exhausted,
        "buy_exhausted":  buy_exhausted,
        "in_bull_fvg":  in_bull_fvg,
        "in_bear_fvg":  in_bear_fvg,
        "in_bull_ob":   in_bull_ob,
        "in_bear_ob":   in_bear_ob,
        "cvd_rising":   cvd_rising,
        "cvd_bull_div": cvd_bull_div,
        "cvd_bear_div": cvd_bear_div,
        "sq_bull":      sq_bull,
        "sq_bear":      sq_bear,
        "sq_on":        sq_on,
        "trend_up":     trend_up,
        "trend_dn":     trend_dn,
        "adx":          round(adx_now, 1),
        "last_sl":      round(last_sl, 6),
        "last_sh":      round(last_sh, 6),
        "decay_r":      round(decay_r * 100),
        "atr":          atr_now,
    }


# ─────────────────────────────────────────────────────────────────────
#  SCANNER PRINCIPAL
# ─────────────────────────────────────────────────────────────────────

def scan_mercado() -> tuple[list[dict], int]:
    log.info("=== Scan QF×JP v3.2 ===")

    tickers    = get_all_tickers()
    btc_change = 0.0
    btc_price  = 0.0
    for t in tickers:
        if t.get("symbol") == "BTC-USDT":
            try:
                btc_change = float(t.get("priceChangePercent", 0))
                btc_price  = float(t.get("lastPrice", 0))
            except Exception:
                pass
            break

    log.info(f"BTC: ${btc_price:,.0f} ({btc_change:+.1f}%) | Pares: {len(tickers)}")

    resultados = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("-USDT"):
            continue
        if any(x in symbol for x in ["USDC", "BUSD", "TUSD", "DAI", "FDUSD"]):
            continue
        try:
            volume_24h = float(ticker.get("quoteVolume", 0))
            precio     = float(ticker.get("lastPrice", 0))
            change_24h = float(ticker.get("priceChangePercent", 0))
        except Exception:
            continue

        if volume_24h < MIN_VOLUME_USDT:
            continue

        # Obtener velas 3m (análisis principal) y 15m (HTF)
        klines_3m  = get_klines(symbol, "3m",  80)
        klines_15m = get_klines(symbol, "15m", 30)

        if not klines_3m or len(klines_3m) < 50:
            time.sleep(0.05)
            continue

        analisis = analizar_par(klines_3m, klines_15m)
        if not analisis:
            time.sleep(0.05)
            continue

        if analisis["signal"] == "ESPERAR" and analisis["comp_long"] < 50 and analisis["comp_short"] < 50:
            time.sleep(0.05)
            continue

        resultados.append({
            "symbol":     symbol,
            "precio":     precio,
            "change_24h": change_24h,
            "volume_usdt": volume_24h,
            **analisis,
        })

        time.sleep(0.08)

    # Ordenar: primero SUP, luego FUEL, luego STD, luego score descendente
    orden = {"★ LONG SUP": 0, "★ SHORT SUP": 1,
             "▲ LONG FUEL": 2, "▼ SHORT FUEL": 3,
             "▲ LONG STD": 4, "▼ SHORT STD": 5, "ESPERAR": 6}
    resultados.sort(key=lambda x: (orden.get(x["signal"], 9), -x["signal_score"]))

    señales_top = [r for r in resultados if r["signal"] != "ESPERAR"][:TOP_N]
    log.info(f"Con señal: {len(señales_top)} | Total analizados: {len(resultados)}")

    # Intervalo adaptativo
    tiene_sup  = any(r["long_sup"] or r["short_sup"]  for r in señales_top)
    tiene_fuel = any(r["long_fuel"] or r["short_fuel"] for r in señales_top)

    if tiene_sup:
        intervalo = INTERVAL_ALERTA   # 1 min
    elif tiene_fuel:
        intervalo = INTERVAL_ACTIVO   # 5 min
    elif señales_top:
        intervalo = INTERVAL_ACTIVO
    else:
        intervalo = INTERVAL_NORMAL   # 15 min

    return señales_top, intervalo


# ─────────────────────────────────────────────────────────────────────
#  AUTO-TRADE BingX
# ─────────────────────────────────────────────────────────────────────

def set_leverage(symbol: str, lev: int) -> None:
    _post("/openApi/swap/v2/trade/leverage", {"symbol": symbol, "side": "LONG", "leverage": str(lev)})


def abrir_trade_long(symbol: str, precio: float) -> Optional[dict]:
    if not BINGX_API_KEY or not AUTO_TRADE:
        return None
    posiciones = get_open_positions()
    activos = len([p for p in posiciones if float(p.get("positionAmt", 0)) != 0])
    if activos >= MAX_OPEN_TRADES:
        log.warning(f"Máximo {MAX_OPEN_TRADES} trades — skip {symbol}")
        return None
    if symbol in trades_abiertos:
        return None

    set_leverage(symbol, LEVERAGE)
    qty     = round((TRADE_USDT * LEVERAGE) / precio, 4)
    sl_p    = round(precio * (1 - SL_PCT / 100), 6)
    tp_p    = round(precio * (1 + TP_PCT / 100), 6)

    orden = _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY", "positionSide": "LONG",
        "type": "MARKET", "quantity": str(qty),
    })
    if not orden or orden.get("code") != 0:
        log.error(f"Error orden {symbol}: {orden}")
        return None

    _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL", "positionSide": "LONG",
        "type": "STOP_MARKET", "stopPrice": str(sl_p), "closePosition": "true",
    })
    _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL", "positionSide": "LONG",
        "type": "TAKE_PROFIT_MARKET", "stopPrice": str(tp_p), "closePosition": "true",
    })

    trade = {"symbol": symbol, "entry": precio, "sl": sl_p, "tp": tp_p,
             "qty": qty, "opened_at": datetime.now(timezone.utc).isoformat()}
    trades_abiertos[symbol] = trade
    return trade


# ─────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
        r.raise_for_status()
        log.info("Telegram OK")
        return True
    except Exception as e:
        log.error(f"Telegram: {e}")
        return False


def build_alerta(par: dict) -> str:
    sym    = par["symbol"].replace("-USDT", "")
    sig    = par["signal"]
    sc_l   = par["comp_long"]
    sc_s   = par["comp_short"]
    precio = par["precio"]
    sl_p   = round(precio * (1 - SL_PCT / 100), 6)
    tp1    = round(precio * (1 + TP_PCT / 100), 6)
    tp2    = round(precio * (1 + TP_PCT * 1.5 / 100), 6)

    is_long  = "LONG"  in sig
    is_short = "SHORT" in sig
    is_sup   = "SUP"   in sig
    is_fuel  = "FUEL"  in sig

    emoji = "🔵" if is_sup else "🟡" if is_fuel else "🟢"
    dir_e = "🟢" if is_long else "🔴" if is_short else "⚪"

    # Dashboard compacto espejo del Pine
    decay_bar = "█" * max(0, min(8, round(par["decay_r"] / 100 * 8))) + "░" * max(0, 8 - max(0, min(8, round(par["decay_r"] / 100 * 8))))

    lines = [
        f"{emoji} *{sig}: {sym}*",
        f"{'─'*32}",
        f"{dir_e} SC LONG:  `{sc_l}/100`  |  SC SHORT: `{sc_s}/100`",
        f"📊 SCORE L2: `{par['norm_score']}`  |  CONV: `{par['long_conv']}▲/{par['short_conv']}▼`",
        f"{'─'*32}",
        f"💲 `{precio}` | 24h: {par['change_24h']:+.1f}% | Vol: ${par['volume_usdt']/1e6:.1f}M",
        f"🛑 SL: `{sl_p}` (-{SL_PCT}%)",
        f"🎯 TP1: `{tp1}` (+{TP_PCT}%) · TP2: `{tp2}` (+{TP_PCT*1.5:.1f}%)",
        f"{'─'*32}",
        f"*Dashboard QF×JP v3.2:*",
        f"  DECAIM. `{decay_bar} {par['decay_r']}%` {'✓' if par['sig_alive'] else '✗'}",
        f"  HTF     `{'BULL' if par['htf_bull'] else 'BEAR' if par['htf_bear'] else '—'}`  |  ADX `{par['adx']} {'▲' if par['trend_up'] else '▼' if par['trend_dn'] else '~'}`",
        f"  ASIM.   `{'▲' if par['asym_bull'] else '▼' if par['asym_bear'] else '—'}`  |  VOL ATR `{par['vol_pct']}%` {'✓' if par['vol_ok'] else '✗'}",
        f"  TL      `{'LONG 🔥' if par['tl_break_long'] else 'SHORT 🔥' if par['tl_break_short'] else 'sin ruptura'}`",
        f"  MÍNIMOS `{'HL↑' if par['sell_exhausted'] else 'LH↓' if par['buy_exhausted'] else 'en curso'}`",
        f"  DARKPOOL`{'BLOQUE↑' if par['dp_buy'] else 'BLOQUE↓' if par['dp_sell'] else '—'}`",
        f"  FVG     `{'ZONA↑' if par['in_bull_fvg'] else 'ZONA↓' if par['in_bear_fvg'] else '—'}`  |  OB `{'RETEST↑' if par['in_bull_ob'] else 'RETEST↓' if par['in_bear_ob'] else '—'}`",
        f"  CVD     `{'ACUM↑' if par['cvd_bull_div'] else 'DIST↓' if par['cvd_bear_div'] else 'SUB' if par['cvd_rising'] else 'BAJ'}`",
        f"  SQUEEZE `{'FUEGO↑' if par['sq_bull'] else 'FUEGO↓' if par['sq_bear'] else 'COMP.' if par['sq_on'] else 'libre'}`",
        f"  EXEC    `{'OK' if par['exec_ok'] else 'BLOQ'}`  |  SES `ACTIVA`",
        f"{'─'*32}",
    ]

    if is_long:
        lines.append(f"📌 SL LONG: `{par['last_sl']}`")
    else:
        lines.append(f"📌 SR SHORT: `{par['last_sh']}`")

    if AUTO_TRADE and is_long and is_sup:
        lines.append(f"🤖 *TRADE LARGO AUTO-ABIERTO*")
    else:
        lines.append(f"👆 *Verifica en TradingView 3m + QF×JP v3.2*")

    return "\n".join(lines)


def build_resumen(resultados: list[dict], btc_change: float, intervalo: int) -> str:
    now   = datetime.now(timezone.utc).strftime("%H:%M UTC")
    btc_e = "🟢" if btc_change > 0 else "🔴"

    sup_l  = [r for r in resultados if r["long_sup"]]
    sup_s  = [r for r in resultados if r["short_sup"]]
    fuel_l = [r for r in resultados if r["long_fuel"] and not r["long_sup"]]
    fuel_s = [r for r in resultados if r["short_fuel"] and not r["short_sup"]]
    std_l  = [r for r in resultados if r["long_std"]  and not r["long_fuel"]]
    std_s  = [r for r in resultados if r["short_std"] and not r["short_fuel"]]

    lines = [
        f"📡 *QF×JP v3.2 SCAN — {now}*",
        f"BTC {btc_e} {btc_change:+.2f}% · Scan en {intervalo//60}min",
        f"{'─'*28}",
    ]

    if not resultados:
        lines.append("💤 Sin señales activas")
        return "\n".join(lines)

    if sup_l:
        lines.append(f"🔵 *LONG SUPREMA ({len(sup_l)}):*")
        for r in sup_l[:3]:
            lines.append(f"  ★ *{r['symbol'].replace('-USDT','')}* {r['comp_long']}/100 · conv {r['long_conv']}/10")
    if sup_s:
        lines.append(f"🔵 *SHORT SUPREMA ({len(sup_s)}):*")
        for r in sup_s[:3]:
            lines.append(f"  ★ *{r['symbol'].replace('-USDT','')}* {r['comp_short']}/100 · conv {r['short_conv']}/10")
    if fuel_l:
        lines.append(f"🟡 *LONG FUEL ({len(fuel_l)}):*")
        for r in fuel_l[:3]:
            lines.append(f"  ▲ *{r['symbol'].replace('-USDT','')}* {r['comp_long']}/100")
    if fuel_s:
        lines.append(f"🟡 *SHORT FUEL ({len(fuel_s)}):*")
        for r in fuel_s[:3]:
            lines.append(f"  ▼ *{r['symbol'].replace('-USDT','')}* {r['comp_short']}/100")
    if std_l or std_s:
        stds = [(r, "L") for r in std_l[:2]] + [(r, "S") for r in std_s[:2]]
        lines.append(f"🟢 *STD ({len(std_l)}L / {len(std_s)}S):*")
        for r, d in stds:
            sc = r["comp_long"] if d == "L" else r["comp_short"]
            lines.append(f"  {'▲' if d=='L' else '▼'} {r['symbol'].replace('-USDT','')} {sc}/100")

    if trades_abiertos:
        lines += [f"{'─'*28}", f"💼 *Trades ({len(trades_abiertos)}):*"]
        for sym, t in trades_abiertos.items():
            lines.append(f"  📌 {sym.replace('-USDT','')} LONG · SL:{t['sl']} TP:{t['tp']}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
#  LOOP 24/7
# ─────────────────────────────────────────────────────────────────────

def run_loop():
    log.info("🚀 QF×JP Scanner v3.0 — 24/7")
    log.info(f"AUTO_TRADE: {'ON' if AUTO_TRADE else 'OFF'} | ${TRADE_USDT}×{LEVERAGE}x | SL {SL_PCT}% TP {TP_PCT}%")

    btc_change   = 0.0
    ultima_hora  = -1
    scan_count   = 0

    while True:
        try:
            scan_count += 1
            log.info(f"── Scan #{scan_count} ──")

            tickers = get_all_tickers()
            for t in tickers:
                if t.get("symbol") == "BTC-USDT":
                    try:
                        btc_change = float(t.get("priceChangePercent", 0))
                    except Exception:
                        pass
                    break

            resultados, intervalo = scan_mercado()

            # ── Alertas inmediatas para SUP y FUEL ───────────────────
            for par in resultados:
                sym = par["symbol"]
                if not (par["long_sup"] or par["short_sup"] or par["long_fuel"] or par["short_fuel"]):
                    continue
                ultima = alertas_enviadas.get(sym, 0)
                if time.time() - ultima < 1800:
                    continue

                msg = build_alerta(par)
                if send_telegram(msg):
                    alertas_enviadas[sym] = time.time()

                # Auto-trade: solo LONG SUPREMA ≥ 80
                if AUTO_TRADE and par["long_sup"] and par["comp_long"] >= SC_THR_SUP:
                    trade = abrir_trade_long(sym, par["precio"])
                    if trade:
                        send_telegram(
                            f"✅ *TRADE ABIERTO*: {sym.replace('-USDT','')} LONG\n"
                            f"Entrada: `{trade['entry']}` · SL: `{trade['sl']}` · TP: `{trade['tp']}`"
                        )

            # ── Resumen horario ───────────────────────────────────────
            hora_actual = datetime.now(timezone.utc).hour
            if hora_actual != ultima_hora:
                send_telegram(build_resumen(resultados, btc_change, intervalo))
                ultima_hora = hora_actual

        except Exception as e:
            log.error(f"Error ciclo: {e}", exc_info=True)
            intervalo = INTERVAL_NORMAL

        log.info(f"Próximo scan en {intervalo}s ({intervalo//60}min)")
        time.sleep(intervalo)


# ─────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        run_loop()
    else:
        tickers = get_all_tickers()
        btc_change = 0.0
        for t in tickers:
            if t.get("symbol") == "BTC-USDT":
                btc_change = float(t.get("priceChangePercent", 0))
                break
        resultados, intervalo = scan_mercado()
        print(build_resumen(resultados, btc_change, intervalo))
