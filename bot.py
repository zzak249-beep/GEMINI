#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  TRADING BOT V13 — APEX ULTRA EDITION                       ║
║  Dual TF (5m entradas + 1H tendencia)                        ║
║  + VWAP Institucional + Squeeze Momentum Anti-Lateral        ║
║  + Pin Bar / Engulfing / Momentum / Inside Bar               ║
║  + Breakeven @ 1.5R | Trailing Stop dinámico                 ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import time
import hmac
import hashlib
import json
import logging
import math
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import requests
import numpy as np

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("BotV13")

# ─────────────────────────────────────────────
#  CONFIG (Railway env vars)
# ─────────────────────────────────────────────
API_KEY    = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_API_SECRET", "")
BASE_URL   = "https://open-api.bingx.com"

TIMEFRAME       = os.getenv("TIMEFRAME", "5m")
EMA_FAST        = int(os.getenv("EMA_FAST", "7"))
EMA_SLOW        = int(os.getenv("EMA_SLOW", "17"))
SLOPE_MIN_DEG   = float(os.getenv("SLOPE_LIMIT", "30"))
ADX_MIN         = float(os.getenv("ADX_MIN", "25"))
RSI_LOW         = float(os.getenv("RSI_LOW", "30"))
RSI_HIGH        = float(os.getenv("RSI_HIGH", "70"))
VOL_MULT        = float(os.getenv("VOL_MULT", "1.2"))
TP_MULT         = float(os.getenv("TP_MULT", "3.0"))
SL_ATR_MULT     = float(os.getenv("SL_ATR_MULT", "1.5"))
MIN_RR          = float(os.getenv("MIN_RR", "2.5"))
MIN_SCORE       = float(os.getenv("MIN_SCORE", "55"))
MAX_TRADES      = int(os.getenv("MAX_TRADES", "9"))
MAX_ORDER_USDT  = float(os.getenv("MAX_ORDER_USDT", "40"))
MIN_ORDER_USDT  = float(os.getenv("MIN_ORDER_USDT", "7"))
RISK_PCT        = float(os.getenv("RISK_PERCENT", "1.5")) / 100
MAX_MARGIN_PCT  = float(os.getenv("MAX_MARGIN_PCT", "25")) / 100
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL", "60"))
SQUEEZE_LEN     = int(os.getenv("SQUEEZE_LEN", "20"))
BB_MULT         = float(os.getenv("BB_MULT", "2.0"))
KC_MULT         = float(os.getenv("KC_MULT", "1.5"))

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
open_trades: dict = {}
trade_lock = threading.Lock()

# ─────────────────────────────────────────────
#  BINGX API
# ─────────────────────────────────────────────
def _sign(params: dict) -> str:
    qs = urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()

def _request(method: str, path: str, params: dict = None, data: dict = None):
    params = params or {}
    ts = str(int(time.time() * 1000))
    params["timestamp"] = ts
    params["signature"] = _sign(params)
    headers = {"X-BX-APIKEY": API_KEY}
    url = BASE_URL + path
    try:
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=10)
        else:
            r = requests.post(url, params=params, json=data, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"API {method} {path}: {e}")
        return None

def get_balance() -> float:
    r = _request("GET", "/openApi/swap/v2/user/balance")
    try:
        return float(r["data"]["balance"]["availableMargin"])
    except Exception:
        return 0.0

def get_klines(symbol: str, interval: str, limit: int = 200) -> list:
    r = _request("GET", "/openApi/swap/v3/quote/klines", {
        "symbol": symbol, "interval": interval, "limit": limit
    })
    try:
        return r["data"]
    except Exception:
        return []

def get_symbols() -> list:
    r = _request("GET", "/openApi/swap/v2/quote/contracts")
    try:
        return [s["symbol"] for s in r["data"] if s.get("status") == 1]
    except Exception:
        return []

def get_open_positions() -> list:
    r = _request("GET", "/openApi/swap/v2/user/positions")
    try:
        return [p for p in r["data"] if float(p.get("positionAmt", 0)) != 0]
    except Exception:
        return []

