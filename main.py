"""
Wavelet MRA Haar 5m — Webhook receiver para TradingView -> BingX + Telegram.

Flujo:
  TradingView (alert() del Pine, formato JSON) --POST--> /webhook/<WEBHOOK_SECRET>
  -> valida y parsea el JSON
  -> si AUTO_TRADE=true: ejecuta en BingX (con circuit breaker + sizing)
  -> en todos los casos: manda la señal a Telegram (para operar manualmente
     si AUTO_TRADE=false, o como confirmación si AUTO_TRADE=true)
  -> persiste el estado para reconciliación tras un restart de Railway

ÁMBITO: esta cuenta de BingX la comparten varios bots de la flota y
operativa manual del usuario. Todo lo que este bot GESTIONA (contar para
los topes, cerrar por falta de SL, la parada de emergencia) se limita por
defecto a las posiciones que él mismo abrió y registró en el estado. Lo
ajeno se informa, no se toca.

Configura la alerta en TradingView con "Webhook URL":
  https://<tu-app>.up.railway.app/webhook/<WEBHOOK_SECRET>
y como mensaje: {{strategy.order.alert_message}}  (o deja que sea el propio
JSON que genera `alert(json_..., alert.freq_once_per_bar_close)` del script;
en ese caso usa "Any alert() function call" al crear la alerta).
"""
import logging
import sys

from flask import Flask, jsonify, request

import bingx_client
import config
import telegram_notifier
from state_manager import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")

app = Flask(__name__)
bx = bingx_client.BingXClient()
state = StateManager()

log.info(
    "=" * 70 + "\nENTORNO BINGX: %s | AUTO_TRADE=%s\n" + "=" * 70,
    "DEMO / VST (dinero simulado)" if config.BINGX_DEMO else "⚠️ PRODUCCIÓN — DINERO REAL ⚠️",
    config.AUTO_TRADE,
)

# --------------------------------------------------------------------------- #
# GUARDIA DE ARRANQUE PARA DINERO REAL. Con AUTO_TRADE=true y BINGX_DEMO=false
# el bot puede abrir/cerrar posiciones reales. Los endpoints /emergency-stop y
# /reset-breaker son el ÚNICO freno manual que existe, y ambos dependen de
# WEBHOOK_SECRET -- si está vacío, esas rutas son inalcanzables (Flask no
# admite un segmento de URL vacío) y no hay forma de pararlo desde fuera sin
# tocar variables de Railway y esperar un redeploy. Se rehúsa a arrancar en
# real sin ese freno en vez de descubrirlo el día que algo va mal.
if config.AUTO_TRADE and not config.BINGX_DEMO and not config.WEBHOOK_SECRET:
    log.critical(
        "AUTO_TRADE=true y BINGX_DEMO=false (dinero real) pero WEBHOOK_SECRET "
        "está vacío -- /emergency-stop y /reset-breaker quedarían inutilizables. "
        "Define WEBHOOK_SECRET en Railway (ej. `openssl rand -hex 24`) antes de arrancar en real."
    )
    sys.exit(1)

# Aviso si el estado (posiciones, circuit breaker, cooldown) no está en una
# ruta que sobreviva a un redeploy. Railway borra el disco del contenedor en
# cada redeploy salvo que STATE_FILE apunte a un Volume montado (ver README
# sección 0). Una ruta relativa como "state.json" NUNCA sobrevive.
if config.AUTO_TRADE and not config.BINGX_DEMO and not config.STATE_FILE.startswith("/"):
    _msg = (
        f"⚠️ STATE_FILE='{config.STATE_FILE}' es una ruta relativa -- se perderá en el "
        "próximo redeploy (circuit breaker y cooldown se resetearán a cero). Monta un "
        "Volume en Railway y pon STATE_FILE=/data/state.json (o la ruta del Volume)."
    )
    log.warning(_msg)
    telegram_notifier.send(_msg)

if not config.BINGX_DEMO and config.AUTO_TRADE:
    telegram_notifier.send(
        "🔴 *Bot arrancado en PRODUCCIÓN con AUTO_TRADE=true* — las órdenes "
        "que ejecute serán con dinero real."
    )

