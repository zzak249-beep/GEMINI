# ════════════════════════════════════════════════════════════════════════
# PARCHE EMA9×VWAP — añadir a scanner.py y config.py de joyful-art
# ════════════════════════════════════════════════════════════════════════


# ── CAMBIO 1: scanner.py IMPORT ──────────────────────────────────────────
# Junto a los otros imports de filtros (después de bb_short_filter):
"""
from ema9_vwap_filter import ema9_vwap_filter
"""


# ── CAMBIO 2: scanner.py bloque 5j ───────────────────────────────────────
# Después del bloque 5i (BB Short), antes de "# ── 6b. Auto-blacklist":
"""
    # ── 5j. EMA9 × VWAP Crossunder SHORT ─────────────────────────────────────
    # Portado de Pine v5 "EMA 9 + VWAP Strategy with ATR Trailing Stop".
    # SHORT cuando EMA9 cruza hacia abajo la VWAP = momentum bajista
    # cruzando el nivel institucional. Sin nueva llamada a API — reutiliza k3m.
    # LONG cuando EMA9 < VWAP = veto opcional (contexto bajista).
    # Activar con EMA9_VWAP_ENABLED=true en Railway (default false).
    if getattr(C, 'EMA9_VWAP_ENABLED', False):
        ev_boost, ev_reason, ev_block = ema9_vwap_filter(
            k3m, sig.direction,
            lookback=getattr(C, 'EMA9_VWAP_LOOKBACK', 5),
            boost_amount=getattr(C, 'EMA9_VWAP_BOOST', 9.0),
            strict=getattr(C, 'EMA9_VWAP_STRICT', False),
            veto_long=getattr(C, 'EMA9_VWAP_VETO_LONG', True),
        )
        if ev_block:
            log.info("[%s] 🚫 EMA9×VWAP veto LONG: %s", symbol, ev_reason)
            diag["counts"]["ema9_vwap_veto_long"] += 1
            return None
        if ev_boost > 0:
            sig.score = min(sig.score + ev_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["ema9_vwap_boost"] += 1
            filter_tags["ema9_vwap"] = ev_reason
            log.info("[%s] 📉 %s → score=%.1f", symbol, ev_reason, sig.score)
"""


# ── CAMBIO 3: config.py — añadir al final ────────────────────────────────
"""
# ── EMA9 × VWAP Crossunder SHORT filter ──────────────────────────────────────
# Portado de Pine "EMA 9 + VWAP Strategy with ATR Trailing Stop".
# SHORT: boost en crossunder. LONG: veto cuando EMA9 < VWAP (contexto bajista).
EMA9_VWAP_ENABLED  = _bool("EMA9_VWAP_ENABLED",  False)
EMA9_VWAP_LOOKBACK = _int("EMA9_VWAP_LOOKBACK",   5)      # barras atrás para crossunder
EMA9_VWAP_BOOST    = _float("EMA9_VWAP_BOOST",    9.0)    # puntos al score SHORT
EMA9_VWAP_STRICT   = _bool("EMA9_VWAP_STRICT",    False)  # True = solo barra exacta
EMA9_VWAP_VETO_LONG = _bool("EMA9_VWAP_VETO_LONG", True)  # veta LONG en contexto bajista
"""
