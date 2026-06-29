"""
BingX Perpetual Futures REST client.
Hedge mode (positionSide required on all orders).
Signing: HMAC-SHA256 over urlencode(sorted(params)).
"""

import hashlib
import hmac
import logging
import time
import urllib.parse

import requests

log = logging.getLogger("bingx")


class BingXClient:
    def __init__(self, api_key: str, secret_key: str, base_url: str):
        self.api_key    = api_key
        self.secret_key = secret_key
        self.base_url   = base_url.rstrip("/")
        self._session   = requests.Session()
        self._session.headers.update({"X-BX-APIKEY": api_key})

    # ── Signing ───────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(
            self.secret_key.encode(),
            qs.encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _ts() -> int:
        return int(time.time() * 1000)

    # ── HTTP ──────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        p = dict(params or {})
        p["timestamp"] = self._ts()
        p["signature"] = self._sign(p)
        r = self._session.get(f"{self.base_url}{path}", params=p, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"BingX [{d.get('code')}] {d.get('msg')}")
        return d.get("data") or {}

    def _post(self, path: str, params: dict) -> dict:
        p = dict(params)
        p["timestamp"] = self._ts()
        p["signature"] = self._sign(p)
        r = self._session.post(f"{self.base_url}{path}", data=p, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"BingX [{d.get('code')}] {d.get('msg')}")
        return d.get("data") or {}

    # ── Market data ───────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str, limit: int = 300) -> list:
        """
        Returns list of candle dicts sorted oldest→newest.
        interval: 1m 3m 5m 15m 30m 1h 2h 4h 1d
        """
        raw = self._get(
            "/openApi/swap/v3/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        candles = [
            {
                "timestamp": int(k["time"]),
                "open":      float(k["open"]),
                "high":      float(k["high"]),
                "low":       float(k["low"]),
                "close":     float(k["close"]),
                "volume":    float(k["volume"]),
            }
            for k in raw
        ]
        return sorted(candles, key=lambda x: x["timestamp"])

    def get_mark_price(self, symbol: str) -> float:
        d = self._get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        return float(d.get("markPrice", 0))

    def get_symbol_info(self, symbol: str) -> dict:
        data = self._get("/openApi/swap/v2/quote/contracts")
        for s in (data if isinstance(data, list) else []):
            if s.get("symbol") == symbol:
                return s
        return {}

    # ── Account ───────────────────────────────────────────────

    def get_balance_data(self) -> dict:
        """
        BingX /v2/user/balance returns data.balance as a single dict,
        not a list. Handle both formats defensively.
        """
        data = self._get("/openApi/swap/v2/user/balance")
        bal = data.get("balance") or {}
        if isinstance(bal, dict):
            return bal
        if isinstance(bal, list):
            for a in bal:
                if isinstance(a, dict) and a.get("asset") == "USDT":
                    return a
        return {}

    def get_equity(self) -> float:
        b = self.get_balance_data()
        return float(b.get("equity", b.get("balance", 0)))

    def get_available_margin(self) -> float:
        b = self.get_balance_data()
        return float(b.get("availableMargin", 0))

    # ── Positions ─────────────────────────────────────────────

    def get_positions(self, symbol: str = None) -> list:
        """
        Returns open positions (non-zero size).
        Each item: {symbol, positionSide, size, entryPrice, unrealizedPnl, leverage}
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        raw = self._get("/openApi/swap/v2/user/positions", params)
        result = []
        for p in (raw if isinstance(raw, list) else []):
            size = abs(float(p.get("positionAmt", 0)))
            if size < 1e-9:
                continue
            result.append({
                "symbol":        p.get("symbol"),
                "positionSide":  p.get("positionSide"),   # "LONG" | "SHORT"
                "size":          size,
                "entryPrice":    float(p.get("avgPrice", 0)),
                "unrealizedPnl": float(p.get("unrealizedProfit", 0)),
                "leverage":      int(p.get("leverage", 1)),
            })
        return result

    # ── Account setup ─────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int):
        for side in ("LONG", "SHORT"):
            try:
                self._post(
                    "/openApi/swap/v2/trade/leverage",
                    {"symbol": symbol, "leverage": leverage, "side": side},
                )
            except Exception as e:
                log.warning(f"set_leverage {side} error: {e}")

    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED"):
        """margin_type: CROSSED | ISOLATED"""
        try:
            self._post(
                "/openApi/swap/v2/trade/marginType",
                {"symbol": symbol, "marginType": margin_type},
            )
        except Exception as e:
            log.warning(f"set_margin_type error (may already be set): {e}")

    # ── Orders ────────────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
    ) -> dict:
        """
        side:          BUY | SELL
        position_side: LONG | SHORT  (Hedge mode)
        """
        return self._post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol":       symbol,
                "side":         side,
                "positionSide": position_side,
                "type":         "MARKET",
                "quantity":     str(quantity),
            },
        )

    def close_position(self, symbol: str, position_side: str, quantity: float) -> dict:
        """Close (full or partial) a position at market."""
        side = "SELL" if position_side == "LONG" else "BUY"
        return self.place_market_order(symbol, side, position_side, quantity)

    def place_stop_market(
        self,
        symbol: str,
        position_side: str,
        stop_price: float,
        quantity: float,
    ) -> dict:
        """Protective stop-market order (close position)."""
        side = "SELL" if position_side == "LONG" else "BUY"
        return self._post(
            "/openApi/swap/v2/trade/order",
            {
                "symbol":        symbol,
                "side":          side,
                "positionSide":  position_side,
                "type":          "STOP_MARKET",
                "stopPrice":    f"{stop_price:.6f}",
                "quantity":      str(quantity),
            },
        )
