"""
Generador de señales en background — sustituye a las alertas de TradingView.

Dos trabajos periódicos:
  1. job_generate_signals: cada vez que cierra una vela, calcula el
     filtro Wavelet MRA Haar sobre velas de BingX (signal_engine) para cada
     símbolo en config.SYMBOLS, y si hay señal la procesa exactamente igual
     que si hubiera llegado por webhook (reutiliza main._handle_entry).
     Si el circuit breaker está activo NO se ejecuta la entrada, pero la
     señal se sigue enviando a Telegram como informativa.
  2. job_reconcile_closed_positions: cada pocos minutos comprueba si alguna
     posición local ya no existe en BingX (se cerró sola por el SL/TP
     embebido en la orden), calcula el PnL realizado real vía el endpoint
     de income, y actualiza el circuit breaker + avisa por Telegram.

DOS CORRECCIONES respecto a la versión anterior:
  - El PnL realizado se pedía desde `None` en el primer cierre de cada
    símbolo tras un despliegue, lo que sumaba el histórico ENTERO de ese
    símbolo (incluidas operaciones manuales anteriores en esta cuenta
    compartida) y se lo atribuía a la operación recién cerrada. Ese número
    alimenta consecutive_losses, así que calibraba mal el circuit breaker.
    Ahora la ventana arranca en el opened_at de la posición.
  - La temporalidad estaba escrita a mano en tres sitios ("5m", el
    5*60*1000 del descarte de vela en curso y el bar_ms de _params()),
    mientras la variable TIMEFRAME de Railway no la leía nadie. Ahora sale
    toda de config.
"""
import datetime as dt
import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import csv
import os

import config
import scanner
import signal_engine
import telegram_notifier

log = logging.getLogger("poller")

_symbols_cache = {"resolved_at": 0, "symbols": []}


def timeframe_to_ms(timeframe: str) -> int:
    """'5m' -> 300000. Una sola definición para todo el módulo."""
    unit = timeframe[-1].lower()
    value = int(timeframe[:-1])
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit)
    if mult is None:
        raise ValueError(f"Timeframe no soportado: {timeframe}")
    return value * mult


TIMEFRAME = getattr(config, "SIGNAL_TIMEFRAME", "5m")
BAR_MS = timeframe_to_ms(TIMEFRAME)


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
        "bar_ms": BAR_MS,
    }


SIGNALS_LOG = os.path.join(
    os.path.dirname(config.STATE_FILE) or ".", "senales_todas.csv")

_LOG_COLS = ["ts_señal", "fecha_utc", "symbol", "side", "timeframe",
             "price", "sl", "tp", "atr", "is_trending", "ejecutada",
             "motivo_no_ejecutada"]


def registrar_senal(alert: dict, sig: dict, ejecutada: bool, motivo: str = ""):
    """
    Guarda TODAS las señales, ejecutadas o no.

    POR QUÉ. Con MAX_CONCURRENT_POSITIONS=1 y varias señales por ciclo,
    el bot opera la PRIMERA que encuentra hueco -- y el orden lo fija el
    volumen de 24h, no ningún criterio de calidad. Las demás solo iban a
    Telegram y se perdían.

    Sin este registro NO SE PUEDE responder la única pregunta que
    importa aquí: ¿habrían ido mejor las que descarté? Con él, dentro de
    un mes se comparan las ejecutadas contra las descartadas y el
    ranking se elige CON DATOS en vez de inventándolo.

    Es solo un CSV en el volumen. No cambia ni una decisión del bot.
    """
    try:
        os.makedirs(os.path.dirname(SIGNALS_LOG) or ".", exist_ok=True)
        nuevo_archivo = not os.path.exists(SIGNALS_LOG)
        with open(SIGNALS_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_LOG_COLS)
            if nuevo_archivo:
                w.writeheader()
            w.writerow({
                "ts_señal": alert.get("time"),
                "fecha_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "symbol": alert.get("symbol"),
                "side": alert.get("positionSide"),
                "timeframe": TIMEFRAME,
                "price": alert.get("price"),
                "sl": alert.get("sl"),
                "tp": alert.get("tp"),
                "atr": sig.get("atr"),
                "is_trending": sig.get("is_trending"),
                "ejecutada": int(bool(ejecutada)),
                "motivo_no_ejecutada": motivo,
            })
    except Exception:
        # El registro NUNCA puede tumbar el bot ni bloquear una entrada.
        log.exception("No se pudo registrar la señal en %s", SIGNALS_LOG)


def _build_alert(symbol: str, signal: dict):
    if signal["long_cond"]:
        side, position_side = "buy", "LONG"
    else:
        side, position_side = "sell", "SHORT"
    return {
        "strategy": f"wavelet_mra_{TIMEFRAME}_python", "exchange": "BingX", "symbol": symbol,
        "side": side, "positionSide": position_side, "signal": "entry",
        "price": signal["close"], "sl": signal["sl"], "tp": signal["tp"],
        "time": signal["timestamp"],
    }


