"""
bot/telegram_notifier.py
Notificaciones ricas en Telegram para NEXUS Bot.

Mensajes:
  🚀 Arranque     — config completa
  ★  Entrada      — todas las capas del scoring
  ✅❌ Salida     — PnL, motivo, balance
  💓 Heartbeat    — estado horario
  ⚠️  Error        — stack trace
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode

from bot.strategy import SignalResult

logger = logging.getLogger(__name__)


class TelegramNotifier:

    def __init__(self, token: str, chat_id: str):
        self._bot     = Bot(token=token)
        self._chat_id = chat_id

    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
        except TelegramError as e:
            logger.error(f"Telegram: {e}")

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ─────────────────────────────────────────────────────────

    async def send_startup(self, config) -> None:
        syms = ", ".join(config.SYMBOLS)
        msg = (
            f"🚀 <b>NEXUS BOT v1.0 — INICIADO</b>\n"
            f"{'─'*32}\n"
            f"💱 Pares: <code>{syms}</code>\n"
            f"⏱ Timeframe: <code>{config.TIMEFRAME}</code>\n"
            f"🔧 Leverage: <b>{config.LEVERAGE}x</b>\n"
            f"⚠️ Riesgo/trade: <b>{config.RISK_PER_TRADE}%</b>\n"
            f"🛑 Max DD diario: <b>{config.MAX_DAILY_LOSS_PCT}%</b>\n"
            f"🎯 Score mínimo: <b>{config.MIN_SCORE}/100</b>\n"
            f"{'─'*32}\n"
            f"🧠 <b>CAPAS ACTIVAS:</b>\n"
            f"  1. SAMA Adaptativa (slope {config.SLOPE_FLAT}°)\n"
            f"  2. Markov + ADX régimen\n"
            f"  3. CVD Sintético + Divergencia ★\n"
            f"  4. Liquidity Sweeps ★\n"
            f"  5. Funding Rate\n"
            f"  6. Kotegawa / STC\n"
            f"⏰ <i>{self._now()}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────────────────────

    async def send_entry(self, symbol: str, side: str,
                         order: dict, signal: SignalResult,
                         balance: float) -> None:
        emoji  = "📈" if side == "LONG" else "📉"
        regime_e = {"TENDENCIA": "🔥", "RANGO": "🌊", "TRANSICION": "⚡"}.get(signal.regime, "❓")

        # Construir desglose de capas
        layers = []
        layers.append(f"  SAMA: <code>{signal.sama_slope:.1f}°</code> {'✅' if signal.sama_bullish and side=='LONG' else ('✅' if signal.sama_bearish and side=='SHORT' else '—')}")
        layers.append(f"  Markov: bull <code>{signal.prob_bull:.1f}%</code> / bear <code>{signal.prob_bear:.1f}%</code>")
        layers.append(f"  CVD: {'↑ compradora' if signal.cvd_bull else '↓ vendedora'}")
        layers.append(f"  Sweep L:{signal.sweep_long} S:{signal.sweep_short} (fuerza {signal.sweep_str:.2f})")
        layers.append(f"  Funding: <code>{signal.funding_rate:.4%}</code>")
        layers.append(f"  Kotegawa: {'✅' if signal.kotegawa_bull else '—'} | STC: <code>{signal.stc_val:.1f}</code>")

        reasons_str = "\n".join(f"  • {r}" for r in signal.reasons)
        layers_str  = "\n".join(layers)

        msg = (
            f"{emoji} <b>NEXUS ENTRADA — {side}</b>\n"
            f"{'─'*32}\n"
            f"💱 Par: <b>{symbol}</b>\n"
            f"💵 Entrada: <code>{signal.entry_price:.4f}</code>\n"
            f"📦 Qty: <code>{order['qty']}</code>\n"
            f"🎯 TP: <code>{order['tp']:.4f}</code>\n"
            f"🛑 SL: <code>{order['sl']:.4f}</code>\n"
            f"{'─'*32}\n"
            f"🧠 <b>ANÁLISIS DE CAPAS</b>\n"
            f"{layers_str}\n"
            f"{'─'*32}\n"
            f"{regime_e} Régimen: <b>{signal.regime}</b> | ADX: <code>{signal.adx:.1f}</code>\n"
            f"💧 VWAP: <code>{signal.vwap:.4f}</code> | RVOL: <code>{signal.rvol:.2f}x</code>\n"
            f"🌊 RSI: <code>{signal.rsi_val:.1f}</code> | POC: <code>{signal.poc:.4f}</code>\n"
            f"{'─'*32}\n"
            f"<b>Razones ({signal.score:.0f}/100):</b>\n{reasons_str}\n"
            f"{'─'*32}\n"
            f"💼 Balance: <code>${balance:.2f} USDT</code>\n"
            f"⏰ <i>{self._now()}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────────────────────

    async def send_exit(self, symbol: str, reason: str,
                        pnl_usdt: float, pnl_pct: float,
                        balance: float) -> None:
        win   = pnl_usdt >= 0
        emoji = "✅" if win else "❌"
        sign  = "+" if win else ""
        reasons = {
            "TP":   "🎯 Take Profit",
            "SL":   "🛑 Stop Loss",
            "TIME": "⏱ Barrera tiempo"
        }
        msg = (
            f"{emoji} <b>NEXUS SALIDA — {symbol}</b>\n"
            f"{'─'*32}\n"
            f"📌 Motivo: <b>{reasons.get(reason, reason)}</b>\n"
            f"💰 PnL: <b>{sign}{pnl_usdt:.4f} USDT</b> "
            f"(<code>{sign}{pnl_pct:.2f}%</code>)\n"
            f"💼 Balance: <code>${balance:.2f} USDT</code>\n"
            f"⏰ <i>{self._now()}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────────────────────

    async def send_heartbeat(self, balance: float, daily_pnl: float,
                             open_pos: int, daily_loss_pct: float,
                             symbols_status: dict) -> None:
        lines = ""
        for sym, sig in symbols_status.items():
            if sig:
                lines += (
                    f"  <b>{sym}</b>: {sig.regime} | "
                    f"score_L={sig.score_long:.0f} S={sig.score_short:.0f} | "
                    f"sweep={'L' if sig.sweep_long else ''}"
                    f"{'S' if sig.sweep_short else ''} | "
                    f"cvd={'↑' if sig.cvd_bull else '↓'}\n"
                )
            else:
                lines += f"  {sym}: —\n"

        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        msg = (
            f"💓 <b>NEXUS HEARTBEAT</b>\n"
            f"{'─'*32}\n"
            f"💼 Balance: <code>${balance:.2f} USDT</code>\n"
            f"{pnl_emoji} PnL diario: <code>{daily_pnl:+.4f} USDT</code>\n"
            f"📊 Posiciones: <b>{open_pos}</b>\n"
            f"⚠️ DD diario: <code>{daily_loss_pct:.2f}%</code>\n"
            f"{'─'*32}\n"
            f"<b>Estado pares:</b>\n{lines}"
            f"⏰ <i>{self._now()}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────────────────────

    async def send_veto(self, symbol: str, reason: str,
                        score: float) -> None:
        msg = (
            f"🚫 <b>SEÑAL VETADA — {symbol}</b>\n"
            f"Score: <code>{score:.0f}</code>\n"
            f"Motivo: <i>{reason}</i>\n"
            f"⏰ <i>{self._now()}</i>"
        )
        await self._send(msg)

    async def send_error(self, error_msg: str) -> None:
        msg = (
            f"⚠️ <b>ERROR CRÍTICO</b>\n"
            f"{'─'*32}\n"
            f"<code>{error_msg[:500]}</code>\n"
            f"⏰ <i>{self._now()}</i>"
        )
        await self._send(msg)

    async def send_paused(self, reason: str) -> None:
        msg = f"⛔ <b>NEXUS PAUSADO</b>\nMotivo: {reason}\n⏰ <i>{self._now()}</i>"
        await self._send(msg)
