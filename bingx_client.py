"""
Cliente mínimo para BingX Perpetual Swap (v2).
Firma HMAC-SHA256: el query string se construye UNA sola vez, ordenado,
y se usa exactamente igual para firmar y para enviar (evita el bug clásico
de firmar en un orden y transmitir en otro).
"""
import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import requests

import config

log = logging.getLogger("bingx")

TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.6


class BingXError(Exception):
    pass


class BingXClient:
    def __init__(self, api_key=None, api_secret=None, base_url=None):
        # .strip() defensivo: una key/secret con un '\n' o espacio colado
        # (típico al pegar variables en Railway) rompe la cabecera HTTP
        # X-BX-APIKEY con un ValueError críptico en pleno reconcile/entrada.
        self.api_key = (api_key or config.BINGX_API_KEY or "").strip()
        self.api_secret = (api_secret or config.BINGX_API_SECRET or "").strip()
        self.base_url = (base_url or config.BINGX_BASE_URL or "").strip()
        self._filters_cache = {}  # symbol -> (fetched_at, filters_dict)
        if not self.api_key or not self.api_secret:
            log.warning("BINGX_API_KEY / BINGX_API_SECRET no configuradas.")

    # ------------------------------------------------------------------ #
    def _signed_request(self, method: str, path: str, params: dict):
        params = {k: v for k, v in params.items() if v is not None}
        params["timestamp"] = str(int(time.time() * 1000))
        params["recvWindow"] = params.get("recvWindow", "10000")

        # orden fijo (sorted) usado TANTO para firmar COMO para transmitir
        ordered_items = sorted(params.items())
        query_string = urlencode(ordered_items)

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = f"{self.base_url}{path}?{query_string}&signature={signature}"
        headers = {"X-BX-APIKEY": self.api_key}

        # Reintentos solo para fallos de red/timeout, NUNCA para rechazos
        # de la API (esos son definitivos: fondos insuficientes, símbolo
        # inválido, etc. — reintentarlos no cambia el resultado y puede
        # duplicar efectos secundarios).
        last_network_exc = None
        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(method, url, headers=headers, timeout=TIMEOUT)
                data = resp.json()
                last_network_exc = None
                break
            except Exception as e:
                last_network_exc = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Fallo de red llamando a %s (intento %d/%d): %s — reintentando",
                        path, attempt, MAX_RETRIES, e,
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        if last_network_exc is not None:
            raise BingXError(f"Fallo de red/parseo llamando a {path}: {last_network_exc}") from last_network_exc

        if data.get("code") not in (0, None):
            raise BingXError(f"BingX API error en {path}: {data}")
        return data.get("data", data)

    # ------------------------------------------------------------------ #
    def get_balance(self) -> float:
        """Devuelve el equity disponible en USDT de la cuenta de swap."""
        data = self._signed_request("GET", "/openApi/swap/v2/user/balance", {})
        balances = data.get("balance", data)
        if isinstance(balances, dict):
            return float(balances.get("equity", balances.get("balance", 0)))
        if isinstance(balances, list):
            for b in balances:
                if b.get("asset") == "USDT":
                    return float(b.get("equity", b.get("balance", 0)))
        return 0.0

    def get_positions(self, symbol: str = None):
        params = {"symbol": symbol} if symbol else {}
        data = self._signed_request("GET", "/openApi/swap/v2/user/positions", params)
        return data if isinstance(data, list) else data.get("positions", [])

    def set_leverage(self, symbol: str, side: str, leverage: int):
        """side: 'LONG' o 'SHORT' (modo hedge, que es el que usa este bot)."""
        return self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            {"symbol": symbol, "side": side, "leverage": leverage},
        )

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        return self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/marginType",
            {"symbol": symbol, "marginType": mode},
        )

    def place_market_order(
        self,
        symbol: str,
        side: str,           # "BUY" / "SELL"
        position_side: str,  # "LONG" / "SHORT"
        quantity: float,
        stop_loss: float = None,
        take_profit: float = None,
        reduce_only: bool = False,
    ):
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": "true" if reduce_only else "false",
        }
        if stop_loss:
            params["stopLoss"] = (
                '{"type":"STOP_MARKET","stopPrice":%s,"workingType":"MARK_PRICE"}'
                % stop_loss
            )
        if take_profit:
            params["takeProfit"] = (
                '{"type":"TAKE_PROFIT_MARKET","stopPrice":%s,"workingType":"MARK_PRICE"}'
                % take_profit
            )
        return self._signed_request("POST", "/openApi/swap/v2/trade/order", params)

    def close_position(self, symbol: str, position_side: str, quantity: float):
        """Cierra con una orden de mercado reduceOnly en sentido contrario."""
        side = "SELL" if position_side == "LONG" else "BUY"
        return self.place_market_order(
            symbol, side, position_side, quantity, reduce_only=True
        )

    def get_symbol_filters(self, symbol: str):
        """Precisión de cantidad/precio para el símbolo (evita rechazos por decimales)."""
        data = self._signed_request(
            "GET", "/openApi/swap/v2/quote/contracts", {"symbol": symbol}
        )
        items = data if isinstance(data, list) else [data]
        return items[0] if items else {}

    def get_symbol_filters_cached(self, symbol: str, ttl_seconds: int = 3600):
        """Igual que get_symbol_filters pero cacheado en memoria (evita una
        llamada extra a BingX en cada entrada; la precisión de un símbolo
        casi nunca cambia)."""
        now = time.time()
        cached = self._filters_cache.get(symbol)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]
        filters = self.get_symbol_filters(symbol)
        self._filters_cache[symbol] = (now, filters)
        return filters

    def round_qty(self, symbol: str, qty: float) -> float:
        """Ajusta qty a la precisión/tamaño mínimo que exige BingX para ese
        símbolo. Si no consigue leer los filtros (símbolo raro, fallo de
        red), devuelve qty redondeada a 3 decimales como fallback seguro
        en vez de reventar la entrada."""
        try:
            filters = self.get_symbol_filters_cached(symbol)
            precision = int(filters.get("quantityPrecision", 3))
            min_qty = float(
                filters.get("tradeMinQuantity", filters.get("minQty", 0)) or 0
            )
        except Exception:
            log.warning("No se pudo leer precisión de %s, uso fallback de 3 decimales", symbol)
            precision, min_qty = 3, 0.0

        rounded = round(qty, precision)
        if min_qty and rounded < min_qty:
            rounded = min_qty
        return rounded

    # ------------------------------------------------------------------ #
    # Mercado público (sin firma) — velas para calcular la señal nosotros
    # mismos en vez de depender de TradingView.
    # ------------------------------------------------------------------ #
    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 200):
        """Devuelve velas OHLCV crudas de BingX (no requiere API key)."""
        url = f"{self.base_url}/openApi/swap/v3/quote/klines"
        try:
            resp = requests.get(
                url,
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=TIMEOUT,
            )
            data = resp.json()
        except Exception as e:
            raise BingXError(f"Fallo obteniendo klines de {symbol}: {e}") from e
        if data.get("code") not in (0, None):
            raise BingXError(f"BingX API error en klines de {symbol}: {data}")
        return data.get("data", [])

    def get_all_symbols(self, quote_filter: str = "USDT"):
        """Lista todos los perpetuos disponibles en BingX (endpoint público,
        sin API key). quote_filter=None para traerlos todos (USDT, USDC, etc)."""
        url = f"{self.base_url}/openApi/swap/v2/quote/contracts"
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            data = resp.json()
        except Exception as e:
            raise BingXError(f"Fallo listando símbolos de BingX: {e}") from e
        if data.get("code") not in (0, None):
            raise BingXError(f"BingX API error listando símbolos: {data}")
        rows = data.get("data", [])
        symbols = []
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            status = r.get("status", r.get("apiStateOpen", 1))
            if status in (0, False, "OFFLINE"):
                continue
            if quote_filter and not sym.endswith(f"-{quote_filter}"):
                continue
            symbols.append(sym)
        return sorted(set(symbols))

    # ------------------------------------------------------------------ #
    # PnL realizado — para saber cuánto se ganó/perdió cuando una posición
    # se cierra sola por el SL/TP embebido en la orden (BingX la cierra él
    # mismo; este bot se entera por reconciliación, no por una orden propia).
    # ------------------------------------------------------------------ #
    def get_income(self, symbol: str, income_type: str = "REALIZED_PNL",
                    start_time: int = None, limit: int = 100):
        params = {"symbol": symbol, "incomeType": income_type, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        data = self._signed_request("GET", "/openApi/swap/v2/user/income", params)
        return data if isinstance(data, list) else data.get("income", [])

    def get_realized_pnl_since(self, symbol: str, start_time_ms: int = None) -> float:
        rows = self.get_income(symbol, "REALIZED_PNL", start_time_ms)
        return sum(float(r.get("income", 0)) for r in rows)
