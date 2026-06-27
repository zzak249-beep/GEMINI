"""
QF×JP Bot v7.7 — Scanner COMPLETO
═══════════════════════════════════════════════════════════════════════════════
MODIFICACIONES JOYFUL-ART (SHORT-only mode):
  ✅ SHORT_ONLY: bloquea todos los LONG si C.SHORT_ONLY=True
  ✅ LATERAL_ADX_MAX: solo opera si ADX < umbral (mercado lateral)
  ✅ IBS Pullback filter (paso 5h): nuevo mínimo + IBS > 0.85
  ✅ BB Short filter (paso 5i): close > upper_BB × (1 + pct)

FIX v7.7:
  ✅ filter_tags{} para journal.on_open()
FIX v7.6:
  ✅ Price Action Framework opcional
FIX v7.5:
  ✅ STC + Asimetría (1m)
FIX v7.4:
  ✅ place_limit_entry() con sl/tp obligatorios
═══════════════════════════════════════════════════════════════════════════════
"""
import asyncio
import logging
import time
import datetime
from collections import Counter
from typing import Optional

import config as C
from bingx_client import BingXClient
from indicators import analyze, Signal, score_to_tier
from risk_manager import RiskManager
from position_manager import PositionManager, OpenTrade
from funding_regime import regime_engine, Regime, Window
from volatility_regime import vol_engine, Regime as VolRegime
from edge_filters import candle_turn_boost, multi_tf_slope_alignment
from btc_correlation import compute_correlation, btc_guard
from stc_asymmetry import stc_asymmetry_filter, stc_volume_slope_filter
from price_action_framework import price_action_filter
from trend_magic_rmi import trend_magic_rmi_filter
from ibs_filter import ibs_pullback_filter
from bb_short_filter import bb_short_filter

# v8.1: Market Structure ChoCh
try:
    from market_structure import ms_filter as _ms_filter
    _MS_AVAILABLE = True
except ImportError:
    _MS_AVAILABLE = False

# v8.2: OI + Funding Cascade Signal
try:
    from oi_cascade_signal import oi_cascade_engine as _oi_engine
    _OI_CASCADE_AVAILABLE = True
except ImportError:
    _OI_CASCADE_AVAILABLE = False

from ws_market_data import ws_cache
import telegram_client as tg

log = logging.getLogger("scanner")

_cb_blacklist: dict[str, float] = {}
CB_COOLDOWN = 600

_oi_cache: dict[str, tuple[float, float]] = {}
OI_CACHE_TTL = 120


async def _get_k_primary(client: BingXClient, symbol: str):
    if getattr(C, 'WS_ENABLED', False):
        cached = ws_cache.get_latest(symbol, C.TIMEFRAME)
        if cached is not None:
            return cached
    return await client.get_klines(symbol, C.TIMEFRAME, 200)


async def _fetch_all(client: BingXClient, symbol: str):
    results = await asyncio.gather(
        _get_k_primary(client, symbol),
        client.get_klines(symbol, C.HTF_TIMEFRAME,  100),
        client.get_klines(symbol, C.HTF2_TIMEFRAME, 100),
        client.get_klines(symbol, C.HTF5_TIMEFRAME, 100),
        client.get_order_book(symbol, 10),
        client.get_funding_rate(symbol),
        client.get_open_interest(symbol),
        return_exceptions=True,
    )
    def _l(r): return r if isinstance(r, list) else []
    def _d(r): return r if isinstance(r, dict) else {}
    def _f(r): return r if isinstance(r, float) else 0.0
    return (_l(results[0]), _l(results[1]), _l(results[2]), _l(results[3]),
            _d(results[4]), _f(results[5]), _f(results[6]))


def _obi(ob: dict) -> float:
    try:
        bv = sum(float(b[1]) for b in ob.get("bids", [])[:5] if len(b) >= 2)
        av = sum(float(a[1]) for a in ob.get("asks", [])[:5] if len(a) >= 2)
        t  = bv + av
        return (bv - av) / t if t > 0 else 0.0
    except Exception:
        return 0.0


async def _get_oi_delta(client: BingXClient, symbol: str) -> float:
    if not getattr(C, 'OI_FILTER_ENABLED', False):
        return 0.0
    now = time.time()
    prev_oi, prev_ts = _oi_cache.get(symbol, (0.0, 0.0))
    try:
        oi = await client.get_open_interest(symbol)
        _oi_cache[symbol] = (oi, now)
        if prev_oi > 0 and (now - prev_ts) < OI_CACHE_TTL * 3:
            return (oi - prev_oi) / prev_oi
    except Exception:
        pass
    return 0.0


