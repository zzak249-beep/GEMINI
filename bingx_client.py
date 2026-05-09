import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
import aiohttp
from typing import Optional
import config


class BingXClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.api_key = config.BINGX_API_KEY
        self.secret  = config.BINGX_SECRET_KEY
        self.session = session

    # ── Auth ─────────────────────────────────────────────────────────
    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(
            self.secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _auth_headers(self):
        return {"X-BX-APIKEY": self.api_key}

    # ── HTTP ──────────────────────────────────────────────────────────
    async def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
        url = f"{config.BASE_URL}{path}"
        async with self.session.get(url, params=params, headers=self._auth_headers()) as r:
            return await r.json()

    async def _post(self, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        url = f"{config.BASE_URL}{path}"
        async with self.session.post(url, params=params, headers=self._auth_headers()) as r:
            return await r.json()

    # ── Market Data ───────────────────────────────────────────────────
    async def get_contracts(self) -> list:
        r = await self._get("/openApi/swap/v2/quote/contracts")
        return r.get("data", [])

    async def get_tickers(self) -> list:
        r = await self._get("/openApi/swap/v2/quote/ticker")
        data = r.get("data", [])
        return data if isinstance(data, list) else []

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 100) -> list:
        r = await self._get("/openApi/swap/v3/quote/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })
        return r.get("data", [])

    async def get_24h_volume_history(self, symbol: str) -> list:
        r = await self._get("/openApi/swap/v3/quote/klines", {
            "symbol": symbol, "interval": "1d", "limit": 8
        })
        return r.get("data", [])

    # ── Account ───────────────────────────────────────────────────────
    async def get_balance(self) -> float:
        r = await self._get("/openApi/swap/v2/user/balance", signed=True)
        try:
            data = r.get("data", {})
            bal  = data.get("balance", {}) if isinstance(data, dict) else {}
            return float(bal.get("availableMargin", 0))
        except Exception:
            return 0.0

    async def get_positions(self) -> list:
        r = await self._get("/openApi/swap/v2/user/positions", signed=True)
        return r.get("data", []) or []

    async def get_open_orders(self, symbol: str) -> list:
        r = await self._get("/openApi/swap/v2/trade/openOrders", {
            "symbol": symbol
        }, signed=True)
        return r.get("data", {}).get("orders", []) or []

    # ── Trading ───────────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        for side in ("LONG", "SHORT"):
            await self._post("/openApi/swap/v2/trade/leverage", {
                "symbol": symbol, "side": side, "leverage": leverage
            })
        await self._post("/openApi/swap/v2/trade/positionSide/dual", {
            "dualSidePosition": "false"
        })

    async def place_market_order(
        self, symbol: str, side: str, qty: float,
        stop_loss: float, take_profit: float,
    ) -> dict:
        sl_json = json.dumps({
            "type": "STOP_MARKET",
            "stopPrice": round(stop_loss, 6),
            "workingType": "MARK_PRICE"
        })
        tp_json = json.dumps({
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": round(take_profit, 6),
            "workingType": "MARK_PRICE"
        })
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": side, "positionSide": "BOTH",
            "type": "MARKET", "quantity": round(qty, 4),
            "stopLoss": sl_json, "takeProfit": tp_json,
        })

    async def close_position_market(self, symbol: str, side: str, qty: float) -> dict:
        close_side = "SELL" if side == "BUY" else "BUY"
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": close_side,
            "positionSide": "BOTH", "type": "MARKET",
            "quantity": round(qty, 4),
        })

    async def cancel_all_orders(self, symbol: str) -> dict:
        return await self._post("/openApi/swap/v2/trade/allOpenOrders", {
            "symbol": symbol
        })

    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        contracts = await self.get_contracts()
        for c in contracts:
            if c.get("symbol") == symbol:
                return c
        return None
