"""
ZigZag Institutional Elite V6 - MULTI-SYMBOL SCANNER
634 símbolos BingX Futures | Railway | Telegram
"""
import os, time, hmac, hashlib, json, asyncio, logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np
from telegram import Bot
from telegram.constants import ParseMode

# ── CONFIG ────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

TIMEFRAME        = os.environ.get("TIMEFRAME",        "15m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.0"))
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN",        "3"))   # era 5 → más señales
VOL_MULT         = float(os.environ.get("VOL_MULT",        "1.2")) # era 1.5 → más permisivo
ATR_LEN          = int(os.environ.get("ATR_LEN",          "14"))
TP_MULT          = float(os.environ.get("TP_MULT",         "2.0"))
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "120"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "3"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "0"))
ONLY_BOT_TRADES  = os.environ.get("ONLY_BOT_TRADES", "false").lower() == "true"
MIN_SCORE        = float(os.environ.get("MIN_SCORE",      "20.0")) # score mínimo para entrada

_raw = os.environ.get("CUSTOM_SYMBOLS", "")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE   = "https://open-api.bingx.com"
INTERVAL_MAP = {"1m":"1m","3m":"3m","5m":"5m","15m":"15m",
                "30m":"30m","1h":"1H","4h":"4H","1d":"1D"}

FALLBACK_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "MATIC-USDT","UNI-USDT","LTC-USDT","BCH-USDT","ATOM-USDT",
    "XLM-USDT","ETC-USDT","NEAR-USDT","APT-USDT","OP-USDT",
    "ARB-USDT","FIL-USDT","ICP-USDT","HBAR-USDT","AAVE-USDT",
    "GRT-USDT","MKR-USDT","CRV-USDT","LDO-USDT","RUNE-USDT",
    "INJ-USDT","SUI-USDT","TIA-USDT","SEI-USDT","WIF-USDT",
    "PEPE-USDT","FLOKI-USDT","WLD-USDT","GMX-USDT","DYDX-USDT",
    "IMX-USDT","GALA-USDT","CHZ-USDT","ZEC-USDT","DASH-USDT",
    "KAVA-USDT","CELO-USDT","FET-USDT","OCEAN-USDT","AGIX-USDT",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

