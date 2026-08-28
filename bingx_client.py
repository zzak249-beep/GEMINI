"""
bingx_client.py
================
Cliente REST para BingX USDT-M Perpetual Futures (Swap).

Basado en el esquema de firma de la API pública de BingX (familia
Binance-like): HMAC-SHA256 sobre el query string ordenado
alfabéticamente, enviado como query params tanto en GET como en POST
(cabecera X-BX-APIKEY). Si en algún momento un pedido de orden devuelve
error 100001 ("signature verification failed"), revisa primero:
  1) BINGX_API_KEY / BINGX_API_SECRET correctos y sin espacios/comillas
  2) reloj del servidor Railway desincronizado (por eso se manda recvWindow)
  3) que no se haya mutado el dict de params entre firmar y enviar

Modo de cuenta: este cliente asume HEDGE MODE (no One-way), por eso todas
las llamadas de orden usan positionSide=LONG/SHORT explícito en vez de
reduceOnly + positionSide=BOTH.

Símbolos en formato BingX: "BTC-USDT" (con guion), no "BTCUSDT".
"""

from __future__ import annotations

import hmac
import logging
import time
from decimal import ROUND_DOWN, Decimal
from hashlib import sha256
from urllib.parse import urlencode

import pandas as pd
import requests

logger = logging.getLogger("bingx_client")


class BingXAPIError(Exception):
    """Error de negocio devuelto por BingX (code != 0)."""

    def __init__(self, code, msg, raw=None):
        self.code = code
        self.msg = msg
        self.raw = raw
        super().__init__(f"BingX error {code}: {msg}")


class PositionNotExistError(BingXAPIError):
    """Código 109420 - la posición ya no existe (ya se cerró)."""


