"""
BingX Perpetual Futures Client — FIRMA CORREGIDA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BingX exige que stopLoss y takeProfit se firmen como el STRING del JSON,
no como objeto. El error "Signature verification failed" ocurría porque
json.dumps() producía un string que luego urllib.parse.urlencode re-codificaba
de forma diferente a como BingX lo esperaba.

Solución: construir el query string manualmente para la firma,
igual que BingX espera recibirlo.
"""
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

    # ── Firma ────────────────────────────────────────────────────────
    def _build_query(self, params: dict) -> str:
        """
        Construye query string EXACTAMENTE como BingX lo firma:
        - Orden alfabético por key
        - urllib.parse.quote para cada valor (safe='')
        - NO doble-codificación de caracteres especiales en JSON strings
        """
        parts = []
        for k in sorted(params.keys()):
            v = params[k]
            # Los valores ya son strings o números — quote igual que BingX
            parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
        return "&".join(parts)

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            self.secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _headers(self) -> dict:
        return {"X-BX-APIKEY": self.api_key}

    # ── HTTP ──────────────────────────────────────────────────────────
    async def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        p = dict(params or {})
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            qs = self._build_query(p)
            p["signature"] = self._sign(qs)
        url = f"{config.BASE_URL}{path}"
        try:
            async with self.session.get(url, params=p, headers=self._headers()) as r:
                data = await r.json(content_type=None)
                code = data.get("code", 0)
                if code != 0 and signed:
                    log.error(f"GET {path} code={code} msg={data.get('msg','')}")
                return data
        except Exception as e:
            log.error(f"GET {path} error: {e}")
            raise

    async def _post(self, path: str, params: dict) -> dict:
        # CRÍTICO: copiar para no mutar el dict original
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        # Firmar ANTES de añadir signature al dict
        qs = self._build_query(p)
        p["signature"] = self._sign(qs)
        url = f"{config.BASE_URL}{path}"
        try:
            async with self.session.post(url, params=p, headers=self._headers()) as r:
                data = await r.json(content_type=None)
                code = data.get("code", -1)
                if code != 0:
                    log.error(f"POST {path} FAIL code={code} msg={data.get('msg','?')}")
                return data
        except Exception as e:
            log.error(f"POST {path} error: {e}")
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
        prec = c.get("quantityPrecision")
        if prec is not None:
            try:
                return int(prec)
            except (ValueError, TypeError):
                pass
        min_qty_str = str(c.get("tradeMinQuantity", "0.001"))
        try:
            mq = float(min_qty_str)
            if mq >= 1:
                return 0
            if "." in min_qty_str:
                return len(min_qty_str.split(".")[1].rstrip("0")) or 1
        except Exception:
            pass
        return 3

    def _floor_qty(self, qty: float, precision: int) -> float:
        factor = 10 ** precision
        return math.floor(qty * factor) / factor

    # ── Trading ───────────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        for side in ("LONG", "SHORT"):
            r = await self._post("/openApi/swap/v2/trade/leverage", {
                "symbol": symbol, "side": side, "leverage": leverage
            })
            code = r.get("code", 0)
            if code not in (0, 80001, -1130):
                log.warning(f"set_leverage {side} code={code}: {r.get('msg','')}")
        # One-way mode
        await self._post("/openApi/swap/v2/trade/positionSide/dual", {
            "dualSidePosition": "false"
        })

    async def place_market_order(
        self, symbol: str, side: str, qty: float,
        stop_loss: float, take_profit: float,
    ) -> dict:
        prec    = await self.get_qty_precision(symbol)
        qty_adj = self._floor_qty(qty, prec)
        if qty_adj <= 0:
            return {"code": -1, "msg": f"qty_adj={qty_adj} ≤ 0 (prec={prec} qty={qty:.8f})"}

        # SL/TP como JSON string — BingX los recibe como string en el query param
        sl_str = json.dumps({
            "type": "STOP_MARKET",
            "stopPrice": round(stop_loss, 6),
            "workingType": "MARK_PRICE"
        }, separators=(',', ':'))
        tp_str = json.dumps({
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": round(take_profit, 6),
            "workingType": "MARK_PRICE"
        }, separators=(',', ':'))

        log.info(f"[{symbol}] ORDER {side} qty={qty_adj} SL={stop_loss:.6g} TP={take_profit:.6g}")

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