# ── BINGX API ─────────────────────────────────────────────────────────────────
def _sign(params: dict) -> str:
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(BINGX_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()

def bx_get(path: str, params: dict = None) -> dict:
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.get(BINGX_BASE + path, params=p,
                     headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()

def bx_post(path: str, payload: dict) -> dict:
    p = dict(payload)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.post(BINGX_BASE + path, json=p,
                      headers={"X-BX-APIKEY": BINGX_API_KEY,
                               "Content-Type": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json()

def get_balance() -> float:
    try:
        data = bx_get("/openApi/swap/v2/user/balance")
        log.info(f"RAW balance response: {json.dumps(data)[:400]}")
        d = data.get("data", {})
        if not isinstance(d, dict):
            log.warning(f"data is not dict: {type(d)} — {str(d)[:200]}")
            return 0.0

        bal = d.get("balance", {})

        # Buscar en bal si es dict
        if isinstance(bal, dict):
            for field in ("availableMargin", "available", "crossWalletBalance",
                          "walletBalance", "equity", "balance"):
                v = bal.get(field)
                if v is not None and v != "" and float(v) != 0.0:
                    result = float(v)
                    log.info(f"Balance: {result:.4f} USDT (bal.{field})")
                    return result
            # Si todos son 0, usar equity (puede ser 0 con posiciones)
            for field in ("equity", "walletBalance", "availableMargin"):
                v = bal.get(field)
                if v is not None and v != "":
                    result = float(v)
                    log.info(f"Balance (zero-ok): {result:.4f} USDT (bal.{field})")
                    return result

        # data directamente tiene los campos
        if d.get("asset") == "USDT":
            for field in ("availableMargin", "available", "walletBalance", "equity"):
                v = d.get(field)
                if v is not None and v != "":
                    result = float(v)
                    log.info(f"Balance: {result:.4f} USDT (data.{field})")
                    return result

        # bal es lista
        if isinstance(bal, list):
            for asset in bal:
                if isinstance(asset, dict) and asset.get("asset") == "USDT":
                    for field in ("availableMargin", "available", "walletBalance", "equity"):
                        v = asset.get(field)
                        if v is not None and v != "":
                            result = float(v)
                            log.info(f"Balance: {result:.4f} USDT (list.{field})")
                            return result

        log.warning(f"Balance not found. Full data: {data}")
        return 0.0
    except Exception as e:
        log.error(f"get_balance error: {e}")
        return 0.0

def get_all_positions() -> dict:
    try:
        data = bx_get("/openApi/swap/v2/user/positions", {})
        result = {}
        positions = data.get("data", [])
        if isinstance(positions, list):
            for p in positions:
                if isinstance(p, dict) and float(p.get("positionAmt", 0)) != 0:
                    result[p["symbol"]] = p
        return result
    except Exception as e:
        log.error(f"get_positions error: {e}")
        return {}

# ── SYMBOL DISCOVERY ──────────────────────────────────────────────────────────
def _symbols_from_contracts() -> list:
    data = bx_get("/openApi/swap/v2/quote/contracts", {})
    contracts = data.get("data", [])
    if not isinstance(contracts, list) or len(contracts) == 0:
        raise ValueError(f"Empty contracts: {str(data)[:200]}")
    usdt = [c for c in contracts
            if isinstance(c, dict) and c.get("asset","") == "USDT" and c.get("status") == 1]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c, dict) and c.get("asset","") == "USDT"]
    if not usdt:
        usdt = [c for c in contracts if isinstance(c, dict) and str(c.get("symbol","")).endswith("-USDT")]
    if not usdt:
        raise ValueError("No USDT contracts found")
    usdt.sort(key=lambda x: float(x.get("tradeAmount", 0) or 0), reverse=True)
    syms = [c["symbol"] for c in usdt if c.get("symbol")]
    log.info(f"[contracts] {len(syms)} USDT symbols")
    return syms

def _symbols_from_ticker() -> list:
    data = bx_get("/openApi/swap/v2/quote/ticker", {})
    tickers = data.get("data", [])
    if not isinstance(tickers, list) or len(tickers) == 0:
        raise ValueError(f"Empty ticker: {str(data)[:200]}")
    usdt = [t for t in tickers if isinstance(t, dict) and str(t.get("symbol","")).endswith("-USDT")]
    if not usdt:
        raise ValueError("No USDT tickers")
    usdt.sort(key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
    syms = [t["symbol"] for t in usdt if t.get("symbol")]
    log.info(f"[ticker] {len(syms)} USDT symbols")
    return syms

def _symbols_from_premium_index() -> list:
    data = bx_get("/openApi/swap/v2/quote/premiumIndex", {})
    items = data.get("data", [])
    if not isinstance(items, list) or len(items) == 0:
        raise ValueError(f"Empty premiumIndex: {str(data)[:200]}")
    usdt = [i for i in items if isinstance(i, dict) and str(i.get("symbol","")).endswith("-USDT")]
    if not usdt:
        raise ValueError("No USDT in premiumIndex")
    syms = [i["symbol"] for i in usdt if i.get("symbol")]
    log.info(f"[premiumIndex] {len(syms)} USDT symbols")
    return syms

def get_all_symbols(limit: int = 0) -> list:
    for fn in (_symbols_from_contracts, _symbols_from_ticker, _symbols_from_premium_index):
        try:
            syms = fn()
            if syms:
                result = syms if limit == 0 else syms[:limit]
                log.info(f"✅ {len(result)} symbols loaded (limit={'ALL' if limit==0 else limit})")
                return result
        except Exception as e:
            log.warning(f"Symbol loader {fn.__name__} failed: {e}")
    log.warning(f"⚠️ All endpoints failed — using fallback ({len(FALLBACK_SYMBOLS)} symbols)")
    return FALLBACK_SYMBOLS if limit == 0 else FALLBACK_SYMBOLS[:limit]

def set_lev(symbol: str):
    for side in ("LONG", "SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol": symbol, "side": side, "leverage": LEVERAGE})
        except Exception:
            pass

def open_order(symbol: str, side: str, qty: float, sl: float, tp: float) -> dict:
    payload = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type":         "MARKET",
        "quantity":     round(qty, 4),
        "stopLoss":     json.dumps({"type":"MARK_PRICE","stopPrice":round(sl,6),"workingType":"MARK_PRICE"}),
        "takeProfit":   json.dumps({"type":"MARK_PRICE","stopPrice":round(tp,6),"workingType":"MARK_PRICE"}),
    }
    return bx_post("/openApi/swap/v2/trade/order", payload)

def get_klines(symbol: str, limit: int = 200) -> pd.DataFrame:
    params = {"symbol": symbol,
              "interval": INTERVAL_MAP.get(TIMEFRAME, "15m"),
              "limit": limit}
    data = bx_get("/openApi/swap/v3/quote/klines", params)
    rows = data.get("data", [])
    if not rows or not isinstance(rows, list):
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.sort_values("open_time").reset_index(drop=True)

# ── STRATEGY ──────────────────────────────────────────────────────────────────
def ph_series(high, left, right):
    """Pivot High: máximo local con 'left' velas a la izq y 'right' a la der."""
    out = pd.Series(np.nan, index=high.index)
    for i in range(left, len(high) - right):
        window = high.iloc[i - left: i + right + 1]
        if high.iloc[i] == window.max():
            out.iloc[i] = high.iloc[i]
    return out

def pl_series(low, left, right):
    """Pivot Low: mínimo local."""
    out = pd.Series(np.nan, index=low.index)
    for i in range(left, len(low) - right):
        window = low.iloc[i - left: i + right + 1]
        if low.iloc[i] == window.min():
            out.iloc[i] = low.iloc[i]
    return out

def calc_atr(high, low, close, period):
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def scan_symbol(symbol: str):
    """
    Señal LONG:  precio cruza por encima del último Pivot High con volumen elevado.
    Señal SHORT: precio cruza por debajo del último Pivot Low con volumen elevado.
    
    Cambios vs V5:
    - PIVOT_LEN reducido (3 por defecto) → más pivots confirmados
    - VOL_MULT reducido (1.2) → barra de volumen más baja
    - Se usan las últimas 2 velas cerradas para detectar cruce (más robusto)
    - Se omite el filtro cc > co (dirección de la vela) para más señales
    """
    try:
        df = get_klines(symbol)
        min_bars = max(PIVOT_LEN * 2 + 2, ATR_LEN + 1, 30)
        if df.empty or len(df) < min_bars:
            return None

        peak   = ph_series(df["high"], PIVOT_LEN, PIVOT_LEN).ffill()
        valley = pl_series(df["low"],  PIVOT_LEN, PIVOT_LEN).ffill()
        vol_ma = df["volume"].rolling(20).mean()
        atr_s  = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)

        # Usamos la última vela CERRADA (i-1) para señal, i es la vela en curso
        i = len(df) - 2   # última vela cerrada
        if i < PIVOT_LEN + 1:
            return None

        prev_close  = float(df["close"].iloc[i - 1])
        curr_close  = float(df["close"].iloc[i])
        cpeak       = float(peak.iloc[i])
        cvalley     = float(valley.iloc[i])
        catr        = float(atr_s.iloc[i])
        vol_now     = float(df["volume"].iloc[i])
        vma         = float(vol_ma.iloc[i])

        if np.isnan(cpeak) or np.isnan(cvalley) or np.isnan(catr) or vma <= 0:
            return None

        vratio = round(vol_now / vma, 2)
        high_vol = vratio >= VOL_MULT

        # Cruce hacia arriba del Pivot High
        is_long  = (prev_close <= cpeak) and (curr_close > cpeak) and high_vol
        # Cruce hacia abajo del Pivot Low
        is_short = (prev_close >= cvalley) and (curr_close < cvalley) and high_vol

        if not is_long and not is_short:
            return None

        direction = "LONG" if is_long else "SHORT"
        if direction == "LONG":
            sl = max(cvalley, curr_close - catr * 2)
            tp = curr_close + (curr_close - sl) * TP_MULT
        else:
            sl = min(cpeak, curr_close + catr * 2)
            tp = curr_close - (sl - curr_close) * TP_MULT

        dist = abs(curr_close - sl)
        if dist == 0:
            return None

        rr    = abs(tp - curr_close) / dist
        score = min(vratio * 20, 40) + min((catr / curr_close) * 5000, 30) + min(rr * 10, 30)

        if score < MIN_SCORE:
            return None

        return {
            "symbol":    symbol,
            "signal":    direction,
            "close":     curr_close,
            "sl":        sl,
            "tp":        tp,
            "atr":       catr,
            "vol_ratio": vratio,
            "score":     round(score, 1),
            "rr":        round(rr, 2),
        }
    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

