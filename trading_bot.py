"""
trading_bot.py
===============
Motor de escaneo multi-símbolo: en cada cierre de vela descarga klines de
TODOS los símbolos configurados (o de todos los perpetuos USDT-M de BingX
si SYMBOLS=ALL), evalúa la estrategia RSI+SuperTrend "Doble Dip" EN
PARALELO (solo lectura, vía ThreadPoolExecutor) y ejecuta entradas/salidas
de forma SECUENCIAL, notificando todo por Telegram.

Por qué secuencial para las órdenes: si dos símbolos dan señal en el mismo
ciclo y se piden en paralelo, ambas peticiones pueden leer el MISMO balance
disponible antes de que la primera orden lo haya consumido, comprometiendo
más margen del que en realidad hay (justo el bug ya visto antes en un bot
similar: "rate limiting / lecturas paralelas de balance"). Procesando las
entradas una por una, cada dimensionamiento ya ve el balance actualizado
tras la operación anterior.

Controles de riesgo específicos de escanear muchos símbolos a la vez
(no vienen en el Pine Script original - se necesitan para no comprometer
más del 100% del equity si varios símbolos dan señal el mismo ciclo):
  - MAX_CONCURRENT_POSITIONS: techo duro de posiciones abiertas a la vez.
  - SYMBOL_COOLDOWN_MINUTES: tras cerrar un símbolo, no se reabre en X min.
  - Circuit breaker global: tras N pérdidas SEGUIDAS (en cualquier
    símbolo) se pausan nuevas entradas un rato; las salidas de posiciones
    ya abiertas NUNCA se pausan por esto.

Fiabilidad 24/7 (igual que en la versión single-symbol):
  - Antes de decidir, se relee el estado REAL de posiciones en BingX (una
    sola llamada para TODOS los símbolos) en vez de fiarse de la memoria.
  - Si una posición desaparece entre ciclos sin que el bot la cerrara
    (probable stop-loss), se detecta y avisa por Telegram, símbolo a
    símbolo, y cuenta como pérdida para el circuit breaker.
  - Al cerrar por señal se cancelan TODAS las órdenes abiertas de ESE
    símbolo, no solo el ID de stop-loss recordado en memoria.
  - DRY_RUN=true (por defecto): calcula y notifica señales sin mandar
    ninguna orden real, manteniendo un set de posiciones "virtuales" en
    memoria para poder probar cooldown / circuit breaker / máximo de
    posiciones sin arriesgar nada.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

        self.symbols: list[str] = []
        self.contract_info: dict[str, dict] = {}
        self._symbols_refreshed_at = 0.0
        self._first_refresh_done = False
        self.leverage_set_symbols: set[str] = set()

        self.stop_orders: dict[str, object] = {}
        self.symbol_last_close: dict[str, float] = {}
        self.dry_run_open: set[str] = set()
        self.last_known_open: set[str] = set()

        self.consecutive_losses = 0
        self.circuit_breaker_until: float | None = None

        self.last_status = {"started_at": datetime.now(timezone.utc).isoformat()}

    # ------------------------------------------------------------------
    # Descubrimiento / refresco de símbolos
    # ------------------------------------------------------------------
    def _default_contract_info(self, symbol: str) -> dict:
        return self.contract_info.get(symbol, {"quantity_precision": 3, "price_precision": 2})

    def _resolve_symbols(self) -> list[str]:
        if not self.config.SCAN_ALL_SYMBOLS:
            return [s.strip() for s in self.config.SYMBOLS.split(",") if s.strip()]

        contracts = self.client.get_all_contracts()
        suffix = f"-{self.config.QUOTE_ASSET_FILTER}"
        excluded = set(self.config.EXCLUDED_SYMBOLS)
        symbols = []
        for c in contracts:
            sym = c["symbol"]
            if not sym.endswith(suffix) or sym in excluded:
                continue
            symbols.append(sym)
            self.contract_info[sym] = {
                "quantity_precision": c["quantity_precision"],
                "price_precision": c["price_precision"],
            }
        return symbols

    def _maybe_refresh_symbols(self, force: bool = False):
        now = time.time()
        if not force and self.symbols and (now - self._symbols_refreshed_at) < self.config.SYMBOL_REFRESH_HOURS * 3600:
            return
        try:
            symbols = self._resolve_symbols()
        except Exception as exc:
            logger.error("No se pudo obtener/actualizar la lista de símbolos: %s", exc)
            if not self.symbols:
                raise
            return

        if not symbols:
            logger.error("La lista de símbolos resultante está vacía, se mantiene la anterior.")
            return

        added = set(symbols) - set(self.symbols)
        removed = set(self.symbols) - set(symbols)
        self.symbols = symbols
        self._symbols_refreshed_at = now
        logger.info("Símbolos a escanear: %d (nuevos: %d, retirados: %d)", len(symbols), len(added), len(removed))
        if self._first_refresh_done and (added or removed):
            self.notifier.info(
                f"ℹ️ Universo de símbolos actualizado: {len(symbols)} en total "
                f"(+{len(added)} nuevos, -{len(removed)} retirados)."
            )
        self._first_refresh_done = True

    # ------------------------------------------------------------------
    # Arranque
    # ------------------------------------------------------------------
    def _initialize(self):
        if not self.config.DRY_RUN:
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

        self._maybe_refresh_symbols(force=True)
        logger.info("Universo inicial: %d símbolos.", len(self.symbols))

        if not self.config.DRY_RUN:
            try:
                positions = self.client.get_all_positions()
                self.last_known_open = set(positions.keys())
                if positions:
                    logger.info("Posiciones LONG ya abiertas al arrancar: %s", list(positions.keys()))
                    self.notifier.info(
                        f"ℹ️ Al arrancar ya hay {len(positions)} posición(es) LONG abierta(s): "
                        f"{', '.join(positions.keys())}. El bot las gestionará."
                    )
                    self._reconcile_stop_orders(positions.keys())
            except Exception as exc:
                logger.warning("No se pudo consultar posiciones existentes al arrancar: %s", exc)

    def _reconcile_stop_orders(self, symbols):
        for symbol in symbols:
            try:
                open_orders = self.client.get_open_orders(symbol)
                stops = [o for o in open_orders if o.get("type") == "STOP_MARKET"]
                if stops:
                    self.stop_orders[symbol] = stops[0].get("orderId")
            except Exception as exc:
                logger.warning("[%s] no se pudieron leer las órdenes abiertas al arrancar: %s", symbol, exc)

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
    # Escaneo concurrente (SOLO LECTURA)
    # ------------------------------------------------------------------
    def _drop_unclosed_candle(self, df: pd.DataFrame) -> pd.DataFrame:
        now = pd.Timestamp.now(tz="UTC")
        tf = pd.Timedelta(minutes=self.config.TIMEFRAME_MINUTES)
        if len(df) and (df.index[-1] + tf) > now:
            df = df.iloc[:-1]
        return df

    def _min_candles_needed(self) -> int:
        base = max(self.config.RSI_LENGTH, self.config.ATR_PERIOD) + self.config.SIG_LENGTH + 5
        if self.config.TREND_FILTER_ENABLED:
            trend_min = self.config.TREND_EMA_LENGTH + self.config.TREND_MAX_BARS_AFTER_BREAK + 20
            base = max(base, trend_min)
        return base

    def _fetch_and_evaluate(self, symbol: str) -> SignalResult | None:
        df = self.client.get_klines(symbol, self.config.TIMEFRAME, self.config.HISTORY_CANDLES)
        df = self._drop_unclosed_candle(df)
        if len(df) < self._min_candles_needed():
            return None
        return evaluate(df, self.config.strategy_params())

    def _scan_all_symbols(self) -> dict:
        results = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.SCAN_CONCURRENCY)) as executor:
            future_map = {executor.submit(self._fetch_and_evaluate, s): s for s in self.symbols}
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    results[symbol] = future.result()
                except Exception as exc:
                    logger.warning("[%s] error al escanear: %s", symbol, exc)
                    results[symbol] = None
        return results

    # ------------------------------------------------------------------
    # Riesgo: cooldown por símbolo y circuit breaker global
    # ------------------------------------------------------------------
    def _in_cooldown(self, symbol: str) -> bool:
        last_close = self.symbol_last_close.get(symbol)
        if not last_close:
            return False
        return (time.time() - last_close) < self.config.SYMBOL_COOLDOWN_MINUTES * 60

    def _circuit_breaker_active(self) -> bool:
        return self.circuit_breaker_until is not None and time.time() < self.circuit_breaker_until

    def _record_trade_result(self, won: bool):
        if won:
            self.consecutive_losses = 0
            return
        self.consecutive_losses += 1
        if self.consecutive_losses >= self.config.MAX_CONSECUTIVE_LOSSES and not self._circuit_breaker_active():
            self.circuit_breaker_until = time.time() + self.config.CIRCUIT_BREAKER_COOLDOWN_MINUTES * 60
            self.notifier.error(
                f"🛑 Circuit breaker: {self.consecutive_losses} pérdidas seguidas. Nuevas entradas "
                f"pausadas {self.config.CIRCUIT_BREAKER_COOLDOWN_MINUTES:.0f} min "
                "(las salidas de posiciones ya abiertas siguen funcionando con normalidad)."
            )

    # ------------------------------------------------------------------
    # Ciclo principal
    # ------------------------------------------------------------------
    def _get_positions_snapshot(self) -> dict:
        if self.config.DRY_RUN:
            return {s: {"amount": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0} for s in self.dry_run_open}
        return self.client.get_all_positions()

    def _run_cycle(self):
        self._maybe_refresh_symbols()
        if not self.symbols:
            logger.warning("Sin símbolos para escanear este ciclo.")
            return

        positions_before = self._get_positions_snapshot()

        vanished = self.last_known_open - set(positions_before.keys())
        for symbol in vanished:
            self._handle_external_close(symbol)

        signals = self._scan_all_symbols()

        closed_this_cycle = set()
        for symbol, position in positions_before.items():
            signal = signals.get(symbol)
            if not (signal and signal.st_sell):
                continue
            try:
                if self._close_long(symbol, signal, position, reason="Cambio de dirección de SuperTrend (bajista)"):
                    closed_this_cycle.add(symbol)
            except Exception as exc:
                logger.exception("[%s] error inesperado al cerrar", symbol)
                self.notifier.error(f"[{symbol}] error inesperado al cerrar: {exc}")

        opened_this_cycle = set()
        if self._circuit_breaker_active():
            remaining_min = (self.circuit_breaker_until - time.time()) / 60.0
            logger.info("Circuit breaker activo (%.0f min restantes): se omiten nuevas entradas.", remaining_min)
        else:
            open_count = len(positions_before) - len(closed_this_cycle)
            available_slots = self.config.MAX_CONCURRENT_POSITIONS - open_count
            for symbol, signal in signals.items():
                if available_slots <= 0:
                    break
                if symbol in positions_before and symbol not in closed_this_cycle:
                    continue
                if self._in_cooldown(symbol):
                    continue
                if not (signal and signal.special_buy):
                    continue
                try:
                    if self._open_long(symbol, signal):
                        opened_this_cycle.add(symbol)
                        available_slots -= 1
                except Exception as exc:
                    logger.exception("[%s] error inesperado al abrir", symbol)
                    self.notifier.error(f"[{symbol}] error inesperado al abrir: {exc}")

        self.last_known_open = (set(positions_before.keys()) - closed_this_cycle) | opened_this_cycle

        n_special = sum(1 for s in signals.values() if s and s.special_buy)
        n_sell = sum(1 for s in signals.values() if s and s.st_sell)
        self.last_status.update(
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "symbols_scanned": len(signals),
                "open_positions": sorted(self.last_known_open),
                "num_open": len(self.last_known_open),
                "special_buy_signals": n_special,
                "st_sell_signals": n_sell,
                "consecutive_losses": self.consecutive_losses,
                "circuit_breaker_active": self._circuit_breaker_active(),
            }
        )
        logger.info(
            "Ciclo OK | %d símbolos escaneados | %d abiertas | %d specialBuy | %d stSell",
            len(signals), len(self.last_known_open), n_special, n_sell,
        )

    def _handle_external_close(self, symbol: str):
        logger.info("[%s] la posición se cerró entre ciclos sin intervención del bot (probable stop-loss).", symbol)
        self.stop_orders.pop(symbol, None)
        self.symbol_last_close[symbol] = time.time()
        self._record_trade_result(won=False)  # si fue el stop-loss, es una pérdida por definición
        self.notifier.info(
            f"ℹ️ La posición LONG en {symbol} ya no está abierta: se cerró entre ciclos "
            "(probablemente por el stop-loss de seguridad, o manualmente)."
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

    def _ensure_leverage(self, symbol: str):
        if symbol in self.leverage_set_symbols:
            return
        try:
            self.client.set_leverage(symbol, self.config.LEVERAGE, side="LONG")
        except Exception as exc:
            logger.warning("[%s] no se pudo fijar apalancamiento: %s", symbol, exc)
        self.leverage_set_symbols.add(symbol)

    def _open_long(self, symbol: str, signal: SignalResult) -> bool:
        if self.config.DRY_RUN:
            self.notifier.info(
                f"🟢 [DRY-RUN] Señal de ENTRADA en {symbol} a ~{signal.close:.4f} "
                f"(RSI={signal.rsi:.2f}, Doble Dip). No se envía orden real."
            )
            self.dry_run_open.add(symbol)
            return True

        try:
            balance = self.client.get_available_balance()
        except Exception as exc:
            logger.error("[%s] no se pudo leer balance: %s", symbol, exc)
            self.notifier.error(f"[{symbol}] señal de entrada pero no se pudo leer el balance: {exc}")
            return False

        info = self._default_contract_info(symbol)
        margin_to_use = balance * (self.config.POSITION_SIZE_PCT / 100.0)
        notional = margin_to_use * self.config.LEVERAGE
        qty = format_quantity(notional / signal.close, info["quantity_precision"])

        if float(qty) <= 0:
            logger.warning("[%s] cantidad calculada es 0 (balance=%.2f, margen=%.2f)", symbol, balance, margin_to_use)
            return False

        self._ensure_leverage(symbol)

        logger.info(
            "[%s] abriendo LONG: balance=%.2f margen=%.2f notional=%.2f qty=%s",
            symbol, balance, margin_to_use, notional, qty,
        )
        try:
            order = self.client.open_long_market(symbol, qty)
        except BingXAPIError as exc:
            logger.error("[%s] error al abrir LONG: %s", symbol, exc)
            self.notifier.error(f"[{symbol}] fallo al abrir LONG: {exc}")
            return False

        fill_price = self._extract_fill_price(order, fallback=signal.close)

        if self.config.STOP_LOSS_PCT > 0:
            stop_price = fill_price * (1 - self.config.STOP_LOSS_PCT / 100.0)
            stop_price_str = f"{stop_price:.{info['price_precision']}f}"
            try:
                sl_order = self.client.place_stop_loss(symbol, stop_price_str, qty)
                self.stop_orders[symbol] = sl_order.get("data", {}).get("order", {}).get("orderId")
            except Exception as exc:
                logger.error("[%s] no se pudo colocar el stop-loss: %s", symbol, exc)
                self.notifier.error(
                    f"[{symbol}] posición abierta pero el stop-loss NO se pudo colocar: {exc}. Revisa manualmente."
                )

        self.notifier.entry(symbol, float(qty), fill_price, self.config.LEVERAGE, signal.rsi)
        return True

    def _close_long(self, symbol: str, signal: SignalResult, position: dict, reason: str) -> bool:
        if self.config.DRY_RUN:
            self.notifier.info(f"🔴 [DRY-RUN] Señal de SALIDA en {symbol} a ~{signal.close:.4f} ({reason}).")
            self.dry_run_open.discard(symbol)
            self.symbol_last_close[symbol] = time.time()  # el cooldown también se prueba en DRY_RUN
            return True

        info = self._default_contract_info(symbol)
        qty = format_quantity(position["amount"], info["quantity_precision"])

        try:
            self.client.cancel_all_open_orders(symbol)
        except Exception as exc:
            logger.warning("[%s] no se pudieron cancelar órdenes abiertas antes de cerrar: %s", symbol, exc)
        self.stop_orders.pop(symbol, None)

        try:
            order = self.client.close_long_market(symbol, qty)
            fill_price = self._extract_fill_price(order, fallback=signal.close)
        except PositionNotExistError:
            logger.info("[%s] la posición ya no existía (probablemente cerrada por el stop-loss).", symbol)
            fill_price = signal.close
        except BingXAPIError as exc:
            logger.error("[%s] error al cerrar LONG: %s", symbol, exc)
            self.notifier.error(f"[{symbol}] fallo al cerrar LONG: {exc}")
            return False

        pnl = position.get("unrealized_pnl")
        won = (pnl > 0) if pnl is not None else (fill_price > position["entry_price"])
        self.notifier.exit(symbol, position["amount"], fill_price, reason, pnl)
        self.symbol_last_close[symbol] = time.time()
        self._record_trade_result(won=won)
        return True