def _session_allowed() -> bool:
    start = getattr(C, 'TRADE_START_UTC', 0)
    end   = getattr(C, 'TRADE_END_UTC',   24)
    if start == 0 and end == 24:
        return True
    h = datetime.datetime.utcnow().hour
    if start < end:
        return start <= h < end
    else:
        return h >= start or h < end


def _fr_boost_block(fr: float, direction: str) -> tuple[float, bool]:
    thr = getattr(C, 'FR_EXTREME_THR', 0.0005)
    if thr <= 0:
        return 0.0, False
    if fr > thr:
        if direction == "LONG":
            return 0.0, True
        if direction == "SHORT":
            return 8.0, False
    if fr < -thr:
        if direction == "SHORT":
            return 0.0, True
        if direction == "LONG":
            return 8.0, False
    return 0.0, False


async def _process_symbol(
    symbol, client, risk, pos_mgr, diag: dict,
    journal=None, btc_klines: list = None,
) -> Optional[Signal]:

    if pos_mgr.is_trading(symbol):
        diag["counts"]["already_trading"] += 1
        return None

    if not _session_allowed():
        diag["counts"]["session_filter"] += 1
        return None

    now = time.time()
    if symbol in _cb_blacklist and now - _cb_blacklist[symbol] < CB_COOLDOWN:
        diag["counts"]["cb_cooldown"] += 1
        return None

    try:
        k3m, k15m, k1h, k4h, ob, fr, oi_raw = await _fetch_all(client, symbol)
    except Exception as e:
        log.debug("[%s] fetch error: %s", symbol, e)
        diag["counts"]["fetch_error"] += 1
        return None

    if len(k3m) < 60:
        diag["counts"]["insufficient_data"] += 1
        return None

    obi = _obi(ob)

    try:
        sig = analyze(symbol, k3m, k15m, k1h, k4h, funding_rate=fr)
    except Exception as e:
        log.warning("[%s] analyze error: %s", symbol, e)
        diag["counts"]["analyze_error"] += 1
        return None

    if sig.direction == "NONE":
        diag["counts"][sig.reason or "no_direction"] += 1
        return None

    # ── SHORT-only mode ──────────────────────────────────────────────────────
    # Bloquea todos los LONG cuando SHORT_ONLY=true en Railway.
    # Diseñado para joyful-art en modo SHORT + lateral.
    if getattr(C, 'SHORT_ONLY', False) and sig.direction == "LONG":
        diag["counts"]["long_blocked"] += 1
        return None

    # ── Lateral market filter (ADX) ──────────────────────────────────────────
    # Si ADX > LATERAL_ADX_MAX → mercado en tendencia fuerte → no operar.
    # 0 = desactivado. Recomendado: 28-32 para mercado lateral.
    _lat_max = getattr(C, 'LATERAL_ADX_MAX', 0.0)
    if _lat_max > 0 and sig.adx > _lat_max:
        diag["counts"]["trending_skip"] += 1
        return None

    # ── VOLATILITY REGIME ────────────────────────────────────────────────────
    vol_sig = vol_engine.update(symbol, sig.atr, sig.entry)
    if getattr(C, 'VOL_REGIME_ENABLED', True):
        if vol_sig.block_entry:
            diag["counts"]["vol_extreme_block"] += 1
            return None
        if vol_sig.regime != VolRegime.NORMAL:
            sl_dist  = abs(sig.entry - sig.sl)  * vol_sig.sl_mult
            tp1_dist = abs(sig.tp1   - sig.entry) * vol_sig.tp_mult
            tp2_dist = abs(sig.tp2   - sig.entry) * vol_sig.tp_mult
            if sig.direction == "LONG":
                sig.sl  = sig.entry - sl_dist
                sig.tp1 = sig.entry + tp1_dist
                sig.tp2 = sig.entry + tp2_dist
            else:
                sig.sl  = sig.entry + sl_dist
                sig.tp1 = sig.entry - tp1_dist
                sig.tp2 = sig.entry - tp2_dist
            diag["counts"][f"vol_{vol_sig.regime.lower()}"] += 1

    diag["score_n"]   += 1
    diag["score_sum"] += sig.score
    if sig.score > diag["score_max"]:
        diag["score_max"]         = sig.score
        diag["score_max_symbol"]  = symbol
        diag["score_max_dir"]     = sig.direction

    if abs(obi) > 0.1:
        boost = 0.0
        if sig.direction == "SHORT" and obi < -0.1:
            boost = abs(obi) * 5
        elif sig.direction == "LONG" and obi > 0.1:
            boost = obi * 5
        if boost > 0:
            sig.score = min(sig.score + boost, 100.0)
            sig.tier  = score_to_tier(sig.score)

    # ── FUNDING REGIME ───────────────────────────────────────────────────────
    regime_sig = regime_engine.update(symbol, fr)
    regime_boost = (
        regime_sig.short_boost if sig.direction == "SHORT"
        else regime_sig.long_boost
    )
    if regime_boost != 0:
        sig.score = max(0.0, min(sig.score + regime_boost, 100.0))
        sig.tier  = score_to_tier(sig.score)
        if abs(regime_boost) >= 8:
            log.info("[%s] 💰 Regime boost %+.0f (%s) → score=%.1f",
                     symbol, regime_boost, regime_sig.reason, sig.score)
        diag["counts"][f"regime_{regime_sig.regime.lower()}"] += 1
        if regime_boost < -5:
            diag["counts"]["regime_block"] += 1
            return None

    fr_boost, fr_blocked = _fr_boost_block(fr, sig.direction)
    if fr_blocked:
        diag["counts"]["fr_extreme_block"] += 1
        return None
    if fr_boost > 0:
        sig.score = min(sig.score + fr_boost, 100.0)
        sig.tier  = score_to_tier(sig.score)
        diag["counts"]["fr_extreme_boost"] += 1

    if sig.circuit_breaker:
        _cb_blacklist[symbol] = now
        await tg.notify_circuit_breaker(symbol)
        diag["counts"]["circuit_breaker"] += 1
        return None

    # ── Turn-of-Candle boost ─────────────────────────────────────────────────
    if getattr(C, 'CANDLE_TURN_ENABLED', True):
        ct_boost, ct_reason = candle_turn_boost(
            sig.direction,
            tolerance_min=getattr(C, 'CANDLE_TURN_TOLERANCE_MIN', 1),
            boost=getattr(C, 'CANDLE_TURN_BOOST', 3.0),
        )
        if ct_boost > 0:
            sig.score = min(sig.score + ct_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["candle_turn_boost"] += 1

    # ── Slope Multi-Timeframe ────────────────────────────────────────────────
    slope_adj, slope_block = 0.0, False
    if getattr(C, 'SLOPE_FILTER_ENABLED', True):
        slope_adj, slope_reason, slope_block = multi_tf_slope_alignment(
            k15m, k1h, k4h, sig.direction
        )
        if slope_block:
            log.info("[%s] 🚫 Slope whipsaw block: %s", symbol, slope_reason)
            diag["counts"]["slope_block"] += 1
            return None
        if slope_adj != 0:
            sig.score = max(0.0, min(sig.score + slope_adj, 100.0))
            sig.tier  = score_to_tier(sig.score)
            diag["counts"][f"slope_adj_{slope_adj:+.0f}"] += 1
            if slope_adj >= 10:
                log.info("[%s] 📈 %s → score=%.1f", symbol, slope_reason, sig.score)

    filter_tags: dict = {}

    # ── 5b. STC + Asimetría (1m) ─────────────────────────────────────────────
    if getattr(C, 'STC_ASYM_ENABLED', False):
        try:
            k1m = await client.get_klines(symbol, "1m", 100)
        except Exception as e:
            k1m = []
        if len(k1m) >= 60:
            stc_boost, stc_reason, stc_block = stc_asymmetry_filter(
                k1m, sig.direction,
                stc_length=getattr(C, 'STC_LENGTH', 10),
                stc_fast=getattr(C, 'STC_FAST', 23),
                stc_slow=getattr(C, 'STC_SLOW', 50),
                stc_factor=getattr(C, 'STC_FACTOR', 0.5),
                stc_oversold=getattr(C, 'STC_OVERSOLD', 25.0),
                stc_overbought=getattr(C, 'STC_OVERBOUGHT', 75.0),
                asym_window=getattr(C, 'ASYM_WINDOW', 20),
                asym_veto_threshold=getattr(C, 'ASYM_VETO_THRESHOLD', 1.5),
                asym_boost_per_x=getattr(C, 'ASYM_BOOST_PER_X', 3.0),
                asym_boost_max=getattr(C, 'ASYM_BOOST_MAX', 12.0),
            )
            if stc_block:
                diag["counts"]["stc_asym_veto"] += 1
                return None
            if stc_boost > 0:
                sig.score = min(sig.score + stc_boost, 100.0)
                sig.tier  = score_to_tier(sig.score)
                diag["counts"]["stc_asym_boost"] += 1
                filter_tags["stc_asym"] = stc_reason

    # ── 5c. STC + Volumen + Slope ────────────────────────────────────────────
    if getattr(C, 'STC_VOL_SLOPE_ENABLED', False):
        try:
            k1m_vs = await client.get_klines(symbol, "1m", 100)
        except Exception as e:
            k1m_vs = []
        if len(k1m_vs) >= 60:
            vs_boost, vs_reason, vs_block = stc_volume_slope_filter(
                k1m_vs, sig.direction,
                slope_adj=slope_adj, slope_block=slope_block,
                stc_length=getattr(C, 'STC_LENGTH', 10),
                stc_fast=getattr(C, 'STC_FAST', 23),
                stc_slow=getattr(C, 'STC_SLOW', 50),
                stc_factor=getattr(C, 'STC_FACTOR', 0.5),
                stc_oversold=getattr(C, 'STC_OVERSOLD', 25.0),
                stc_overbought=getattr(C, 'STC_OVERBOUGHT', 75.0),
                vol_window=getattr(C, 'STC_VOL_WINDOW', 20),
                vol_recent_n=getattr(C, 'STC_VOL_RECENT_N', 3),
                vol_min_ratio=getattr(C, 'STC_VOL_MIN_RATIO', 1.3),
                vol_boost_max=getattr(C, 'STC_VOL_BOOST_MAX', 8.0),
                slope_boost_mult=getattr(C, 'STC_SLOPE_BOOST_MULT', 0.5),
            )
            if vs_block:
                diag["counts"]["stc_vol_slope_veto"] += 1
                return None
            if vs_boost > 0:
                sig.score = min(sig.score + vs_boost, 100.0)
                sig.tier  = score_to_tier(sig.score)
                diag["counts"]["stc_vol_slope_boost"] += 1
                filter_tags["stc_vol_slope"] = vs_reason

    # ── 5d. Price Action Framework ───────────────────────────────────────────
    if getattr(C, 'PRICE_ACTION_ENABLED', False):
        pa_boost, pa_reason, pa_block = price_action_filter(
            k3m, sig.direction,
            lookback=getattr(C, 'PA_LOOKBACK', 20),
            body_mult=getattr(C, 'PA_BODY_MULT', 2.0),
            wick_mult=getattr(C, 'PA_WICK_MULT', 1.5),
            touch_tol_pct=getattr(C, 'PA_TOUCH_TOL_PCT', 0.1),
            min_touches=getattr(C, 'PA_MIN_TOUCHES', 3),
            boost_amount=getattr(C, 'PA_BOOST_AMOUNT', 6.0),
        )
        if pa_block:
            diag["counts"]["price_action_veto"] += 1
            return None
        if pa_boost > 0:
            sig.score = min(sig.score + pa_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["price_action_boost"] += 1
            filter_tags["price_action"] = pa_reason

    # ── 5e. Trend Magic + RMI ────────────────────────────────────────────────
    if getattr(C, 'TREND_MAGIC_RMI_ENABLED', False):
        tmr_boost, tmr_reason, tmr_block = trend_magic_rmi_filter(
            k3m, sig.direction,
            cci_len=getattr(C, 'TMR_CCI_LEN', 20),
            atr_len=getattr(C, 'TMR_ATR_LEN', 5),
            atr_mult=getattr(C, 'TMR_ATR_MULT', 1.0),
            rmi_len=getattr(C, 'TMR_RMI_LEN', 14),
            pmom=getattr(C, 'TMR_PMOM', 66.0),
            nmom=getattr(C, 'TMR_NMOM', 30.0),
            boost_amount=getattr(C, 'TMR_BOOST_AMOUNT', 7.0),
        )
        if tmr_block:
            diag["counts"]["trend_magic_rmi_veto"] += 1
            return None
        if tmr_boost > 0:
            sig.score = min(sig.score + tmr_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["trend_magic_rmi_boost"] += 1
            filter_tags["trend_magic_rmi"] = tmr_reason

    # ── 5f. Market Structure ChoCh ───────────────────────────────────────────
    if getattr(C, 'MS_ENABLED', False) and _MS_AVAILABLE:
        ms_boost, ms_reason, _ = _ms_filter(
            k4h, sig.direction,
            ms_len=getattr(C, 'MS_LEN', 10),
        )
        if ms_boost != 0:
            sig.score = max(0.0, min(sig.score + ms_boost, 100.0))
            sig.tier  = score_to_tier(sig.score)
            filter_tags["market_structure"] = ms_reason
            diag["counts"][f"ms_{ms_boost:+.0f}"] += 1

    # ── 5g. OI + Funding Rate Cascade Signal ─────────────────────────────────
    if getattr(C, 'OI_CASCADE_ENABLED', False) and _OI_CASCADE_AVAILABLE:
        try:
            _oi_engine.update(symbol, oi_raw, fr,
                              k3m[-1][4] if k3m else 0.0, sig.atr)
            oi_boost, oi_reason, oi_block = _oi_engine.signal_for_direction(
                symbol, sig.direction)
            if oi_block:
                diag["counts"]["oi_cascade_block"] += 1
                return None
            if oi_boost != 0:
                sig.score = max(0.0, min(sig.score + oi_boost, 100.0))
                sig.tier  = score_to_tier(sig.score)
                filter_tags["oi_cascade"] = oi_reason
                diag["counts"][f"oi_cascade_{oi_boost:+.0f}"] += 1
        except Exception as e:
            log.debug("[%s] oi_cascade error: %s", symbol, e)

    # ── 5h. IBS Pullback SHORT ───────────────────────────────────────────────
    # Portado de Pine "10 Bar Low Pullback": nuevo mínimo N barras + IBS > 0.85
    # = sell the rip intrabarra en contexto bajista (below EMA).
    # Solo actúa en SHORT. Para LONG: neutral (no veta).
    # Activar con IBS_PULLBACK_ENABLED=true en Railway (default false).
    if getattr(C, 'IBS_PULLBACK_ENABLED', False):
        ibs_boost, ibs_reason, ibs_block = ibs_pullback_filter(
            k3m, sig.direction,
            lookback=getattr(C, 'IBS_LOOKBACK', 10),
            ibs_threshold=getattr(C, 'IBS_THRESHOLD', 0.85),
            ema_period=getattr(C, 'IBS_EMA_PERIOD', 50),
            use_ema_filter=getattr(C, 'IBS_USE_EMA', True),
            boost_amount=getattr(C, 'IBS_BOOST', 8.0),
        )
        if ibs_block:
            log.info("[%s] 🚫 IBS veto (encima EMA): %s", symbol, ibs_reason)
            diag["counts"]["ibs_veto"] += 1
            return None
        if ibs_boost > 0:
            sig.score = min(sig.score + ibs_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["ibs_boost"] += 1
            filter_tags["ibs_pullback"] = ibs_reason
            log.info("[%s] 📉 %s → score=%.1f", symbol, ibs_reason, sig.score)

    # ── 5i. BB Short — sobrecompra extrema ───────────────────────────────────
    # Portado de Pine "BB Short DCA Strategy": close > upper_BB × (1 + pct/100)
    # Para SHORT: boost. Para LONG: veto opcional (sobrecompra extrema).
    # DCA/pyramiding del Pine original NO implementado.
    # Activar con BB_SHORT_ENABLED=true en Railway (default false).
    if getattr(C, 'BB_SHORT_ENABLED', False):
        bb_boost, bb_reason, bb_block = bb_short_filter(
            k3m, sig.direction,
            bb_length=getattr(C, 'BB_SHORT_LENGTH', 20),
            bb_std=getattr(C, 'BB_SHORT_STD', 2.0),
            signal_above_pct=getattr(C, 'BB_SHORT_ABOVE_PCT', 1.0),
            boost_amount=getattr(C, 'BB_SHORT_BOOST', 10.0),
            veto_long=getattr(C, 'BB_SHORT_VETO_LONG', True),
        )
        if bb_block:
            log.info("[%s] 🚫 BB_short veto LONG sobrecompra: %s", symbol, bb_reason)
            diag["counts"]["bb_short_veto_long"] += 1
            return None
        if bb_boost > 0:
            sig.score = min(sig.score + bb_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["bb_short_boost"] += 1
            filter_tags["bb_short"] = bb_reason
            log.info("[%s] 📉 %s → score=%.1f", symbol, bb_reason, sig.score)

    # ── 6b. Auto-blacklist + Streak Breaker ──────────────────────────────────
    if journal:
        auto_bl, auto_bl_reason = journal.is_symbol_auto_blacklisted(symbol)
        if auto_bl:
            diag["counts"]["auto_blacklist"] += 1
            return None
        streak_paused, streak_reason = journal.is_streak_paused()
        if streak_paused:
            diag["counts"]["streak_breaker"] += 1
            return None

    # ── 7. Adaptive threshold ────────────────────────────────────────────────
    adaptive_offset = journal.get_adaptive_offset() if journal else 0.0
    effective_min   = C.MIN_SCORE + adaptive_offset

    _counter_penalty = getattr(C, 'COUNTER_TREND_PENALTY', 8.0)
    _htf_s = float(sig.htf_score) if hasattr(sig, 'htf_score') else 0.5
    if sig.direction == "LONG" and _htf_s < 0.45:
        effective_min += _counter_penalty
        diag["counts"]["counter_trend_penalty"] = diag["counts"].get("counter_trend_penalty", 0) + 1
    elif sig.direction == "SHORT" and _htf_s > 0.55:
        effective_min += _counter_penalty
        diag["counts"]["counter_trend_penalty"] = diag["counts"].get("counter_trend_penalty", 0) + 1

    if sig.score < effective_min:
        diag["counts"]["score_bajo"] += 1
        return None

    _min_conviction = getattr(C, 'MIN_CONVICTION', 5)
    if hasattr(sig, 'conviction') and sig.conviction < _min_conviction:
        diag["counts"]["conviction_bajo"] += 1
        return None

    if not risk.tier_ok(sig.tier):
        diag["counts"][f"tier_bajo({sig.tier})"] += 1
        return None

    diag["counts"]["signal_qualified"] += 1
    log.info("[%s] Señal %s tier=%s score=%.1f fr=%.4f obi=%.2f",
             symbol, sig.direction, sig.tier, sig.score, fr, obi)

    if C.MODE == "SIGNAL":
        await tg.notify_signal(sig)
        return sig

    # ── LIVE ─────────────────────────────────────────────────────────────────
    unrealized = await pos_mgr.get_unrealized_pnl()
    can, reason = await risk.can_trade(unrealized_pnl=unrealized)
    if not can:
        log.info("[%s] Bloqueado por risk: %s", symbol, reason)
        diag["counts"]["risk_blocked"] += 1
        return None

    trade_confirmed  = False
    dir_reserved     = False
    dir_token        = None
    btc_corr         = 0.0
    btc_reserved     = False
    btc_token        = None

    try:
        sym_ok, sym_reason = risk.symbol_allowed(symbol)
        if not sym_ok:
            diag["counts"]["symbol_blocked"] += 1
            return None

        dir_ok, dir_reason, dir_token = risk.direction_allowed(sig.direction)
        if not dir_ok:
            log.info("[%s] Bloqueado por correlación: %s", symbol, dir_reason)
            diag["counts"]["correlation_blocked"] += 1
            return None
        dir_reserved = True

        if btc_klines and getattr(C, 'BTC_CORR_ENABLED', True) and symbol != "BTC-USDT":
            btc_corr = compute_correlation(k3m, btc_klines)
            btc_guard.threshold  = getattr(C, 'BTC_CORR_THRESHOLD', 0.5)
            btc_guard.window_sec = getattr(C, 'BTC_CORR_WINDOW_SEC', 1800)
            btc_guard.max_same   = getattr(C, 'BTC_CORR_MAX_SAME', 3)
            btc_reserved = abs(btc_corr) >= btc_guard.threshold
            if btc_reserved:
                btc_ok, btc_reason, btc_token = btc_guard.allowed(sig.direction, btc_corr)
                if not btc_ok:
                    log.info("[%s] 🔗 %s", symbol, btc_reason)
                    diag["counts"]["btc_correlation_blocked"] += 1
                    btc_reserved = False
                    return None

        oi_delta = await _get_oi_delta(client, symbol)
        if getattr(C, 'OI_FILTER_ENABLED', False) and oi_delta < -0.05:
            diag["counts"]["oi_declining"] += 1
            return None
        if oi_delta > 0.02:
            sig.score = min(sig.score + 3, 100.0)
            sig.tier  = score_to_tier(sig.score)

        try:
            balance = await client.get_balance()
        except Exception as e:
            log.error("[%s] get_balance error: %s", symbol, e)
            return None

        if balance < 5.0:
            balance = C.CAPITAL

        qty = risk.kelly_position_size(balance, sig.entry, sig.sl, sig.score, sig.tier, symbol=symbol)
        if qty <= 0:
            return None

        log.info("[%s] qty=%.6f notional=%.2f USDT", symbol, qty, qty * sig.entry)
        await tg.notify_signal(sig)

        entry_resp = {}
        used_limit = False
        if getattr(C, 'LIMIT_ORDERS_ENABLED', False):
            lmt_resp = await client.place_limit_entry(
                symbol, sig.direction, qty, sig.entry,
                sl_price=sig.sl, tp1_price=sig.tp1, tp2_price=sig.tp2,
                timeout_s=getattr(C, 'LIMIT_TIMEOUT_SECS', 15),
            )
            if lmt_resp.get("code", -1) == 0:
                entry_resp = lmt_resp
                used_limit = True
                log.info("[%s] Entrada LÍMITE OK ✅", symbol)

        if not used_limit:
            try:
                results = await client.open_trade(
                    symbol=symbol, direction=sig.direction, quantity=qty,
                    sl_price=sig.sl, tp1_price=sig.tp1, tp2_price=sig.tp2,
                )
            except Exception as e:
                log.error("[%s] open_trade error: %s", symbol, e)
                return None
            entry_resp = results.get("entry", {})

        if entry_resp.get("code", -1) != 0:
            log.error("[%s] Entrada rechazada: %s", symbol, entry_resp)
            return None

        order_id = str(
            entry_resp.get("data", {}).get("order", {}).get("orderId", "unknown")
            or entry_resp.get("data", {}).get("orderId", "unknown")
        )

        trade = OpenTrade(
            symbol=symbol, direction=sig.direction,
            entry=sig.entry, sl=sig.sl, tp1=sig.tp1, tp2=sig.tp2,
            qty=qty, atr=sig.atr, order_id=order_id,
        )
        await pos_mgr.register_trade(trade)
        await tg.notify_trade_opened(sig, qty, order_id)
        trade_confirmed = True

        if journal:
            journal.on_open(
                symbol=symbol, direction=sig.direction, tier=sig.tier,
                score=sig.score, fr=fr, obi=obi, oi_delta=oi_delta,
                htf_score=sig.htf_score, adx=sig.adx,
                filter_tags=filter_tags,
            )

        return sig

    finally:
        if not trade_confirmed:
            await risk.release_reservation()
            if dir_reserved:
                risk.release_direction_reservation(sig.direction, dir_token)
            if btc_reserved:
                btc_guard.release(sig.direction, btc_corr, btc_token)


def _new_diag() -> dict:
    return {
        "counts": Counter(), "score_n": 0, "score_sum": 0.0,
        "score_max": 0.0, "score_max_symbol": "", "score_max_dir": "",
    }


async def _harvest_scan(
    symbols: list, client: BingXClient,
    risk: RiskManager, pos_mgr: PositionManager,
    diag: dict, journal=None,
):
    harvest_thr = getattr(C, 'HARVEST_FR_THR', 0.0010)
    if harvest_thr <= 0:
        return

    window = regime_engine._classify_window()
    if window not in (Window.PREFUND_MAX, Window.PREFUND_PREP):
        return

    htf = regime_engine.hours_to_next_funding()
    log.info("🌾 Harvest scan — ventana %s (%.1fh hasta funding) | %d símbolos",
             window, htf, len(symbols))

    candidates = []
    for symbol in symbols[:30]:
        if pos_mgr.is_trading(symbol):
            continue
        try:
            fr = await client.get_funding_rate(symbol)
        except Exception:
            continue
        is_harv, direction, yield_pct = regime_engine.is_harvest_opportunity(
            symbol, fr, harvest_thr
        )
        if is_harv:
            candidates.append((symbol, direction, fr, yield_pct))

    if not candidates:
        return

    candidates.sort(key=lambda x: x[3], reverse=True)
    symbol, direction, fr, yield_pct = candidates[0]

    unrealized = await pos_mgr.get_unrealized_pnl()
    can, reason = await risk.can_trade(unrealized_pnl=unrealized)
    if not can:
        return

    try:
        balance = await client.get_balance()
    except Exception:
        return
    if balance < 5:
        balance = C.CAPITAL

    try:
        k3m = await client.get_klines(symbol, C.TIMEFRAME, 50)
        if len(k3m) < 20:
            return
        import numpy as np
        highs  = np.array([c[2] for c in k3m[-20:]])
        lows   = np.array([c[3] for c in k3m[-20:]])
        closes = np.array([c[4] for c in k3m[-20:]])
        tr = np.maximum(highs - lows,
             np.maximum(abs(highs - np.roll(closes, 1)),
                        abs(lows  - np.roll(closes, 1))))
        atr   = float(np.mean(tr[1:]))
        price = float(k3m[-1][4])
    except Exception as e:
        log.debug("Harvest klines error: %s", e)
        return

    harvest_notional = getattr(C, 'MAX_NOTIONAL_USDT', 200) * 0.25
    qty = harvest_notional / price / C.LEVERAGE
    qty = client._round_qty(symbol, qty)
    if qty <= 0:
        return

    sl_mult = 1.0
    if direction == "LONG":
        sl_price  = price - atr * sl_mult
        tp1_price = price + atr * 1.0
        tp2_price = price + atr * 2.0
    else:
        sl_price  = price + atr * sl_mult
        tp1_price = price - atr * 1.0
        tp2_price = price - atr * 2.0

    log.info("🌾 HARVEST %s %s @ %.6f | yield=%.3f%%/8h", symbol, direction, price, yield_pct*100)
    await tg.notify_harvest_opportunity(symbol, direction, fr, yield_pct, htf)

    try:
        results = await client.open_trade(
            symbol=symbol, direction=direction, quantity=qty,
            sl_price=sl_price, tp1_price=tp1_price, tp2_price=tp2_price,
        )
    except Exception as e:
        log.error("Harvest open_trade error: %s", e)
        return

    entry_resp = results.get("entry", {})
    if entry_resp.get("code", -1) != 0:
        return

    order_id = str(
        entry_resp.get("data", {}).get("order", {}).get("orderId", "harvest") or "harvest"
    )
    trade = OpenTrade(
        symbol=symbol, direction=direction,
        entry=price, sl=sl_price, tp1=tp1_price, tp2=tp2_price,
        qty=qty, atr=atr, order_id=order_id,
    )
    await pos_mgr.register_trade(trade)
    if journal:
        journal.on_open(symbol=symbol, direction=direction, tier="HARVEST",
                        score=90.0, fr=fr, obi=0.0, oi_delta=0.0)
    diag["counts"]["harvest_opened"] += 1


_current_symbols: list[str] = []


def get_current_symbols() -> list[str]:
    return list(_current_symbols)


async def scan_loop(client, risk, pos_mgr, complement=None, journal=None):
    log.info("Scanner v7.7 SHORT-ONLY | Modo=%s | SHORT_ONLY=%s | LATERAL_ADX_MAX=%s | Interval=%ds",
             C.MODE,
             getattr(C, 'SHORT_ONLY', False),
             getattr(C, 'LATERAL_ADX_MAX', 0.0),
             C.SCAN_INTERVAL)
    symbols:   list[str] = []
    iteration: int       = 0

    while True:
        start = time.time()
        iteration += 1
        diag = _new_diag()

        if iteration == 1 or iteration % 10 == 0 or not symbols:
            try:
                all_syms = await client.get_all_symbols()
                if all_syms:
                    if complement and complement.get_exclusive_symbols():
                        symbols = complement.get_exclusive_symbols()
                        log.info("Modo EXCLUSIVO: %d símbolos", len(symbols))
                    else:
                        symbols = all_syms
                        log.info("Símbolos activos: %d", len(symbols))
                    _current_symbols[:] = symbols
                else:
                    log.warning("get_all_symbols vacío (iter=%d)", iteration)
            except Exception as e:
                log.error("get_all_symbols error: %s", e)
                if not symbols:
                    await asyncio.sleep(30)
                    continue

        if not symbols:
            await asyncio.sleep(10)
            continue

        if iteration % 20 == 0:
            try:
                balance    = await client.get_balance()
                unrealized = await pos_mgr.get_unrealized_pnl()
                await tg.notify_status(risk.status(unrealized_pnl=unrealized), balance, len(symbols))
            except Exception:
                pass

        btc_klines = None
        if getattr(C, 'BTC_CORR_ENABLED', True):
            try:
                btc_klines = await client.get_klines("BTC-USDT", C.TIMEFRAME, 80)
            except Exception as e:
                log.debug("BTC klines fetch error: %s", e)

        BATCH = 20
        signals_found = 0
        for i in range(0, len(symbols), BATCH):
            batch   = symbols[i:i+BATCH]
            results = await asyncio.gather(
                *[_process_symbol(s, client, risk, pos_mgr, diag, journal, btc_klines)
                  for s in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Signal) and r.direction != "NONE":
                    signals_found += 1
            await asyncio.sleep(0.2)

        elapsed = time.time() - start

        top5    = diag["counts"].most_common(5)
        avg_sc  = diag["score_sum"] / diag["score_n"] if diag["score_n"] else 0.0
        top_str = " | ".join(f"{k}={v}" for k, v in top5) if top5 else "—"

        adaptive_str = ""
        if journal and journal.get_adaptive_offset() != 0.0:
            adaptive_str = f" | adaptive_offset={journal.get_adaptive_offset():+.0f}"

        log.info(
            "Iter %d | %d símbolos | %d señales | %.1fs | "
            "direccionales=%d avg_score=%.1f max_score=%.1f(%s %s)%s | %s",
            iteration, len(symbols), signals_found, elapsed,
            diag["score_n"], avg_sc, diag["score_max"],
            diag["score_max_symbol"], diag["score_max_dir"],
            adaptive_str, top_str,
        )

        if iteration % 5 == 0 and signals_found == 0:
            try:
                await tg.notify_diagnostics(
                    iteration, len(symbols), diag["score_n"], avg_sc,
                    diag["score_max"], diag["score_max_symbol"], diag["score_max_dir"],
                    top5,
                )
            except Exception:
                pass

        if journal and iteration % 50 == 0 and journal.total_closed() > 0:
            try:
                await tg.notify_journal_report(journal.stats())
            except Exception:
                pass

        if iteration % 8 == 0 and C.MODE == "LIVE":
            await _harvest_scan(symbols[:50], client, risk, pos_mgr, diag, journal)

        await asyncio.sleep(max(0.0, C.SCAN_INTERVAL - elapsed))
