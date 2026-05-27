"""
BingX Client v5 — Maker/Limit orders, OFI, Funding Rate, Open Interest
"""
import asyncio, hashlib, hmac, time, logging
from urllib.parse import urlencode
import aiohttp

log = logging.getLogger("BingX")
BASE = "https://open-api.bingx.com"

class BingXClient:
    def __init__(self, api_key, secret):
        self.api_key = api_key; self.secret = secret; self._session = None

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key},
                timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    def _sign(self, params):
        q = urlencode(sorted(params.items()))
        return hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()

    async def _get(self, path, params=None, signed=False):
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time()*1000)
            params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.get(BASE+path, params=params) as r:
            data = await r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"GET {path}: {data.get('msg',data)}")
        return data.get("data", data)

    async def _post(self, path, params=None):
        params = params or {}
        params["timestamp"] = int(time.time()*1000)
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.post(BASE+path, params=params) as r:
            data = await r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"POST {path}: {data.get('msg',data)}")
        return data.get("data", data)

    # ── Mercado ─────────────────────────────────────────────
    async def get_all_tickers(self):
        data = await self._get("/openApi/swap/v2/quote/ticker")
        return data if isinstance(data, list) else []

    async def get_klines(self, symbol, interval, limit=300):
        data = await self._get("/openApi/swap/v2/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit})
        result = [[int(k["time"]),float(k["open"]),float(k["high"]),
                   float(k["low"]),float(k["close"]),float(k["volume"])]
                  for k in (data if isinstance(data, list) else [])]
        return sorted(result, key=lambda x: x[0])

    async def get_ticker(self, symbol):
        data = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        t = data[0] if isinstance(data, list) else data
        return {"last": float(t["lastPrice"]), "bid": float(t.get("bidPrice",0)),
                "ask": float(t.get("askPrice",0)), "volume": float(t.get("volume",0)),
                "quoteVolume": float(t.get("quoteVolume",0))}

    async def get_order_book(self, symbol, depth=10):
        """Order book para OFI — retorna bids y asks con qty."""
        try:
            data = await self._get("/openApi/swap/v2/quote/depth",
                {"symbol": symbol, "limit": depth})
            bids = [[float(x[0]), float(x[1])] for x in data.get("bids", [])]
            asks = [[float(x[0]), float(x[1])] for x in data.get("asks", [])]
            return {"bids": bids, "asks": asks}
        except Exception as e:
            log.warning(f"order_book {symbol}: {e}")
            return {"bids": [], "asks": []}

    async def get_funding_rate(self, symbol):
        """Funding rate actual del perpetuo."""
        try:
            data = await self._get("/openApi/swap/v2/quote/premiumIndex",
                {"symbol": symbol})
            if isinstance(data, list): data = data[0]
            return {
                "funding_rate": float(data.get("lastFundingRate", 0)),
                "next_funding_time": int(data.get("nextFundingTime", 0)),
                "mark_price": float(data.get("markPrice", 0)),
            }
        except Exception as e:
            log.warning(f"funding_rate {symbol}: {e}")
            return {"funding_rate": 0.0, "next_funding_time": 0, "mark_price": 0.0}

    async def get_open_interest(self, symbol):
        """Open Interest en contratos y USDT."""
        try:
            data = await self._get("/openApi/swap/v2/quote/openInterest",
                {"symbol": symbol})
            if isinstance(data, list): data = data[0]
            return {
                "open_interest": float(data.get("openInterest", 0)),
                "open_interest_value": float(data.get("openInterestValue", 0)),
            }
        except Exception as e:
            log.warning(f"open_interest {symbol}: {e}")
            return {"open_interest": 0.0, "open_interest_value": 0.0}

    async def get_liquidation_orders(self, symbol, limit=20):
        """Órdenes de liquidación recientes — zonas magnéticas."""
        try:
            data = await self._get("/openApi/swap/v2/quote/forceOrders",
                {"symbol": symbol, "limit": limit})
            return data if isinstance(data, list) else []
        except Exception as e:
            log.warning(f"liquidations {symbol}: {e}")
            return []

    # ── Cuenta ──────────────────────────────────────────────
    async def get_balance(self):
        data = await self._get("/openApi/swap/v2/user/balance", signed=True)
        for a in data.get("balance", []):
            if a.get("asset") == "USDT":
                return float(a.get("availableMargin", 0))
        return 0.0

    async def get_positions(self, symbol=""):
        p = {"symbol": symbol} if symbol else {}
        data = await self._get("/openApi/swap/v2/user/positions", p, signed=True)
        return data if isinstance(data, list) else []

    # ── Órdenes ─────────────────────────────────────────────
    async def set_leverage(self, symbol, leverage, side="LONG"):
        try:
            await self._post("/openApi/swap/v2/trade/leverage",
                {"symbol": symbol, "leverage": leverage, "side": side})
        except Exception as e:
            log.warning(f"leverage {symbol}: {e}")

    async def place_limit_order(self, symbol, side, size, price,
                                 sl_price, tp_price=None, post_only=True):
        """Orden limit con post-only (maker garantizado)."""
        await self.set_leverage(symbol, 10, side)
        await asyncio.sleep(0.15)
        params = {
            "symbol": symbol,
            "side": "BUY" if side=="LONG" else "SELL",
            "positionSide": side,
            "type": "LIMIT",
            "quantity": f"{size:.4f}",
            "price": f"{price:.4f}",
            "timeInForce": "GTX" if post_only else "GTC",  # GTX = post-only
            "stopLossPrice": f"{sl_price:.4f}",
        }
        if tp_price:
            params["takeProfitPrice"] = f"{tp_price:.4f}"
        try:
            data = await self._post("/openApi/swap/v2/trade/order", params)
            log.info(f"LIMIT {symbol} {side} {size}@{price:.4f} → {data.get('orderId','?')}")
            return data
        except Exception as e:
            log.error(f"limit_order {symbol}: {e}"); return None

    async def place_market_order(self, symbol, side, size, sl_price, tp_price=None):
        """Orden market (fallback si limit no se llena)."""
        await self.set_leverage(symbol, 10, side)
        await asyncio.sleep(0.15)
        params = {
            "symbol": symbol,
            "side": "BUY" if side=="LONG" else "SELL",
            "positionSide": side,
            "type": "MARKET",
            "quantity": f"{size:.4f}",
            "stopLossPrice": f"{sl_price:.4f}",
        }
        if tp_price:
            params["takeProfitPrice"] = f"{tp_price:.4f}"
        try:
            return await self._post("/openApi/swap/v2/trade/order", params)
        except Exception as e:
            log.error(f"market_order {symbol}: {e}"); return None

    async def cancel_order(self, symbol, order_id):
        try:
            return await self._post("/openApi/swap/v2/trade/cancel",
                {"symbol": symbol, "orderId": str(order_id)})
        except Exception as e:
            log.warning(f"cancel {symbol} {order_id}: {e}")

    async def get_order_status(self, symbol, order_id):
        try:
            data = await self._get("/openApi/swap/v2/trade/order",
                {"symbol": symbol, "orderId": str(order_id)}, signed=True)
            return data
        except Exception as e:
            log.warning(f"order_status {symbol}: {e}"); return None

    async def close_position(self, symbol, side):
        positions = await self.get_positions(symbol)
        size = 0.0
        for p in positions:
            if p.get("positionSide")==side and float(p.get("positionAmt",0))!=0:
                size = abs(float(p["positionAmt"])); break
        if size == 0: return None
        params = {"symbol": symbol,
                  "side": "SELL" if side=="LONG" else "BUY",
                  "positionSide": side, "type": "MARKET",
                  "quantity": f"{size:.4f}", "reduceOnly": "true"}
        try:
            return await self._post("/openApi/swap/v2/trade/order", params)
        except Exception as e:
            log.error(f"close {symbol}: {e}"); return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
