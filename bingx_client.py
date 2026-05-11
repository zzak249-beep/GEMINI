"""
BingX Perpetual Futures Client — V32 Fix
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXES vs versión original:
  1. _post mutaba el dict del llamador (timestamp/signature quedaban del ciclo anterior)
  2. Firma HMAC: BingX quiere query-string ordenada alfabéticamente, no por sorted()
  3. stopLoss/takeProfit: el JSON debe enviarse como STRING en query param, no serializado 2 veces
  4. positionSide=BOTH solo válido en one-way mode — verificamos y adaptamos
  5. Logs completos de request/response para debug
"""

import asyncio
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
    def _sign(self, params: dict) -> str:
        """
        BingX requiere: sorted alfabéticamente, NO por sorted(items()).
        urllib.parse.urlencode con dict ordena por inserción en Python 3.7+.
        Usamos sorted() sobre las keys para garantizar orden alfabético.
        """
        query = "&".join(f"{k}={urllib.parse.quote(str(params[k]), safe='')}"
                        for k in sorted(params.keys()))
        sig = hmac.new(
            self.secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        log.debug(f"  SIGN query={query[:120]}... sig={sig[:16]}...")
        return sig

    def _headers(self) -> dict:
        return {"X-BX-APIKEY": self.api_key}

    # ── HTTP ──────────────────────────────────────────────────────────
    async def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        # Copiar para no mutar el dict del llamador
        p = dict(params or {})
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            p["signature"] = self._sign(p)
        url = f"{config.BASE_URL}{path}"
        try:
            async with self.session.get(url, params=p, headers=self._headers()) as r:
                data = await r.json(content_type=None)
                log.debug(f"GET {path} → code={data.get('code')} msg={data.get('msg','')}")
                return data
        except Exception as e:
            log.error(f"GET {path} exception: {e}")
            raise

    async def _post(self, path: str, params: dict) -> dict:
        # CRÍTICO: copiar para no mutar — el llamador puede reusar el dict
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = self._sign(p)
        url = f"{config.BASE_URL}{path}"
        # BingX acepta los params como query string en POST (igual que GET)
        try:
            async with self.session.post(url, params=p, headers=self._headers()) as r:
                data = await r.json(content_type=None)
                code = data.get("code", -1)
                if code != 0:
                    log.error(
                        f"POST {path} FAIL code={code} msg={data.get('msg','?')}"
                        f"\n  params_sent={json.dumps({k: v for k, v in p.items() if k != 'signature'})}"
                    )
                else:
                    log.debug(f"POST {path} OK")
                return data
        except Exception as e:
            log.error(f"POST {path} exception: {e}")
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
            log.error(f"get_balance parse error: {e} | raw={r}")
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
        # Inferir desde tradeMinQuantity
        min_qty_str = str(c.get("tradeMinQuantity", "0.001"))
        try:
            mq = float(min_qty_str)
            if mq >= 1:
                return 0
            if "." in min_qty_str:
                return len(min_qty_str.split(".")[1].rstrip("0")) or 1
        except Exception:
            pass
        return 3  # fallback seguro

    def _floor_qty(self, qty: float, precision: int) -> float:
        factor = 10 ** precision
        return math.floor(qty * factor) / factor

    # ── Trading ───────────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        """
        BingX en one-way mode: el campo 'side' en set_leverage
        acepta LONG/SHORT pero internamente aplica a ambos.
        """
        errors = []
        for side in ("LONG", "SHORT"):
            r = await self._post("/openApi/swap/v2/trade/leverage", {
                "symbol": symbol, "side": side, "leverage": leverage
            })
            code = r.get("code", 0)
            # 0 = OK, algunos códigos de error son normales (ya configurado)
            if code not in (0, 80001, -1130):
                errors.append(f"{side}: code={code} {r.get('msg','')}")

        # One-way mode
        r = await self._post("/openApi/swap/v2/trade/positionSide/dual", {
            "dualSidePosition": "false"
        })
        code = r.get("code", 0)
        if code not in (0, 80001):
            log.debug(f"positionSide/dual code={code}: {r.get('msg','')} (puede ser normal)")

        if errors:
            log.warning(f"[{symbol}] set_leverage warnings: {errors}")

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        # Ajustar qty a la precisión del contrato
        prec    = await self.get_qty_precision(symbol)
        qty_adj = self._floor_qty(qty, prec)

        if qty_adj <= 0:
            log.error(f"[{symbol}] qty_adj={qty_adj} después de precision={prec}, qty_original={qty}")
            return {"code": -1, "msg": f"qty_adj={qty_adj} ≤ 0 (precision={prec}, qty={qty:.8f})"}

        # SL/TP como strings JSON — BingX los espera como string en el query param
        sl_str = json.dumps({
            "type":        "STOP_MARKET",
            "stopPrice":   float(f"{stop_loss:.6g}"),
            "workingType": "MARK_PRICE"
        }, separators=(',', ':'))

        tp_str = json.dumps({
            "type":        "TAKE_PROFIT_MARKET",
            "stopPrice":   float(f"{take_profit:.6g}"),
            "workingType": "MARK_PRICE"
        }, separators=(',', ':'))

        log.info(
            f"[{symbol}] → ORDER side={side} qty={qty_adj} (prec={prec}) "
            f"SL={stop_loss:.6g} TP={take_profit:.6g}"
        )

        r = await self._post("/openApi/swap/v2/trade/order", {
            "symbol":       symbol,
            "side":         side,
            "positionSide": "BOTH",
            "type":         "MARKET",
            "quantity":     qty_adj,
            "stopLoss":     sl_str,
            "takeProfit":   tp_str,
        })

        return r

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