# Reconciliación al arrancar (best-effort; si BingX no está configurado,
# se registra el error y el bot sigue en modo señal/Telegram).
if config.BINGX_API_KEY:
    try:
        state.reconcile(bx)
    except Exception:
        log.exception("Reconciliación inicial falló, continuando de todas formas")

    # Valida que los símbolos de SYMBOLS existen en BingX -- si hay un typo
    # (ej. "BTCUSDT" en vez de "BTC-USDT"), es mejor avisar ahora que
    # descubrirlo por un error silencioso repetido cada 5 minutos.
    _invalid_symbols = []
    for _sym in config.SYMBOLS:
        try:
            _filters = bx.get_symbol_filters(_sym)
            if not _filters:
                _invalid_symbols.append(_sym)
        except Exception:
            log.exception("No se pudo validar el símbolo %s al arrancar", _sym)
            _invalid_symbols.append(_sym)
    if _invalid_symbols:
        _msg = f"⚠️ Símbolos en SYMBOLS que BingX no reconoce: {_invalid_symbols}. Revisa el formato (BASE-QUOTE, ej. BTC-USDT)."
        log.warning(_msg)
        telegram_notifier.send(_msg)

# --------------------------------------------------------------------------- #
# Generador de señales propio (no depende de TradingView). Se activa por
# defecto (SIGNAL_SOURCE=python). Si prefieres seguir usando el webhook de
# TradingView, pon SIGNAL_SOURCE=tradingview y ENABLE_SCHEDULER=false.
# --------------------------------------------------------------------------- #
_scheduler = None
if config.SIGNAL_SOURCE == "python" and config.ENABLE_SCHEDULER:
    import poller
    import sys as _sys
    _scheduler = poller.start(_sys.modules[__name__], bx, state)


# --------------------------------------------------------------------------- #
def _live_positions():
    """Todas las posiciones abiertas en la cuenta (incluye las ajenas)."""
    return [
        p for p in bx.get_positions()
        if float(p.get("positionAmt", p.get("positionSize", 0)) or 0) != 0
    ]


# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET"])
def health():
    return jsonify(
        status="ok",
        bingx_env=("demo/VST" if config.BINGX_DEMO else "PRODUCCIÓN REAL"),
        bingx_base_url=bx.base_url,
        auto_trade=config.AUTO_TRADE,
        signal_source=config.SIGNAL_SOURCE,
        symbols=("ALL (perpetuos USDT)" if config.SCAN_ALL_SYMBOLS else config.SYMBOLS),
        open_positions=state.open_count(),
        halted=state.state.get("trading_halted"),
    )


@app.route("/status", methods=["GET"])
def status():
    """Diagnóstico más completo: próximas ejecuciones del scheduler,
    posiciones abiertas conocidas y estado del circuit breaker."""
    jobs = []
    if _scheduler:
        for job in _scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })

    # Cuenta compartida: distinguir lo nuestro de lo ajeno es justo lo que
    # hacía falta para diagnosticar por qué el bot no abría.
    nuestras = set(state.state.get("positions", {}).keys())
    try:
        vivas = {p.get("symbol") for p in _live_positions()}
    except Exception as e:
        vivas, err = set(), str(e)
    else:
        err = None

    return jsonify(
        auto_trade=config.AUTO_TRADE,
        signal_source=config.SIGNAL_SOURCE,
        symbols=config.SYMBOLS,
        scheduler_jobs=jobs,
        open_positions=state.state.get("positions", {}),
        consecutive_losses=state.state.get("consecutive_losses"),
        trading_halted=state.state.get("trading_halted"),
        halt_reason=state.state.get("halt_reason"),
        daily_start_equity=state.state.get("daily_start_equity"),
        daily_date=state.state.get("daily_date"),
        posiciones_propias_vivas=sorted(nuestras & vivas),
        posiciones_ajenas_en_la_cuenta=sorted(vivas - nuestras),
        error_leyendo_bingx=err,
    )