class BingXClient:
    BASE_URL = "https://open-api.bingx.com"

    def __init__(self, api_key: str, api_secret: str, recv_window: int = 10000, timeout: int = 15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = recv_window
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-BX-APIKEY": self.api_key})

    # ------------------------------------------------------------------
    # Núcleo: firma y envío de peticiones
    # ------------------------------------------------------------------
    def _sign(self, params: dict) -> str:
        query = urlencode(sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), digestmod=sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = True, retries: int = 3):
        params = dict(params or {})
        if signed:
            params["timestamp"] = str(int(time.time() * 1000))
            params["recvWindow"] = str(self.recv_window)
            query = self._sign(params)
        else:
            query = urlencode(sorted(params.items()))

        url = f"{self.BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"

        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout)
                data = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                logger.warning("Fallo de red/parseo en %s %s (intento %s/%s): %s", method, path, attempt, retries, exc)
                time.sleep(min(2 ** attempt, 10))
        else:
            raise RuntimeError(f"No se pudo contactar a BingX tras {retries} intentos: {last_exc}")

        code = data.get("code")
        if code not in (0, None):
            msg = data.get("msg", "sin mensaje")
            if code == 109420:
                raise PositionNotExistError(code, msg, raw=data)
            raise BingXAPIError(code, msg, raw=data)
        return data

    # ------------------------------------------------------------------
    # Mercado
    # ------------------------------------------------------------------
    def get_klines(self, symbol: str, interval: str, limit: int = 500, end_time: int | None = None) -> pd.DataFrame:
        """Devuelve DataFrame ordenado ascendentemente por tiempo, con
        columnas open/high/low/close/volume (float) indexado por el
        instante de apertura de cada vela (UTC).

        `end_time` (ms epoch, opcional) permite paginar hacia atrás en el
        histórico: se usa en backtest.py para reunir más velas de las que
        entrega una sola llamada.
        """
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time is not None:
            params["endTime"] = int(end_time)
        data = self._request("GET", "/openApi/swap/v3/quote/klines", params, signed=False)
        rows = data.get("data", [])
        if not rows:
            if end_time is not None:
                # Paginando hacia atrás en el histórico (backtest): una
                # respuesta vacía aquí solo significa "no hay más velas
                # antiguas disponibles", no es un error.
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            raise RuntimeError(f"Respuesta de klines vacía para {symbol}: {data}")

        parsed = []
        for row in rows:
            if isinstance(row, dict):
                parsed.append(
                    {
                        "time": int(row["time"]),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0.0)),
                    }
                )
            else:
                # Fallback defensivo si la API devolviera arrays crudos.
                # Orden documentado: [time, open, high, low, close, volume].
                # OJO: en versiones antiguas de BingX este orden venía
                # alterado (bug histórico close/high intercambiados) - si
                # ves velas absurdas al arrancar, verifica esto contra el
                # gráfico real del símbolo antes de operar en real.
                parsed.append(
                    {
                        "time": int(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]) if len(row) > 5 else 0.0,
                    }
                )

        df = pd.DataFrame(parsed).drop_duplicates(subset="time").sort_values("time")
        df.index = pd.to_datetime(df["time"], unit="ms", utc=True)
        return df[["open", "high", "low", "close", "volume"]]

    def get_contract_info(self, symbol: str) -> dict:
        data = self._request(
            "GET", "/openApi/swap/v2/quote/contracts", {"symbol": symbol}, signed=False
        )
        rows = data.get("data", [])
        for row in rows:
            if row.get("symbol") == symbol:
                return {
                    "quantity_precision": int(row.get("quantityPrecision", 3)),
                    "price_precision": int(row.get("pricePrecision", 2)),
                }
        raise RuntimeError(f"Símbolo {symbol} no encontrado en /quote/contracts: {data}")

    # ------------------------------------------------------------------
    # Cuenta
    # ------------------------------------------------------------------
    def get_available_balance(self) -> float:
        data = self._request("GET", "/openApi/swap/v2/user/balance", {})
        balance = data.get("data", {}).get("balance", {})
        for key in ("availableMargin", "balance", "equity"):
            if key in balance:
                try:
                    return float(balance[key])
                except (TypeError, ValueError):
                    continue
        raise RuntimeError(f"No se pudo leer el balance disponible: {data}")

    def get_position(self, symbol: str, position_side: str = "LONG") -> dict | None:
        data = self._request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
        for pos in data.get("data", []):
            if pos.get("symbol") == symbol and pos.get("positionSide") == position_side:
                amt = float(pos.get("positionAmt", 0) or 0)
                if abs(amt) > 0:
                    return {
                        "amount": abs(amt),
                        "entry_price": float(pos.get("avgPrice", 0) or 0),
                        "unrealized_pnl": float(pos.get("unrealizedProfit", 0) or 0),
                        "leverage": pos.get("leverage"),
                    }
        return None

    def set_leverage(self, symbol: str, leverage: int, side: str = "LONG") -> None:
        self._request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            {"symbol": symbol, "side": side, "leverage": leverage},
        )

    # ------------------------------------------------------------------
    # Órdenes (modo Hedge -> positionSide explícito, sin reduceOnly)
    # ------------------------------------------------------------------
    def open_long_market(self, symbol: str, quantity: str) -> dict:
        return self._request(
            "POST",
            "/openApi/swap/v2/trade/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "positionSide": "LONG",
                "type": "MARKET",
                "quantity": quantity,
            },
        )

    def close_long_market(self, symbol: str, quantity: str) -> dict:
        return self._request(
            "POST",
            "/openApi/swap/v2/trade/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "positionSide": "LONG",
                "type": "MARKET",
                "quantity": quantity,
            },
        )

    def place_stop_loss(self, symbol: str, stop_price: str, quantity: str) -> dict:
        """STOP_MARKET de protección para el LONG (red de seguridad,
        no forma parte de la estrategia original - ver STOP_LOSS_PCT)."""
        return self._request(
            "POST",
            "/openApi/swap/v2/trade/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "positionSide": "LONG",
                "type": "STOP_MARKET",
                "stopPrice": stop_price,
                "quantity": quantity,
                "workingType": "MARK_PRICE",
            },
        )

    def cancel_order(self, symbol: str, order_id) -> dict:
        return self._request(
            "DELETE",
            "/openApi/swap/v2/trade/order",
            {"symbol": symbol, "orderId": order_id},
        )

    def get_open_orders(self, symbol: str) -> list:
        data = self._request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
        payload = data.get("data", [])
        # Defensivo: algunas respuestas anidan la lista bajo "orders", otras
        # la devuelven directamente como lista.
        if isinstance(payload, dict):
            return payload.get("orders", []) or []
        if isinstance(payload, list):
            return payload
        return []

    def cancel_all_open_orders(self, symbol: str) -> list:
        """Cancela toda orden abierta del símbolo (en la práctica, el único
        tipo de orden que este bot deja pendiente es su propio stop-loss).
        Devuelve los orderId que sí se pudieron cancelar."""
        cancelled = []
        for order in self.get_open_orders(symbol):
            order_id = order.get("orderId")
            if not order_id:
                continue
            try:
                self.cancel_order(symbol, order_id)
                cancelled.append(order_id)
            except BingXAPIError as exc:
                logger.warning("No se pudo cancelar orden %s: %s", order_id, exc)
        return cancelled


def format_quantity(value: float, precision: int) -> str:
    """Trunca (no redondea hacia arriba) a la precisión del contrato para
    no exceder el margen disponible por errores de redondeo."""
    quant = Decimal(1).scaleb(-precision) if precision > 0 else Decimal(1)
    d = Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN)
    return format(d, "f")
