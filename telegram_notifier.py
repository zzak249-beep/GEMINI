import aiohttp
from datetime import datetime
import config

TELEGRAM_API = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"


async def send(session: aiohttp.ClientSession, text: str, parse_mode: str = "HTML"):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"[TG] {text}")
        return
    try:
        async with session.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }) as r:
            pass
    except Exception as e:
        print(f"[TG-ERROR] {e}")


async def bot_start(session):
    await send(session,
        "🤖 <b>ZigZag V32 — Apex Quantum Shield</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Bot iniciado\n"
        f"📊 Timeframe:    <code>{config.TIMEFRAME}</code>\n"
        f"⚡ Leverage:     <code>{config.LEVERAGE}x</code>\n"
        f"💰 Riesgo/trade: <code>{config.RISK_PCT}%</code>\n"
        f"🔴 SHORT:        <code>close > verde + {config.SHORT_PIPS}pips | EMA ext↑</code>\n"
        f"🟢 LONG:         <code>close < roja  - {config.LONG_PIPS}pips | EMA ext↓</code>\n"
        f"⏱ Time-stop:    <code>{config.TIME_STOP_MINUTES} min</code>\n"
        f"📦 Max pos:      <code>{config.MAX_POSITIONS}</code>\n"
        f"🏆 Pares:        <code>{config.TOP_PAIRS}</code>\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )


async def scanner_result(session, pairs: list, balance: float):
    txt = "\n".join(f"  • <code>{p}</code>" for p in pairs[:20])
    await send(session,
        f"🔭 <b>SCAN DIARIO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>\n"
        f"🏆 {len(pairs)} pares activos:\n{txt}"
    )


async def signal_channel_fade(session, symbol: str, side: str,
                               green: float, red: float, close: float,
                               trigger: float, canal_w: float, vol_ratio: float,
                               adx: float, rr: float):
    emoji = "🔴 SHORT" if side == "SELL" else "🟢 LONG"
    desc  = (f"Close {close:.4f} > Verde+pips {trigger:.4f}"
             if side == "SELL"
             else f"Close {close:.4f} < Roja-pips {trigger:.4f}")
    await send(session,
        f"{emoji} <b>SEÑAL</b> — <code>{symbol}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {desc}\n"
        f"🟩 Verde: <code>{green:.4f}</code>\n"
        f"🟥 Roja:  <code>{red:.4f}</code>\n"
        f"📏 Canal: <code>{canal_w:.4f}</code>\n"
        f"📊 Vol:   <code>{vol_ratio:.2f}x</code>\n"
        f"📈 ADX:   <code>{adx:.1f}</code>\n"
        f"⚖️ RR:    <code>1:{rr:.2f}</code>"
    )


async def trade_entry(session, symbol: str, side: str, entry: float,
                      sl: float, tp: float, qty: float, balance: float,
                      rr: float, atr: float, adx: float, vol_ratio: float):
    emoji  = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
    sl_pct = abs(entry - sl) / entry * 100
    tp_pct = abs(tp - entry) / entry * 100
    await send(session,
        f"{emoji} <b>ENTRADA</b> — <code>{symbol}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💲 Entrada:  <code>{entry:.6g}</code>\n"
        f"🛑 SL:       <code>{sl:.6g}</code>  (-{sl_pct:.2f}%)\n"
        f"🎯 TP:       <code>{tp:.6g}</code>  (+{tp_pct:.2f}%)\n"
        f"📦 Qty:      <code>{qty:.4f}</code>\n"
        f"⚖️ RR:       <code>1:{rr:.2f}</code>\n"
        f"🌊 ATR:      <code>{atr:.4f}</code>\n"
        f"📈 ADX:      <code>{adx:.1f}</code>\n"
        f"📊 Vol:      <code>{vol_ratio:.2f}x</code>\n"
        f"⏱ T-Stop:   <code>{config.TIME_STOP_MINUTES} min</code>\n"
        f"💵 Balance:  <code>{balance:.2f} USDT</code>\n"
        f"⏰ {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )


async def trade_exit(session, symbol: str, side: str, entry: float,
                     exit_price: float, pnl: float, pnl_pct: float, reason: str):
    emoji = "✅" if pnl >= 0 else "❌"
    dir_e = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
    await send(session,
        f"{emoji} <b>CIERRE</b> — <code>{symbol}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_e}\n"
        f"💲 Entrada: <code>{entry:.6g}</code>\n"
        f"💲 Salida:  <code>{exit_price:.6g}</code>\n"
        f"💰 PnL:     <code>{pnl:+.4f} USDT ({pnl_pct:+.2f}%)</code>\n"
        f"📋 Motivo:  <code>{reason}</code>\n"
        f"⏰ {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )


async def daily_summary(session, trades: int, wins: int, pnl: float, balance: float):
    wr = (wins / trades * 100) if trades else 0
    await send(session,
        f"📊 <b>RESUMEN DIARIO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Trades:   <code>{trades}</code>\n"
        f"✅ Wins:     <code>{wins}  ({wr:.1f}%)</code>\n"
        f"❌ Losses:   <code>{trades - wins}</code>\n"
        f"💰 PnL neto: <code>{pnl:+.4f} USDT</code>\n"
        f"💵 Balance:  <code>{balance:.2f} USDT</code>\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d')} UTC"
    )


async def daily_loss_limit(session, pnl: float, limit: float, balance: float):
    await send(session,
        f"🚨 <b>LÍMITE PÉRDIDA DIARIA</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 PnL hoy:  <code>{pnl:+.4f} USDT</code>\n"
        f"🛑 Límite:   <code>-{limit:.1f}%</code>\n"
        f"💵 Balance:  <code>{balance:.2f} USDT</code>\n"
        "⏸️ Trading PAUSADO hasta mañana"
    )


async def error_alert(session, msg: str):
    await send(session,
        f"⚠️ <b>ERROR</b>\n<code>{msg[:400]}</code>\n"
        f"⏰ {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )
