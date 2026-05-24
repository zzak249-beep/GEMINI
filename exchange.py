"""
BingX Perpetual Futures — REST connector
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)
BASE_URL = "https://open-api.bingx.com"


class BingXClient:
    def __init__(self, api_key: str, secret: str, paper: bool = True):
        self.api_key = api_key
        self.secret  = secret
        self.paper   = paper
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key}
            )
        return self._session

    def _sign(self, params: dict) -> str:
        qs = urlencode(sorted(params.items()))
        return hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    async def _get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        sess = await self._get_session()
        async with sess.get(BASE_URL + path, params=params) as r:
            data = await r.json()
            if data.get("code", 0) != 0:
                raise RuntimeError(f"BingX GET error {path}: {data}")
            return data

    async def _post(self, path: str, params: dict = None) -> dict:
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        sess = await self._get_session()
        async with sess.post(BASE_URL + path, params=params) as r:
            data = await r.json()
            if data.get("code", 0) != 0:
                raise RuntimeError(f"BingX POST error {path}: {data}")
            return data

    # ── Market data ──────────────────────────────────────────
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        """
        BingX puede devolver klines como:
          - lista de listas: [[open_time, open, high, low, close, volume], ...]
          - lista de dicts:  [{"open": ..., "close": ..., ...}, ...]
        Este método maneja ambos formatos.
        """
        data = await self._get(
            "/openApi/swap/v2/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit}
        )
        rows = data.get("data", [])

        if not rows:
            raise RuntimeError(f"No klines data for {symbol}")

        # Detectar formato
        first = rows[0]

        if isinstance(first, dict):
            # Formato dict — los keys pueden variar entre versiones de la API
            # Intentar mapeo estándar
            df = pd.DataFrame(rows)
            df.columns = [c.lower() for c in df.columns]

            # Renombrar columnas comunes de BingX
            rename_map = {
                'opentime': 'open_time', 'o': 'open', 'h': 'high',
                'l': 'low', 'c': 'close', 'v': 'volume',
                'time': 'open_time', 't': 'open_time',
            }
            df = df.rename(columns=rename_map)

            # Asegurar columnas necesarias
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col not in df.columns:
                    raise RuntimeError(f"Columna '{col}' no encontrada en klines. Cols: {list(df.columns)}")

        elif isinstance(first, (list, tuple)):
            # Formato lista [open_time, open, high, low, close, volume]
            df = pd.DataFrame(rows, columns=['open_time', 'open', 'high', 'low', 'close', 'volume'])

        else:
            raise RuntimeError(f"Formato klines desconocido: {type(first)}")

        # Convertir a float
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        # Índice temporal
        if 'open_time' in df.columns:
            df['open_time'] = pd.to_numeric(df['open_time'], errors='coerce')
            df.set_index('open_time', inplace=True)
        else:
            df.index = range(len(df))

        df = df[['open', 'high', 'low', 'close', 'volume']].copy()
        df = df[df['close'] > 0].reset_index(drop=True)  # eliminar filas vacías
        return df

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        return data["data"]

    async def get_all_symbols(self) -> list:
        """Retorna lista de todos los símbolos de futuros perpetuos disponibles"""
        data = await self._get("/openApi/swap/v2/quote/contracts")
        contracts = data.get("data", [])
        symbols = []
        for c in contracts:
            sym = c.get("symbol") or c.get("contractId", "")
            if sym and "USDT" in sym:
                symbols.append(sym)
        return symbols

    async def get_balance(self) -> float:
        data = await self._get("/openApi/swap/v2/user/balance")
        for asset in data["data"]["balance"]:
            if asset["asset"] == "USDT":
                return float(asset["availableMargin"])
        return 0.0

    async def get_positions(self, symbol: str) -> list:
        data = await self._get("/openApi/swap/v2/user/positions", {"symbol": symbol})
        return data.get("data", [])

    # ── Orders ───────────────────────────────────────────────
    async def place_order(self, symbol, side, position_side, qty,
                          order_type="MARKET", price=None, reduce_only=False) -> dict:
        if self.paper:
            logger.info(f"[PAPER] {side} {position_side} {qty:.6f} {symbol}")
            return {"orderId": f"paper_{int(time.time())}", "paper": True}

        params = {
            "symbol": symbol, "side": side,
            "positionSide": position_side, "type": order_type, "quantity": qty,
        }
        if price and order_type == "LIMIT":
            params["price"] = price
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/openApi/swap/v2/trade/order", params)

    async def set_sl_tp(self, symbol, position_side, sl_price, tp_price, qty) -> dict:
        if self.paper:
            logger.info(f"[PAPER] SL={sl_price:.6f} TP={tp_price:.6f}")
            return {"paper": True}
        sl_side = "SELL" if position_side == "LONG" else "BUY"
        await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": sl_side, "positionSide": position_side,
            "type": "STOP_MARKET", "stopPrice": sl_price,
            "quantity": qty, "reduceOnly": "true",
        })
        await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": sl_side, "positionSide": position_side,
            "type": "TAKE_PROFIT_MARKET", "stopPrice": tp_price,
            "quantity": qty, "reduceOnly": "true",
        })
        return {"ok": True}

    async def close_position(self, symbol, position_side, qty) -> dict:
        side = "SELL" if position_side == "LONG" else "BUY"
        return await self.place_order(symbol, side, position_side, qty, reduce_only=True)

    async def set_leverage(self, symbol, leverage) -> dict:
        if self.paper:
            return {"paper": True}
        return await self._post("/openApi/swap/v2/trade/leverage",
                                {"symbol": symbol, "side": "LONG", "leverage": leverage})

    async def close(self):
        if self._session:
            await self._session.close()
