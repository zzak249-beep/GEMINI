import aiohttp
from datetime import datetime
import config

TELEGRAM_API = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"

async def send(session: aiohttp.ClientSession, text: str, parse_mode: str = "HTML"):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"[TG] {text}"); return
    try:
        async with session.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": config.TELEGRAM_CHAT_ID, "text": text,
            "parse_mode": parse_mode, "disable_web_page_preview": True
        }) as r: pass
    except Exception as e:
        print(f"[TG-ERROR] {e}")

async def bot_start(session):
    await send(session,
        "🤖 <b>ZigZag V33 Elite</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Bot iniciado\n"
        f"📊 TF: <code>{config.TIMEFRAME}</code>  ⚡ {config.LEVERAGE}x  💰 {config.RISK_PCT}%\n"
        f"🔴 SHORT: <code>+{config.SHORT_PIPS}p + RSI>{config.RSI_OB} + ADX<{config.ADX_MAX}</code>\n"
        f"🟢 LONG:  <code>-{config.LONG_PIPS}p + RSI<{config.RSI_OS} + ADX<{config.ADX_MAX}</code>\n"
        f"🔒 Breakeven: <code>{config.BREAKEVEN_ATR}×ATR</code>  "
        f"📈 Trail: <code>{config.TRAIL_ATR}×ATR</code>\n"
        f"⏱ Time-stop: <code>{config.TIME_STOP_MINUTES}min</code>  "
        f"📦 Max pos: <code>{config.MAX_POSITIONS}</code>\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    )

async def scanner_result(session, pairs: list, balance: float):
    txt = "\n".join(f"  • <code>{p}</code>" for p in pairs[:20])
    await send(session,
        f"🔭 <b>SCAN</b> — {len(pairs)} pares activos\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>\n{txt}"
    )

async def signal_channel_fade(session, symbol, side, green, red, close,
                               trigger, canal_w, vol_ratio, adx, rr, rsi=50.0):
    emoji = "🔴 SHORT" if side=="SELL" else "🟢 LONG"
    desc  = (f"Close {close:.5g} > {trigger:.5g}" if side=="SELL"
             else f"Close {close:.5g} < {trigger:.5g}")
    await send(session,
        f"{emoji} <b>SEÑAL</b> — <code>{symbol}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {desc}\n"
        f"🟩 Verde: <code>{green:.5g}</code>  🟥 Roja: <code>{red:.5g}</code>\n"
        f"📊 Vol: <code>{vol_ratio:.2f}x</code>  ADX: <code>{adx:.1f}</code>  "
        f"RSI: <code>{rsi:.1f}</code>  RR: <code>1:{rr:.2f}</code>"
    )

async def trade_entry(session, symbol, side, entry, sl, tp, qty,
                      balance, rr, atr, adx, vol_ratio, rsi=50.0):
    emoji = "🟢 LONG" if side=="BUY" else "🔴 SHORT"
    sl_pct = abs(entry-sl)/entry*100
    tp_pct = abs(tp-entry)/entry*100
    await send(session,
        f"{emoji} <b>ENTRADA</b> — <code>{symbol}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💲 Entrada: <code>{entry:.6g}</code>\n"
        f"🛑 SL: <code>{sl:.6g}</code> (-{sl_pct:.2f}%)\n"
        f"🎯 TP: <code>{tp:.6g}</code> (+{tp_pct:.2f}%)\n"
        f"📦 Qty: <code>{qty}</code>  ⚖️ RR: <code>1:{rr:.2f}</code>\n"
        f"📊 Vol: <code>{vol_ratio:.2f}x</code>  ADX: <code>{adx:.1f}</code>  "
        f"RSI: <code>{rsi:.1f}</code>\n"
        f"🔒 BE: <code>{config.BREAKEVEN_ATR}×ATR</code>  "
        f"📈 Trail: <code>{config.TRAIL_ATR}×ATR</code>\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>  "
        f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
    )

async def trade_exit(session, symbol, side, entry, exit_price, pnl, pnl_pct, reason):
    emoji = "✅" if pnl>=0 else "❌"
    dir_e = "🟢 LONG" if side=="BUY" else "🔴 SHORT"
    await send(session,
        f"{emoji} <b>CIERRE</b> — <code>{symbol}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_e}\n"
        f"💲 Entrada: <code>{entry:.6g}</code>  Salida: <code>{exit_price:.6g}</code>\n"
        f"💰 PnL: <code>{pnl:+.4f} USDT ({pnl_pct:+.2f}%)</code>\n"
        f"📋 <code>{reason}</code>  ⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
    )

async def daily_summary(session, trades, wins, pnl, balance):
    wr = (wins/trades*100) if trades else 0
    await send(session,
        f"📊 <b>RESUMEN DIARIO</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Trades: <code>{trades}</code>  ✅ Wins: <code>{wins} ({wr:.1f}%)</code>  "
        f"❌ Losses: <code>{trades-wins}</code>\n"
        f"💰 PnL: <code>{pnl:+.4f} USDT</code>  "
        f"💵 Balance: <code>{balance:.2f} USDT</code>"
    )

async def daily_loss_limit(session, pnl, limit, balance):
    await send(session,
        f"🚨 <b>LÍMITE PÉRDIDA</b>\n"
        f"📉 PnL: <code>{pnl:+.4f} USDT</code>  🛑 Límite: <code>-{limit:.1f}%</code>\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>  ⏸️ PAUSADO hasta mañana"
    )

async def error_alert(session, msg: str):
    await send(session,
        f"⚠️ <b>ERROR</b>\n<code>{msg[:400]}</code>\n"
        f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
    )
