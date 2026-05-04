"""
ZigZag Institutional Elite V6 — Trading Bot
BingX Futures | Railway Deployment | Telegram Reports
"""

import os
import time
import hmac
import hashlib
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
import pandas as pd
import numpy as np
from telegram import Bot
from telegram.constants import ParseMode

# ─────────────────────────────────────────────
# CONFIG — from environment variables
# ─────────────────────────────────────────────
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SYMBOL           = os.environ.get("SYMBOL", "BTC-USDT")
TIMEFRAME        = os.environ.get("TIMEFRAME", "15m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT", "1.0"))   # % of balance per trade
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN", "5"))
VOL_MULT         = float(os.environ.get("VOL_MULT", "1.5"))
ATR_LEN          = int(os.environ.get("ATR_LEN", "14"))
TP_MULT          = float(os.environ.get("TP_MULT", "2.0"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS", "60"))
LEVERAGE         = int(os.environ.get("LEVERAGE", "5"))

BINGX_BASE = "https://open-api.bingx.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BINGX API LAYER
# ─────────────────────────────────────────────

def _sign(params: dict, secret: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def bingx_get(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params, BINGX_SECRET_KEY)
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    r = requests.get(BINGX_BASE + path, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def bingx_post(path: str, payload: dict) -> dict:
    payload["timestamp"] = int(time.time() * 1000)
    payload["signature"] = _sign(payload, BINGX_SECRET_KEY)
    headers = {"X-BX-APIKEY": BINGX_API_KEY, "Content-Type": "application/json"}
    r = requests.post(BINGX_BASE + path, json=payload, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def get_balance() -> float:
    data = bingx_get("/openApi/swap/v2/user/balance")
    for asset in data.get("data", {}).get("balance", []):
        if asset.get("asset") == "USDT":
            return float(asset.get("availableMargin", 0))
    return 0.0


def get_position() -> Optional[dict]:
    data = bingx_get("/openApi/swap/v2/user/positions", {"symbol": SYMBOL})
    positions = data.get("data", [])
    for p in positions:
        if float(p.get("positionAmt", 0)) != 0:
            return p
    return None


def set_leverage():
    try:
        bingx_post("/openApi/swap/v2/trade/leverage", {
            "symbol": SYMBOL,
            "side": "LONG",
            "leverage": LEVERAGE,
        })
        bingx_post("/openApi/swap/v2/trade/leverage", {
            "symbol": SYMBOL,
            "side": "SHORT",
            "leverage": LEVERAGE,
        })
    except Exception as e:
        log.warning(f"Leverage set error: {e}")


def open_order(side: str, qty: float, sl: float, tp: float) -> dict:
    """side: 'BUY' or 'SELL'"""
    payload = {
        "symbol": SYMBOL,
        "side": side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type": "MARKET",
        "quantity": round(qty, 4),
        "stopLoss": json.dumps({"type": "MARK_PRICE", "stopPrice": round(sl, 2), "workingType": "MARK_PRICE"}),
        "takeProfit": json.dumps({"type": "MARK_PRICE", "stopPrice": round(tp, 2), "workingType": "MARK_PRICE"}),
    }
    return bingx_post("/openApi/swap/v2/trade/order", payload)


def close_position(position: dict) -> dict:
    side = "SELL" if float(position["positionAmt"]) > 0 else "BUY"
    pos_side = "LONG" if float(position["positionAmt"]) > 0 else "SHORT"
    qty = abs(float(position["positionAmt"]))
    payload = {
        "symbol": SYMBOL,
        "side": side,
        "positionSide": pos_side,
        "type": "MARKET",
        "quantity": round(qty, 4),
    }
    return bingx_post("/openApi/swap/v2/trade/order", payload)


def get_klines(limit: int = 200) -> pd.DataFrame:
    interval_map = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
                    "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D"}
    params = {
        "symbol": SYMBOL,
        "interval": interval_map.get(TIMEFRAME, "15m"),
        "limit": limit,
    }
    data = bingx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data", [])
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.sort_values("open_time").reset_index(drop=True)

# ─────────────────────────────────────────────
# ZIGZAG STRATEGY ENGINE
# ─────────────────────────────────────────────

def pivot_high(high: pd.Series, left: int, right: int) -> pd.Series:
    result = pd.Series(np.nan, index=high.index)
    for i in range(left, len(high) - right):
        window = high.iloc[i - left: i + right + 1]
        if high.iloc[i] == window.max():
            result.iloc[i] = high.iloc[i]
    return result


def pivot_low(low: pd.Series, left: int, right: int) -> pd.Series:
    result = pd.Series(np.nan, index=low.index)
    for i in range(left, len(low) - right):
        window = low.iloc[i - left: i + right + 1]
        if low.iloc[i] == window.min():
            result.iloc[i] = low.iloc[i]
    return result


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_signals(df: pd.DataFrame) -> dict:
    n = len(df)
    if n < max(PIVOT_LEN * 2 + 2, ATR_LEN + 1, 21):
        return {"signal": None}

    ph = pivot_high(df["high"], PIVOT_LEN, PIVOT_LEN)
    pl = pivot_low(df["low"], PIVOT_LEN, PIVOT_LEN)

    # Carry-forward last known peak/valley
    peak   = ph.ffill()
    valley = pl.ffill()

    vol_ma = df["volume"].rolling(20).mean()
    inst_vol = df["volume"] > (vol_ma * VOL_MULT)

    atr_val = atr(df["high"], df["low"], df["close"], ATR_LEN)

    close   = df["close"]
    open_   = df["open"]

    # Current bar (last completed)
    i = n - 1
    prev_close = close.iloc[i - 1]
    curr_close = close.iloc[i]
    curr_peak   = peak.iloc[i]
    curr_valley = valley.iloc[i]
    curr_inst   = inst_vol.iloc[i]
    curr_atr    = atr_val.iloc[i]
    bullish_body = curr_close > open_.iloc[i]
    bearish_body = curr_close < open_.iloc[i]

    long_breakout  = (prev_close <= curr_peak) and (curr_close > curr_peak) and curr_inst and bullish_body
    short_breakout = (prev_close >= curr_valley) and (curr_close < curr_valley) and curr_inst and bearish_body

    signal = None
    sl = tp = None

    if long_breakout:
        signal = "LONG"
        sl = curr_valley if not np.isnan(curr_valley) else curr_close - curr_atr * 2
        tp = curr_close + (curr_close - sl) * TP_MULT

    elif short_breakout:
        signal = "SHORT"
        sl = curr_peak if not np.isnan(curr_peak) else curr_close + curr_atr * 2
        tp = curr_close - (sl - curr_close) * TP_MULT

    return {
        "signal": signal,
        "close": curr_close,
        "peak": curr_peak,
        "valley": curr_valley,
        "sl": sl,
        "tp": tp,
        "atr": curr_atr,
        "inst_vol": curr_inst,
        "vol_ratio": round(df["volume"].iloc[i] / vol_ma.iloc[i], 2) if vol_ma.iloc[i] > 0 else 0,
    }


def calc_quantity(balance: float, entry: float, sl: float) -> float:
    risk_amount = balance * (RISK_PERCENT / 100)
    sl_distance = abs(entry - sl)
    if sl_distance == 0:
        return 0
    qty = (risk_amount * LEVERAGE) / entry
    return max(round(qty, 4), 0.001)

# ─────────────────────────────────────────────
# TELEGRAM REPORTS
# ─────────────────────────────────────────────

async def _send(msg: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)


def send_telegram(msg: str):
    try:
        asyncio.run(_send(msg))
    except Exception as e:
        log.warning(f"Telegram error: {e}")


def report_signal(sig: dict, qty: float, balance: float):
    direction = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    msg = (
        f"<b>⚡ ZigZag Elite — ENTRADA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Par:</b> {SYMBOL} | {TIMEFRAME}\n"
        f"<b>Dirección:</b> {direction}\n"
        f"<b>Precio entrada:</b> <code>{sig['close']:.4f}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.4f}</code>\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.4f}</code>\n"
        f"<b>RR Ratio:</b> 1:{TP_MULT}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Cantidad:</b>    {qty} contratos\n"
        f"<b>Balance:</b>     {balance:.2f} USDT\n"
        f"<b>Riesgo:</b>      {RISK_PERCENT}% = {balance * RISK_PERCENT / 100:.2f} USDT\n"
        f"<b>Volumen:</b>     {sig['vol_ratio']}x MA (⚡ institucional)\n"
        f"<b>ATR:</b>         {sig['atr']:.4f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    send_telegram(msg)


def report_close(position: dict, reason: str):
    amt = float(position.get("positionAmt", 0))
    entry = float(position.get("avgPrice", 0))
    direction = "🟢 LONG" if amt > 0 else "🔴 SHORT"
    pnl = float(position.get("unrealizedProfit", 0))
    pnl_emoji = "✅" if pnl >= 0 else "❌"
    msg = (
        f"<b>{pnl_emoji} ZigZag Elite — CIERRE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Par:</b> {SYMBOL}\n"
        f"<b>Dirección:</b> {direction}\n"
        f"<b>Entrada:</b>   <code>{entry:.4f}</code>\n"
        f"<b>PnL:</b>       <code>{pnl:+.4f} USDT</code>\n"
        f"<b>Razón:</b>     {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    send_telegram(msg)


def report_error(context: str, error: Exception):
    msg = (
        f"⚠️ <b>Error en Bot</b>\n"
        f"<b>Contexto:</b> {context}\n"
        f"<b>Error:</b> <code>{str(error)[:300]}</code>\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )
    send_telegram(msg)


def report_startup(balance: float):
    msg = (
        f"🚀 <b>ZigZag Institutional Elite V6</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Estado:</b> ✅ Bot activo\n"
        f"<b>Par:</b>       {SYMBOL}\n"
        f"<b>Temporalidad:</b> {TIMEFRAME}\n"
        f"<b>Apalancamiento:</b> {LEVERAGE}x\n"
        f"<b>Riesgo/trade:</b>  {RISK_PERCENT}%\n"
        f"<b>TP Ratio:</b>    1:{TP_MULT}\n"
        f"<b>Vol Mult:</b>    {VOL_MULT}x\n"
        f"<b>Balance:</b>    {balance:.2f} USDT\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    send_telegram(msg)

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main():
    log.info("=== ZigZag Institutional Elite V6 starting ===")
    set_leverage()
    balance = get_balance()
    log.info(f"Balance: {balance:.2f} USDT")
    report_startup(balance)

    consecutive_errors = 0

    while True:
        try:
            df = get_klines(limit=200)
            sig = compute_signals(df)
            position = get_position()
            balance = get_balance()

            log.info(
                f"Signal={sig['signal']} | Close={sig.get('close','?')} "
                f"| Peak={sig.get('peak','?'):.4f} | Valley={sig.get('valley','?'):.4f} "
                f"| InstVol={sig.get('inst_vol','?')}"
            )

            # ── ENTRY ──────────────────────────────────
            if sig["signal"] and position is None:
                qty = calc_quantity(balance, sig["close"], sig["sl"])
                if qty <= 0:
                    log.warning("Quantity calculated as 0, skipping.")
                else:
                    side = "BUY" if sig["signal"] == "LONG" else "SELL"
                    result = open_order(side, qty, sig["sl"], sig["tp"])
                    log.info(f"Order placed: {result}")
                    report_signal(sig, qty, balance)

            consecutive_errors = 0

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            send_telegram("🛑 <b>Bot detenido manualmente</b>")
            break
        except Exception as e:
            consecutive_errors += 1
            log.exception(f"Loop error #{consecutive_errors}: {e}")
            if consecutive_errors <= 3:
                report_error("Loop principal", e)
            if consecutive_errors >= 10:
                report_error("CRÍTICO: 10 errores consecutivos, deteniendo bot", e)
                break

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
