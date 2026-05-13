import hashlib
import hmac
import json
import logging
import math
import time
import urllib.parse
import aiohttp
from typing import Optional
import config

log = logging.getLogger("bingx")


class BingXClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.api_key = config.BINGX_API_KEY
        self.secret  = config.BINGX_SECRET_KEY
        self.session = session
        self._contract_cache: dict = {}

    # ── Firma ─────────────────────────────────────────────────────────
    def _sign(self, params: dict) -> str:
        """HMAC-SHA256 sobre query string ordenada alfabéticamente."""
        query = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(
            self.secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _build_url(self, path: str, params: dict) -> str:
        """
        Construye URL con query string en el MISMO orden que la firma.
        Evita que aiohttp reordene los params y rompa la verificación.
        """
        query = urllib.parse.urlencode(sorted(params.items()))
        return f"{config.BASE_URL}{path}?{query}"

    def _headers(self) -> dict:
        return {"X-BX-APIKEY": self.api_key}

    # ── HTTP ──────────────────────────────────────────────────────────
    async def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        p = dict(params or {})
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            p["signature"] = self._sign(p)
        url = self._build_url(path, p) if p else f"{config.BASE_URL}{path}"
        try:
            async with self.session.get(url, headers=self._headers()) as r:
                return await r.json(content_type=None)
        except Exception as e:
            log.error(f"GET {path}: {e}")
            raise

    async def _post(self, path: str, params: dict) -> dict:
        p = dict(params)                          # nunca mutar el dict original
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = self._sign(p)
        # URL con query string exactamente igual a la cadena firmada
        url = self._build_url(path, p)
        try:
            async with self.session.post(url, headers=self._headers()) as r:
                data = await r.json(content_type=None)
                code = data.get("code", 0)
                if code != 0:
                    log.error(f"POST {path} code={code} msg={data.get('msg','?')}")
                return data
        except Exception as e:
            log.error(f"POST {path}: {e}")
            raise

    # ── Market Data ───────────────────────────────────────────────────
    async def get_contracts(self) -> list:
        r = await self._get("/openApi/swap/v2/quote/contracts")
        data = r.get("data", []) or []
        for c in data:
            sym = c.get("symbol", "")
            if sym:
                self._contract_cache[sym] = c
        return data

    async def get_tickers(self) -> list:
        r = await self._get("/openApi/swap/v2/quote/ticker")
        data = r.get("data", [])
        return data if isinstance(data, list) else []

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 100) -> list:
        r = await self._get("/openApi/swap/v3/quote/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })
        return r.get("data", []) or []

    async def get_24h_volume_history(self, symbol: str) -> list:
        r = await self._get("/openApi/swap/v3/quote/klines", {
            "symbol": symbol, "interval": "1d", "limit": 8
        })
        return r.get("data", []) or []

    # ── Account ───────────────────────────────────────────────────────
    async def get_balance(self) -> float:
        r = await self._get("/openApi/swap/v2/user/balance", signed=True)
        try:
            data = r.get("data", {})
            if isinstance(data, dict):
                bal = data.get("balance", {})
                if isinstance(bal, dict):
                    return float(bal.get("availableMargin", 0))
                return float(data.get("availableMargin", 0))
        except Exception as e:
            log.error(f"get_balance parse: {e} raw={r}")
        return 0.0

    async def get_positions(self) -> list:
        r = await self._get("/openApi/swap/v2/user/positions", signed=True)
        data = r.get("data", [])
        return data if isinstance(data, list) else []

    async def get_open_orders(self, symbol: str) -> list:
        r = await self._get("/openApi/swap/v2/trade/openOrders",
                            {"symbol": symbol}, signed=True)
        return r.get("data", {}).get("orders", []) or []

    # ── Qty Precision ─────────────────────────────────────────────────
    async def get_qty_precision(self, symbol: str) -> int:
        if symbol not in self._contract_cache:
            await self.get_contracts()
        c = self._contract_cache.get(symbol, {})
        try:
            prec = c.get("quantityPrecision")
            if prec is not None:
                return int(prec)
        except Exception:
            pass
        try:
            min_qty_str = str(c.get("tradeMinQuantity", "0.001"))
            if "." in min_qty_str:
                return len(min_qty_str.split(".")[1].rstrip("0")) or 1
            return 0
        except Exception:
            return 3

    def _floor_qty(self, qty: float, precision: int) -> float:
        factor = 10 ** precision
        return math.floor(qty * factor) / factor

    # ── Trading ───────────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        """
        Establece leverage. Ignora errores no críticos.
        NO llama a positionSide/dual (endpoint eliminado en cuentas nuevas).
        """
        for side in ("LONG", "SHORT"):
            try:
                r = await self._post("/openApi/swap/v2/trade/leverage", {
                    "symbol": symbol, "side": side, "leverage": leverage
                })
                code = r.get("code", 0)
                # 0=OK, 80001=ya configurado, otros son warnings no bloqueantes
                if code not in (0, 80001, -1130, 100001):
                    log.warning(f"[{symbol}] leverage {side}: code={code}")
            except Exception as e:
                log.warning(f"[{symbol}] set_leverage {side}: {e}")

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        prec    = await self.get_qty_precision(symbol)
        qty_adj = self._floor_qty(qty, prec)

        if qty_adj <= 0:
            msg = f"qty_adj={qty_adj} ≤ 0 (prec={prec} qty={qty:.8f}) — balance insuficiente para este par"
            log.warning(f"[{symbol}] ✗ {msg}")
            return {"code": -1, "msg": msg}

        sl_str = json.dumps({
            "type":        "STOP_MARKET",
            "stopPrice":   round(stop_loss, 6),
            "workingType": "MARK_PRICE"
        }, separators=(',', ':'))

        tp_str = json.dumps({
            "type":        "TAKE_PROFIT_MARKET",
            "stopPrice":   round(take_profit, 6),
            "workingType": "MARK_PRICE"
        }, separators=(',', ':'))

        log.info(f"[{symbol}] → {side} qty={qty_adj} SL={stop_loss:.6g} TP={take_profit:.6g}")

        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol":       symbol,
            "side":         side,
            "positionSide": "BOTH",
            "type":         "MARKET",
            "quantity":     qty_adj,
            "stopLoss":     sl_str,
            "takeProfit":   tp_str,
        })

    async def close_position_market(self, symbol: str, side: str, qty: float) -> dict:
        close_side = "SELL" if side == "BUY" else "BUY"
        prec       = await self.get_qty_precision(symbol)
        qty_adj    = self._floor_qty(qty, prec)
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol":       symbol,
            "side":         close_side,
            "positionSide": "BOTH",
            "type":         "MARKET",
            "quantity":     qty_adj,
        })

    async def cancel_all_orders(self, symbol: str) -> dict:
        return await self._post("/openApi/swap/v2/trade/allOpenOrders", {
            "symbol": symbol
        })

    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        if symbol not in self._contract_cache:
            await self.get_contracts()
        return self._contract_cache.get(symbol)
