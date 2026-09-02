"""
Generador de señales en background — sustituye a las alertas de TradingView.

Dos trabajos periódicos:
  1. job_generate_signals: cada vez que cierra una vela de 5m, calcula el
     filtro Wavelet MRA Haar sobre velas de BingX (signal_engine) para cada
     símbolo en config.SYMBOLS, y si hay señal la procesa exactamente igual
     que si hubiera llegado por webhook (reutiliza main._handle_entry).
  2. job_reconcile_closed_positions: cada pocos minutos comprueba si alguna
     posición local ya no existe en BingX (se cerró sola por el SL/TP
     embebido en la orden), calcula el PnL realizado real vía el endpoint
     de income, y actualiza el circuit breaker + avisa por Telegram.
"""
import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import scanner
import signal_engine
import telegram_notifier

log = logging.getLogger("poller")

_symbols_cache = {"resolved_at": 0, "symbols": []}


def _resolve_symbols(bx):
    """Devuelve la lista de símbolos a vigilar. En modo normal es
    config.SYMBOLS tal cual; en modo SCAN_ALL_SYMBOLS la descubre vía la
    API pública de BingX y la cachea unas horas para no pedirla cada ciclo."""
    if not config.SCAN_ALL_SYMBOLS:
        return config.SYMBOLS

    now = time.time()
    ttl = config.SCAN_ALL_REFRESH_HOURS * 3600
    if _symbols_cache["symbols"] and (now - _symbols_cache["resolved_at"]) < ttl:
        return _symbols_cache["symbols"]

    try:
        all_symbols = bx.get_all_symbols(quote_filter="USDT")
    except Exception:
        log.exception("No se pudo listar todos los símbolos de BingX, se usa la caché anterior si hay")
        return _symbols_cache["symbols"]

    # Filtra por liquidez y prioriza los más líquidos primero -- en vez de
    # coger los primeros N alfabéticamente (que podían ser microcaps con
    # spread altísimo), nos quedamos con los N de más volumen real.
    volumes = bx.get_24h_quote_volumes()
    if volumes:
        liquid = [s for s in all_symbols if volumes.get(s, 0) >= config.MIN_24H_VOLUME_USDT]
        if liquid:
            liquid.sort(key=lambda s: volumes.get(s, 0), reverse=True)
            all_symbols = liquid
        else:
            log.warning(
                "Ningún símbolo supera MIN_24H_VOLUME_USDT=%s, se usa la lista sin filtrar por liquidez",
                config.MIN_24H_VOLUME_USDT,
            )
    else:
        log.warning("No se pudo leer volumen de 24h, se omite el filtro de liquidez este ciclo")

    limited = all_symbols[: config.SCAN_ALL_MAX_SYMBOLS]
    if len(all_symbols) > config.SCAN_ALL_MAX_SYMBOLS:
        log.warning(
            "BingX tiene %d perpetuos USDT; se vigilan solo los primeros %d "
            "(sube SCAN_ALL_MAX_SYMBOLS si quieres más, con cuidado del rate limit)",
            len(all_symbols), config.SCAN_ALL_MAX_SYMBOLS,
        )
    _symbols_cache["symbols"] = limited
    _symbols_cache["resolved_at"] = now
    log.info("Universo de símbolos resuelto (modo ALL): %d símbolos", len(limited))
    return limited


def _params():
    return {
        "lookback_energy": config.WAVELET_LOOKBACK_ENERGY,
        "k_dominance": config.WAVELET_K_DOMINANCE,
        "cooldown_bars": config.WAVELET_COOLDOWN_BARS,
        "atr_length": config.WAVELET_ATR_LENGTH,
        "atr_mult_sl": config.WAVELET_ATR_MULT_SL,
        "atr_mult_tp": config.WAVELET_ATR_MULT_TP,
        "bar_ms": 5 * 60 * 1000,
    }


def _build_alert(symbol: str, signal: dict):
    if signal["long_cond"]:
        side, position_side = "buy", "LONG"
    else:
        side, position_side = "sell", "SHORT"
    return {
        "strategy": "wavelet_mra_5m_python", "exchange": "BingX", "symbol": symbol,
        "side": side, "positionSide": position_side, "signal": "entry",
        "price": signal["close"], "sl": signal["sl"], "tp": signal["tp"],
        "time": signal["timestamp"],
    }


