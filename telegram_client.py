"""
QF×JP Bot v6.4 — Telegram Client
"""
import asyncio
import logging
import aiohttp
import config as C

log = logging.getLogger("telegram")
BASE_URL = f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}"


async def _send(text: str) -> bool:
    if not C.TELEGRAM_TOKEN or not C.TELEGRAM_CHAT_ID:
        return False
    payload = {
        "chat_id": C.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{BASE_URL}/sendMessage", json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    data = await r.json()
                    if data.get("ok"):
                        return True
                    log.warning("Telegram error: %s", data)
                    return False
        except Exception as e:
            if attempt == 2:
                log.error("Telegram fallo: %s", e)
                return False
            await asyncio.sleep(2)
    return False


def _tier_e(t): return {"STD":"⚪","FUEL":"🔥","SUP":"💎"}.get(t,"⚪")
def _dir_e(d):  return "🟢" if d == "LONG" else "🔴"
def _bar(s):    return "█" * int(s/10) + "░" * (10 - int(s/10))


async def notify_signal(sig) -> bool:
    vdi_s = "🟢 BULL" if sig.vdi > 0 else "🔴 BEAR"
    msg = (
        f"📡 <b>SEÑAL — QF×JP v6.4</b>\n"
        f"{'━'*22}\n"
        f"<b>Par:</b>       {sig.symbol}\n"
        f"<b>Dir:</b>       {_dir_e(sig.direction)} {sig.direction}\n"
        f"<b>Tier:</b>      {_tier_e(sig.tier)} {sig.tier}\n"
        f"<b>Score:</b>     {sig.score}/100  {_bar(sig.score)}\n"
        f"{'━'*22}\n"
        f"<b>Entry:</b>     {sig.entry:.6f}\n"
        f"<b>SL:</b>        {sig.sl:.6f}\n"
        f"<b>TP1 (50%):</b> {sig.tp1:.6f}\n"
        f"<b>TP2 (50%):</b> {sig.tp2:.6f}\n"
        f"<b>ATR:</b>       {sig.atr:.6f}\n"
        f"{'━'*22}\n"
        f"<b>TL Ruptura:</b>  {sig.tl_break} {'🔥' if sig.tl_break_active else ''}\n"
        f"<b>Estructura:</b>  {sig.structure}\n"
        f"<b>ADX:</b>        {sig.adx:.1f}\n"
        f"<b>MFI:</b>        {sig.mfi:.1f}\n"
        f"<b>VDI:</b>        {vdi_s} ({sig.vdi:+.2f}σ)\n"
        f"<b>HTF Score:</b>  {sig.htf_score*100:.0f}%\n"
        f"<b>CVD:</b>        {sig.cvd:+.3f}\n"
        f"<b>Momentum:</b>   {sig.momentum:+.3f}\n"
        f"{'━'*22}\n"
        f"<i>Mode: {C.MODE}</i>"
    )
    return await _send(msg)


async def notify_trade_opened(sig, qty: float, order_id: str) -> bool:
    msg = (
        f"✅ <b>TRADE ABIERTO — QF×JP v6.4</b>\n"
        f"{'━'*22}\n"
        f"<b>Par:</b>     {sig.symbol}\n"
        f"<b>Dir:</b>     {_dir_e(sig.direction)} {sig.direction}\n"
        f"<b>Tier:</b>    {_tier_e(sig.tier)} {sig.tier}  Score: {sig.score}/100\n"
        f"<b>Qty:</b>     {qty}\n"
        f"{'━'*22}\n"
        f"<b>Entry:</b>   {sig.entry:.6f}\n"
        f"<b>SL:</b>      {sig.sl:.6f}\n"
        f"<b>TP1:</b>     {sig.tp1:.6f}\n"
        f"<b>TP2:</b>     {sig.tp2:.6f}\n"
        f"{'━'*22}\n"
        f"<b>OrderID:</b> <code>{order_id}</code>"
    )
    return await _send(msg)


async def notify_trade_closed(symbol, direction, entry, close_price,
                               qty, reason, pnl_usdt) -> bool:
    pnl_e = "🟢" if pnl_usdt >= 0 else "🔴"
    sl_dist = abs(entry - close_price)
    if sl_dist > 0:
        rr = (close_price - entry) / sl_dist if direction == "LONG" \
             else (entry - close_price) / sl_dist
    else:
        rr = 0.0
    msg = (
        f"{pnl_e} <b>TRADE CERRADO — QF×JP v6.4</b>\n"
        f"{'━'*22}\n"
        f"<b>Par:</b>     {symbol}\n"
        f"<b>Dir:</b>     {_dir_e(direction)} {direction}\n"
        f"<b>Razón:</b>   {reason}\n"
        f"{'━'*22}\n"
        f"<b>Entry:</b>   {entry:.6f}\n"
        f"<b>Cierre:</b>  {close_price:.6f}\n"
        f"<b>Qty:</b>     {qty}\n"
        f"<b>PnL:</b>     {pnl_usdt:+.2f} USDT\n"
        f"<b>R:R:</b>     {rr:.2f}"
    )
    return await _send(msg)


async def notify_error(context: str, error: str) -> bool:
    msg = f"⚠️ <b>ERROR</b>\n<b>Contexto:</b> {context}\n<b>Error:</b> <code>{error[:300]}</code>"
    return await _send(msg)


async def notify_status(status: dict, balance: float, n_symbols: int) -> bool:
    msg = (
        f"📊 <b>STATUS — QF×JP v6.4</b>\n"
        f"{'━'*22}\n"
        f"<b>Mode:</b>        {C.MODE}\n"
        f"<b>Balance:</b>     {balance:.2f} USDT\n"
        f"<b>Símbolos:</b>    {n_symbols}\n"
        f"<b>Posiciones:</b>  {status['open_positions']}/{status['max_open_trades']}\n"
        f"<b>Trades hoy:</b>  {status['daily_trades']}/{status['max_daily_trades']}\n"
        f"<b>PnL hoy:</b>     {status['daily_pnl']:+.2f} USDT\n"
        f"<b>Límite loss:</b> {status['daily_loss_limit']:.2f} USDT"
    )
    return await _send(msg)


async def notify_circuit_breaker(symbol: str) -> bool:
    return await _send(f"⚡ <b>CIRCUIT BREAKER</b> — {symbol}\nVela gigante detectada.")