def place_order(symbol: str, side: str, qty: float, sl: float, tp: float, leverage: int) -> Optional[dict]:
    # Set leverage
    _request("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol, "side": side, "leverage": leverage
    })
    # Margin mode isolated
    _request("POST", "/openApi/swap/v2/trade/marginType", {
        "symbol": symbol, "marginType": "ISOLATED"
    })
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type": "MARKET",
        "quantity": qty,
        "stopLoss": json.dumps({"type": "MARK_PRICE", "stopPrice": sl, "price": sl, "workingType": "MARK_PRICE"}),
        "takeProfit": json.dumps({"type": "MARK_PRICE", "stopPrice": tp, "price": tp, "workingType": "MARK_PRICE"}),
    }
    return _request("POST", "/openApi/swap/v2/trade/order", params)

def close_position(symbol: str, side: str, qty: float):
    close_side = "SELL" if side == "LONG" else "BUY"
    _request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": close_side,
        "positionSide": side,
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": True,
    })

# ─────────────────────────────────────────────
#  KLINE PARSING
# ─────────────────────────────────────────────
def parse_klines(raw: list) -> dict:
    """Convert raw klines to numpy arrays."""
    if not raw or len(raw) < 50:
        return {}
    opens  = np.array([float(k["open"])   for k in raw])
    highs  = np.array([float(k["high"])   for k in raw])
    lows   = np.array([float(k["low"])    for k in raw])
    closes = np.array([float(k["close"])  for k in raw])
    vols   = np.array([float(k["volume"]) for k in raw])
    times  = np.array([int(k["time"])     for k in raw])
    return {"o": opens, "h": highs, "l": lows, "c": closes, "v": vols, "t": times}

# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def ema(arr: np.ndarray, period: int) -> np.ndarray:
    result = np.zeros_like(arr)
    k = 2.0 / (period + 1)
    result[0] = arr[0]
    for i in range(1, len(arr)):
        result[i] = arr[i] * k + result[i - 1] * (1 - k)
    return result

def sma(arr: np.ndarray, period: int) -> np.ndarray:
    result = np.full_like(arr, np.nan)
    for i in range(period - 1, len(arr)):
        result[i] = arr[i - period + 1:i + 1].mean()
    return result

