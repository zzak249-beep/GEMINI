"""
bingx_client.py — Cliente BingX Perpetual Futures (One-Way mode)
Añadido: get_all_symbols() + get_ticker_24h() para el escáner
"""
import hmac, hashlib, time, logging, urllib.parse
import requests
import config as C

log = logging.getLogger(__name__)


class BingXClient:
    def __init__(self):
        self.api_key = C.BINGX_API_KEY
        self.secret  = C.BINGX_SECRET_KEY
        self.session = requests.Session()
        self.session.headers.update({"X-BX-APIKEY": self.api_key})

    # ── Firma ──────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"]  = int(time.time() * 1000)
        params["recvWindow"] = 5000
        qs  = urllib.parse.urlencode(sorted(params.items()))
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _get(self, path, params=None, auth=False):
        p = self._sign(params or {}) if auth else (params or {})
        try:
            r = self.session.get(C.BINGX_BASE_URL + path, params=p, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"GET {path}: {e}")
            return {}

    def _post(self, path, params=None):
        p   = self._sign(params or {})
        url = C.BINGX_BASE_URL + path + "?" + urllib.parse.urlencode(sorted(p.items()))
        try:
            r = self.session.post(url, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"POST {path}: {e}")
            return {}

    def _delete(self, path, params=None):
        p = self._sign(params or {})
        try:
            r = self.session.delete(C.BINGX_BASE_URL + path, params=p, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"DELETE {path}: {e}")
            return {}

    # ── Escáner de mercado ─────────────────────────────────────

    def get_all_symbols(self) -> list[str]:
        """Devuelve todos los símbolos de futuros perpetuos USDT activos en BingX."""
        data = self._get("/openApi/swap/v2/quote/contracts")
        symbols = []
        for item in data.get("data", []):
            sym = item.get("symbol", "")
            # Solo pares USDT activos
            if sym.endswith("-USDT") and item.get("status", 1) != 0:
                symbols.append(sym)
        log.info(f"BingX contratos activos: {len(symbols)}")
        return symbols

    def get_ticker_24h(self, symbol: str = None) -> list[dict]:
        """
        Devuelve tickers 24h. Si symbol es None devuelve todos.
        Cada dict incluye: symbol, quoteVolume (vol USDT 24h), lastPrice, priceChangePercent
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/openApi/swap/v2/quote/ticker", params)
        tickers = data.get("data", [])
        if isinstance(tickers, dict):
            tickers = [tickers]
        return tickers or []

    def scan_by_volume(self, min_volume: float, top_n: int, blacklist: list) -> list[str]:
        """
        Escanea todos los tickers de BingX y devuelve los `top_n` símbolos
        con mayor volumen 24h USDT que superen `min_volume`.
        Excluye los símbolos en `blacklist`.
        """
        tickers = self.get_ticker_24h()
        if not tickers:
            log.warning("scan_by_volume: no se recibieron tickers")
            return []

        results = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            if sym in blacklist:
                continue
            try:
                vol = float(t.get("quoteVolume", t.get("volume", 0)))
            except Exception:
                continue
            if vol >= min_volume:
                results.append((sym, vol))

        results.sort(key=lambda x: x[1], reverse=True)
        selected = [s for s, _ in results[:top_n]]
        log.info(f"Escáner: {len(results)} símbolos ≥ {min_volume/1e6:.1f}M → top {top_n}: {selected}")
        return selected

    # ── Market data ────────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str, limit: int = 250) -> list:
        data = self._get("/openApi/swap/v2/quote/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })
        return data.get("data", [])

    def get_funding_rate(self, symbol: str) -> float:
        data = self._get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        try: return float(data["data"]["lastFundingRate"])
        except: return 0.0

    def get_mark_price(self, symbol: str) -> float:
        data = self._get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        try: return float(data["data"]["markPrice"])
        except: return 0.0

    # ── Cuenta ─────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._get("/openApi/swap/v2/user/balance", auth=True)
        try:
            result = data.get("data", {})
            if isinstance(result, dict):
                bal = result.get("balance", result)
                if isinstance(bal, dict):
                    return float(bal.get("availableMargin", 0))
                return float(result.get("availableMargin", 0))
            if isinstance(result, list):
                for item in result:
                    if item.get("asset") in ("USDT", "USD"):
                        return float(item.get("availableMargin", 0))
        except Exception as e:
            log.error(f"Balance parse error: {e} | raw={data}")
        return 0.0

    def get_positions(self, symbol: str = None) -> list:
        params = {}
        if symbol: params["symbol"] = symbol
        data = self._get("/openApi/swap/v2/trade/openPositions", params, auth=True)
        positions = data.get("data", []) or []
        return [p for p in positions if float(p.get("positionAmt", 0)) != 0]

    # ── Configuración ──────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int):
        for side in ("LONG", "SHORT"):
            self._post("/openApi/swap/v2/trade/leverage", {
                "symbol": symbol, "side": side, "leverage": leverage
            })
        log.info(f"Leverage {leverage}x → {symbol}")

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        self._post("/openApi/swap/v2/trade/marginType", {
            "symbol": symbol, "marginType": margin_type
        })

    # ── Órdenes ────────────────────────────────────────────────

    def market_order(self, symbol: str, side: str, qty: float) -> dict:
        """side: BUY | SELL — One-Way mode sin positionSide"""
        data = self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": side,
            "type": "MARKET", "quantity": round(qty, 6),
        })
        log.info(f"Market {side} {qty} {symbol} → code={data.get('code')}")
        return data

    def cancel_all(self, symbol: str):
        self._delete("/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol})

    def close_position(self, symbol: str, position: dict) -> dict:
        amt  = float(position.get("positionAmt", 0))
        if amt == 0: return {}
        side = "SELL" if amt > 0 else "BUY"
        return self.market_order(symbol, side, abs(amt))
