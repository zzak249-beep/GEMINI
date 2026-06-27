# ════════════════════════════════════════════════════════════════════════
# PARCHE — 3 NUEVOS FILTROS SHORT
# OI Divergence + FR Spike + Volume Exhaustion
# ════════════════════════════════════════════════════════════════════════


# ── CAMBIO 1: scanner.py IMPORTS ─────────────────────────────────────────
# Junto a los otros imports de filtros (después de ema9_vwap_filter):
"""
from oi_divergence_filter  import oi_div_engine
from fr_spike_filter       import fr_spike_engine
from volume_exhaustion_filter import volume_exhaustion_filter
"""


# ── CAMBIO 2: scanner.py — update de OI y FR en _process_symbol ──────────
# Busca (después de sig = analyze(...)):
#     if sig.direction == "NONE":
#         ...
#         return None
#
# Añade JUSTO DESPUÉS (antes del bloque SHORT_ONLY):
"""
    # Actualizar motores con estado para OI divergence y FR spike
    # — se hace aquí para acumular historial incluso cuando la señal
    # no pasa los filtros, así los motores tienen datos de todos los ciclos.
    if oi_raw > 0:
        oi_div_engine.update(symbol, sig.entry if sig.entry > 0 else k3m[-1][4], oi_raw)
    fr_spike_engine.update(symbol, fr)
"""


# ── CAMBIO 3: scanner.py — bloque 5k (OI Divergence) ────────────────────
# Después del bloque 5j (EMA9×VWAP), antes de "# ── 6b. Auto-blacklist":
"""
    # ── 5k. OI Divergence SHORT ───────────────────────────────────────────────
    # precio subiendo pero OI bajando = rally sin respaldo = SHORT.
    # Motor con estado: necesita 2+ ciclos de historia por símbolo.
    # Activar con OI_DIV_ENABLED=true en Railway (default false).
    if getattr(C, 'OI_DIV_ENABLED', False):
        oi_boost, oi_reason, oi_block = oi_div_engine.signal(
            symbol, sig.direction,
            min_price_rise_pct=getattr(C, 'OI_DIV_MIN_PRICE_RISE_PCT', 1.0),
            min_oi_fall_pct=getattr(C, 'OI_DIV_MIN_OI_FALL_PCT', 2.0),
            lookback=getattr(C, 'OI_DIV_LOOKBACK', 5),
            boost_amount=getattr(C, 'OI_DIV_BOOST', 9.0),
            veto_long=getattr(C, 'OI_DIV_VETO_LONG', True),
        )
        if oi_block:
            log.info("[%s] 🚫 OI_div veto LONG: %s", symbol, oi_reason)
            diag["counts"]["oi_div_veto_long"] += 1
            return None
        if oi_boost > 0:
            sig.score = min(sig.score + oi_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["oi_div_boost"] += 1
            filter_tags["oi_divergence"] = oi_reason
            log.info("[%s] 📉 %s → score=%.1f", symbol, oi_reason, sig.score)
"""


# ── CAMBIO 4: scanner.py — bloque 5l (FR Spike) ──────────────────────────
# Después del bloque 5k:
"""
    # ── 5l. FR Spike SHORT ────────────────────────────────────────────────────
    # FR acelerando hacia arriba = longs sobreacumulándose = SHORT inminente.
    # Complementa FR_REGIME_ENABLED (que detecta nivel, no aceleración).
    # Motor con estado: necesita 3+ ciclos de historia.
    # Activar con FR_SPIKE_ENABLED=true en Railway (default false).
    if getattr(C, 'FR_SPIKE_ENABLED', False):
        frs_boost, frs_reason, frs_block = fr_spike_engine.signal(
            symbol, sig.direction, fr,
            spike_mult=getattr(C, 'FR_SPIKE_MULT', 2.5),
            min_fr_abs=getattr(C, 'FR_SPIKE_MIN_ABS', 0.0003),
            lookback=getattr(C, 'FR_SPIKE_LOOKBACK', 8),
            boost_amount=getattr(C, 'FR_SPIKE_BOOST', 8.0),
        )
        if frs_block:
            log.info("[%s] 🚫 FR_spike veto LONG: %s", symbol, frs_reason)
            diag["counts"]["fr_spike_veto_long"] += 1
            return None
        if frs_boost > 0:
            sig.score = min(sig.score + frs_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["fr_spike_boost"] += 1
            filter_tags["fr_spike"] = frs_reason
            log.info("[%s] 📉 %s → score=%.1f", symbol, frs_reason, sig.score)
"""