@app.route("/signal-check/<symbol>", methods=["GET"])
def signal_check(symbol):
    """Diagnóstico: calcula la señal actual para un símbolo sin ejecutar
    nada, para verificar que el motor de señales lee bien BingX."""
    import signal_engine
    import poller as _poller
    try:
        rows = bx.get_klines(symbol.upper(), interval="5m", limit=config.WAVELET_LOOKBACK_ENERGY + 60)
        df = signal_engine.klines_to_df(rows)
        sig = signal_engine.compute_signal(df, _poller._params(), last_signal_ts=state.get_last_signal_ts(symbol.upper()))
        return jsonify(symbol=symbol.upper(), bars=len(df), signal=sig)
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/scan", methods=["GET"])
def scan():
    """Analiza TODAS las monedas de BingX (o las que estén en SYMBOLS) con
    el filtro wavelet, sin ejecutar ni notificar nada -- solo para mirar.
    Parámetros opcionales: ?quote=USDT (default) y ?limit=N (símbolos máx.).
    Puede tardar varios segundos/minutos si escaneas todo el universo.
    """
    import poller as _poller
    import scanner as _scanner

    quote = request.args.get("quote", "USDT").upper()
    limit = int(request.args.get("limit", config.SCAN_ALL_MAX_SYMBOLS))
    notify = request.args.get("notify", "false").lower() == "true"

    try:
        if config.SYMBOLS and not config.SCAN_ALL_SYMBOLS:
            symbols = config.SYMBOLS
        else:
            symbols = bx.get_all_symbols(quote_filter=quote)[:limit]
    except Exception as e:
        return jsonify(error=f"no se pudo listar símbolos: {e}"), 500

    results = _scanner.scan_symbols(bx, config, symbols)
    ranked = _scanner.rank_results(results)

    if notify:
        telegram_notifier.send(_scanner.format_scan_summary(ranked))

    return jsonify(symbols_requested=len(symbols), **ranked)


@app.route("/webhook/<secret>", methods=["POST"])
def webhook(secret):
    if not config.WEBHOOK_SECRET or secret != config.WEBHOOK_SECRET:
        log.warning("Intento de webhook con secret inválido")
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True)
    if payload is None:
        # TradingView a veces manda el JSON como texto plano
        try:
            import json as _json
            payload = _json.loads(request.data.decode("utf-8"))
        except Exception:
            log.error("Payload no parseable: %s", request.data)
            return jsonify(error="invalid json"), 400

    log.info("Alerta recibida: %s", payload)

    signal = payload.get("signal")
    try:
        if signal == "entry":
            _handle_entry(payload)
        elif signal == "exit":
            _handle_exit(payload)
        else:
            log.warning("Tipo de señal desconocido: %s", signal)
            return jsonify(error="unknown signal type"), 400
    except Exception as e:
        log.exception("Error procesando la alerta")
        telegram_notifier.send(f"🚨 Error procesando alerta: `{e}`\nPayload: `{payload}`")
        return jsonify(error=str(e)), 500

    return jsonify(status="processed"), 200


