"""
telegram_notifier.py
"""
import logging
from datetime import datetime, timezone
import requests
import config as C
from strategy import Signal

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self):
        self.url = f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/sendMessage"

    def _send(self, text: str):
        if not C.TELEGRAM_TOKEN or not C.TELEGRAM_CHAT_ID:
            return
        try:
            requests.post(self.url, json={
                "chat_id": C.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            log.error(f"Telegram: {e}")

    def entry(self, symbol: str, sig: Signal, qty: float, balance: float):
        d    = "🟢" if sig.direction == "LONG" else "🔴"
        q    = "⭐" if sig.quality == "FUERTE" else "▶️"
        rr   = abs(sig.tp - sig.entry) / max(abs(sig.entry - sig.sl), 1e-9)
        cvd  = "↑ RISING" if sig.cvd_rising else "↓ FALLING"
        div  = ""
        if sig.bull_div: div = "\n💎 <b>CVD Bull Div — acumulación oculta</b>"
        if sig.bear_div: div = "\n💎 <b>CVD Bear Div — distribución oculta</b>"
        reasons = "\n".join(f"  {r}" for r in sig.reasons)
        ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")

        self._send(
            f"{q} {d} <b>{sig.direction} {sig.quality}</b> — {symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entrada:     <code>{sig.entry:.5f}</code>\n"
            f"🛑 Stop Loss:   <code>{sig.sl:.5f}</code>\n"
            f"🎯 Take Profit: <code>{sig.tp:.5f}</code>\n"
            f"📐 RR:          <b>{rr:.2f}:1</b>\n"
            f"🎲 Cantidad:    <code>{qty:.6f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Score:</b>      {sig.score:+.3f}\n"
            f"⏳ <b>Decay:</b>      {sig.decay_pct:.0f}%\n"
            f"🌊 <b>CVD:</b>        {cvd}{div}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Confluencias:</b>\n{reasons}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: <code>{balance:.2f} USDT</code>\n"
            f"🕐 {ts} UTC"
        )

    def close(self, symbol: str, direction: str, entry: float,
              close_p: float, pnl: float, reason: str):
        icon = "✅" if pnl >= 0 else "❌"
        d    = "🟢" if direction == "LONG" else "🔴"
        ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._send(
            f"{icon} <b>CIERRE {reason}</b> — {symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{d} {direction}\n"
            f"📍 Entrada: <code>{entry:.5f}</code>\n"
            f"🏁 Salida:  <code>{close_p:.5f}</code>\n"
            f"💵 PnL:     <b>{'+'if pnl>=0 else ''}{pnl:.2f} USDT</b>\n"
            f"🕐 {ts} UTC"
        )

    def warn(self, msg: str):
        self._send(f"⚠️ {msg}")

    def info(self, msg: str):
        self._send(f"ℹ️ {msg}")

    def startup(self, symbol: str, balance: float):
        self._send(
            f"🚀 <b>CVD Bot iniciado</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Par:      <b>{symbol}</b>\n"
            f"⏱️  TF:       3min → HTF 15min\n"
            f"🔑 Apal.:    {C.LEVERAGE}x ISOLATED\n"
            f"⚠️  Riesgo:   {C.RISK_PER_TRADE*100:.1f}% / operación\n"
            f"🛑 Límite:   {C.DAILY_LOSS_LIMIT*100:.0f}% diario\n"
            f"📐 RR:       {C.TP_RR}:1\n"
            f"💰 Balance:  <code>{balance:.2f} USDT</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Condiciones de entrada:</b>\n"
            f"  ✅ Score {'>'} {C.SCORE_THR} o {'<'} -{C.SCORE_THR}\n"
            f"  ✅ Decaimiento {'>'} {C.DECAY_THR*100:.0f}%\n"
            f"  ✅ CVD rising (LONG) / falling (SHORT)\n"
            f"  ❌ Sin divergencia contraria"
        )