def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    tr = np.concatenate([[h[0] - l[0]], tr])
    result = np.zeros_like(tr)
    result[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result

def rsi(c: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.zeros(len(c))
    avg_loss = np.zeros(len(c))
    avg_gain[period] = gain[:period].mean()
    avg_loss[period] = loss[:period].mean()
    for i in range(period + 1, len(c)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period
    rs = np.where(avg_loss == 0, 100, avg_gain / avg_loss)
    return np.where(avg_loss == 0, 100, 100 - 100 / (1 + rs))

def adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> tuple:
    n = len(c)
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    tr_arr   = np.zeros(n)
    for i in range(1, n):
        up   = h[i] - h[i - 1]
        down = l[i - 1] - l[i]
        plus_dm[i]  = up   if up > down and up > 0   else 0
        minus_dm[i] = down if down > up and down > 0 else 0
        tr_arr[i]   = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr14    = np.zeros(n)
    pDM14    = np.zeros(n)
    mDM14    = np.zeros(n)
    atr14[period]  = tr_arr[1:period + 1].sum()
    pDM14[period]  = plus_dm[1:period + 1].sum()
    mDM14[period]  = minus_dm[1:period + 1].sum()
    for i in range(period + 1, n):
        atr14[i] = atr14[i - 1] - atr14[i - 1] / period + tr_arr[i]
        pDM14[i] = pDM14[i - 1] - pDM14[i - 1] / period + plus_dm[i]
        mDM14[i] = mDM14[i - 1] - mDM14[i - 1] / period + minus_dm[i]
    pDI = np.where(atr14 > 0, 100 * pDM14 / atr14, 0)
    mDI = np.where(atr14 > 0, 100 * mDM14 / atr14, 0)
    dx  = np.where((pDI + mDI) > 0, 100 * abs(pDI - mDI) / (pDI + mDI), 0)
    adx_arr = np.zeros(n)
    adx_arr[2 * period] = dx[period:2 * period + 1].mean()
    for i in range(2 * period + 1, n):
        adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period
    return adx_arr, pDI, mDI

def bollinger(c: np.ndarray, period: int, mult: float) -> tuple:
    mid   = sma(c, period)
    std   = np.array([c[max(0, i - period + 1):i + 1].std() for i in range(len(c))])
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower

def keltner(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int, mult: float) -> tuple:
    mid   = sma(c, period)
    r     = atr(h, l, c, period)
    upper = mid + mult * r
    lower = mid - mult * r
    return upper, mid, lower

def squeeze_momentum(h: np.ndarray, l: np.ndarray, c: np.ndarray,
                     period: int = 20, bb_mult: float = 2.0, kc_mult: float = 1.5) -> np.ndarray:
    """
    Returns array: True = squeeze ON (no trade), False = squeeze OFF (trade allowed).
    Squeeze ON when BB is INSIDE KC (market compressing).
    """
    bb_upper, _, bb_lower = bollinger(c, period, bb_mult)
    kc_upper, _, kc_lower = keltner(h, l, c, period, kc_mult)
    sqz_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    return sqz_on

def vwap(h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray, t: np.ndarray) -> np.ndarray:
    """
    Session VWAP — resets at midnight UTC (daily session).
    """
    typical = (h + l + c) / 3
    result  = np.zeros(len(c))
    cum_tv  = 0.0
    cum_v   = 0.0
    prev_day = -1
    for i in range(len(c)):
        ts_s   = t[i] / 1000
        day    = int(ts_s // 86400)
        if day != prev_day:
            cum_tv  = 0.0
            cum_v   = 0.0
            prev_day = day
        cum_tv  += typical[i] * v[i]
        cum_v   += v[i]
        result[i] = cum_tv / cum_v if cum_v > 0 else typical[i]
    return result

def slope_deg(e: np.ndarray, atr_arr: np.ndarray, lookback: int = 3) -> float:
    """ATR-normalised slope in degrees (like Pine Script)."""
    if len(e) < lookback + 1:
        return 0.0
    diff  = e[-1] - e[-lookback - 1]
    norm  = atr_arr[-1] if atr_arr[-1] > 0 else 1e-9
    angle = math.atan(diff / norm) * (180 / math.pi)
    return angle

# ─────────────────────────────────────────────
#  CANDLE PATTERNS
# ─────────────────────────────────────────────
def detect_patterns(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
                    atr_arr: np.ndarray) -> dict:
    """Detect last-candle patterns. Returns dict with pattern names."""
    i = -2  # last closed candle (index -1 is current forming)
    body    = abs(c[i] - o[i])
    rng     = h[i] - l[i]
    atr_val = atr_arr[i]
    upper_w = h[i] - max(c[i], o[i])
    lower_w = min(c[i], o[i]) - l[i]
    patterns = {}

    # Pin Bar
    if rng > 0:
        is_bull_pin = (lower_w >= 2 * body) and (lower_w >= 0.6 * rng) and (body < 0.35 * rng)
        is_bear_pin = (upper_w >= 2 * body) and (upper_w >= 0.6 * rng) and (body < 0.35 * rng)
        if is_bull_pin: patterns["pin_bull"] = True
        if is_bear_pin: patterns["pin_bear"] = True

    # Engulfing
    prev_body = abs(c[i - 1] - o[i - 1])
    if prev_body > 0:
        bull_eng = (c[i] > o[i]) and (o[i] < o[i - 1]) and (c[i] > c[i - 1]) and (body >= prev_body * 1.1)
        bear_eng = (c[i] < o[i]) and (o[i] > o[i - 1]) and (c[i] < c[i - 1]) and (body >= prev_body * 1.1)
        if bull_eng: patterns["engulf_bull"] = True
        if bear_eng: patterns["engulf_bear"] = True

    # Momentum candle (big body, small wicks)
    if atr_val > 0 and body >= 1.5 * atr_val and body >= 0.7 * rng:
        if c[i] > o[i]: patterns["momentum_bull"] = True
        else:            patterns["momentum_bear"] = True

    # Inside Bar
    if h[i] <= h[i - 1] and l[i] >= l[i - 1]:
        patterns["inside_bar"] = True

    return patterns

# ─────────────────────────────────────────────
#  H1 CONTEXT
# ─────────────────────────────────────────────
def get_h1_context(symbol: str) -> dict:
    raw = get_klines(symbol, "1h", 100)
    d   = parse_klines(raw)
    if not d:
        return {"bias": "NEUTRAL", "score_bonus": 0}
    c, h, l = d["c"], d["h"], d["l"]
    e7   = ema(c, EMA_FAST)
    e17  = ema(c, EMA_SLOW)
    atr_ = atr(h, l, c, 14)
    adx_, pdi, mdi = adx(h, l, c, 14)
    sqz  = squeeze_momentum(h, l, c, SQUEEZE_LEN, BB_MULT, KC_MULT)
    vwap_ = vwap(h, l, c, d["v"], d["t"])

    bull_h1 = (e7[-1] > e17[-1]) and (c[-1] > vwap_[-1]) and (pdi[-1] > mdi[-1])
    bear_h1 = (e7[-1] < e17[-1]) and (c[-1] < vwap_[-1]) and (mdi[-1] > pdi[-1])
    h1_slope = slope_deg(e7, atr_)

    # S/R zones (simple swing high/low in last 20 bars)
    lookback = 20
    swing_hi = h[-lookback:].max()
    swing_lo = l[-lookback:].min()
    near_resist = c[-1] >= swing_hi * 0.995
    near_support = c[-1] <= swing_lo * 1.005

    bias = "BULL" if bull_h1 else ("BEAR" if bear_h1 else "NEUTRAL")
    bonus = 0
    if bias != "NEUTRAL" and abs(h1_slope) >= SLOPE_MIN_DEG:
        bonus = 20  # golden setup bonus
    if near_resist and bias == "BULL":
        bonus -= 15
    if near_support and bias == "BEAR":
        bonus -= 15
    if sqz[-1]:
        bonus -= 10  # H1 also squeezing = reduce score

    return {
        "bias": bias,
        "score_bonus": bonus,
        "near_resist": near_resist,
        "near_support": near_support,
        "h1_slope": h1_slope,
        "h1_sqz": bool(sqz[-1]),
        "vwap_h1": vwap_[-1],
    }

# ─────────────────────────────────────────────
#  SIGNAL SCORING
# ─────────────────────────────────────────────
def score_signal(d: dict, direction: str, h1: dict) -> tuple[float, dict]:
    """
    Returns (score, details). direction = 'LONG' | 'SHORT'.
    Score ≥ MIN_SCORE = valid signal.
    """
    c, h, l, v = d["c"], d["h"], d["l"], d["v"]
    e_fast = ema(c, EMA_FAST)
    e_slow = ema(c, EMA_SLOW)
    atr_   = atr(h, l, c, 14)
    rsi_   = rsi(c, 14)
    adx_, pdi, mdi = adx(h, l, c, 14)
    vol_ma = sma(v, 20)
    sqz    = squeeze_momentum(h, l, c, SQUEEZE_LEN, BB_MULT, KC_MULT)
    vwap_  = vwap(h, l, c, v, d["t"])
    pats   = detect_patterns(h[..., :], h, l, c, atr_)  # pass arrays correctly
    slope  = slope_deg(e_fast, atr_)

    score   = 0
    details = {}

    # --- 5m EMA alignment ---
    ema_bull = e_fast[-1] > e_slow[-1]
    ema_bear = e_fast[-1] < e_slow[-1]
    if direction == "LONG"  and ema_bull: score += 15; details["ema"] = "✅ bull"
    if direction == "SHORT" and ema_bear: score += 15; details["ema"] = "✅ bear"
    else: details["ema"] = "❌"

    # --- Slope ---
    if direction == "LONG"  and slope >= SLOPE_MIN_DEG:  score += 15; details["slope"] = f"✅ {slope:.1f}°"
    elif direction == "SHORT" and slope <= -SLOPE_MIN_DEG: score += 15; details["slope"] = f"✅ {slope:.1f}°"
    else: details["slope"] = f"❌ {slope:.1f}°"

    # --- ADX ---
    if adx_[-1] >= ADX_MIN:
        score += 10; details["adx"] = f"✅ {adx_[-1]:.1f}"
        if direction == "LONG"  and pdi[-1] > mdi[-1]: score += 5
        if direction == "SHORT" and mdi[-1] > pdi[-1]: score += 5
    else:
        details["adx"] = f"❌ {adx_[-1]:.1f}"

    # --- Squeeze OFF (key filter from V6.5) ---
    if not sqz[-1]:
        score += 15; details["squeeze"] = "✅ OFF (moving)"
    else:
        score -= 20; details["squeeze"] = "🚫 ON (lateral — skip)"

    # --- VWAP filter (key filter from V6.5) ---
    above_vwap = c[-1] > vwap_[-1]
    below_vwap = c[-1] < vwap_[-1]
    if direction == "LONG"  and above_vwap: score += 15; details["vwap"] = f"✅ above {vwap_[-1]:.4f}"
    elif direction == "SHORT" and below_vwap: score += 15; details["vwap"] = f"✅ below {vwap_[-1]:.4f}"
    else: details["vwap"] = f"❌ wrong side of VWAP"

    # --- RSI ---
    if RSI_LOW < rsi_[-1] < RSI_HIGH:
        score += 5; details["rsi"] = f"✅ {rsi_[-1]:.1f}"
    else:
        score -= 5; details["rsi"] = f"❌ {rsi_[-1]:.1f}"

    # --- Volume ---
    if vol_ma[-1] > 0 and v[-2] >= vol_ma[-2] * VOL_MULT:
        score += 5; details["vol"] = "✅ high"
    else:
        details["vol"] = "low"

    # --- Candle patterns ---
    if direction == "LONG":
        if pats.get("pin_bull"):       score += 15; details["pattern"] = "📌 Pin Bull"
        elif pats.get("engulf_bull"):  score += 15; details["pattern"] = "🔄 Engulf Bull"
        elif pats.get("momentum_bull"):score += 10; details["pattern"] = "🚀 Momentum Bull"
        elif pats.get("inside_bar"):   score += 5;  details["pattern"] = "📦 Inside Bar"
        else:                                        details["pattern"] = "none"
    else:
        if pats.get("pin_bear"):       score += 15; details["pattern"] = "📌 Pin Bear"
        elif pats.get("engulf_bear"):  score += 15; details["pattern"] = "🔄 Engulf Bear"
        elif pats.get("momentum_bear"):score += 10; details["pattern"] = "🚀 Momentum Bear"
        elif pats.get("inside_bar"):   score += 5;  details["pattern"] = "📦 Inside Bar"
        else:                                        details["pattern"] = "none"

    # --- H1 confirmation ---
    score += h1["score_bonus"]
    details["h1"] = f"{h1['bias']} bonus={h1['score_bonus']:+d}"
    if h1.get("h1_sqz"):
        details["h1"] += " H1_SQZ⚠️"

    return score, details

# ─────────────────────────────────────────────
#  POSITION SIZING (fixed from V11)
# ─────────────────────────────────────────────
def calc_qty(balance: float, entry: float, sl: float, leverage: int,
             symbol_info: dict) -> tuple[float, float]:
    """
    Returns (qty, notional_usdt).
    Formula: notional = risk_usdt / dist_sl_pct
    """
    risk_usdt  = balance * RISK_PCT
    dist_sl    = abs(entry - sl) / entry
    if dist_sl <= 0:
        return 0.0, 0.0
    notional   = risk_usdt / dist_sl
    # Cap by margin
    max_margin = balance * MAX_MARGIN_PCT
    max_notional_by_margin = max_margin * leverage
    notional   = min(notional, max_notional_by_margin, MAX_ORDER_USDT * leverage)
    notional   = max(notional, MIN_ORDER_USDT * leverage)
    qty        = notional / entry
    # Round to exchange precision
    step = float(symbol_info.get("tradeMinQuantity", 0.001))
    qty  = math.floor(qty / step) * step
    return round(qty, 6), round(notional / leverage, 2)

def get_symbol_info(symbol: str) -> dict:
    r = _request("GET", "/openApi/swap/v2/quote/contracts")
    if r:
        for s in r.get("data", []):
            if s["symbol"] == symbol:
                return s
    return {}

# ─────────────────────────────────────────────
#  TRADE MANAGEMENT (breakeven + trailing)
# ─────────────────────────────────────────────
def manage_open_trades():
    """Breakeven at 1.5R, trailing stop after 2R."""
    positions = get_open_positions()
    pos_symbols = {p["symbol"]: p for p in positions}

    with trade_lock:
        for sym, trade in list(open_trades.items()):
            if sym not in pos_symbols:
                log.info(f"  ✅ {sym} cerrada (salió del exchange)")
                del open_trades[sym]
                continue

            pos   = pos_symbols[sym]
            price = float(pos.get("markPrice", trade["entry"]))
            entry = trade["entry"]
            sl    = trade["sl"]
            tp    = trade["tp"]
            side  = trade["side"]
            risk  = abs(entry - sl)

            if risk <= 0:
                continue

            pnl_r = (price - entry) / risk if side == "LONG" else (entry - price) / risk

            # Breakeven at 1.5R
            if pnl_r >= 1.5 and not trade.get("be_done"):
                new_sl = entry + 0.0005 * entry if side == "LONG" else entry - 0.0005 * entry
                log.info(f"  🔒 {sym} BREAKEVEN @ {new_sl:.4f} (R={pnl_r:.2f})")
                trade["sl"]     = new_sl
                trade["be_done"] = True

            # Trailing: move SL to lock 50% of profit beyond 2R
            if pnl_r >= 2.0 and trade.get("be_done"):
                if side == "LONG":
                    trail_sl = price - risk * 0.5
                    if trail_sl > trade["sl"]:
                        trade["sl"] = trail_sl
                        log.info(f"  📈 {sym} trailing SL → {trail_sl:.4f}")
                else:
                    trail_sl = price + risk * 0.5
                    if trail_sl < trade["sl"]:
                        trade["sl"] = trail_sl
                        log.info(f"  📉 {sym} trailing SL → {trail_sl:.4f}")

# ─────────────────────────────────────────────
#  MAIN SCAN
# ─────────────────────────────────────────────
def scan_symbol(symbol: str, balance: float):
    with trade_lock:
        if symbol in open_trades:
            return
        if len(open_trades) >= MAX_TRADES:
            return

    # Fetch 5m data
    raw5 = get_klines(symbol, TIMEFRAME, 200)
    d5   = parse_klines(raw5)
    if not d5 or len(d5["c"]) < 60:
        return

    c, h, l = d5["c"], d5["h"], d5["l"]
    e_fast = ema(c, EMA_FAST)
    e_slow = ema(c, EMA_SLOW)
    atr_   = atr(h, l, c, 14)
    adx_, pdi, mdi = adx(h, l, c, 14)
    sqz    = squeeze_momentum(h, l, c, SQUEEZE_LEN, BB_MULT, KC_MULT)
    vwap_  = vwap(h, l, c, d5["v"], d5["t"])

    # Quick pre-filter to avoid wasting API calls on bad setups
    if adx_[-1] < ADX_MIN * 0.8:
        return
    if sqz[-1]:  # squeeze on = skip immediately
        return

    # Determine direction
    ema_cross_up   = e_fast[-2] <= e_slow[-2] and e_fast[-1] > e_slow[-1]
    ema_cross_down = e_fast[-2] >= e_slow[-2] and e_fast[-1] < e_slow[-1]
    ema_bull = e_fast[-1] > e_slow[-1]
    ema_bear = e_fast[-1] < e_slow[-1]

    direction = None
    if ema_bull and c[-1] > vwap_[-1]:  direction = "LONG"
    if ema_bear and c[-1] < vwap_[-1]:  direction = "SHORT"
    if direction is None:
        return

    # H1 context
    h1 = get_h1_context(symbol)
    if h1["bias"] == "NEUTRAL":
        return
    if direction == "LONG"  and h1["bias"] != "BULL": return
    if direction == "SHORT" and h1["bias"] != "BEAR": return

    # Full scoring
    score, details = score_signal(d5, direction, h1)
    entry = c[-1]
    atr_v = atr_[-1]

    # SL on signal candle (tighter, from Pine Script V6.5)
    if direction == "LONG":
        sl = min(l[-2], l[-1]) - atr_v * 0.3
        tp = entry + (entry - sl) * TP_MULT
    else:
        sl = max(h[-2], h[-1]) + atr_v * 0.3
        tp = entry - (sl - entry) * TP_MULT

    rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0

    # Status log
    rsi_v = rsi(c, 14)[-1]
    log.info(f"  {symbol} | {direction} | Score={score:.0f} | ADX={adx_[-1]:.1f} | "
             f"RSI={rsi_v:.1f} | SQZ={'ON' if sqz[-1] else 'OFF'} | "
             f"VWAP={'above' if c[-1]>vwap_[-1] else 'below'} | R:R={rr:.1f}")

    if score < MIN_SCORE:
        return
    if rr < MIN_RR:
        return

    # Get leverage from symbol info
    sinfo    = get_symbol_info(symbol)
    leverage = int(sinfo.get("maxLeverage", 5))
    leverage = min(leverage, 10)

    qty, margin = calc_qty(balance, entry, sl, leverage, sinfo)
    if qty <= 0 or margin < MIN_ORDER_USDT:
        return

    log.info(f"  🚄 ENTRADA {direction} {symbol} | entry={entry:.4f} SL={sl:.4f} TP={tp:.4f} "
             f"| qty={qty} margin={margin}U | score={score:.0f}")
    for k, v in details.items():
        log.info(f"      {k}: {v}")

    side = "BUY" if direction == "LONG" else "SELL"
    result = place_order(symbol, side, qty, sl, tp, leverage)
    if result and result.get("code") == 0:
        with trade_lock:
            open_trades[symbol] = {
                "side": direction, "entry": entry,
                "sl": sl, "tp": tp, "qty": qty,
                "score": score, "time": time.time(),
            }
        log.info(f"  ✅ Orden ejecutada: {symbol}")
    else:
        log.warning(f"  ❌ Error orden {symbol}: {result}")

# ─────────────────────────────────────────────
#  BANNER
# ─────────────────────────────────────────────
def print_banner(balance: float, symbols_count: int, open_count: int):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  🚀 EMA+ADX+VWAP+SQUEEZE Elite V13.0 — APEX ULTRA           ║
║  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   ║
║  🔀 Dual TF: {TIMEFRAME} entradas + 1H tendencia                    ║
║  EMA {EMA_FAST}/{EMA_SLOW} | Slope≥{SLOPE_MIN_DEG}° | ADX≥{ADX_MIN} | Score≥{MIN_SCORE}         ║
║  TP: {TP_MULT}x | SL: candle+0.3ATR | Min R:R: {MIN_RR}            ║
║  🌊 VWAP: ✅ filtro dirección institucional                  ║
║  🔵 Squeeze: ✅ anti-lateral (BB vs KC)                      ║
║  🔒 Breakeven @ 1.5R | Trailing @ 2R                        ║
║  📐 Patrones: PIN+ENGULF+MOMENTUM+INSIDE                     ║
║  💰 Balance: {balance:.2f} USDT | Trades: {open_count}/{MAX_TRADES}          ║
║  📊 Símbolos: {symbols_count} | 🕐 {now}  ║
╚══════════════════════════════════════════════════════════════╝""")

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def main():
    log.info("🚀 Bot V13 Apex Ultra iniciando...")
    if not API_KEY or not API_SECRET:
        log.error("❌ BINGX_API_KEY / BINGX_API_SECRET no configuradas")
        return

    cycle = 0
    while True:
        cycle += 1
        try:
            balance    = get_balance()
            positions  = get_open_positions()
            symbols    = get_symbols()

            # Sync open_trades with real positions
            pos_syms = {p["symbol"] for p in positions}
            with trade_lock:
                for sym in list(open_trades.keys()):
                    if sym not in pos_syms:
                        log.info(f"  ✅ {sym} cerrada — removida del registro")
                        del open_trades[sym]

            print_banner(balance, len(symbols), len(open_trades))
            log.info(f"📋 Ciclo #{cycle} | Balance: {balance:.2f}U | Posiciones: {len(positions)}")

            # Manage existing trades (breakeven/trailing)
            if open_trades:
                manage_open_trades()

            # Skip scan if max trades reached
            if len(open_trades) >= MAX_TRADES:
                log.info(f"  ⏸️  Max trades ({MAX_TRADES}) alcanzado — esperando...")
            elif balance < MIN_ORDER_USDT:
                log.warning(f"  ⚠️  Balance insuficiente: {balance:.2f} USDT")
            else:
                log.info(f"  🔍 Escaneando {len(symbols)} símbolos...")
                for symbol in symbols:
                    with trade_lock:
                        if len(open_trades) >= MAX_TRADES:
                            break
                    try:
                        scan_symbol(symbol, balance)
                        time.sleep(0.15)  # rate limit
                    except Exception as e:
                        log.debug(f"  {symbol}: {e}")

        except Exception as e:
            log.error(f"❌ Error ciclo principal: {e}")

        log.info(f"  ⏰ Esperando {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
