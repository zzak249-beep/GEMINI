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

TERCERA CORRECCIÓN (08/09/2026) — GUARDA DE ORIENTACIÓN DE SL/TP.
    En los logs de ese día, LYN-USDT SHORT salió con sl=0.035412 POR DEBAJO
    de la entrada 0.03559 y tp=0.035698 POR ENCIMA. En un corto eso es al
    revés: el stop donde está tu beneficio y el objetivo donde está tu
    pérdida.

    Los PRECIOS estaban bien calculados; lo que estaba mal eran las
    ETIQUETAS. El nivel de abajo está a 2.5 ATR (es el take profit) y se
    llamaba 'sl'; el de arriba está a 1.5 ATR (es el stop) y se llamaba
    'tp'. Emparejándolos al revés el ATR implícito cuadra al 1.4%, el mismo
    margen de redondeo que tiene el LONG de SOLV consigo mismo (0.4%).

    ESTE ARCHIVO NO ES EL ORIGEN DEL FALLO. _build_alert pasa signal["sl"]
    y signal["tp"] tal cual vienen de signal_engine.compute_signal: el
    intercambio ocurre allí. Aquí solo se corta antes de ejecutar, se anota
    en el CSV y se avisa. Arregla compute_signal o el bug sigue vivo.
"""
import datetime as dt
import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import csv
import os

import atrous
import config
import guardas as gd
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
             "motivo_no_ejecutada", "orientacion_ok",
             # Contexto de la à trous de Haar causal (atrous.py). SOLO MIDE:
             # no entra en ninguna decisión de este archivo. Se anota para
             # comparar dentro de unas semanas si separa ganadoras de
             # perdedoras (ver atrous.py y NOTAS_MOTOR.md) antes de activar
             # nada como filtro de verdad.
             "at_pendiente", "at_macro", "at_acuerdo", "at_ruido"]


def _cabecera_existente(ruta: str):
    """
    Columnas del CSV que ya está en disco, si lo hay.

    Añadir una columna nueva al CSV rompía el registro en silencio: DictWriter
    lanza ValueError si le pasas una clave que no está en fieldnames, y el
    except de registrar_senal se lo tragaba. Resultado: dejaba de anotarse
    TODO, no solo la columna nueva. Se lee la cabecera real y se escribe
    contra ella.
    """
    try:
        if not os.path.exists(ruta) or os.path.getsize(ruta) == 0:
            return None
        with open(ruta, newline="", encoding="utf-8") as f:
            fila = next(csv.reader(f), None)
        return fila or None
    except Exception:
        log.exception("No se pudo leer la cabecera de %s", ruta)
        return None


def registrar_senal(alert: dict, sig: dict, ejecutada: bool, motivo: str = "",
                    orientacion_ok: bool = True, at_ctx: dict = None):
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
        columnas = _cabecera_existente(SIGNALS_LOG)
        nuevo_archivo = columnas is None
        if nuevo_archivo:
            columnas = _LOG_COLS
        fila = {
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
            "orientacion_ok": int(bool(orientacion_ok)),
        }
        at_ctx = at_ctx or {}
        fila["at_pendiente"] = at_ctx.get("at_pendiente")
        fila["at_macro"] = at_ctx.get("at_macro")
        fila["at_acuerdo"] = at_ctx.get("at_acuerdo")
        fila["at_ruido"] = at_ctx.get("at_ruido")
        with open(SIGNALS_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
            if nuevo_archivo:
                w.writeheader()
            w.writerow(fila)
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


def _orientacion_valida(alert: dict):
    """
    Última puerta antes de ejecutar: ¿el stop está del lado que le toca?

    LONG  -> sl POR DEBAJO de la entrada, tp POR ENCIMA.
    SHORT -> sl POR ENCIMA, tp POR DEBAJO.

    Devuelve (ok, motivo, sl_bueno, tp_bueno). NO se autocorrige: si el bot
    arreglara los campos en silencio, el fallo seguiría en compute_signal y
    nadie volvería a mirarlo. Se bloquea y se avisa con los valores buenos
    para que el arreglo se haga donde toca.
    """
    return gd.revisar_sl_tp(
        alert.get("side", ""), alert.get("positionSide", ""),
        alert.get("price", 0), alert.get("sl", 0), alert.get("tp", 0),
    )


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

    invertidas = 0

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

            # Contexto de la à trous de Haar causal -- solo mide y se anota
            # en el CSV (ver atrous.py). No cambia ninguna decisión de este
            # ciclo: un fallo aquí no debe tumbar la señal.
            try:
                at_ctx = atrous.contexto(df["close"].tolist(), alert["positionSide"])
            except Exception:
                log.exception("%s: fallo calculando contexto atrous (no afecta a la señal)", symbol)
                at_ctx = {}

            # ── GUARDA DE ORIENTACIÓN ─────────────────────────────────
            ok, motivo_or, sl_bueno, tp_bueno = _orientacion_valida(alert)
            if not ok:
                invertidas += 1
                log.error("%s NO ejecutada — %s | correcto sería sl=%.10g tp=%.10g",
                          symbol, motivo_or, sl_bueno, tp_bueno)
                registrar_senal(alert, sig, False, motivo_or, orientacion_ok=False, at_ctx=at_ctx)
                if gd.debe_avisar(f"orient:{symbol}",
                                  getattr(config, "GUARD_AVISO_MIN", 30)):
                    telegram_notifier.send(
                        f"🚫 *{symbol}* — señal NO ejecutada\n"
                        f"{motivo_or}\n"
                        f"Correcto sería `sl={sl_bueno:.10g}` `tp={tp_bueno:.10g}`\n"
                        f"_El fallo está en signal_engine.compute_signal, no aquí. "
                        f"No se autocorrige a propósito._"
                    )
                continue
            # ──────────────────────────────────────────────────────────

            if halted:
                registrar_senal(alert, sig, False,
                                f"circuit breaker: {state.state.get('halt_reason')}",
                                at_ctx=at_ctx)
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
                # El motivo tiene que decir la verdad: con AUTO_TRADE=false
                # NO es que la señal se rechazara, es que el bot no opera.
                # Anotarlas todas como "sin hueco o rechazada" ensucia la
                # columna con la que después se comparan ejecutadas contra
                # descartadas — y en modo manual TODAS caerían del lado malo.
                motivo_no = ("" if ejecutada
                             else "modo manual (AUTO_TRADE=false)"
                             if not config.AUTO_TRADE
                             else "sin hueco o rechazada en _handle_entry")
                registrar_senal(alert, sig, ejecutada, motivo_no, at_ctx=at_ctx)
        except Exception:
            log.exception("Error generando señal para %s", symbol)
        finally:
            if config.SCAN_ALL_SYMBOLS:
                time.sleep(scanner.REQUEST_PACING_SECONDS)

    # Si TODAS las de un lado salen invertidas, esto no es un caso raro: es
    # que compute_signal está mal para ese lado y hay que arreglarlo ya.
    if invertidas:
        log.error("Ciclo terminado con %d señal(es) bloqueadas por orientación de SL/TP",
                  invertidas)


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
    log.info("Guarda de orientación SL/TP ACTIVA: una señal con el stop del lado "
             "equivocado se bloquea y se anota en %s", SIGNALS_LOG)
    return scheduler
