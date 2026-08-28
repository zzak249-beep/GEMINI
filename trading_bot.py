"""
trading_bot.py
===============
Orquesta el ciclo completo: espera al cierre de cada vela de 15m,
descarga klines de BingX, evalúa la estrategia RSI+SuperTrend "Doble Dip"
y ejecuta entradas/salidas en BingX Futures, notificando todo por Telegram.

Diseño clave para fiabilidad 24/7:
  - Antes de cada decisión se relee la posición REAL en BingX (no se
    confía solo en el estado en memoria) para evitar duplicar órdenes
    tras un reinicio del proceso en Railway.
  - El stop-loss de seguridad (STOP_LOSS_PCT) se coloca como orden
    STOP_MARKET real en el exchange, no como vigilancia por polling:
    sigue protegiendo la cuenta aunque el bot se caiga.
  - Si la posición desaparece entre ciclos sin que el bot la haya
    cerrado (el stop-loss saltó solo), se detecta y se avisa por
    Telegram en vez de quedar en silencio.
  - Al reiniciar con una posición ya abierta, el bot intenta recuperar
    el orderId de su stop-loss existente; y al cerrar por señal,
    cancela TODAS las órdenes abiertas del símbolo (no solo el ID que
    recuerda en memoria), para no dejar nunca un stop huérfano.
  - Comprobación de credenciales/conectividad al arrancar: si
    BINGX_API_KEY/SECRET son inválidas, falla de inmediato con aviso
    claro en vez de esperar a la primera señal real.
  - DRY_RUN=true (por defecto) calcula y notifica las señales sin
    enviar ninguna orden real - úsalo para validar el comportamiento
    antes de pasar a operar con dinero real.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd

from bingx_client import BingXAPIError, BingXClient, PositionNotExistError, format_quantity
from indicators import SignalResult, evaluate
from telegram_notifier import TelegramNotifier

logger = logging.getLogger("trading_bot")


def seconds_until_next_boundary(timeframe_minutes: int) -> float:
    now_ts = datetime.now(timezone.utc).timestamp()
    epoch_minutes = now_ts / 60.0
    next_boundary_minutes = (int(epoch_minutes // timeframe_minutes) + 1) * timeframe_minutes
    return (next_boundary_minutes * 60.0) - now_ts


class TradingBot:
    def __init__(self, config):
        self.config = config
        self.client = BingXClient(config.BINGX_API_KEY, config.BINGX_API_SECRET)
        self.notifier = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
        self.contract_info = {"quantity_precision": 3, "price_precision": 2}
        self.state = {"stop_order_id": None}
        self.last_status = {"started_at": datetime.now(timezone.utc).isoformat()}

    # ------------------------------------------------------------------
    def _initialize(self):
        try:
            self.contract_info = self.client.get_contract_info(self.config.SYMBOL)
            logger.info("Info de contrato %s: %s", self.config.SYMBOL, self.contract_info)
        except Exception as exc:
            logger.error("No se pudo leer la info del contrato, uso valores por defecto: %s", exc)

        if not self.config.DRY_RUN:
            # Comprobación de conectividad/credenciales en caliente: si la
            # API key/secret están mal, falla aquí de forma clara en vez de
            # descubrirlo días después, cuando por fin salte una señal real.
            try:
                balance = self.client.get_available_balance()
                logger.info("Conexión con BingX OK. Balance disponible: %.2f USDT", balance)
            except Exception as exc:
                msg = (
                    f"No se pudo conectar con BingX al arrancar (revisa BINGX_API_KEY / "
                    f"BINGX_API_SECRET y los permisos de la key): {exc}"
                )
                logger.error(msg)
                self.notifier.error(msg)
                raise RuntimeError(msg) from exc

            try:
                self.client.set_leverage(self.config.SYMBOL, self.config.LEVERAGE, side="LONG")
            except Exception as exc:
                logger.warning("No se pudo fijar apalancamiento automáticamente: %s", exc)

            try:
                position = self.client.get_position(self.config.SYMBOL, "LONG")
                self.state["in_position"] = position is not None
                if position:
                    logger.info("Posición LONG ya abierta al arrancar: %s", position)
                    self.notifier.info(
                        f"ℹ️ Al arrancar ya hay una posición LONG abierta en {self.config.SYMBOL}: "
                        f"{position['amount']} @ {position['entry_price']:.4f}. "
                        "El bot la gestionará (la cerrará si SuperTrend da señal de venta)."
                    )
                    self._reconcile_stop_order()
            except Exception as exc:
                logger.warning("No se pudo consultar posición existente al arrancar: %s", exc)

    def _reconcile_stop_order(self):
        """Si al arrancar ya hay una posición abierta, intenta recuperar el
        orderId de su stop-loss existente (por si el bot se reinició y
        perdió el estado en memoria) para poder cancelarlo correctamente
        más adelante en vez de depender de un ID que ya no recuerda."""
        try:
            open_orders = self.client.get_open_orders(self.config.SYMBOL)
            stop_orders = [o for o in open_orders if o.get("type") == "STOP_MARKET"]
            if stop_orders:
                self.state["stop_order_id"] = stop_orders[0].get("orderId")
                logger.info("Stop-loss existente vinculado tras reinicio: %s", self.state["stop_order_id"])
        except Exception as exc:
            logger.warning("No se pudieron leer las órdenes abiertas al arrancar: %s", exc)

    def run(self):
        self._initialize()
        self.notifier.startup(self.config.summary())
        logger.info("Bot iniciado.\n%s", self.config.summary())

        while True:
            try:
                sleep_s = seconds_until_next_boundary(self.config.TIMEFRAME_MINUTES)
                sleep_s += self.config.POLL_BUFFER_SECONDS
                time.sleep(max(sleep_s, 1))
                self._run_cycle()
            except Exception as exc:
                logger.exception("Error en el ciclo principal")
                self.notifier.error(f"Ciclo principal: {exc}")
                time.sleep(30)

    # ------------------------------------------------------------------
    def _drop_unclosed_candle(self, df: pd.DataFrame) -> pd.DataFrame:
        now = pd.Timestamp.now(tz="UTC")
        tf = pd.Timedelta(minutes=self.config.TIMEFRAME_MINUTES)
        if len(df) and (df.index[-1] + tf) > now:
            df = df.iloc[:-1]
        return df

    def _run_cycle(self):
        df = self.client.get_klines(self.config.SYMBOL, self.config.TIMEFRAME, self.config.HISTORY_CANDLES)
        df = self._drop_unclosed_candle(df)
        if len(df) < max(self.config.RSI_LENGTH, self.config.ATR_PERIOD) + self.config.SIG_LENGTH + 5:
            logger.warning("Muy pocas velas cerradas (%d) para calcular indicadores con fiabilidad.", len(df))
            return

        signal = evaluate(df, self.config.strategy_params())
        logger.info(
            "Vela %s | close=%.4f RSI=%.2f señal=%.2f cross=%d ST=%.4f dir=%d specialBuy=%s stSell=%s",
            signal.candle_time, signal.close, signal.rsi, signal.rsi_signal,
            signal.cross_count, signal.supertrend, signal.direction,
            signal.special_buy, signal.st_sell,
        )

        if self.config.DRY_RUN:
            position = None
            in_position = self.state.get("dry_run_in_position", False)
        else:
            position = self.client.get_position(self.config.SYMBOL, "LONG")
            in_position = position is not None

        was_in_position = self.state.get("in_position", False)

        self.last_status.update(
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "candle_time": str(signal.candle_time),
                "close": signal.close,
                "rsi": signal.rsi,
                "supertrend_direction": signal.direction,
                "in_position": bool(in_position),
                "special_buy": signal.special_buy,
                "st_sell": signal.st_sell,
            }
        )

        if not in_position and signal.special_buy:
            self._open_long(signal)
        elif in_position and signal.st_sell:
            self._close_long(signal, position, reason="Cambio de dirección de SuperTrend (bajista)")
        elif (not self.config.DRY_RUN) and was_in_position and not in_position:
            self._handle_external_close()

        if not self.config.DRY_RUN:
            self.state["in_position"] = in_position

    def _handle_external_close(self):
        """La posición estaba abierta en el ciclo anterior y ahora ya no
        existe, sin que este ciclo la haya cerrado por señal de SuperTrend.
        Motivo casi siempre: saltó el stop-loss de seguridad (o se cerró
        manualmente en el exchange). Sin esto, el bot se quedaría "plano"
        en silencio y el usuario no se enteraría salvo mirando BingX."""
        logger.info("La posición se cerró entre ciclos sin intervención del bot (probable stop-loss).")
        self.state["stop_order_id"] = None
        self.notifier.info(
            f"ℹ️ La posición LONG en {self.config.SYMBOL} ya no está abierta: se cerró entre "
            "ciclos (probablemente por el stop-loss de seguridad, o manualmente). El bot "
            "queda a la espera de una nueva señal de entrada."
        )

    # ------------------------------------------------------------------
    def _extract_fill_price(self, order_response: dict, fallback: float) -> float:
        try:
            data = order_response.get("data", {}).get("order", {})
            for key in ("avgPrice", "price"):
                val = data.get(key)
                if val and float(val) > 0:
                    return float(val)
        except (AttributeError, TypeError, ValueError):
            pass
        return fallback

    def _open_long(self, signal: SignalResult):
        precision = self.contract_info.get("quantity_precision", 3)

        if self.config.DRY_RUN:
            self.notifier.info(
                f"🟢 [DRY-RUN] Señal de ENTRADA en {self.config.SYMBOL} a ~{signal.close:.4f} "
                f"(RSI={signal.rsi:.2f}, cruce Doble Dip). No se envía orden real."
            )
            self.state["dry_run_in_position"] = True
            return

        try:
            balance = self.client.get_available_balance()
        except Exception as exc:
            logger.error("No se pudo leer balance: %s", exc)
            self.notifier.error(f"Señal de entrada detectada pero no se pudo leer el balance: {exc}")
            return

        margin_to_use = balance * (self.config.POSITION_SIZE_PCT / 100.0)
        notional = margin_to_use * self.config.LEVERAGE
        raw_qty = notional / signal.close
        qty = format_quantity(raw_qty, precision)

        if float(qty) <= 0:
            logger.warning("Cantidad calculada es 0. Balance=%.2f margen=%.2f", balance, margin_to_use)
            self.notifier.error(
                f"Señal de entrada detectada pero la cantidad calculada dio 0. "
                f"Balance disponible: {balance:.2f} USDT."
            )
            return

        logger.info("Abriendo LONG: balance=%.2f margen=%.2f notional=%.2f qty=%s", balance, margin_to_use, notional, qty)
        try:
            order = self.client.open_long_market(self.config.SYMBOL, qty)
        except BingXAPIError as exc:
            logger.error("Error al abrir LONG: %s", exc)
            self.notifier.error(f"Fallo al abrir LONG en {self.config.SYMBOL}: {exc}")
            return

        fill_price = self._extract_fill_price(order, fallback=signal.close)

        if self.config.STOP_LOSS_PCT > 0:
            price_precision = self.contract_info.get("price_precision", 2)
            stop_price = fill_price * (1 - self.config.STOP_LOSS_PCT / 100.0)
            stop_price_str = f"{stop_price:.{price_precision}f}"
            try:
                sl_order = self.client.place_stop_loss(self.config.SYMBOL, stop_price_str, qty)
                self.state["stop_order_id"] = (
                    sl_order.get("data", {}).get("order", {}).get("orderId")
                )
            except Exception as exc:
                logger.error("No se pudo colocar el stop-loss de seguridad: %s", exc)
                self.notifier.error(
                    f"Posición LONG abierta en {self.config.SYMBOL} pero el stop-loss de "
                    f"seguridad NO se pudo colocar: {exc}. Revisa la posición manualmente."
                )

        self.notifier.entry(self.config.SYMBOL, float(qty), fill_price, self.config.LEVERAGE, signal.rsi)

    def _close_long(self, signal: SignalResult, position: dict | None, reason: str):
        if self.config.DRY_RUN:
            self.notifier.info(
                f"🔴 [DRY-RUN] Señal de SALIDA en {self.config.SYMBOL} a ~{signal.close:.4f} ({reason})."
            )
            self.state["dry_run_in_position"] = False
            return

        precision = self.contract_info.get("quantity_precision", 3)
        qty = format_quantity(position["amount"], precision)

        # Cancela cualquier orden abierta (el stop-loss) antes de cerrar por
        # mercado. Se cancelan TODAS las abiertas del símbolo, no solo el
        # orderId recordado en memoria, para que un reinicio del bot no deje
        # un stop-loss huérfano tras el cierre.
        try:
            self.client.cancel_all_open_orders(self.config.SYMBOL)
        except Exception as exc:
            logger.warning("No se pudieron cancelar las órdenes abiertas antes de cerrar: %s", exc)
        self.state["stop_order_id"] = None

        try:
            order = self.client.close_long_market(self.config.SYMBOL, qty)
            fill_price = self._extract_fill_price(order, fallback=signal.close)
        except PositionNotExistError:
            logger.info("La posición ya no existía en BingX (probablemente cerrada por el stop-loss).")
            fill_price = signal.close
        except BingXAPIError as exc:
            logger.error("Error al cerrar LONG: %s", exc)
            self.notifier.error(f"Fallo al cerrar LONG en {self.config.SYMBOL}: {exc}")
            return

        self.notifier.exit(self.config.SYMBOL, position["amount"], fill_price, reason, position.get("unrealized_pnl"))