# --------------------------------------------------------------------------- #
def _handle_entry(alert: dict):
    tv_symbol = alert["symbol"]
    symbol = config.tv_symbol_to_bingx(tv_symbol)
    position_side = alert["positionSide"]          # LONG / SHORT
    price = float(alert["price"])
    sl = float(alert["sl"])
    tp = float(alert["tp"])

    if not config.AUTO_TRADE:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False)
        )
        return

    if state.get_open(symbol):
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(
                alert, executed=False, error=f"ya hay posición abierta en {symbol}"
            )
        )
        return

    if state.open_count() >= config.MAX_CONCURRENT_POSITIONS:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(
                alert, executed=False, error="límite de posiciones concurrentes alcanzado"
            )
        )
        return

    # Tope de seguridad ABSOLUTO contra las posiciones REALES en BingX que
    # son DE ESTE BOT (no el estado local a secas, que puede haberse perdido
    # si Railway reinició el contenedor sin un Volume persistente).
    #
    # Antes esto contaba TODAS las posiciones de la cuenta. Con otros bots
    # de la flota y operativa manual compartiendo cuenta, bastaban unas
    # pocas posiciones ajenas para dejar este bot bloqueado sin motivo.
    try:
        propias = state.count_own_live_positions(bx)
    except Exception as e:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error=f"no se pudo verificar posiciones reales en BingX: {e}")
        )
        return
    if propias >= config.HARD_MAX_TOTAL_POSITIONS:
        telegram_notifier.send(
            f"⛔ Tope de seguridad alcanzado: {propias} posiciones de ESTE bot abiertas en "
            f"BingX (límite HARD_MAX_TOTAL_POSITIONS={config.HARD_MAX_TOTAL_POSITIONS}). "
            f"Entrada en {symbol} bloqueada."
        )
        return

    # No abrir encima de una posición ajena en el mismo símbolo: en hedge se
    # fusionarían en una sola posición y ninguno de los dos bots sabría ya
    # cuál es la suya.
    try:
        ajenas_mismo_simbolo = [
            p for p in _live_positions()
            if p.get("symbol") == symbol and not state.is_ours(symbol)
        ]
    except Exception:
        ajenas_mismo_simbolo = []
    if ajenas_mismo_simbolo:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(
                alert, executed=False,
                error=f"ya hay una posición en {symbol} que NO es de este bot (otro bot o manual)",
            )
        )
        return

    try:
        equity = bx.get_balance()
    except Exception as e:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error=f"no se pudo leer balance: {e}")
        )
        return

    allowed, reason = state.check_circuit_breaker(equity)
    if not allowed:
        telegram_notifier.send(f"⛔ Trading pausado (circuit breaker): {reason}")
        return

    # Sizing. Dos modos:
    #   - por RIESGO (default): qty = (equity x RISK_PCT) / distancia_al_SL.
    #     La pérdida si salta el stop es constante; el margen varía.
    #   - por MARGEN FIJO (MARGIN_PER_TRADE_USDT > 0): nocional =
    #     margen x LEVERAGE. El margen es constante; el riesgo varía y
    #     queda acotado por MAX_RISK_PCT_ABS.
    risk_amount = equity * (config.RISK_PCT_PER_TRADE / 100)
    stop_distance = abs(price - sl)
    if stop_distance <= 0:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error="distancia a SL inválida")
        )
        return

    # FRENO 3: stop demasiado estrecho. Con qty = riesgo / distancia, un
    # stop muy pegado dispara el nocional (RIVER: stop al 0.76% -> 396 USDT
    # de nocional sobre 150 de equity) y ademas deja el coste pesando
    # demasiado sobre el resultado.
    stop_pct = stop_distance / price * 100.0
    if config.MIN_STOP_DISTANCE_PCT and stop_pct < config.MIN_STOP_DISTANCE_PCT:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(
                alert, executed=False,
                error=(f"stop demasiado estrecho: {stop_pct:.2f}% del precio "
                       f"(mínimo {config.MIN_STOP_DISTANCE_PCT}%). Con un stop así "
                       f"el nocional se dispara y el coste se come el resultado"),
            )
        )
        return
    if config.MARGIN_PER_TRADE_USDT > 0:
        qty = bx.round_qty(symbol, (config.MARGIN_PER_TRADE_USDT * config.LEVERAGE) / price)
        riesgo_real = qty * stop_distance
        riesgo_pct = (riesgo_real / equity * 100) if equity > 0 else 999.0
        if riesgo_pct > config.MAX_RISK_PCT_ABS:
            telegram_notifier.send(
                telegram_notifier.format_entry_signal(
                    alert, executed=False,
                    error=(f"margen fijo {config.MARGIN_PER_TRADE_USDT} USDT x "
                           f"{config.LEVERAGE}x arriesgaría {riesgo_pct:.2f}% del equity "
                           f"en este stop (tope {config.MAX_RISK_PCT_ABS}%)"),
                )
            )
            return
        log.info("%s: margen fijo %.2f USDT x%s -> qty=%s (riesgo %.2f%% del equity)",
                 symbol, config.MARGIN_PER_TRADE_USDT, config.LEVERAGE, qty, riesgo_pct)
    else:
        qty = bx.round_qty(symbol, risk_amount / stop_distance)
    if qty <= 0:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error="qty tras redondeo de precisión es 0")
        )
        return

    # Suelo de nocional. El sizing por riesgo da nocional = riesgo / stop%,
    # así que en símbolos de stop ancho salían posiciones de céntimos donde
    # las comisiones se comen cualquier resultado. Si el nocional queda por
    # debajo del mínimo, se SUBE la cantidad -- pero eso aumenta el riesgo
    # real por encima de RISK_PCT_PER_TRADE, así que se comprueba contra
    # MAX_RISK_PCT_ABS y, si lo supera, se descarta la señal en vez de
    # operarla con un riesgo no autorizado.
    notional = qty * price
    if (not config.MARGIN_PER_TRADE_USDT) and config.MIN_NOTIONAL_USDT and notional < config.MIN_NOTIONAL_USDT:
        qty_minima = bx.round_qty_up(symbol, config.MIN_NOTIONAL_USDT / price)
        riesgo_forzado = qty_minima * stop_distance
        riesgo_pct = (riesgo_forzado / equity * 100) if equity > 0 else 999.0

        if riesgo_pct > config.MAX_RISK_PCT_ABS:
            telegram_notifier.send(
                telegram_notifier.format_entry_signal(
                    alert, executed=False,
                    error=(f"para llegar al mínimo de {config.MIN_NOTIONAL_USDT} USDT haría falta "
                           f"arriesgar {riesgo_pct:.2f}% del equity (tope {config.MAX_RISK_PCT_ABS}%). "
                           f"Stop demasiado ancho en este símbolo"),
                )
            )
            return

        log.info("%s: nocional %.2f < mínimo %.2f USDT -- qty %s -> %s (riesgo %.2f%% del equity)",
                 symbol, notional, config.MIN_NOTIONAL_USDT, qty, qty_minima, riesgo_pct)
        qty = qty_minima
        notional = qty * price

    # FRENO 2: tope de nocional por posicion. El sizing por riesgo no tiene
    # techo por si mismo: si la distancia al stop tiende a cero, el nocional
    # tiende a infinito. RIVER acabo con 2.6x el patrimonio en un simbolo.
    if config.MAX_NOTIONAL_PCT_EQUITY:
        tope_notional = equity * config.MAX_NOTIONAL_PCT_EQUITY / 100.0
        if notional > tope_notional:
            telegram_notifier.send(
                telegram_notifier.format_entry_signal(
                    alert, executed=False,
                    error=(f"nocional {notional:.0f} USDT = "
                           f"{notional/equity:.1f}x el equity ({equity:.0f}); "
                           f"tope {config.MAX_NOTIONAL_PCT_EQUITY:.0f}% "
                           f"({tope_notional:.0f} USDT). Stop demasiado estrecho "
                           f"para el tamaño que exige"),
                )
            )
            return

    # Fija el leverage ANTES de calcular el margen requerido, y usa el valor
    # que BingX confirma en la respuesta -- no config.LEVERAGE a ciegas.
    # Algunos símbolos (sobre todo en SYMBOLS=ALL, altcoins poco comunes)
    # tienen un tope de apalancamiento propio menor al pedido; si se asume
    # que se aplicó el LEVERAGE de config y BingX en realidad usó menos, el
    # cálculo local de margen sale bien pero BingX exige mucho más margen
    # real al mandar la orden -- de ahí los rechazos "insufficient margin"
    # aunque el chequeo local decía que sobraba equity.
    actual_leverage = config.LEVERAGE
    try:
        lev_resp = bx.set_leverage(symbol, position_side, config.LEVERAGE)
        confirmed = None
        if isinstance(lev_resp, dict):
            confirmed = lev_resp.get("leverage") or lev_resp.get("longLeverage") or lev_resp.get("shortLeverage")
        if confirmed:
            actual_leverage = float(confirmed)
            if actual_leverage != config.LEVERAGE:
                log.warning(
                    "%s: BingX confirmó leverage=%s (pedido %s) -- se recalcula el margen con el real",
                    symbol, actual_leverage, config.LEVERAGE,
                )
            # FRENO 1. MAS apalancamiento del pedido es el caso peligroso, y
            # es justo el que no se contemplaba: acerca la liquidacion Y
            # ADEMAS hace que required_margin (que divide por el real) salga
            # MENOR, o sea que relaja la comprobacion de seguridad justo
            # cuando el riesgo sube. RIVER se abrio a 19x con la liquidacion
            # a ~4.8%, se movio 4.40% en contra y perdio el 101% del margen.
            if config.REJECT_HIGHER_LEVERAGE and actual_leverage > config.LEVERAGE:
                log.error("%s: BingX aplicó %sx (pedido %sx) -- NO se opera",
                          symbol, actual_leverage, config.LEVERAGE)
                telegram_notifier.send(
                    telegram_notifier.format_entry_signal(
                        alert, executed=False,
                        error=(f"BingX aplicó {actual_leverage:.0f}x en vez de "
                               f"{config.LEVERAGE}x. Liquidación quedaría a "
                               f"~{100/actual_leverage:.1f}% en contra. "
                               f"Baja el apalancamiento de {symbol} a mano en BingX"),
                    )
                )
                return
    except Exception as e:
        log.warning("No se pudo fijar/confirmar leverage para %s (%s) -- se asume config.LEVERAGE=%s",
                    symbol, e, config.LEVERAGE)

    # Comprobación de margen: evita mandar una orden que BingX rechazaría
    # por fondos insuficientes (o que consumiría casi todo el margen
    # disponible sin que quede colchón para el resto de posiciones).
    required_margin = (qty * price) / max(actual_leverage, 1)
    if required_margin > equity * 0.95:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(
                alert, executed=False,
                error=f"margen insuficiente (necesita ~{required_margin:.2f} USDT con leverage {actual_leverage}x, equity {equity:.2f} USDT)",
            )
        )
        return

    # Apertura protegida: abre, lee el tamaño REAL rellenado, verifica el
    # SL/TP contra openOrders y, si no consigue dejar un stop puesto, CIERRA
    # la posición. Antes esto eran tres llamadas sueltas sin marcha atrás:
    # si el SL fallaba, la posición se quedaba abierta y desnuda.
    res = bx.open_protected_position(
        symbol=symbol,
        position_side=position_side,
        quantity=qty,
        stop_loss=sl,
        take_profit=tp,
        leverage=None,          # ya fijado y confirmado arriba
        margin_mode="ISOLATED",
    )

    if not res.get("ok"):
        motivo = res.get("error") or "fallo desconocido en la apertura"
        if res.get("closed"):
            motivo += " — la posición se cerró, no quedó desprotegida"
        elif res.get("quantity"):
            motivo += " — ⚠️ POSICIÓN POSIBLEMENTE ABIERTA SIN STOP, REVISA BINGX"
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error=motivo)
        )
        log.error("Entrada NO completada en %s %s: %s", symbol, position_side, motivo)
        return

    state.record_open(symbol, position_side, res["quantity"], price, sl, tp)
    telegram_notifier.send(
        telegram_notifier.format_entry_signal(alert, executed=True, qty=res["quantity"])
    )
    if not res.get("has_tp"):
        telegram_notifier.send(f"⚠️ *{symbol}*: abierta con SL pero SIN TP confirmado.")