def job_generate_signals(main_module, bx, state):
    symbols = _resolve_symbols(bx)
    if not symbols:
        log.warning("Sin símbolos que vigilar este ciclo (lista vacía)")
        return

    # El circuit breaker se consulta UNA vez por ciclo, no por símbolo: si
    # está activo NO se abre ninguna posición, pero el escaneo continúa y
    # cada señal se manda a Telegram como informativa. Así el breaker deja
    # de silenciar el flujo de señales -- solo corta la ejecución, que es
    # lo que debe proteger.
    halted = bool(state.state.get("trading_halted"))
    if halted:
        log.info(
            "Circuit breaker activo (%s) -- este ciclo genera señales SOLO informativas, no se ejecuta nada",
            state.state.get("halt_reason"),
        )

    for symbol in symbols:
        try:
            rows = bx.get_klines(
                symbol, interval=TIMEFRAME,
                limit=config.WAVELET_LOOKBACK_ENERGY + 60,
            )
            df = signal_engine.klines_to_df(rows)
            if len(df) < 20:
                log.warning("Pocas velas para %s (%d), se salta este ciclo", symbol, len(df))
                continue

            # descarta la vela en curso si aún no ha cerrado
            now_ms = int(time.time() * 1000)
            if df["open_time"].iloc[-1] + BAR_MS > now_ms:
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

            if halted:
                registrar_senal(alert, sig, False,
                                f"circuit breaker: {state.state.get('halt_reason')}")
                telegram_notifier.send(
                    telegram_notifier.format_entry_signal(
                        alert,
                        executed=False,
                        error=(
                            f"circuit breaker activo ({state.state.get('halt_reason')}) "
                            f"— señal informativa, no se ha ejecutado"
                        ),
                    )
                )
            else:
                # Se registra ANTES de intentar la entrada: si _handle_entry
                # falla o la rechaza, la señal queda igualmente anotada.
                antes = state.open_count()
                main_module._handle_entry(alert)
                ejecutada = state.open_count() > antes
                registrar_senal(
                    alert, sig, ejecutada,
                    "" if ejecutada else "sin hueco o rechazada en _handle_entry")
        except Exception:
            log.exception("Error generando señal para %s", symbol)
        finally:
            if config.SCAN_ALL_SYMBOLS:
                time.sleep(scanner.REQUEST_PACING_SECONDS)


def _pnl_window_start(state, symbol: str):
    """Desde cuándo mirar el PnL realizado de un símbolo, en ms.

    Prioridad: el último chequeo -> el opened_at de la posición -> None.

    El suelo por opened_at es lo importante. Sin él, en el primer cierre de
    cada símbolo tras un despliegue la ventana empezaba en None y
    get_income devolvía el histórico ENTERO: el PnL de operaciones
    anteriores (incluidas las manuales del usuario en esta cuenta
    compartida) se sumaba y se atribuía a la operación recién cerrada. Ese
    número alimenta consecutive_losses, así que un histórico en verde podía
    resetear la racha de pérdidas y desarmar el circuit breaker.
    """
    since = state.get_last_income_check_ts(symbol)
    if since:
        return since

    pos = state.state.get("positions", {}).get(symbol) or {}
    opened_at = pos.get("opened_at")
    if opened_at:
        try:
            ts = dt.datetime.fromisoformat(str(opened_at))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            # Un minuto de margen: el income de BingX puede registrarse con
            # algo de desfase respecto al momento de apertura.
            return int(ts.timestamp() * 1000) - 60_000
        except (ValueError, TypeError):
            log.warning("%s: opened_at ilegible (%s)", symbol, opened_at)

    log.warning("%s: sin ventana de PnL fiable -- se registra el cierre SIN PnL "
                "en vez de sumar histórico ajeno a la racha", symbol)
    return None


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

        since = _pnl_window_start(state, symbol)
        pnl = None
        if since is not None:
            try:
                pnl = bx.get_realized_pnl_since(symbol, since)
            except Exception:
                log.exception("No se pudo leer PnL realizado de %s, se registra sin PnL", symbol)
        # Sin ventana fiable NO se pasa PnL: es mejor no contabilizar esta
        # operación en la racha que contabilizarla con un número que
        # incluye histórico que no es suyo.

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

    # El cron sigue la temporalidad configurada en vez de un */5 fijo.
    minutos = max(1, BAR_MS // 60_000)
    minute_expr = f"*/{minutos}" if minutos <= 30 else "0"

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        job_generate_signals,
        CronTrigger(minute=minute_expr, second=config.SIGNAL_SECOND_OFFSET),
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
        "Scheduler de señales iniciado (%s, barrido cada %d min, segundo %d). Modo: %s",
        TIMEFRAME, minutos, config.SIGNAL_SECOND_OFFSET,
        "ALL (todos los perpetuos USDT)" if config.SCAN_ALL_SYMBOLS else config.SYMBOLS,
    )
    return scheduler
