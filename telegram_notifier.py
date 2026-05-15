import aiohttp
from datetime import datetime
import config

TELEGRAM_API=f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"

async def send(session:aiohttp.ClientSession,text:str,parse_mode:str="HTML"):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"[TG] {text}"); return
    try:
        async with session.post(f"{TELEGRAM_API}/sendMessage",json={
            "chat_id":config.TELEGRAM_CHAT_ID,"text":text,
            "parse_mode":parse_mode,"disable_web_page_preview":True}) as r: pass
    except Exception as e: print(f"[TG-ERROR] {e}")

async def bot_start(session):
    await send(session,
        "🤖 <b>ZigZag V33 Elite</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Iniciado | TF:<code>{config.TIMEFRAME}</code> "
        f"⚡<code>{config.LEVERAGE}x</code> 💰<code>{config.RISK_PCT}%</code>\n"
        f"🔴 SHORT: <code>+{config.SHORT_PIPS}p + RSI>{config.RSI_OB} + ADX<{config.ADX_MAX}</code>\n"
        f"🟢 LONG:  <code>-{config.LONG_PIPS}p + RSI<{config.RSI_OS} + ADX<{config.ADX_MAX}</code>\n"
        f"🔒 BE:<code>{config.BREAKEVEN_ATR}×ATR</code> 📈 Trail:<code>{config.TRAIL_ATR}×ATR</code> "
        f"⏱<code>{config.TIME_STOP_MINUTES}min</code>\n"
        f"⚠️ Si balance=0 → transfiere fondos a <b>Futuros Perpetuos</b> en BingX\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")

async def scanner_result(session,pairs:list,balance:float):
    txt="\n".join(f"  • <code>{p}</code>" for p in pairs[:20])
    msg=(f"🔭 <b>SCAN</b> — {len(pairs)} pares\n"
         f"💵 Balance: <code>{balance:.2f} USDT</code>")
    if balance==0.0:
        msg+="\n⚠️ <b>balance=0</b> — transfiere USDT de Spot a Futuros Perpetuos en BingX"
    await send(session,msg+f"\n{txt}")

async def signal_channel_fade(session,symbol,side,green,red,close,
                               trigger,canal_w,vol_ratio,adx,rr,rsi=50.0):
    emoji="🔴 SHORT" if side=="SELL" else "🟢 LONG"
    desc=f"Close {close:.5g} {'>' if side=='SELL' else '<'} {trigger:.5g}"
    await send(session,
        f"{emoji} <b>SEÑAL</b> — <code>{symbol}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {desc}\n"
        f"🟩 Verde:<code>{green:.5g}</code>  🟥 Roja:<code>{red:.5g}</code>\n"
        f"RSI:<code>{rsi:.1f}</code>  ADX:<code>{adx:.1f}</code>  "
        f"Vol:<code>{vol_ratio:.2f}x</code>  RR:<code>1:{rr:.2f}</code>")

async def trade_entry(session,symbol,side,entry,sl,tp,qty,balance,rr,atr,adx,vol_ratio,rsi=50.0):
    emoji="🟢 LONG" if side=="BUY" else "🔴 SHORT"
    sl_pct=abs(entry-sl)/entry*100; tp_pct=abs(tp-entry)/entry*100
    await send(session,
        f"{emoji} <b>ENTRADA</b> — <code>{symbol}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💲<code>{entry:.6g}</code>  🛑SL:<code>{sl:.6g}</code>(-{sl_pct:.2f}%)  "
        f"🎯TP:<code>{tp:.6g}</code>(+{tp_pct:.2f}%)\n"
        f"📦Qty:<code>{qty}</code>  RR:<code>1:{rr:.2f}</code>  "
        f"RSI:<code>{rsi:.1f}</code>  ADX:<code>{adx:.1f}</code>\n"
        f"💵Bal:<code>{balance:.2f} USDT</code>  ⏰{datetime.utcnow().strftime('%H:%M')} UTC")

async def trade_exit(session,symbol,side,entry,exit_price,pnl,pnl_pct,reason):
    emoji="✅" if pnl>=0 else "❌"
    dir_e="🟢 LONG" if side=="BUY" else "🔴 SHORT"
    await send(session,
        f"{emoji} <b>CIERRE</b> — <code>{symbol}</code>  {dir_e}\n"
        f"Entrada:<code>{entry:.6g}</code> → Salida:<code>{exit_price:.6g}</code>\n"
        f"💰 PnL:<code>{pnl:+.4f} USDT ({pnl_pct:+.2f}%)</code>  📋<code>{reason}</code>\n"
        f"⏰{datetime.utcnow().strftime('%H:%M')} UTC")

async def daily_summary(session,trades,wins,pnl,balance):
    wr=(wins/trades*100) if trades else 0
    await send(session,
        f"📊 <b>RESUMEN DIARIO</b>\n"
        f"Trades:<code>{trades}</code>  Wins:<code>{wins}({wr:.1f}%)</code>  "
        f"Losses:<code>{trades-wins}</code>\n"
        f"PnL:<code>{pnl:+.4f} USDT</code>  Balance:<code>{balance:.2f} USDT</code>")

async def daily_loss_limit(session,pnl,limit,balance):
    await send(session,
        f"🚨 <b>LÍMITE PÉRDIDA</b>  PnL:<code>{pnl:+.4f}</code>  "
        f"Límite:<code>-{limit:.1f}%</code>  Balance:<code>{balance:.2f}</code>  ⏸️ PAUSADO")

async def error_alert(session,msg:str):
    await send(session,f"⚠️ <b>ERROR</b>\n<code>{msg[:400]}</code>\n"
               f"⏰{datetime.utcnow().strftime('%H:%M')} UTC")