def _handle_exit(alert: dict):
    tv_symbol = alert["symbol"]
    symbol = config.tv_symbol_to_bingx(tv_symbol)
    position_side = alert["positionSide"]

    if not config.AUTO_TRADE:
        telegram_notifier.send(telegram_notifier.format_exit_signal(alert, executed=False))
        return

    pos = state.get_open(symbol)
    if not pos:
        # puede que ya se haya cerrado por SL/TP directamente en BingX; solo avisamos
        telegram_notifier.send(
            telegram_notifier.format_exit_signal(
                alert, executed=False, error="no había posición registrada localmente (¿cerrada ya por SL/TP?)"
            )
        )
        return

    try:
        exit_price = float(alert.get("price", 0))
        # Verificado: un 'ok' de la API no garantiza que la posición quede a
        # cero (puede ejecutarse parcialmente). Si no se cierra, no se
        # registra el cierre ni se contabiliza el PnL.
        cerrada = bx.close_position_and_verify(symbol, position_side)
        if not cerrada:
            telegram_notifier.send(
                f"🚨 *{symbol}*: la señal de salida no consiguió cerrar la posición. "
                f"CIÉRRALA A MANO EN BINGX."
            )
            return
        entry_price = pos["entry_price"]
        pnl = (exit_price - entry_price) if position_side == "LONG" else (entry_price - exit_price)
    except Exception as e:
        log.exception("Fallo cerrando orden en BingX")
        telegram_notifier.send(
            telegram_notifier.format_exit_signal(alert, executed=False, error=str(e))
        )
        return

    state.record_close(symbol, pnl=pnl)
    telegram_notifier.send(telegram_notifier.format_exit_signal(alert, executed=True))


