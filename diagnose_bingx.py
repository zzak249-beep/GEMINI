"""
diagnose_bingx.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ejecuta esto LOCALMENTE o en Railway como one-shot para saber
exactamente qué falla. Pon tus keys abajo o como env vars.

  python diagnose_bingx.py

Verás PASS/FAIL en cada paso con el error exacto de BingX.
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import aiohttp

# ── PON TUS KEYS AQUÍ (o usa variables de entorno) ───────────────────
API_KEY    = os.getenv("BINGX_API_KEY", "TU_API_KEY")
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "TU_SECRET_KEY")
BASE_URL   = "https://open-api.bingx.com"
TEST_SYMBOL = "BTC-USDT"   # Símbolo para probar órdenes
TEST_QTY    = 0.001        # Cantidad MUY pequeña para test

# ─────────────────────────────────────────────────────────────────────

def sign(params: dict) -> str:
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()

def headers():
    return {"X-BX-APIKEY": API_KEY}

async def get(session, path, params=None, signed=False):
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = sign(params)
    async with session.get(f"{BASE_URL}{path}", params=params, headers=headers()) as r:
        return await r.json(content_type=None)

async def post(session, path, params):
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign(params)
    async with session.post(f"{BASE_URL}{path}", params=params, headers=headers()) as r:
        return await r.json(content_type=None)

def ok(label, r):
    code = r.get("code", 0)
    if code == 0:
        print(f"  ✅ PASS  {label}")
        return True
    else:
        print(f"  ❌ FAIL  {label}  →  code={code}  msg={r.get('msg','?')}")
        return False

async def main():
    print("=" * 60)
    print("  BingX API Diagnostic")
    print("=" * 60)

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:

        # ── 1. Ping / Server Time ────────────────────────────────────
        print("\n[1] Conectividad y tiempo de servidor")
        r = await get(s, "/openApi/swap/v2/server/time")
        ok("Server time", r)
        server_ts = r.get("data", {}).get("serverTime", 0)
        local_ts  = int(time.time() * 1000)
        drift_ms  = abs(server_ts - local_ts)
        print(f"      server_ts={server_ts}  local_ts={local_ts}  drift={drift_ms}ms")
        if drift_ms > 2000:
            print(f"  ⚠️  DRIFT ALTO ({drift_ms}ms) — puede causar errores de firma!")

        # ── 2. Balance ───────────────────────────────────────────────
        print("\n[2] Balance de cuenta")
        r = await get(s, "/openApi/swap/v2/user/balance", signed=True)
        if ok("Get balance", r):
            data = r.get("data", {})
            bal  = data.get("balance", {}) if isinstance(data, dict) else {}
            margin = bal.get("availableMargin", "?") if isinstance(bal, dict) else data.get("availableMargin", "?")
            print(f"      availableMargin = {margin} USDT")
        else:
            print(f"      RAW response: {json.dumps(r, indent=2)[:300]}")

        # ── 3. Posiciones abiertas ────────────────────────────────────
        print("\n[3] Posiciones abiertas")
        r = await get(s, "/openApi/swap/v2/user/positions", signed=True)
        if ok("Get positions", r):
            positions = r.get("data", []) or []
            active = [p for p in positions if float(p.get("positionAmt", p.get("posAmt", 0))) != 0]
            print(f"      {len(active)} posiciones activas")
        else:
            print(f"      RAW: {json.dumps(r, indent=2)[:300]}")

        # ── 4. Klines ────────────────────────────────────────────────
        print(f"\n[4] Klines {TEST_SYMBOL} 3m")
        r = await get(s, "/openApi/swap/v3/quote/klines", {
            "symbol": TEST_SYMBOL, "interval": "3m", "limit": 10
        })
        if ok("Get klines", r):
            data = r.get("data", [])
            print(f"      {len(data)} velas recibidas")
            if data:
                last = data[-1]
                print(f"      Última vela: {json.dumps(last)[:120]}")
        else:
            print(f"      RAW: {json.dumps(r, indent=2)[:300]}")

        # ── 5. Ticker ────────────────────────────────────────────────
        print(f"\n[5] Ticker")
        r = await get(s, "/openApi/swap/v2/quote/ticker")
        if ok("Get tickers", r):
            data = r.get("data", [])
            btc  = next((t for t in data if t.get("symbol") == TEST_SYMBOL), None)
            if btc:
                print(f"      BTC last={btc.get('lastPrice')}  vol={btc.get('quoteVolume')}")

        # ── 6. Set leverage ──────────────────────────────────────────
        print(f"\n[6] Set leverage para {TEST_SYMBOL}")
        for side in ("LONG", "SHORT"):
            r = await post(s, "/openApi/swap/v2/trade/leverage", {
                "symbol": TEST_SYMBOL, "side": side, "leverage": 5
            })
            code = r.get("code", -1)
            if code == 0:
                print(f"  ✅ PASS  leverage {side}")
            else:
                print(f"  ⚠️  leverage {side}  →  code={code}  msg={r.get('msg','?')}  (puede ser normal si ya está configurado)")

        # ── 7. One-way mode ──────────────────────────────────────────
        print(f"\n[7] One-way mode (dualSidePosition=false)")
        r = await post(s, "/openApi/swap/v2/trade/positionSide/dual", {
            "dualSidePosition": "false"
        })
        code = r.get("code", -1)
        if code == 0:
            print(f"  ✅ PASS  positionSide/dual")
        else:
            print(f"  ⚠️  positionSide/dual  →  code={code}  msg={r.get('msg','?')}  (puede ser normal)")

        # ── 8. ORDEN DE TEST (sin SL/TP) ─────────────────────────────
        print(f"\n[8] Orden MARKET sin SL/TP — BUY {TEST_QTY} {TEST_SYMBOL}")
        print("    (Esto ABRIRÁ UNA POSICIÓN REAL. Comenta si no quieres)")
        r = await post(s, "/openApi/swap/v2/trade/order", {
            "symbol":       TEST_SYMBOL,
            "side":         "BUY",
            "positionSide": "BOTH",
            "type":         "MARKET",
            "quantity":     TEST_QTY,
        })
        order_ok = ok("Market order (sin SL/TP)", r)
        print(f"      RAW: {json.dumps(r, indent=2)[:400]}")

        if order_ok:
            # Cerrar inmediatamente
            print(f"\n[8b] Cerrando posición de test...")
            await asyncio.sleep(1)
            r2 = await post(s, "/openApi/swap/v2/trade/order", {
                "symbol":       TEST_SYMBOL,
                "side":         "SELL",
                "positionSide": "BOTH",
                "type":         "MARKET",
                "quantity":     TEST_QTY,
            })
            ok("Close test position", r2)
        else:
            # ── 8c. Probar con positionSide=LONG ─────────────────────
            print(f"\n[8c] Reintentando con positionSide=LONG (hedge mode)")
            r = await post(s, "/openApi/swap/v2/trade/order", {
                "symbol":       TEST_SYMBOL,
                "side":         "BUY",
                "positionSide": "LONG",
                "type":         "MARKET",
                "quantity":     TEST_QTY,
            })
            ok("Market order (positionSide=LONG)", r)
            print(f"      RAW: {json.dumps(r, indent=2)[:400]}")

        # ── 9. ORDEN CON SL/TP ───────────────────────────────────────
        print(f"\n[9] Orden MARKET con SL/TP — BUY {TEST_QTY} {TEST_SYMBOL}")
        # Obtener precio actual
        r_tick = await get(s, "/openApi/swap/v2/quote/ticker")
        btc_data = next((t for t in r_tick.get("data", []) if t.get("symbol") == TEST_SYMBOL), {})
        price = float(btc_data.get("lastPrice", 0))
        if price > 0:
            sl_price = round(price * 0.97, 2)   # -3%
            tp_price = round(price * 1.03, 2)   # +3%
            sl_json  = json.dumps({"type": "STOP_MARKET", "stopPrice": sl_price, "workingType": "MARK_PRICE"})
            tp_json  = json.dumps({"type": "TAKE_PROFIT_MARKET", "stopPrice": tp_price, "workingType": "MARK_PRICE"})
            print(f"    price={price}  SL={sl_price}  TP={tp_price}")
            r = await post(s, "/openApi/swap/v2/trade/order", {
                "symbol":       TEST_SYMBOL,
                "side":         "BUY",
                "positionSide": "BOTH",
                "type":         "MARKET",
                "quantity":     TEST_QTY,
                "stopLoss":     sl_json,
                "takeProfit":   tp_json,
            })
            order_sl_ok = ok("Market order CON SL/TP", r)
            print(f"      RAW: {json.dumps(r, indent=2)[:400]}")
            if order_sl_ok:
                await asyncio.sleep(1)
                r2 = await post(s, "/openApi/swap/v2/trade/order", {
                    "symbol":       TEST_SYMBOL,
                    "side":         "SELL",
                    "positionSide": "BOTH",
                    "type":         "MARKET",
                    "quantity":     TEST_QTY,
                })
                ok("Close SL/TP test position", r2)
        else:
            print("    No se pudo obtener precio actual, saltando test 9")

    print("\n" + "=" * 60)
    print("  Diagnóstico completo.")
    print("  Busca los ❌ FAIL para saber exactamente qué falla.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
