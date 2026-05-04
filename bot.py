"""
ZigZag Institutional Elite V6 - MULTI-SYMBOL SCANNER
Analiza hasta 100 monedas en paralelo | BingX Futures | Railway | Telegram
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
# Chat ID: puede ser número positivo (usuario) o negativo (grupo). Se guarda como string.
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

TIMEFRAME        = os.environ.get("TIMEFRAME",        "15m")
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",   "1.0"))
PIVOT_LEN        = int(os.environ.get("PIVOT_LEN",        "5"))
VOL_MULT         = float(os.environ.get("VOL_MULT",        "1.5"))
ATR_LEN          = int(os.environ.get("ATR_LEN",          "14"))
TP_MULT          = float(os.environ.get("TP_MULT",         "2.0"))
LEVERAGE         = int(os.environ.get("LEVERAGE",         "5"))
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",     "60"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",  "3"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",     "10"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",      "50"))
# Solo cuenta posiciones abiertas POR EL BOT (ignora las manuales si se activa)
ONLY_BOT_TRADES  = os.environ.get("ONLY_BOT_TRADES", "false").lower() == "true"

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
                     headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=10)
    r.raise_for_status()
    return r.json()

def bx_post(path: str, payload: dict) -> dict:
    p = dict(payload)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.post(BINGX_BASE + path, json=p,
                      headers={"X-BX-APIKEY": BINGX_API_KEY,
                               "Content-Type": "application/json"}, timeout=10)
    r.raise_for_status()
    return r.json()

def get_balance() -> float:
    """Retorna el balance disponible en USDT de Futuros — prueba todos los campos conocidos."""
    try:
        data = bx_get("/openApi/swap/v2/user/balance")
        log.info(f"Balance raw: {data}")   # log completo para diagnóstico
        d = data.get("data", {})

        # Formato 1: data.balance = [{asset, availableMargin, ...}]
        if isinstance(d, dict):
            for asset in d.get("balance", []):
                if isinstance(asset, dict) and asset.get("asset") == "USDT":
                    for field in ("availableMargin", "available", "walletBalance",
                                  "crossWalletBalance", "equity", "balance"):
                        v = asset.get(field)
                        if v not in (None, "", "0", 0):
                            log.info(f"Balance field used: {field} = {v}")
                            return float(v)

        # Formato 2: data = {asset, availableMargin, ...}  (dict directo)
        if isinstance(d, dict) and d.get("asset") == "USDT":
            for field in ("availableMargin", "available", "walletBalance", "equity", "balance"):
                v = d.get(field)
                if v not in (None, "", "0", 0):
                    return float(v)

        # Formato 3: data = lista de assets
        if isinstance(d, list):
            for asset in d:
                if isinstance(asset, dict) and asset.get("asset") == "USDT":
                    for field in ("availableMargin", "available", "walletBalance", "equity", "balance"):
                        v = asset.get(field)
                        if v not in (None, "", "0", 0):
                            return float(v)

        # Formato 4: BingX a veces devuelve en data directamente
        # Buscar cualquier clave que tenga "margin" o "balance" con valor > 0
        def _search(obj, depth=0):
            if depth > 4:
                return None
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, (int, float)) and float(v) > 0:
                        if any(x in k.lower() for x in ("margin","available","equity","balance","wallet")):
                            log.info(f"Balance fallback field: {k} = {v}")
                            return float(v)
                for v in obj.values():
                    r = _search(v, depth+1)
                    if r:
                        return r
            elif isinstance(obj, list):
                for item in obj:
                    r = _search(item, depth+1)
                    if r:
                        return r
            return None

        found = _search(d)
        if found:
            return found

        log.warning(f"Could not parse balance from: {data}")
        return 0.0
    except Exception as e:
        log.error(f"get_balance error: {e}")
        return 0.0

def get_all_positions() -> dict:
    """Devuelve {symbol: position} para todas las posiciones con size != 0."""
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

def get_top_symbols(limit: int) -> list:
    try:
        data = bx_get("/openApi/swap/v2/quote/contracts", {})
        contracts = data.get("data", [])
        if not isinstance(contracts, list) or len(contracts) == 0:
            raise ValueError("Empty contracts list")
        usdt = [c for c in contracts
                if isinstance(c, dict)
                and c.get("asset", "") == "USDT"
                and c.get("status") == 1]
        usdt.sort(key=lambda x: float(x.get("tradeAmount", 0)), reverse=True)
        symbols = [c["symbol"] for c in usdt[:limit]]
        log.info(f"Loaded {len(symbols)} symbols by 24h volume.")
        return symbols
    except Exception as e:
        log.warning(f"Contracts API failed ({e}), using fallback list.")
        return FALLBACK_SYMBOLS[:limit]

def set_lev(symbol: str):
    for side in ("LONG", "SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol": symbol, "side": side, "leverage": LEVERAGE})
        except Exception:
            pass

def open_order(symbol: str, side: str, qty: float, sl: float, tp: float) -> dict:
    payload = {
        "symbol":     symbol,
        "side":       side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type":       "MARKET",
        "quantity":   round(qty, 4),
        "stopLoss":   json.dumps({"type":"MARK_PRICE","stopPrice":round(sl, 6),"workingType":"MARK_PRICE"}),
        "takeProfit": json.dumps({"type":"MARK_PRICE","stopPrice":round(tp, 6),"workingType":"MARK_PRICE"}),
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
    out = pd.Series(np.nan, index=high.index)
    for i in range(left, len(high) - right):
        w = high.iloc[i - left: i + right + 1]
        if high.iloc[i] == w.max():
            out.iloc[i] = high.iloc[i]
    return out

def pl_series(low, left, right):
    out = pd.Series(np.nan, index=low.index)
    for i in range(left, len(low) - right):
        w = low.iloc[i - left: i + right + 1]
        if low.iloc[i] == w.min():
            out.iloc[i] = low.iloc[i]
    return out

def calc_atr(high, low, close, period):
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def scan_symbol(symbol: str):
    try:
        df = get_klines(symbol)
        if df.empty or len(df) < max(PIVOT_LEN*2+2, ATR_LEN+1, 21):
            return None

        peak   = ph_series(df["high"], PIVOT_LEN, PIVOT_LEN).ffill()
        valley = pl_series(df["low"],  PIVOT_LEN, PIVOT_LEN).ffill()
        vol_ma = df["volume"].rolling(20).mean()
        inst   = df["volume"] > (vol_ma * VOL_MULT)
        atr_s  = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)

        i       = len(df) - 1
        pc      = float(df["close"].iloc[i-1])
        cc      = float(df["close"].iloc[i])
        co      = float(df["open"].iloc[i])
        cpeak   = float(peak.iloc[i])
        cvalley = float(valley.iloc[i])
        cinst   = bool(inst.iloc[i])
        catr    = float(atr_s.iloc[i])
        vma     = float(vol_ma.iloc[i]) if float(vol_ma.iloc[i]) > 0 else 1
        vratio  = round(float(df["volume"].iloc[i]) / vma, 2)

        is_long  = (pc <= cpeak)   and (cc > cpeak)   and cinst and (cc > co)
        is_short = (pc >= cvalley) and (cc < cvalley) and cinst and (cc < co)

        if not is_long and not is_short:
            return None

        direction = "LONG" if is_long else "SHORT"
        if direction == "LONG":
            sl = cvalley if not np.isnan(cvalley) else cc - catr * 2
            tp = cc + (cc - sl) * TP_MULT
        else:
            sl = cpeak if not np.isnan(cpeak) else cc + catr * 2
            tp = cc - (sl - cc) * TP_MULT

        rr    = abs(tp - cc) / abs(cc - sl) if abs(cc - sl) > 0 else 0
        score = min(vratio*20, 40) + min((catr/cc)*5000, 30) + min(rr*10, 30)

        return {"symbol": symbol, "signal": direction, "close": cc,
                "sl": sl, "tp": tp, "atr": catr, "vol_ratio": vratio,
                "score": round(score, 1), "rr": round(rr, 2)}
    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

def calc_qty(balance: float, entry: float, sl: float) -> float:
    risk  = balance * (RISK_PERCENT / 100)
    dist  = abs(entry - sl)
    if dist == 0:
        return 0
    return max(round((risk * LEVERAGE) / entry, 4), 0.001)

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def _send(msg: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    # chat_id puede ser int o str; Bot acepta ambos
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
        f"<b>Balance Futuros:</b> {balance:.2f} USDT\n"
        f"<b>Top pares:</b> {', '.join(symbols[:10])}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals: list, total: int, open_count: int):
    if not signals:
        return
    lines = [
        f"🔍 <b>{len(signals)} señal(es) / {total} monedas</b> | Trades: {open_count}/{MAX_OPEN_TRADES}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals[:10]:  # máx 10 en el mensaje
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
    """Mensaje de diagnóstico al arrancar para verificar conectividad."""
    pos_list = list(positions.keys()) if positions else ["ninguna"]
    tg(
        f"🔧 <b>DEBUG — Conexión verificada</b>\n"
        f"<b>Balance API:</b> {balance:.4f} USDT\n"
        f"<b>Posiciones detectadas:</b> {len(positions)}\n"
        f"<b>Pares:</b> {', '.join(pos_list[:5])}\n"
        f"<b>Símbolos a escanear:</b> {len(symbols)}\n"
        f"<b>Chat ID usado:</b> <code>{TELEGRAM_CHAT_ID}</code>"
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== ZigZag Institutional Elite V6 MULTI-SCANNER starting ===")

    symbols  = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_top_symbols(MAX_SYMBOLS)
    balance  = get_balance()
    positions = get_all_positions()

    log.info(f"Balance: {balance:.4f} USDT | Symbols: {len(symbols)} | Open positions: {len(positions)}")

    # Mensaje de debug para verificar que Telegram y BingX funcionan
    tg_debug(balance, positions, symbols)
    tg_startup(balance, symbols)

    log.info("Pre-setting leverage...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))
    log.info("Leverage ready. Entering main loop.")

    errors = 0
    entered_this_cycle: set = set()

    while True:
        t0 = time.time()
        try:
            balance   = get_balance()
            positions = get_all_positions()
            open_count = len(positions)

            log.info(f"── Cycle | {balance:.4f} USDT | {open_count}/{MAX_OPEN_TRADES} trades ──")

            # Escaneo paralelo
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

            # Ejecutar entradas por orden de score
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
                    log.info(f"✅ ORDER {sym} {side} qty={qty} | response: {res}")
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
