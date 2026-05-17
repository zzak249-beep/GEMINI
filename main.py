"""
main.py — NEXUS Bot v1.0 — BingX Perpetual Futures
═══════════════════════════════════════════════════════════════
Flujo por ciclo (~60s):
  1. Obtener klines + funding rate de BingX
  2. Analizar con NexusStrategy (6 capas)
  3. Verificar riesgo (can_trade, sizing, barreras)
  4. Ejecutar órdenes en BingX vía CCXT
  5. Gestionar posiciones abiertas (barrera de tiempo)
  6. Notificar a Telegram
  7. Heartbeat horario

Railway health check: servidor HTTP en HEALTH_PORT (default 8080)
═══════════════════════════════════════════════════════════════
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone

from aiohttp import web

from config import Config
from bot.strategy import NexusStrategy, SignalResult
from bot.bingx_client import BingXClient
from bot.risk_manager import RiskManager, PositionState
from bot.telegram_notifier import TelegramNotifier
from bot.utils import setup_logging

setup_logging("INFO")
logger = logging.getLogger("nexus.main")


# ──────────────────────────────────────────────────────────────
# HEALTH CHECK (Railway lo requiere para no reiniciar el worker)
# ──────────────────────────────────────────────────────────────

async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_health_server(port: int) -> None:
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server escuchando en :{port}")


# ──────────────────────────────────────────────────────────────
# BOT PRINCIPAL
# ──────────────────────────────────────────────────────────────

class NexusBot:

    def __init__(self):
        self.cfg       = Config()
        self.client    = BingXClient(self.cfg.BINGX_API_KEY, self.cfg.BINGX_SECRET_KEY)
        self.strategy  = NexusStrategy(self.cfg)
        self.risk      = RiskManager(self.cfg)
        self.notifier  = TelegramNotifier(self.cfg.TELEGRAM_TOKEN, self.cfg.TELEGRAM_CHAT_ID)

        self._pos_state:     dict[str, PositionState] = {}
        self._last_signals:  dict[str, SignalResult]  = {}
        self._last_heartbeat = datetime.min.replace(tzinfo=timezone.utc)
        self._current_bar    = 0
        self._daily_pnl      = 0.0

    # ─────────────────────────────────────────────────────────
    # ARRANQUE
    # ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info(f"Iniciando NEXUS Bot — {self.cfg}")

        # Health check server (Railway)
        await start_health_server(self.cfg.HEALTH_PORT)

        # Conectar BingX
        await self.client.connect()

        # Configurar leverage por símbolo
        for symbol in self.cfg.SYMBOLS:
            await self.client.setup_symbol(symbol, self.cfg.LEVERAGE)

        await self.notifier.send_startup(self.cfg)
        await self._main_loop()

    # ─────────────────────────────────────────────────────────
    # BUCLE PRINCIPAL
    # ─────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        while True:
            try:
                self._current_bar += 1
                balance = await self.client.get_balance()

                for symbol in self.cfg.SYMBOLS:
                    await self._process_symbol(symbol, balance)

                await self._maybe_heartbeat(balance)
                await asyncio.sleep(self.cfg.LOOP_INTERVAL)

            except KeyboardInterrupt:
                logger.info("Apagado por usuario")
                await self.notifier.send_paused("Apagado manual")
                break
            except Exception as e:
                logger.exception(f"Error en bucle principal: {e}")
                await self.notifier.send_error(str(e))
                await asyncio.sleep(30)

    # ─────────────────────────────────────────────────────────
    # PROCESADO POR SÍMBOLO
    # ─────────────────────────────────────────────────────────

    async def _process_symbol(self, symbol: str, balance: float) -> None:
        try:
            # 1. Datos de mercado
            df = await self.client.get_klines(symbol, self.cfg.TIMEFRAME, limit=300)
            if df is None or len(df) < 150:
                logger.warning(f"{symbol}: datos insuficientes")
                return

            # 2. Funding rate
            funding_rate = await self.client.get_funding_rate(symbol)

            # 3. Análisis de 6 capas
            signal = self.strategy.analyze(df, symbol, funding_rate)
            self._last_signals[symbol] = signal

            # 4. Gestionar posición existente
            position = await self.client.get_position(symbol)
            has_pos  = position and abs(position.get("size", 0)) > 0

            if has_pos:
                await self._manage_open_position(symbol, position, signal, balance)
                return

            # 5. Limpiar estado si posición ya no existe
            if symbol in self._pos_state:
                del self._pos_state[symbol]

            # 6. Buscar nueva entrada
            if signal.vetoed:
                logger.debug(f"{symbol}: señal vetada — {signal.veto_reason}")
                return

            if not self.risk.can_trade(symbol):
                return

            if balance <= 0:
                logger.warning(f"{symbol}: balance cero")
                return

            if signal.long:
                await self._enter_long(symbol, signal, balance)
            elif signal.short:
                await self._enter_short(symbol, signal, balance)

        except Exception as e:
            logger.error(f"_process_symbol {symbol}: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────
    # GESTIÓN DE POSICIÓN ABIERTA
    # ─────────────────────────────────────────────────────────

    async def _manage_open_position(self, symbol: str, position: dict,
                                    signal: SignalResult, balance: float) -> None:
        state = self._pos_state.get(symbol)

        if state is None:
            # Posición abierta externamente — registrar
            if signal.atr14 > 0:
                tp, sl = self.risk.compute_barriers(
                    position["entry_price"], signal.atr14, position["side"]
                )
            else:
                tp, sl = 0.0, 0.0
            self._pos_state[symbol] = PositionState(
                symbol=symbol, side=position["side"],
                entry_price=position["entry_price"],
                quantity=abs(position["size"]),
                tp_price=tp, sl_price=sl,
                entry_bar=self._current_bar
            )
            return

        # Barrera de tiempo
        if self.risk.check_time_exit(state, self._current_bar):
            logger.info(f"{symbol}: barrera de tiempo activada")
            result = await self.client.close_position(symbol, position)
            if result:
                pnl      = await self.client.get_last_trade_pnl(symbol)
                pnl_pct  = (pnl / balance * 100) if balance > 0 else 0.0
                self._daily_pnl += pnl
                self.risk.register_close(symbol, pnl_pct)
                del self._pos_state[symbol]
                await self.notifier.send_exit(symbol, "TIME", pnl, pnl_pct, balance)

    # ─────────────────────────────────────────────────────────
    # ENTRADAS
    # ─────────────────────────────────────────────────────────

    async def _enter_long(self, symbol: str, signal: SignalResult,
                          balance: float) -> None:
        qty = self.risk.calculate_position_size(signal, balance)
        tp, sl = self.risk.compute_barriers(signal.entry_price, signal.atr14, "LONG")

        order = await self.client.open_long(symbol, qty, tp, sl)
        if order:
            self.risk.register_open(symbol)
            self._pos_state[symbol] = PositionState(
                symbol=symbol, side="LONG",
                entry_price=signal.entry_price, quantity=order["qty"],
                tp_price=tp, sl_price=sl,
                entry_bar=self._current_bar
            )
            await self.notifier.send_entry(symbol, "LONG", order, signal, balance)

    async def _enter_short(self, symbol: str, signal: SignalResult,
                           balance: float) -> None:
        qty = self.risk.calculate_position_size(signal, balance)
        tp, sl = self.risk.compute_barriers(signal.entry_price, signal.atr14, "SHORT")

        order = await self.client.open_short(symbol, qty, tp, sl)
        if order:
            self.risk.register_open(symbol)
            self._pos_state[symbol] = PositionState(
                symbol=symbol, side="SHORT",
                entry_price=signal.entry_price, quantity=order["qty"],
                tp_price=tp, sl_price=sl,
                entry_bar=self._current_bar
            )
            await self.notifier.send_entry(symbol, "SHORT", order, signal, balance)

    # ─────────────────────────────────────────────────────────
    # HEARTBEAT HORARIO
    # ─────────────────────────────────────────────────────────

    async def _maybe_heartbeat(self, balance: float) -> None:
        now  = datetime.now(timezone.utc)
        diff = (now - self._last_heartbeat).total_seconds()
        if diff >= 3600:
            self._last_heartbeat = now
            await self.notifier.send_heartbeat(
                balance        = balance,
                daily_pnl      = self._daily_pnl,
                open_pos       = self.risk.open_positions,
                daily_loss_pct = self.risk.daily_loss_pct,
                symbols_status = self._last_signals
            )


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = NexusBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("NEXUS Bot detenido")
        sys.exit(0)
