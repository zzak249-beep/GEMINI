import hashlib, hmac, json, logging, math, time, urllib.parse
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

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _build_url(self, path: str, params: dict) -> str:
        return f"{config.BASE_URL}{path}?{urllib.parse.urlencode(sorted(params.items()))}"

    def _headers(self): return {"X-BX-APIKEY": self.api_key}

    async def _get(self, path, params=None, signed=False):
        p = dict(params or {})
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            p["signature"] = self._sign(p)
        url = self._build_url(path, p) if p else f"{config.BASE_URL}{path}"
        try:
            async with self.session.get(url, headers=self._headers()) as r:
                return await r.json(content_type=None)
        except Exception as e:
            log.error(f"GET {path}: {e}"); raise

    async def _post(self, path, params):
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = self._sign(p)
        try:
            async with self.session.post(self._build_url(path, p), headers=self._headers()) as r:
                data = await r.json(content_type=None)
                if data.get("code", 0) != 0:
                    log.error(f"POST {path} code={data.get('code')} msg={data.get('msg','?')}")
                return data
        except Exception as e:
            log.error(f"POST {path}: {e}"); raise

    async def get_contracts(self):
        r = await self._get("/openApi/swap/v2/quote/contracts")
        data = r.get("data", []) or []
        for c in data:
            if c.get("symbol"): self._contract_cache[c["symbol"]] = c
        return data

    async def get_tickers(self):
        r = await self._get("/openApi/swap/v2/quote/ticker")
        d = r.get("data", [])
        return d if isinstance(d, list) else []

    async def get_klines(self, symbol, interval="1m", limit=100):
        r = await self._get("/openApi/swap/v3/quote/klines",
                            {"symbol": symbol, "interval": interval, "limit": limit})
        return r.get("data", []) or []

    async def get_24h_volume_history(self, symbol):
        r = await self._get("/openApi/swap/v3/quote/klines",
                            {"symbol": symbol, "interval": "1d", "limit": 8})
        return r.get("data", []) or []

    async def get_balance(self) -> float:
        r = await self._get("/openApi/swap/v2/user/balance", signed=True)
        log.info(f"RAW balance: {str(r)[:400]}")
        try:
            d = r.get("data", {})
            # Formato anidado: data.balance.availableMargin
            if isinstance(d, dict):
                bal = d.get("balance", {})
                if isinstance(bal, dict):
                    for k in ("availableMargin","available","equity","balance"):
                        v = float(bal.get(k, 0))
                        if v > 0: return v
                # Formato plano: data.availableMargin
                for k in ("availableMargin","available","equity","balance","crossWalletBalance"):
                    try:
                        v = float(d.get(k, 0))
                        if v > 0: return v
                    except: pass
            # Formato lista
            if isinstance(d, list):
                for item in d:
                    if isinstance(item, dict):
                        for k in ("availableMargin","available","balance"):
                            try:
                                v = float(item.get(k, 0))
                                if v > 0: return v
                            except: pass
        except Exception as e:
            log.error(f"balance parse: {e}")
        log.warning("balance=0 — transfiere fondos de Spot a Futuros Perpetuos en BingX")
        return 0.0

    async def get_positions(self):
        r = await self._get("/openApi/swap/v2/user/positions", signed=True)
        d = r.get("data", [])
        return d if isinstance(d, list) else []

    async def get_qty_precision(self, symbol: str) -> int:
        if symbol not in self._contract_cache: await self.get_contracts()
        c = self._contract_cache.get(symbol, {})
        try:
            p = c.get("quantityPrecision")
            if p is not None: return int(p)
        except: pass
        try:
            s = str(c.get("tradeMinQuantity", "0.001"))
            if "." in s: return len(s.split(".")[1].rstrip("0")) or 1
            return 0
        except: return 3

    def _floor_qty(self, qty: float, precision: int) -> float:
        f = 10 ** precision
        return math.floor(qty * f) / f

    async def set_leverage(self, symbol: str, leverage: int):
        for side in ("LONG", "SHORT"):
            try:
                r = await self._post("/openApi/swap/v2/trade/leverage",
                                     {"symbol": symbol, "side": side, "leverage": leverage})
                if r.get("code", 0) not in (0, 80001, -1130):
                    log.warning(f"[{symbol}] leverage {side}: {r.get('msg','')}")
            except Exception as e:
                log.warning(f"[{symbol}] leverage: {e}")

    async def place_market_order(self, symbol, side, qty, stop_loss, take_profit):
        prec    = await self.get_qty_precision(symbol)
        qty_adj = self._floor_qty(qty, prec)
        if qty_adj <= 0:
            return {"code": -1, "msg": f"qty_adj={qty_adj}<=0"}
        sl_str = json.dumps({"type":"STOP_MARKET","stopPrice":round(stop_loss,6),
                             "workingType":"MARK_PRICE"}, separators=(',',':'))
        tp_str = json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":round(take_profit,6),
                             "workingType":"MARK_PRICE"}, separators=(',',':'))
        log.info(f"[{symbol}] ORDER {side} qty={qty_adj} SL={stop_loss:.5g} TP={take_profit:.5g}")
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": side, "positionSide": "BOTH",
            "type": "MARKET", "quantity": qty_adj,
            "stopLoss": sl_str, "takeProfit": tp_str,
        })

    async def close_position_market(self, symbol, side, qty):
        prec = await self.get_qty_precision(symbol)
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": "SELL" if side == "BUY" else "BUY",
            "positionSide": "BOTH", "type": "MARKET",
            "quantity": self._floor_qty(qty, prec),
        })

    async def get_symbol_info(self, symbol) -> Optional[dict]:
        if symbol not in self._contract_cache: await self.get_contracts()
        return self._contract_cache.get(symbol)
