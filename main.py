"""
QF×JP Bot v6.3 — Main
FastAPI: /health + /status + /close/{symbol}
Arranca: scanner_loop + position_monitor_loop
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

import config as C
from bingx_client import BingXClient
from risk_manager import RiskManager
from position_manager import PositionManager
from scanner import scan_loop
import telegram_client as tg

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-16s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("main")

# Singletons
client:   BingXClient   = None
risk:     RiskManager   = None
pos_mgr:  PositionManager = None


async def _run_scanner():
    try:
        await scan_loop(client, risk, pos_mgr)
    except Exception as e:
        log.critical("Scanner crash: %s", e, exc_info=True)
        await tg.notify_error("scanner_crash", str(e))


async def _run_position_monitor():
    if C.MODE == "LIVE":
        try:
            await pos_mgr.monitor_loop()
        except Exception as e:
            log.critical("PositionMonitor crash: %s", e, exc_info=True)
            await tg.notify_error("position_monitor_crash", str(e))
    else:
        log.info("PositionMonitor desactivado en modo SIGNAL")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, risk, pos_mgr
    log.info("═" * 50)
    log.info("  QF×JP Bot v6.3 — PREDATOR·ENTRY")
    log.info("  Modo: %s | Capital: %.2f USDT", C.MODE, C.CAPITAL)
    log.info("  Leverage: %dx | Min tier: %s", C.LEVERAGE, C.MIN_TIER)
    log.info("  Scan interval: %ds | TOP_N: %s",
             C.SCAN_INTERVAL,
             C.TOP_N_SYMBOLS if C.TOP_N_SYMBOLS > 0 else "TODAS")
    log.info("═" * 50)

    client  = BingXClient()
    risk    = RiskManager()
    pos_mgr = PositionManager(client, risk)

    if not C.BINGX_API_KEY or not C.BINGX_SECRET_KEY:
        log.error("BINGX_API_KEY / BINGX_SECRET_KEY no configurados")
    if not C.TELEGRAM_TOKEN or not C.TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado — sin notificaciones")

    # Balance inicial (best-effort)
    try:
        balance = await client.get_balance()
        log.info("Balance disponible: %.4f USDT", balance)
    except Exception as e:
        log.warning("No se pudo obtener balance inicial: %s", e)
        balance = 0.0

    await tg.notify_status(risk.status(), balance, 0)

    # Reconciliar posiciones abiertas en BingX (recuperación tras redeploy)
    if C.MODE == "LIVE":
        try:
            await pos_mgr.reconcile_on_startup()
        except Exception as e:
            log.warning("reconcile_on_startup falló: %s", e)

    # Lanzar loops en background
    scanner_task = asyncio.create_task(_run_scanner())
    monitor_task = asyncio.create_task(_run_position_monitor())
    log.info("Loops iniciados")

    yield   # ← servidor corriendo

    # Shutdown
    scanner_task.cancel()
    monitor_task.cancel()
    if client:
        await client.close()
    log.info("Bot detenido.")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="QF×JP Bot v6.3", docs_url=None, redoc_url=None, lifespan=lifespan)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "6.3", "mode": C.MODE}


@app.get("/status")
async def status():
    if risk is None:
        return JSONResponse({"error": "not_ready"}, status_code=503)
    try:
        balance = await client.get_balance()
    except Exception:
        balance = -1.0
    tracked = pos_mgr.get_tracked() if pos_mgr else {}
    return {
        "version": "6.3",
        "mode": C.MODE,
        "balance_usdt": round(balance, 2),
        "risk": risk.status(),
        "open_trades": {
            sym: {
                "direction": t.direction,
                "entry": t.entry,
                "sl": t.sl,
                "tp1": t.tp1,
                "tp2": t.tp2,
                "qty": t.qty,
                "be_moved": t.be_moved,
            }
            for sym, t in tracked.items()
        },
    }


@app.post("/close/{symbol}")
async def close_symbol(symbol: str):
    """Cierre manual forzado de una posición."""
    if C.MODE != "LIVE":
        raise HTTPException(status_code=400, detail="Solo disponible en modo LIVE")
    if pos_mgr is None:
        raise HTTPException(status_code=503, detail="not_ready")
    symbol = symbol.upper()
    if not pos_mgr.is_trading(symbol):
        raise HTTPException(status_code=404, detail=f"{symbol} no tiene posición abierta")
    await pos_mgr.close_position_emergency(symbol, reason="manual_close")
    return {"status": "ok", "symbol": symbol, "action": "close_requested"}


@app.get("/positions")
async def positions():
    """Posiciones abiertas directamente desde BingX."""
    if client is None:
        raise HTTPException(status_code=503, detail="not_ready")
    try:
        raw = await client.get_open_positions()
        return {"count": len(raw), "positions": raw}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = C.PORT
    log.info("Arrancando servidor en puerto %d", port)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
    )