def job_generate_signals(main_module, bx, state):
    symbols = _resolve_symbols(bx)
    if not symbols:
        log.warning("Sin símbolos que vigilar este ciclo (lista vacía)")
        return

    for symbol in symbols:
        try:
            rows = bx.get_klines(
                symbol, interval="5m",
                limit=config.WAVELET_LOOKBACK_ENERGY + 60,
            )
            df = signal_engine.klines_to_df(rows)
            if len(df) < 20:
                log.warning("Pocas velas para %s (%d), se salta este ciclo", symbol, len(df))
                continue

            # descarta la vela en curso si aún no ha cerrado
            now_ms = int(time.time() * 1000)
            if df["open_time"].iloc[-1] + 5 * 60 * 1000 > now_ms:
                df = df.iloc[:-1]
            if len(df) < 20:
                continue

            last_ts = state.get_last_signal_ts(symbol)
            sig = signal_engine.compute_signal(df, _params(), last_signal_ts=last_ts)

            if not (sig["long_cond"] or sig["short_cond"]):
                continue
            if sig["timestamp"] == last_ts:
                continue  # ya procesamos esta vela (evita duplicar en restart)

            alert = _build_alert(symbol, sig)
            log.info("Señal generada para %s: %s", symbol, alert)
            state.set_last_signal_ts(symbol, sig["timestamp"])
            main_module._handle_entry(alert)
        except Exception:
            log.exception("Error generando señal para %s", symbol)
        finally:
            if config.SCAN_ALL_SYMBOLS:
                time.sleep(scanner.REQUEST_PACING_SECONDS)


def job_reconcile_closed_positions(bx, state):
    try:
        live_raw = bx.get_positions()
    except Exception:
        log.exception("Reconciliación periódica: no se pudo leer posiciones de BingX")
        return

    live_symbols = set()
    for p in live_raw:
        amt = float(p.get("positionAmt", p.get("positionSize", 0)) or 0)
        if amt != 0:
            live_symbols.add(p.get("symbol"))

    for symbol in list(state.state["positions"].keys()):
        if symbol in live_symbols:
            continue
        pnl = None
        try:
            since = state.get_last_income_check_ts(symbol)
            pnl = bx.get_realized_pnl_since(symbol, since)
        except Exception:
            log.exception("No se pudo leer PnL realizado de %s, se registra sin PnL", symbol)
        state.set_last_income_check_ts(symbol, int(time.time() * 1000))
        state.record_close(symbol, pnl=pnl)
        icon = "✅" if (pnl or 0) >= 0 else "🔴"
        pnl_txt = f"{pnl:.2f} USDT" if pnl is not None else "desconocido (revisa BingX)"
        telegram_notifier.send(
            f"{icon} *{symbol}* — posición cerrada sola en BingX (SL/TP).\nPnL realizado: `{pnl_txt}`"
        )


def job_scan_report(bx, state):
    """Resumen periódico por Telegram del estado del filtro en TODO el
    universo vigilado — no ejecuta nada, es puramente informativo. Útil
    sobre todo en modo SCAN_ALL_SYMBOLS para tener una foto de qué monedas
    están en régimen tendencial ahora mismo."""
    symbols = _resolve_symbols(bx)
    if not symbols:
        return
    try:
        results = scanner.scan_symbols(bx, config, symbols)
        ranked = scanner.rank_results(results)
        telegram_notifier.send(scanner.format_scan_summary(ranked))
    except Exception:
        log.exception("Fallo generando el resumen de escaneo periódico")


def start(main_module, bx, state):
    """Arranca el scheduler. Devuelve el objeto (guárdalo en una variable
    global de main.py para que no lo recoja el garbage collector)."""
    if not config.SYMBOLS and not config.SCAN_ALL_SYMBOLS:
        log.warning("Sin símbolos configurados (SYMBOLS vacío y SCAN_ALL_SYMBOLS=false), el generador de señales no arranca")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        job_generate_signals,
        CronTrigger(minute="*/5", second=15),
        args=[main_module, bx, state],
        id="generate_signals",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_reconcile_closed_positions,
        CronTrigger(minute="*/2"),
        args=[bx, state],
        id="reconcile_closes",
        max_instances=1,
        coalesce=True,
    )
    if config.SCAN_REPORT_ENABLED:
        scheduler.add_job(
            job_scan_report,
            CronTrigger(hour=f"*/{max(1, int(config.SCAN_REPORT_INTERVAL_HOURS))}"),
            args=[bx, state],
            id="scan_report",
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    log.info(
        "Scheduler de señales iniciado. Modo: %s",
        "ALL (todos los perpetuos USDT)" if config.SCAN_ALL_SYMBOLS else config.SYMBOLS,
    )
    return scheduler