# ── CAMBIO 5: scanner.py — bloque 5m (Volume Exhaustion) ─────────────────
# Después del bloque 5l:
"""
    # ── 5m. Volume Exhaustion SHORT ───────────────────────────────────────────
    # Rally con volumen decreciente = compradores agotados = techo probable.
    # Stateless: reutiliza k3m sin nueva llamada a API.
    # Activar con VOL_EXHAUST_ENABLED=true en Railway (default false).
    if getattr(C, 'VOL_EXHAUST_ENABLED', False):
        ve_boost, ve_reason, ve_block = volume_exhaustion_filter(
            k3m, sig.direction,
            lookback=getattr(C, 'VOL_EXHAUST_LOOKBACK', 12),
            min_up_bars=getattr(C, 'VOL_EXHAUST_MIN_UP_BARS', 3),
            vol_slope_threshold=getattr(C, 'VOL_EXHAUST_SLOPE_THR', -0.05),
            boost_amount=getattr(C, 'VOL_EXHAUST_BOOST', 8.0),
        )
        if ve_boost > 0:
            sig.score = min(sig.score + ve_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["vol_exhaust_boost"] += 1
            filter_tags["vol_exhaustion"] = ve_reason
            log.info("[%s] 📉 %s → score=%.1f", symbol, ve_reason, sig.score)
"""


# ── CAMBIO 6: config.py — añadir al final ────────────────────────────────
"""
# ── OI Divergence SHORT filter ────────────────────────────────────────────────
# Señal: precio sube pero OI baja = rally sin respaldo institucional → SHORT.
# Motor con estado (clase): necesita 2+ ciclos de historia. Primeros 2-3 min
# sin señal mientras acumula datos. OI_DIV_VETO_LONG=true veta LONGs en rally débil.
OI_DIV_ENABLED            = _bool("OI_DIV_ENABLED",            False)
OI_DIV_MIN_PRICE_RISE_PCT = _float("OI_DIV_MIN_PRICE_RISE_PCT", 1.0)
OI_DIV_MIN_OI_FALL_PCT    = _float("OI_DIV_MIN_OI_FALL_PCT",    2.0)
OI_DIV_LOOKBACK           = _int("OI_DIV_LOOKBACK",             5)
OI_DIV_BOOST              = _float("OI_DIV_BOOST",              9.0)
OI_DIV_VETO_LONG          = _bool("OI_DIV_VETO_LONG",           True)

# ── FR Spike SHORT filter ─────────────────────────────────────────────────────
# Señal: FR acelera hacia arriba (FR_actual > avg × mult) = longs sobreacumulados.
# Complementa FR_REGIME_ENABLED (nivel) con detección de aceleración (spike).
# Motor con estado: necesita 3+ ciclos (~3 min) de historia antes de dar señal.
FR_SPIKE_ENABLED  = _bool("FR_SPIKE_ENABLED",  False)
FR_SPIKE_MULT     = _float("FR_SPIKE_MULT",    2.5)
FR_SPIKE_MIN_ABS  = _float("FR_SPIKE_MIN_ABS", 0.0003)
FR_SPIKE_LOOKBACK = _int("FR_SPIKE_LOOKBACK",  8)
FR_SPIKE_BOOST    = _float("FR_SPIKE_BOOST",   8.0)

# ── Volume Exhaustion SHORT filter ────────────────────────────────────────────
# Señal: rally con volumen decreciente en barras alcistas = compradores agotados.
# Stateless: reutiliza k3m, sin nueva llamada a API.
VOL_EXHAUST_ENABLED   = _bool("VOL_EXHAUST_ENABLED",   False)
VOL_EXHAUST_LOOKBACK  = _int("VOL_EXHAUST_LOOKBACK",   12)
VOL_EXHAUST_MIN_UP_BARS = _int("VOL_EXHAUST_MIN_UP_BARS", 3)
VOL_EXHAUST_SLOPE_THR = _float("VOL_EXHAUST_SLOPE_THR", -0.05)
VOL_EXHAUST_BOOST     = _float("VOL_EXHAUST_BOOST",     8.0)
"""
