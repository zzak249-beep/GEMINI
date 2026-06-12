"""
QF×JP Bot v6.7 — Telegram Client
NUEVO v6.7:
  + notify_partial_close — fix crash en _partial_close_tp2
  + WinRate tracker — acumula wins/losses en RAM, report horario
  + notify_winrate_report — mensaje horario con estadísticas en vivo
  + notify_status incluye WR% actualizado
"""
import asyncio
import logging
import time
from collections import deque

import aiohttp
import config as C

log = logging.getLogger("telegram")
BASE_URL = f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}"

# ── WinRate tracker en RAM ─────────────────────────────────────────────────────
# Ventana deslizante de las últimas N operaciones (máx 200)
_MAX_HISTORY = 200
_trade_results: deque = deque(maxlen=_MAX_HISTORY)   # cada entry: {"pnl": float, "ts": float, "symbol": str, "reason": str}
_hourly_snapshot_ts: float = 0.0


def record_trade_result(symbol: str, pnl: float, reason: str = ""):
    """Llamar desde remove_trade o notify_trade_closed para acumular estadísticas."""
    _trade_results.append({
        "pnl": pnl,
        "ts": time.time(),
        "symbol": symbol,
        "reason": reason,
    })


def _compute_stats(window_hours: float = 24.0) -> dict:
    """Estadísticas de la ventana de tiempo indicada."""
    now = time.time()
    cutoff = now - window_hours * 3600
    recent = [r for r in _trade_results if r["ts"] >= cutoff]

    if not recent:
        return {"count": 0, "wins": 0, "losses": 0, "wr": 0.0,
                "gross_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0, "best": 0.0, "worst": 0.0}

    wins   = [r["pnl"] for r in recent if r["pnl"] > 0]
    losses = [r["pnl"] for r in recent if r["pnl"] <= 0]
    gross  = sum(r["pnl"] for r in recent)
    gross_wins   = sum(wins)   if wins   else 0.0
    gross_losses = abs(sum(losses)) if losses else 0.0

    return {
        "count":         len(recent),
        "wins":          len(wins),
        "losses":        len(losses),
        "wr":            len(wins) / len(recent) * 100 if recent else 0.0,
        "gross_pnl":     round(gross, 2),
        "avg_win":       round(gross_wins  / len(wins),   2) if wins   else 0.0,
        "avg_loss":      round(gross_losses / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999.0,
        "best":          round(max(wins),    2) if wins   else 0.0,
        "worst":         round(min(losses),  2) if losses else 0.0,
    }


# ── Core send ─────────────────────────────────────────────────────────────────

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


# ── Emojis / helpers ──────────────────────────────────────────────────────────

def _tier_e(t): return {"STD": "⚪", "FUEL": "🔥", "SUP": "💎"}.get(t, "⚪")
def _dir_e(d):  return "🟢" if d == "LONG" else "🔴"
def _bar(s):    return "█" * int(s / 10) + "░" * (10 - int(s / 10))
def _wr_e(wr):
    if wr >= 65: return "🏆"
    if wr >= 55: return "✅"
    if wr >= 45: return "⚠️"
    return "🔴"


# ── Notificaciones de trade ───────────────────────────────────────────────────

async def notify_signal(sig) -> bool:
    vdi_s = "🟢 BULL" if sig.vdi > 0 else "🔴 BEAR"
    msg = (
        f"📡 <b>SEÑAL — QF×JP v6.7</b>\n"
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
        f"✅ <b>TRADE ABIERTO — QF×JP v6.7</b>\n"
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
    # Registrar resultado para WR tracker
    record_trade_result(symbol, pnl_usdt, reason)

    pnl_e = "🟢" if pnl_usdt >= 0 else "🔴"
    sl_dist = abs(entry - close_price)
    rr = 0.0
    if sl_dist > 0:
        rr = ((close_price - entry) / sl_dist if direction == "LONG"
              else (entry - close_price) / sl_dist)

    # WR en tiempo real (últimas 24h)
    stats = _compute_stats(24.0)
    wr_line = (f"\n<b>WR 24h:</b>   {_wr_e(stats['wr'])} {stats['wr']:.0f}%  "
               f"({stats['wins']}W/{stats['losses']}L  PF={stats['profit_factor']})"
               if stats["count"] > 0 else "")

    msg = (
        f"{pnl_e} <b>TRADE CERRADO — QF×JP v6.7</b>\n"
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
        f"{wr_line}"
    )
    return await _send(msg)


async def notify_partial_close(symbol: str, direction: str, close_price: float,
                                qty: float, pnl: float, reason: str) -> bool:
    """v6.7 FIX: función que faltaba — llamada desde _partial_close_tp2."""
    pnl_e = "🟢" if pnl >= 0 else "🔴"
    msg = (
        f"{pnl_e} <b>CIERRE PARCIAL — QF×JP v6.7</b>\n"
        f"{'━'*22}\n"
        f"<b>Par:</b>     {symbol}\n"
        f"<b>Dir:</b>     {_dir_e(direction)} {direction}\n"
        f"<b>Razón:</b>   {reason}\n"
        f"<b>Precio:</b>  {close_price:.6f}\n"
        f"<b>Qty:</b>     {qty}\n"
        f"<b>PnL:</b>     {pnl:+.2f} USDT"
    )
    return await _send(msg)


# ── Report horario de WinRate ─────────────────────────────────────────────────

async def notify_winrate_report() -> bool:
    """
    Reporte completo de WR. Llamar cada hora desde scan_loop.
    Incluye ventanas 1h, 4h y 24h para contexto.
    """
    global _hourly_snapshot_ts
    _hourly_snapshot_ts = time.time()

    s1h  = _compute_stats(1.0)
    s4h  = _compute_stats(4.0)
    s24h = _compute_stats(24.0)

    def _row(label, s):
        if s["count"] == 0:
            return f"<b>{label}:</b>  sin trades\n"
        return (
            f"<b>{label}:</b>  {_wr_e(s['wr'])} {s['wr']:.0f}%  "
            f"{s['wins']}W/{s['losses']}L  "
            f"PnL {s['gross_pnl']:+.2f}$  "
            f"PF {s['profit_factor']}\n"
        )

    msg = (
        f"📈 <b>WINRATE REPORT — QF×JP v6.7</b>\n"
        f"{'━'*22}\n"
        + _row("1h ", s1h)
        + _row("4h ", s4h)
        + _row("24h", s24h)
        + f"{'━'*22}\n"
    )

    # Detalles 24h si hay suficientes trades
    if s24h["count"] >= 3:
        msg += (
            f"<b>Avg Win:</b>   +{s24h['avg_win']:.2f} USDT\n"
            f"<b>Avg Loss:</b>  -{s24h['avg_loss']:.2f} USDT\n"
            f"<b>Best:</b>      +{s24h['best']:.2f} USDT\n"
            f"<b>Worst:</b>     -{abs(s24h['worst']):.2f} USDT\n"
        )

    return await _send(msg)


# ── Status ────────────────────────────────────────────────────────────────────

async def notify_status(status: dict, balance: float, n_symbols: int) -> bool:
    stats = _compute_stats(24.0)
    wr_line = (f"\n<b>WR 24h:</b>     {_wr_e(stats['wr'])} {stats['wr']:.0f}%  "
               f"({stats['wins']}W/{stats['losses']}L)"
               if stats["count"] > 0 else "")

    msg = (
        f"📊 <b>STATUS — QF×JP v6.7</b>\n"
        f"{'━'*22}\n"
        f"<b>Mode:</b>        {C.MODE}\n"
        f"<b>Balance:</b>     {balance:.2f} USDT\n"
        f"<b>Símbolos:</b>    {n_symbols}\n"
        f"<b>Posiciones:</b>  {status['open_positions']}/{status['max_open_trades']}\n"
        f"<b>Trades hoy:</b>  {status['daily_trades']}/{status['max_daily_trades']}\n"
        f"<b>PnL hoy:</b>     {status['daily_pnl']:+.2f} USDT\n"
        f"<b>Límite loss:</b> {status['daily_loss_limit']:.2f} USDT"
        f"{wr_line}"
    )
    return await _send(msg)


# ── Otros ─────────────────────────────────────────────────────────────────────

async def notify_error(context: str, error: str) -> bool:
    msg = (f"⚠️ <b>ERROR</b>\n"
           f"<b>Contexto:</b> {context}\n"
           f"<b>Error:</b> <code>{error[:300]}</code>")
    return await _send(msg)


async def notify_circuit_breaker(symbol: str) -> bool:
    return await _send(f"⚡ <b>CIRCUIT BREAKER</b> — {symbol}\nVela gigante detectada.")