# --------------------------------------------------------------------------- #
@app.route("/reset-breaker/<secret>", methods=["POST"])
def reset_breaker(secret):
    """Endpoint manual para reactivar el trading tras un circuit breaker.

    Reancla además el equity de referencia del día al equity actual: sin
    eso, un reset con la cuenta ya caída vuelve a disparar el breaker en la
    siguiente señal, porque el drawdown se sigue midiendo contra el equity
    de ANTES de la caída.
    """
    if secret != config.WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 401
    try:
        equity = bx.get_balance()
    except Exception:
        log.exception("No se pudo leer el balance al resetear el breaker")
        equity = None
    state.manual_reset_breaker(reanclar_equity=equity)
    telegram_notifier.send(
        f"✅ Circuit breaker reseteado manualmente."
        + (f" Ancla diaria movida a {equity:.4f} USDT." if equity else "")
    )
    return jsonify(status="reset", daily_start_equity=equity), 200


@app.route("/emergency-stop/<secret>", methods=["POST"])
def emergency_stop(secret):
    """Botón de pánico: pausa el trading YA y cierra posiciones.

    Por defecto cierra SOLO las posiciones de este bot. La versión anterior
    cerraba TODAS las de la cuenta, incluidas las de otros bots de la flota
    y las manuales del usuario -- una parada de emergencia de un bot no
    debería liquidar operativa ajena.

    Para cerrar todo de verdad (incluida la operativa ajena), hay que
    pedirlo explícitamente: ?scope=all
    """
    if secret != config.WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 401

    scope = request.args.get("scope", "own").lower()

    state.state["trading_halted"] = True
    state.state["halt_reason"] = "PARADA DE EMERGENCIA manual"
    state._save()

    try:
        live_positions = _live_positions()
    except Exception as e:
        telegram_notifier.send(f"🚨 Parada de emergencia: no se pudo leer posiciones de BingX: {e}")
        return jsonify(error=str(e)), 500

    if scope == "all":
        objetivo = live_positions
    else:
        objetivo = [p for p in live_positions if state.is_ours(p.get("symbol"))]

    omitidas = [p.get("symbol") for p in live_positions if p not in objetivo]

    closed, failed = [], []
    for p in objetivo:
        sym = p.get("symbol")
        side = p.get("positionSide", "LONG")
        try:
            if bx.close_position_and_verify(sym, side):
                state.record_close(sym)
                closed.append(sym)
            else:
                failed.append((sym, "no llegó a cero tras varios intentos"))
        except Exception as e:
            log.exception("Fallo cerrando %s en parada de emergencia", sym)
            failed.append((sym, str(e)))

    msg = (
        f"🛑 *PARADA DE EMERGENCIA* (alcance: {'TODA la cuenta' if scope == 'all' else 'solo este bot'})"
        f" — trading pausado.\nCerradas: {closed or 'ninguna'}"
    )
    if omitidas:
        msg += f"\nNo tocadas (ajenas a este bot): {omitidas}"
    if failed:
        msg += f"\n⚠️ Fallaron: {failed} — CIÉRRALAS A MANO EN BINGX AHORA."
    telegram_notifier.send(msg)

    return jsonify(status="stopped", scope=scope, closed=closed,
                   skipped=omitidas, failed=failed), 200


