"""
bot/bingx_client.py
Cliente asíncrono para BingX Perpetual Futures (USDT-M) vía CCXT.

Maneja:
  - Conexión y carga de exchange info (precisiones)
  - Configuración de leverage (modo one-way por defecto)
  - OHLCV, balance, posición abierta, funding rate
  - Apertura con TP + SL automáticos
  - Cierre de posición (barrera de tiempo)
  - Cancelación de órdenes pendientes
"""
import asyncio
import logging
import math
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd

logger = logging.getLogger(__name__)

# Mapa de timeframe NEXUS → CCXT BingX
_TF_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "2h": "2h", "4h": "4h",
    "6h": "6h", "12h": "12h", "1d": "1d",
}


class BingXClient:

    def __init__(self, api_key: str, secret_key: str):
        self.api_key    = api_key
        self.secret_key = secret_key
        self._exchange: Optional[ccxt.bingx] = None
        self._markets:  dict = {}

    # ─────────────────────────────────────────────────────────
    # CONEXIÓN
    # ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._exchange = ccxt.bingx({
            "apiKey":  self.api_key,
            "secret":  self.secret_key,
            "options": {"defaultType": "swap"},  # perpetual futures
        })
        await self._exchange.load_markets()
        self._markets = self._exchange.markets
        logger.info(f"BingX conectado — {len(self._markets)} mercados cargados")

    async def disconnect(self) -> None:
        if self._exchange:
            await self._exchange.close()

    # ─────────────────────────────────────────────────────────
    # UTILIDADES DE PRECISIÓN
    # ─────────────────────────────────────────────────────────

    def _get_market(self, symbol: str) -> dict:
        return self._markets.get(symbol, {})

    def _round_price(self, symbol: str, price: float) -> float:
        mkt  = self._get_market(symbol)
        prec = mkt.get("precision", {}).get("price", 0.01)
        if prec and prec > 0:
            return round(math.floor(price / prec) * prec, 8)
        return round(price, 4)

    def _round_qty(self, symbol: str, qty: float) -> float:
        mkt  = self._get_market(symbol)
        prec = mkt.get("precision", {}).get("amount", 0.001)
        if prec and prec > 0:
            return round(math.floor(qty / prec) * prec, 8)
        return round(qty, 4)

    def _min_qty(self, symbol: str) -> float:
        mkt = self._get_market(symbol)
        return float(mkt.get("limits", {}).get("amount", {}).get("min", 0.001) or 0.001)

    # ─────────────────────────────────────────────────────────
    # CONFIGURACIÓN DE SÍMBOLO
    # ─────────────────────────────────────────────────────────

    async def setup_symbol(self, symbol: str, leverage: int) -> None:
        try:
            await self._exchange.set_leverage(leverage, symbol)
            logger.info(f"{symbol}: leverage={leverage}x configurado")
        except Exception as e:
            if "No need to change" not in str(e):
                logger.warning(f"setup_symbol {symbol}: {e}")

    # ─────────────────────────────────────────────────────────
    # DATOS DE MERCADO
    # ─────────────────────────────────────────────────────────

    async def get_klines(self, symbol: str, timeframe: str,
                         limit: int = 300) -> Optional[pd.DataFrame]:
        tf = _TF_MAP.get(timeframe, timeframe)
        try:
            raw = await self._exchange.fetch_ohlcv(symbol, tf, limit=limit)
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=["open_time", "open", "high", "low", "close", "volume"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df.set_index("open_time", inplace=True)
            return df
        except Exception as e:
            logger.error(f"get_klines {symbol}: {e}")
            return None

    async def get_balance(self) -> float:
        """Saldo USDT disponible (wallet balance)."""
        try:
            balance = await self._exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            return float(usdt.get("total", 0.0) or 0.0)
        except Exception as e:
            logger.error(f"get_balance: {e}")
            return 0.0

    async def get_position(self, symbol: str) -> Optional[dict]:
        try:
            positions = await self._exchange.fetch_positions([symbol])
            for p in positions:
                if p["symbol"] == symbol:
                    size = float(p.get("contracts", 0) or 0)
                    side_raw = p.get("side", "")
                    side = "LONG" if side_raw == "long" else ("SHORT" if side_raw == "short" else "FLAT")
                    if size == 0:
                        side = "FLAT"
                    return {
                        "symbol":      symbol,
                        "size":        size if side == "LONG" else -size if side == "SHORT" else 0,
                        "side":        side,
                        "entry_price": float(p.get("entryPrice", 0) or 0),
                        "unrealized":  float(p.get("unrealizedPnl", 0) or 0),
                        "leverage":    int(p.get("leverage", 1) or 1),
                    }
            return None
        except Exception as e:
            logger.error(f"get_position {symbol}: {e}")
            return None

    async def get_funding_rate(self, symbol: str) -> float:
        """Tasa de financiación actual del par."""
        try:
            fr = await self._exchange.fetch_funding_rate(symbol)
            return float(fr.get("fundingRate", 0.0) or 0.0)
        except Exception as e:
            logger.debug(f"get_funding_rate {symbol}: {e}")
            return 0.0

    # ─────────────────────────────────────────────────────────
    # ÓRDENES
    # ─────────────────────────────────────────────────────────

    async def open_long(self, symbol: str, qty: float,
                        tp_price: float, sl_price: float) -> Optional[dict]:
        qty = self._round_qty(symbol, qty)
        tp  = self._round_price(symbol, tp_price)
        sl  = self._round_price(symbol, sl_price)

        if qty < self._min_qty(symbol):
            logger.warning(f"{symbol}: qty {qty} < mínimo {self._min_qty(symbol)}")
            return None

        try:
            # 1. Orden de mercado
            entry = await self._exchange.create_order(
                symbol=symbol, type="market", side="buy", amount=qty
            )
            logger.info(f"{symbol} LONG abierto qty={qty} ~{tp_price:.4f} TP / {sl_price:.4f} SL")

            # 2. TP
            await self._exchange.create_order(
                symbol=symbol, type="TAKE_PROFIT_MARKET", side="sell", amount=qty,
                params={"stopPrice": tp, "closePosition": True, "workingType": "MARK_PRICE"}
            )
            # 3. SL
            await self._exchange.create_order(
                symbol=symbol, type="STOP_MARKET", side="sell", amount=qty,
                params={"stopPrice": sl, "closePosition": True, "workingType": "MARK_PRICE"}
            )
            return {"order": entry, "tp": tp, "sl": sl, "qty": qty, "side": "LONG"}

        except Exception as e:
            logger.error(f"open_long {symbol}: {e}")
            return None

    async def open_short(self, symbol: str, qty: float,
                         tp_price: float, sl_price: float) -> Optional[dict]:
        qty = self._round_qty(symbol, qty)
        tp  = self._round_price(symbol, tp_price)
        sl  = self._round_price(symbol, sl_price)

        if qty < self._min_qty(symbol):
            logger.warning(f"{symbol}: qty {qty} < mínimo {self._min_qty(symbol)}")
            return None

        try:
            entry = await self._exchange.create_order(
                symbol=symbol, type="market", side="sell", amount=qty
            )
            logger.info(f"{symbol} SHORT abierto qty={qty} ~{tp_price:.4f} TP / {sl_price:.4f} SL")

            await self._exchange.create_order(
                symbol=symbol, type="TAKE_PROFIT_MARKET", side="buy", amount=qty,
                params={"stopPrice": tp, "closePosition": True, "workingType": "MARK_PRICE"}
            )
            await self._exchange.create_order(
                symbol=symbol, type="STOP_MARKET", side="buy", amount=qty,
                params={"stopPrice": sl, "closePosition": True, "workingType": "MARK_PRICE"}
            )
            return {"order": entry, "tp": tp, "sl": sl, "qty": qty, "side": "SHORT"}

        except Exception as e:
            logger.error(f"open_short {symbol}: {e}")
            return None

    async def close_position(self, symbol: str, position: dict) -> Optional[dict]:
        """Cierre de mercado para barrera de tiempo."""
        size = abs(position.get("size", 0))
        if size == 0:
            return None
        qty  = self._round_qty(symbol, size)
        side = "sell" if position["side"] == "LONG" else "buy"
        try:
            await self.cancel_all_orders(symbol)
            order = await self._exchange.create_order(
                symbol=symbol, type="market", side=side, amount=qty,
                params={"reduceOnly": True}
            )
            logger.info(f"{symbol}: posición cerrada por barrera de tiempo")
            return order
        except Exception as e:
            logger.error(f"close_position {symbol}: {e}")
            return None

    async def cancel_all_orders(self, symbol: str) -> None:
        try:
            await self._exchange.cancel_all_orders(symbol)
        except Exception as e:
            logger.debug(f"cancel_all_orders {symbol}: {e}")

    async def get_last_trade_pnl(self, symbol: str) -> float:
        try:
            trades = await self._exchange.fetch_my_trades(symbol, limit=5)
            if trades:
                return float(trades[-1].get("info", {}).get("realizedPnl", 0) or 0)
            return 0.0
        except Exception as e:
            logger.debug(f"get_last_trade_pnl {symbol}: {e}")
            return 0.0
