"""
Escáner: calcula el filtro Wavelet MRA Haar sobre TODOS los perpetuos de
BingX (o un subconjunto), sin ejecutar ni notificar nada por sí solo — es
para *analizar*, no para operar. Útil para:
  - ver cuántos/qué símbolos están en régimen "trending" ahora mismo
  - detectar señales activas en cualquier moneda sin tener que listarlas
    una a una en SYMBOLS
  - investigar si el filtro tiene alguna base (RESEARCH.md, punto 3): mirar
    si el edge aparece en varios símbolos correlacionados o es un fluke de
    uno solo

Respeta el rate limit compartido de BingX para datos de mercado (500
peticiones / 10s por IP): mete una pequeña pausa entre símbolos.
"""
import logging
import time

import signal_engine

log = logging.getLogger("scanner")

REQUEST_PACING_SECONDS = 0.08  # ~12 req/s -> deja margen de sobra bajo el límite de BingX


def _timeframe_ms(timeframe: str) -> int:
    unit = timeframe[-1].lower()
    value = int(timeframe[:-1])
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit)
    if mult is None:
        raise ValueError(f"Timeframe no soportado: {timeframe}")
    return value * mult


def _wavelet_params(config):
    # La temporalidad sale de config, no escrita a mano: antes había un
    # "5m" en get_klines y un 5*60*1000 aquí, mientras la variable
    # TIMEFRAME de Railway no la leía nadie. Cambiarla dejaba dos sitios
    # con el valor viejo, en silencio.
    timeframe = getattr(config, "SIGNAL_TIMEFRAME", "5m")
    return {
        "lookback_energy": config.WAVELET_LOOKBACK_ENERGY,
        "k_dominance": config.WAVELET_K_DOMINANCE,
        "cooldown_bars": config.WAVELET_COOLDOWN_BARS,
        "atr_length": config.WAVELET_ATR_LENGTH,
        "atr_mult_sl": config.WAVELET_ATR_MULT_SL,
        "atr_mult_tp": config.WAVELET_ATR_MULT_TP,
        "bar_ms": _timeframe_ms(timeframe),
    }


def scan_symbols(bx, config, symbols, klines_limit=None):
    """Calcula la señal actual (última vela cerrada) para cada símbolo.
    Devuelve una lista de dicts, uno por símbolo, con el resultado o un
    campo "error" si ese símbolo falló (no aborta el escaneo entero por
    un símbolo problemático)."""
    limit = klines_limit or (config.WAVELET_LOOKBACK_ENERGY + 60)
    timeframe = getattr(config, "SIGNAL_TIMEFRAME", "5m")
    params = _wavelet_params(config)
    results = []

    for symbol in symbols:
        try:
            rows = bx.get_klines(symbol, interval=timeframe, limit=limit)
            df = signal_engine.klines_to_df(rows)
            if len(df) < 20:
                results.append({"symbol": symbol, "error": "pocas velas disponibles"})
                continue
            sig = signal_engine.compute_signal(df, params)
            sig["symbol"] = symbol
            results.append(sig)
        except Exception as e:
            log.warning("Escaneo: fallo en %s: %s", symbol, e)
            results.append({"symbol": symbol, "error": str(e)})
        time.sleep(REQUEST_PACING_SECONDS)

    return results


def rank_results(results, top_n=20):
    """Ordena: primero señales activas (long/short ahora mismo), luego
    símbolos en régimen 'trending' (más cerca de dar señal), el resto al
    final. Descarta los que dieron error."""
    ok = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]

    def _score(r):
        if r.get("long_cond") or r.get("short_cond"):
            return 2
        if r.get("is_trending"):
            return 1
        return 0

    ranked = sorted(ok, key=_score, reverse=True)
    return {
        "active_signals": [r for r in ranked if r.get("long_cond") or r.get("short_cond")],
        "trending_no_signal": [r for r in ranked if r.get("is_trending") and not (r.get("long_cond") or r.get("short_cond"))][:top_n],
        "total_scanned": len(results),
        "total_ok": len(ok),
        "total_errors": len(errors),
        "errors": errors[:10],  # solo una muestra, no todos si hay muchos
    }


def _precio(v) -> str:
    """%.6g en vez de .4f: con .4f, un token a 0.006974 se mostraba como
    0.0070 y cualquiera por debajo de 0.0001, como 0.0000."""
    try:
        return f"{float(v):.6g}"
    except (TypeError, ValueError):
        return str(v)


def format_scan_summary(ranked: dict) -> str:
    lines = [
        f"🔍 *Escaneo BingX* — {ranked['total_ok']}/{ranked['total_scanned']} símbolos leídos",
    ]
    active = ranked["active_signals"]
    if active:
        lines.append(f"\n*Señales activas ahora ({len(active)}):*")
        for r in active[:15]:
            icon = "🟢" if r.get("long_cond") else "🔴"
            side = "LONG" if r.get("long_cond") else "SHORT"
            lines.append(f"{icon} {r['symbol']} — {side} @ {_precio(r.get('close'))}")
    else:
        lines.append("\nSin señales activas en este ciclo.")

    trending = ranked["trending_no_signal"]
    if trending:
        lines.append(f"\n*En régimen tendencial, sin cruce todavía ({len(trending)}):*")
        lines.append(", ".join(r["symbol"] for r in trending[:20]))

    return "\n".join(lines)