@app.route("/positions", methods=["GET"])
def positions():
    """Posiciones REALES en BingX ahora mismo (consulta directa al
    exchange, no el JSON local), separadas en propias y ajenas."""
    try:
        live = _live_positions()
    except Exception as e:
        return jsonify(error=str(e)), 500
    propias = [p for p in live if state.is_ours(p.get("symbol"))]
    ajenas = [p for p in live if not state.is_ours(p.get("symbol"))]
    return jsonify(count=len(live), propias=propias, ajenas=ajenas)


@app.route("/diagnose", methods=["GET"])
def diagnose():
    """Por qué NO se abren operaciones, puerta por puerta.

    Recorre los mismos filtros que _handle_entry, en el mismo orden, y
    dice cuál está cerrado. Evita tener que deducirlo por ausencia de
    órdenes, que es lo que obliga a mirar cinco archivos distintos.
    """
    puertas = []

    if not config.AUTO_TRADE:
        puertas.append({"puerta": "AUTO_TRADE", "bloquea": True,
                        "detalle": "AUTO_TRADE=false -> solo señales, no se ejecuta nada"})
    if not (config.BINGX_API_KEY and config.BINGX_API_SECRET):
        puertas.append({"puerta": "credenciales", "bloquea": True,
                        "detalle": "faltan BINGX_API_KEY / BINGX_API_SECRET"})
    if config.BINGX_DEMO:
        puertas.append({"puerta": "BINGX_DEMO", "bloquea": False,
                        "detalle": "operando contra saldo de práctica (VST)"})

    halted = bool(state.state.get("trading_halted"))
    if halted:
        puertas.append({
            "puerta": "circuit_breaker", "bloquea": True,
            "detalle": state.state.get("halt_reason"),
            "como_liberar": "POST /reset-breaker/<WEBHOOK_SECRET> "
                            "(subir MAX_DAILY_DRAWDOWN_PCT NO lo libera: "
                            "trading_halted está persistido en el estado)",
        })

    propias = state.open_count()
    if propias >= config.MAX_CONCURRENT_POSITIONS:
        puertas.append({"puerta": "MAX_CONCURRENT_POSITIONS", "bloquea": True,
                        "detalle": f"{propias} >= {config.MAX_CONCURRENT_POSITIONS}"})

    try:
        vivas_propias = state.count_own_live_positions(bx)
        if vivas_propias >= config.HARD_MAX_TOTAL_POSITIONS:
            puertas.append({"puerta": "HARD_MAX_TOTAL_POSITIONS", "bloquea": True,
                            "detalle": f"{vivas_propias} >= {config.HARD_MAX_TOTAL_POSITIONS}"})
    except Exception as e:
        vivas_propias = None
        puertas.append({"puerta": "lectura_posiciones", "bloquea": True,
                        "detalle": f"no se pudo consultar BingX: {e}"})

    equity = None
    try:
        equity = bx.get_balance()
        # El suelo de nocional puede rechazar TODAS las señales si el
        # equity es pequeño: para llegar al mínimo habría que arriesgar
        # más de MAX_RISK_PCT_ABS.
        if config.MARGIN_PER_TRADE_USDT > 0:
            puertas.append({
                "puerta": "MARGIN_PER_TRADE_USDT", "bloquea": False,
                "detalle": (f"margen fijo {config.MARGIN_PER_TRADE_USDT} USDT x "
                            f"{config.LEVERAGE}x = "
                            f"{config.MARGIN_PER_TRADE_USDT * config.LEVERAGE:.0f} USDT "
                            f"de posición; MIN_NOTIONAL_USDT y el sizing por riesgo "
                            f"quedan ignorados"),
            })
        elif config.MIN_NOTIONAL_USDT and equity:
            puertas.append({
                "puerta": "MIN_NOTIONAL_USDT", "bloquea": False,
                "detalle": (f"mínimo {config.MIN_NOTIONAL_USDT} USDT por operación; "
                            f"se descarta la señal si llegar ahí exige arriesgar "
                            f">{config.MAX_RISK_PCT_ABS}% del equity ({equity:.2f} USDT)"),
            })
    except Exception as e:
        puertas.append({"puerta": "balance", "bloquea": True,
                        "detalle": f"no se pudo leer el balance: {e}"})

    jobs = []
    if _scheduler:
        for job in _scheduler.get_jobs():
            jobs.append({"id": job.id,
                         "next_run": job.next_run_time.isoformat() if job.next_run_time else None})
    else:
        puertas.append({"puerta": "scheduler", "bloquea": True,
                        "detalle": f"no arrancó (SIGNAL_SOURCE={config.SIGNAL_SOURCE}, "
                                   f"ENABLE_SCHEDULER={config.ENABLE_SCHEDULER})"})

    bloqueantes = [p for p in puertas if p["bloquea"]]
    return jsonify(
        puede_abrir=(len(bloqueantes) == 0),
        bloqueado_por=[p["puerta"] for p in bloqueantes],
        puertas=puertas,
        equity=equity,
        posiciones_propias=propias,
        posiciones_propias_vivas=vivas_propias,
        scheduler_jobs=jobs,
    )


@app.route("/unprotected", methods=["GET"])
def unprotected():
    """Posiciones abiertas SIN stop en la cuenta. Solo informa: este bot no
    cierra nada ajeno. Útil para revisar de un vistazo qué está expuesto."""
    try:
        live = _live_positions()
    except Exception as e:
        return jsonify(error=str(e)), 500
    salida = []
    for p in live:
        sym = p.get("symbol")
        side = str(p.get("positionSide", "")).upper()
        has_sl, has_tp = bx.protection_status(sym, side)
        if not has_sl:
            salida.append({
                "symbol": sym, "positionSide": side,
                "quantity": abs(float(p.get("positionAmt", 0) or 0)),
                "has_tp": has_tp, "de_este_bot": state.is_ours(sym),
            })
    return jsonify(count=len(salida), sin_stop=salida)


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