def calc_qty(balance: float, entry: float, sl: float) -> float:
    risk = balance * (RISK_PERCENT / 100)
    dist = abs(entry - sl)
    if dist == 0:
        return 0
    return max(round((risk * LEVERAGE) / entry, 4), 0.001)

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def _send(msg: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def tg(msg: str):
    try:
        asyncio.run(_send(msg))
    except Exception as e:
        log.warning(f"Telegram send error: {e}")

def tg_startup(balance: float, symbols: list):
    tg(
        f"🚀 <b>ZigZag Elite V6 — MULTI-SCANNER</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Bot activo\n"
        f"<b>Monedas:</b> {len(symbols)} | <b>TF:</b> {TIMEFRAME}\n"
        f"<b>Lev:</b> {LEVERAGE}x | <b>Riesgo:</b> {RISK_PERCENT}%\n"
        f"<b>Max trades:</b> {MAX_OPEN_TRADES} | <b>TP:</b> 1:{TP_MULT}\n"
        f"<b>Pivot:</b> {PIVOT_LEN} | <b>Vol:</b> {VOL_MULT}x | <b>MinScore:</b> {MIN_SCORE}\n"
        f"<b>Balance Futuros:</b> {balance:.2f} USDT\n"
        f"<b>Top pares:</b> {', '.join(symbols[:8])}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals: list, total: int, open_count: int):
    if not signals:
        return
    lines = [
        f"🔍 <b>{len(signals)} señal(es) / {total} monedas</b> | Trades: {open_count}/{MAX_OPEN_TRADES}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals[:10]:
        e = "🟢" if s["signal"] == "LONG" else "🔴"
        lines.append(f"{e} <b>{s['symbol']}</b> Score:{s['score']} Vol:{s['vol_ratio']}x RR:1:{s['rr']}")
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_entry(sig: dict, qty: float, balance: float):
    d = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    tg(
        f"<b>⚡ ENTRADA — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.6g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.6g}</code>\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.6g}</code>\n"
        f"<b>RR:</b> 1:{sig['rr']} | <b>Vol:</b> {sig['vol_ratio']}x ⚡\n"
        f"<b>Qty:</b> {qty} | <b>Riesgo:</b> {balance*RISK_PERCENT/100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_debug(balance: float, positions: dict, symbols: list):
    pos_list = list(positions.keys()) if positions else ["ninguna"]
    tg(
        f"🔧 <b>DEBUG — Conexión verificada</b>\n"
        f"<b>Balance API:</b> {balance:.4f} USDT\n"
        f"<b>Posiciones detectadas:</b> {len(positions)}\n"
        f"<b>Pares:</b> {', '.join(pos_list[:5])}\n"
        f"<b>Símbolos a escanear:</b> {len(symbols)}\n"
        f"<b>PIVOT_LEN:</b> {PIVOT_LEN} | <b>VOL_MULT:</b> {VOL_MULT} | <b>MIN_SCORE:</b> {MIN_SCORE}\n"
        f"<b>Chat ID:</b> <code>{TELEGRAM_CHAT_ID}</code>"
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== ZigZag Institutional Elite V6 MULTI-SCANNER starting ===")

    if CUSTOM_SYMBOLS:
        symbols = CUSTOM_SYMBOLS
        log.info(f"Using CUSTOM_SYMBOLS: {len(symbols)}")
    else:
        symbols = get_all_symbols(MAX_SYMBOLS)

    if not symbols:
        log.error("CRITICAL: No symbols! Using fallback.")
        symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()

    log.info(f"Balance: {balance:.4f} USDT | Symbols: {len(symbols)} | Open: {len(positions)}")
    log.info(f"Config — PIVOT_LEN:{PIVOT_LEN} VOL_MULT:{VOL_MULT} MIN_SCORE:{MIN_SCORE}")

    tg_debug(balance, positions, symbols)
    tg_startup(balance, symbols)

    log.info("Pre-setting leverage...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))
    log.info(f"Leverage ready for {len(symbols)} symbols. Entering main loop.")

    errors = 0
    entered_this_cycle: set = set()

    while True:
        t0 = time.time()
        try:
            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)

            log.info(f"── Cycle | {balance:.4f} USDT | {open_count}/{MAX_OPEN_TRADES} trades | {len(symbols)} symbols ──")

            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futures = {ex.submit(scan_symbol, s): s for s in symbols}
                for f in as_completed(futures):
                    r = f.result()
                    if r:
                        signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Signals found: {len(signals)}/{len(symbols)}")

            if signals:
                tg_scan(signals, len(symbols), open_count)
                # Log top 5 para diagnóstico
                for s in signals[:5]:
                    log.info(f"  → {s['symbol']} {s['signal']} score={s['score']} vol={s['vol_ratio']}x rr={s['rr']}")

            for sig in signals:
                sym = sig["symbol"]
                if sym in positions or sym in entered_this_cycle:
                    continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info(f"Max open trades ({MAX_OPEN_TRADES}) reached.")
                    break
                if balance < 5:
                    log.warning(f"Balance too low ({balance:.2f} USDT), skipping.")
                    break

                qty = calc_qty(balance, sig["close"], sig["sl"])
                if qty <= 0:
                    continue

                side = "BUY" if sig["signal"] == "LONG" else "SELL"
                try:
                    set_lev(sym)
                    res = open_order(sym, side, qty, sig["sl"], sig["tp"])
                    log.info(f"✅ ORDER {sym} {side} qty={qty} | {res}")
                    tg_entry(sig, qty, balance)
                    entered_this_cycle.add(sym)
                    open_count += 1
                    time.sleep(0.5)
                except Exception as e:
                    log.error(f"Order error {sym}: {e}")
                    tg(f"⚠️ Error orden <b>{sym}</b>: <code>{str(e)[:150]}</code>")

            entered_this_cycle.clear()
            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot detenido manualmente</b>")
            break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors <= 3:
                tg(f"⚠️ <b>Error ciclo #{errors}</b>\n<code>{str(e)[:200]}</code>")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: 10 errores consecutivos. Bot detenido.</b>")
                break

        sleep = max(0, LOOP_SECONDS - (time.time() - t0))
        log.info(f"Sleeping {sleep:.1f}s")
        time.sleep(sleep)

if __name__ == "__main__":
    main()
