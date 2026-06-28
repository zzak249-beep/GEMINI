"""
QF×JP Bot — Scanner JOYFUL-ART v7.7 + MEJORAS v6
═══════════════════════════════════════════════════════════════════════════════
BASE: Scanner SHORT-only v7.7 con filtros IBS, BB Short, EMA9×VWAP,
      OI Divergence, FR Spike, Volume Exhaustion

NUEVAS MEJORAS (portadas de EMA9×VWAP Bot v6):
  ✅ 5n. RSI 15m — confirmación cross-timeframe (más fiable que RSI 3m)
  ✅ 5o. EMA55 1H — contexto macro bajista/alcista como boost
  ✅ Session filter 7-21 UTC por defecto (sesión asiática 60% falsos)
  ✅ VWAP Slope check — detecta VWAP plana y reduce peso de señal
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
from ema9_vwap_filter import ema9_vwap_filter
from oi_divergence_filter import oi_div_engine
from fr_spike_filter import fr_spike_engine
from volume_exhaustion_filter import volume_exhaustion_filter

try:
    from market_structure import ms_filter as _ms_filter
    _MS_AVAILABLE = True
except ImportError:
    _MS_AVAILABLE = False

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

# Cache RSI 15m para no refetchear en cada iteración
_rsi15m_cache: dict[str, tuple[float, float]] = {}
RSI15M_CACHE_TTL = 120  # 2 min


# ── Helpers de indicadores ────────────────────────────────────────────────────

def _ema_simple(values: list, period: int) -> list:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out

def _rma_simple(values: list, period: int) -> list:
    if not values:
        return []
    k = 1.0 / period
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out

def _rsi_simple(klines: list, period: int = 14) -> float:
    """RSI simple sobre klines. Retorna valor [-1] o 50 si datos insuficientes."""
    if len(klines) < period + 2:
        return 50.0
    closes = [k[4] for k in klines]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = _rma_simple(gains, period)
    al = _rma_simple(losses, period)
    if not ag or not al or al[-1] == 0:
        return 100.0 if (al[-1] == 0) else 50.0
    rs = ag[-1] / al[-1]
    return round(100.0 - 100.0 / (1.0 + rs), 1)

def _ema55_1h(klines_1h: list) -> float:
    """EMA55 sobre 1H. Retorna 0 si datos insuficientes."""
    if len(klines_1h) < 57:
        return 0.0
    closes = [k[4] for k in klines_1h]
    ema = _ema_simple(closes, 55)
    return ema[-1] if ema else 0.0

def _vwap_slope_pct(klines: list, bars: int = 5) -> float:
    """Pendiente de VWAP en % sobre últimas `bars` velas."""
    if len(klines) < bars + 2:
        return 1.0
    def _vwap_val(klines_slice):
        pv = sum(((k[2]+k[3]+k[4])/3)*k[5] for k in klines_slice)
        vol = sum(k[5] for k in klines_slice)
        return pv / vol if vol > 0 else 0
    v_now  = _vwap_val(klines)
    v_prev = _vwap_val(klines[:-bars])
    if v_prev <= 0:
        return 1.0
    return abs((v_now - v_prev) / v_prev * 100)


# ── Funciones de fetching ─────────────────────────────────────────────────────

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
    start = getattr(C, 'TRADE_START_UTC', 7)   # default 7 = London open
    end   = getattr(C, 'TRADE_END_UTC',   21)  # default 21 = NY close
    if start == 0 and end == 24:
        return True
    h = datetime.datetime.utcnow().hour
    if start < end:
        return start <= h < end
    return h >= start or h < end


def _fr_boost_block(fr: float, direction: str) -> tuple[float, bool]:
    thr = getattr(C, 'FR_EXTREME_THR', 0.0005)
    if thr <= 0:
        return 0.0, False
    if fr > thr:
        if direction == "LONG":  return 0.0, True
        if direction == "SHORT": return 8.0, False
    if fr < -thr:
        if direction == "SHORT": return 0.0, True
        if direction == "LONG":  return 8.0, False
    return 0.0, False


# ── Proceso por símbolo ───────────────────────────────────────────────────────

async def _process_symbol(
    symbol, client, risk, pos_mgr, diag: dict,
    journal=None, btc_klines: list = None,
) -> Optional[Signal]:

    if pos_mgr.is_trading(symbol):
        diag["counts"]["already_trading"] += 1
        return None

    # ── Session filter ───────────────────────────────────────────────────────
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
    if getattr(C, 'SHORT_ONLY', False) and sig.direction == "LONG":
        diag["counts"]["long_blocked"] += 1
        return None

    # ── Lateral market filter ────────────────────────────────────────────────
    _lat_max = getattr(C, 'LATERAL_ADX_MAX', 0.0)
    if _lat_max > 0 and sig.adx > _lat_max:
        diag["counts"]["trending_skip"] += 1
        return None

    # ── Volatility Regime ────────────────────────────────────────────────────
    vol_sig = vol_engine.update(symbol, sig.atr, sig.entry)
    if getattr(C, 'VOL_REGIME_ENABLED', True):
        if vol_sig.block_entry:
            diag["counts"]["vol_extreme_block"] += 1
            return None
        if vol_sig.regime != VolRegime.NORMAL:
            sl_dist  = abs(sig.entry - sig.sl)    * vol_sig.sl_mult
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
        diag["score_max"]        = sig.score
        diag["score_max_symbol"] = symbol
        diag["score_max_dir"]    = sig.direction

    # OBI boost
    if abs(obi) > 0.1:
        boost = 0.0
        if sig.direction == "SHORT" and obi < -0.1: boost = abs(obi) * 5
        elif sig.direction == "LONG" and obi > 0.1: boost = obi * 5
        if boost > 0:
            sig.score = min(sig.score + boost, 100.0)
            sig.tier  = score_to_tier(sig.score)

    # ── Funding Regime ───────────────────────────────────────────────────────
    regime_sig   = regime_engine.update(symbol, fr)
    regime_boost = (regime_sig.short_boost if sig.direction == "SHORT"
                    else regime_sig.long_boost)
    if regime_boost != 0:
        sig.score = max(0.0, min(sig.score + regime_boost, 100.0))
        sig.tier  = score_to_tier(sig.score)
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

    # ── Slope Multi-Timeframe ────────────────────────────────────────────────
    slope_adj, slope_block = 0.0, False
    if getattr(C, 'SLOPE_FILTER_ENABLED', True):
        slope_adj, slope_reason, slope_block = multi_tf_slope_alignment(
            k15m, k1h, k4h, sig.direction
        )
        if slope_block:
            diag["counts"]["slope_block"] += 1
            return None
        if slope_adj != 0:
            sig.score = max(0.0, min(sig.score + slope_adj, 100.0))
            sig.tier  = score_to_tier(sig.score)

    filter_tags: dict = {}

    # ── 5b. STC + Asimetría ──────────────────────────────────────────────────
    if getattr(C, 'STC_ASYM_ENABLED', False):
        try: k1m = await client.get_klines(symbol, "1m", 100)
        except Exception: k1m = []
        if len(k1m) >= 60:
            stc_boost, stc_reason, stc_block = stc_asymmetry_filter(
                k1m, sig.direction,
                stc_length=getattr(C,'STC_LENGTH',10),
                stc_fast=getattr(C,'STC_FAST',23),
                stc_slow=getattr(C,'STC_SLOW',50),
                stc_factor=getattr(C,'STC_FACTOR',0.5),
                stc_oversold=getattr(C,'STC_OVERSOLD',25.0),
                stc_overbought=getattr(C,'STC_OVERBOUGHT',75.0),
                asym_window=getattr(C,'ASYM_WINDOW',20),
                asym_veto_threshold=getattr(C,'ASYM_VETO_THRESHOLD',1.5),
                asym_boost_per_x=getattr(C,'ASYM_BOOST_PER_X',3.0),
                asym_boost_max=getattr(C,'ASYM_BOOST_MAX',12.0),
            )
            if stc_block:
                diag["counts"]["stc_asym_veto"] += 1
                return None
            if stc_boost > 0:
                sig.score = min(sig.score + stc_boost, 100.0)
                sig.tier  = score_to_tier(sig.score)
                diag["counts"]["stc_asym_boost"] += 1
                filter_tags["stc_asym"] = stc_reason

    # ── 5d. Price Action Framework ───────────────────────────────────────────
    if getattr(C, 'PRICE_ACTION_ENABLED', False):
        pa_boost, pa_reason, pa_block = price_action_filter(
            k3m, sig.direction,
            lookback=getattr(C,'PA_LOOKBACK',20),
            body_mult=getattr(C,'PA_BODY_MULT',2.0),
            wick_mult=getattr(C,'PA_WICK_MULT',1.5),
            touch_tol_pct=getattr(C,'PA_TOUCH_TOL_PCT',0.1),
            min_touches=getattr(C,'PA_MIN_TOUCHES',3),
            boost_amount=getattr(C,'PA_BOOST_AMOUNT',6.0),
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
            cci_len=getattr(C,'TMR_CCI_LEN',20),
            atr_len=getattr(C,'TMR_ATR_LEN',5),
            atr_mult=getattr(C,'TMR_ATR_MULT',1.0),
            rmi_len=getattr(C,'TMR_RMI_LEN',14),
            pmom=getattr(C,'TMR_PMOM',66.0),
            nmom=getattr(C,'TMR_NMOM',30.0),
            boost_amount=getattr(C,'TMR_BOOST_AMOUNT',7.0),
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
            k4h, sig.direction, ms_len=getattr(C,'MS_LEN',10))
        if ms_boost != 0:
            sig.score = max(0.0, min(sig.score + ms_boost, 100.0))
            sig.tier  = score_to_tier(sig.score)
            filter_tags["market_structure"] = ms_reason
            diag["counts"][f"ms_{ms_boost:+.0f}"] += 1

    # ── 5g. OI + Funding Cascade ─────────────────────────────────────────────
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
        except Exception as e:
            log.debug("[%s] oi_cascade: %s", symbol, e)

    # ── 5h. IBS Pullback SHORT ───────────────────────────────────────────────
    if getattr(C, 'IBS_PULLBACK_ENABLED', False):
        ibs_boost, ibs_reason, ibs_block = ibs_pullback_filter(
            k3m, sig.direction,
            lookback=getattr(C,'IBS_LOOKBACK',10),
            ibs_threshold=getattr(C,'IBS_THRESHOLD',0.85),
            ema_period=getattr(C,'IBS_EMA_PERIOD',50),
            use_ema_filter=getattr(C,'IBS_USE_EMA',True),
            boost_amount=getattr(C,'IBS_BOOST',8.0),
        )
        if ibs_block:
            diag["counts"]["ibs_veto"] += 1
            return None
        if ibs_boost > 0:
            sig.score = min(sig.score + ibs_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["ibs_boost"] += 1
            filter_tags["ibs_pullback"] = ibs_reason
            log.info("[%s] 📉 IBS: %s → %.1f", symbol, ibs_reason, sig.score)

    # ── 5i. BB Short ─────────────────────────────────────────────────────────
    if getattr(C, 'BB_SHORT_ENABLED', False):
        bb_boost, bb_reason, bb_block = bb_short_filter(
            k3m, sig.direction,
            bb_length=getattr(C,'BB_SHORT_LENGTH',20),
            bb_std=getattr(C,'BB_SHORT_STD',2.0),
            signal_above_pct=getattr(C,'BB_SHORT_ABOVE_PCT',1.0),
            boost_amount=getattr(C,'BB_SHORT_BOOST',10.0),
            veto_long=getattr(C,'BB_SHORT_VETO_LONG',True),
        )
        if bb_block:
            diag["counts"]["bb_short_veto_long"] += 1
            return None
        if bb_boost > 0:
            sig.score = min(sig.score + bb_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["bb_short_boost"] += 1
            filter_tags["bb_short"] = bb_reason

    # ── 5j. EMA9×VWAP crossunder ─────────────────────────────────────────────
    if getattr(C, 'EMA9_VWAP_ENABLED', False):
        ev_boost, ev_reason, ev_block = ema9_vwap_filter(
            k3m, sig.direction,
            lookback=getattr(C,'EMA9_VWAP_LOOKBACK',5),
            boost_amount=getattr(C,'EMA9_VWAP_BOOST',9.0),
            strict=getattr(C,'EMA9_VWAP_STRICT',False),
            veto_long=getattr(C,'EMA9_VWAP_VETO_LONG',True),
        )
        if ev_block:
            diag["counts"]["ema9_vwap_veto_long"] += 1
            return None
        if ev_boost > 0:
            sig.score = min(sig.score + ev_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["ema9_vwap_boost"] += 1
            filter_tags["ema9_vwap"] = ev_reason

    # ── 5k. OI Divergence ────────────────────────────────────────────────────
    if getattr(C, 'OI_DIV_ENABLED', False) and oi_raw > 0:
        oi_div_engine.update(symbol, sig.entry, oi_raw)
        oid_boost, oid_reason, oid_block = oi_div_engine.signal(
            symbol, sig.direction,
            min_price_rise_pct=getattr(C,'OI_DIV_MIN_PRICE_RISE_PCT',1.0),
            min_oi_fall_pct=getattr(C,'OI_DIV_MIN_OI_FALL_PCT',2.0),
            lookback=getattr(C,'OI_DIV_LOOKBACK',5),
            boost_amount=getattr(C,'OI_DIV_BOOST',9.0),
            veto_long=getattr(C,'OI_DIV_VETO_LONG',True),
        )
        if oid_block:
            diag["counts"]["oi_div_veto_long"] += 1
            return None
        if oid_boost > 0:
            sig.score = min(sig.score + oid_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["oi_div_boost"] += 1
            filter_tags["oi_divergence"] = oid_reason

    # ── 5l. FR Spike ─────────────────────────────────────────────────────────
    if getattr(C, 'FR_SPIKE_ENABLED', False):
        fr_spike_engine.update(symbol, fr)
        frs_boost, frs_reason, frs_block = fr_spike_engine.signal(
            symbol, sig.direction, fr,
            spike_mult=getattr(C,'FR_SPIKE_MULT',2.5),
            min_fr_abs=getattr(C,'FR_SPIKE_MIN_ABS',0.0003),
            lookback=getattr(C,'FR_SPIKE_LOOKBACK',8),
            boost_amount=getattr(C,'FR_SPIKE_BOOST',8.0),
        )
        if frs_block:
            diag["counts"]["fr_spike_veto_long"] += 1
            return None
        if frs_boost > 0:
            sig.score = min(sig.score + frs_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["fr_spike_boost"] += 1
            filter_tags["fr_spike"] = frs_reason

    # ── 5m. Volume Exhaustion ────────────────────────────────────────────────
    if getattr(C, 'VOL_EXHAUST_ENABLED', False):
        ve_boost, ve_reason, _ = volume_exhaustion_filter(
            k3m, sig.direction,
            lookback=getattr(C,'VOL_EXHAUST_LOOKBACK',12),
            min_up_bars=getattr(C,'VOL_EXHAUST_MIN_UP_BARS',3),
            vol_slope_threshold=getattr(C,'VOL_EXHAUST_SLOPE_THR',-0.05),
            boost_amount=getattr(C,'VOL_EXHAUST_BOOST',8.0),
        )
        if ve_boost > 0:
            sig.score = min(sig.score + ve_boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            diag["counts"]["vol_exhaust_boost"] += 1
            filter_tags["vol_exhaustion"] = ve_reason

    # ── 5n. RSI 15m — confirmación cross-timeframe (NUEVO v6) ────────────────
    # RSI de 15m es más fiable que el de 3m: menos ruido, más contexto.
    # SHORT: RSI15m < 50 confirma contexto bajista en timeframe superior.
    # Si RSI15m muy bajo (<40) → boost adicional.
    # Si RSI15m muy alto (>65) en SHORT → señal de calidad dudosa, penalizar.
    if getattr(C, 'RSI15M_FILTER_ENABLED', True) and len(k15m) >= 16:
        now_ts = time.time()
        cached_rsi = _rsi15m_cache.get(symbol)
        if cached_rsi and (now_ts - cached_rsi[1]) < RSI15M_CACHE_TTL:
            rsi15m_val = cached_rsi[0]
        else:
            rsi15m_val = _rsi_simple(k15m, 14)
            _rsi15m_cache[symbol] = (rsi15m_val, now_ts)

        rsi15m_max_short = getattr(C, 'RSI15M_SHORT_MAX', 60.0)
        rsi15m_min_long  = getattr(C, 'RSI15M_LONG_MIN',  40.0)
        rsi15m_req       = getattr(C, 'RSI15M_REQUIRED',  False)

        if sig.direction == "SHORT":
            if rsi15m_val < 40:
                sig.score = min(sig.score + 6, 100.0)
                sig.tier  = score_to_tier(sig.score)
                filter_tags["rsi15m"] = f"rsi15m={rsi15m_val:.1f}(fuerte_bajista)"
                diag["counts"]["rsi15m_boost"] += 1
            elif rsi15m_val > rsi15m_max_short:
                if rsi15m_req:
                    diag["counts"][f"rsi15m_fail({rsi15m_val:.0f})"] += 1
                    return None
                sig.score = max(0.0, sig.score - 5)
                sig.tier  = score_to_tier(sig.score)
                diag["counts"]["rsi15m_penalizado"] += 1
        elif sig.direction == "LONG":
            if rsi15m_val > 60:
                sig.score = min(sig.score + 6, 100.0)
                sig.tier  = score_to_tier(sig.score)
                filter_tags["rsi15m"] = f"rsi15m={rsi15m_val:.1f}(fuerte_alcista)"
                diag["counts"]["rsi15m_boost"] += 1
            elif rsi15m_val < rsi15m_min_long:
                if rsi15m_req:
                    diag["counts"][f"rsi15m_fail({rsi15m_val:.0f})"] += 1
                    return None
                sig.score = max(0.0, sig.score - 5)
                sig.tier  = score_to_tier(sig.score)

        log.debug("[%s] RSI15m=%.1f dir=%s", symbol, rsi15m_val, sig.direction)

    # ── 5o. EMA55 1H — contexto macro (NUEVO v6) ─────────────────────────────
    # Si precio < EMA55 en 1H → contexto bajista macro → boost SHORT +8
    # Si precio > EMA55 en 1H → contexto alcista macro → boost LONG +8
    # No bloquea, solo añade convicción cuando el macro acompaña.
    if getattr(C, 'EMA55_BOOST_ENABLED', True):
        ema55_val = _ema55_1h(k1h)
        if ema55_val > 0:
            curr_price = sig.entry
            if sig.direction == "SHORT" and curr_price < ema55_val:
                sig.score = min(sig.score + 8, 100.0)
                sig.tier  = score_to_tier(sig.score)
                filter_tags["ema55_1h"] = f"below_ema55_1h={ema55_val:.6f}"
                diag["counts"]["ema55_boost"] += 1
                log.debug("[%s] EMA55 boost SHORT (precio %.6f < ema55 %.6f)",
                          symbol, curr_price, ema55_val)
            elif sig.direction == "LONG" and curr_price > ema55_val:
                sig.score = min(sig.score + 8, 100.0)
                sig.tier  = score_to_tier(sig.score)
                filter_tags["ema55_1h"] = f"above_ema55_1h={ema55_val:.6f}"
                diag["counts"]["ema55_boost"] += 1

    # ── 5p. VWAP Slope — boost si VWAP inclinada (NUEVO v6) ──────────────────
    # Para joyful-art (lateral) no filtramos VWAP plana, pero sí boosteamos
    # cuando la VWAP está más inclinada (señal más fuerte).
    # Si slope > 0.02% → la tendencia es real, no solo ruido.
    vwap_slope_pct = _vwap_slope_pct(k3m, bars=getattr(C,'VWAP_SLOPE_BARS',5))
    vwap_boost_thr = getattr(C, 'VWAP_SLOPE_BOOST_THR', 0.02)
    if vwap_slope_pct > vwap_boost_thr:
        sig.score = min(sig.score + 4, 100.0)
        sig.tier  = score_to_tier(sig.score)
        filter_tags["vwap_slope"] = f"slope={vwap_slope_pct:.3f}%"
        diag["counts"]["vwap_slope_boost"] += 1

    # ── Auto-blacklist + Streak Breaker ──────────────────────────────────────
    if journal:
        auto_bl, _ = journal.is_symbol_auto_blacklisted(symbol)
        if auto_bl:
            diag["counts"]["auto_blacklist"] += 1
            return None
        streak_paused, _ = journal.is_streak_paused()
        if streak_paused:
            diag["counts"]["streak_breaker"] += 1
            return None

    # ── Adaptive threshold ────────────────────────────────────────────────────
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

    trade_confirmed = False
    dir_reserved    = False
    dir_token       = None
    btc_corr        = 0.0
    btc_reserved    = False
    btc_token       = None

    try:
        sym_ok, sym_reason = risk.symbol_allowed(symbol)
        if not sym_ok:
            diag["counts"]["symbol_blocked"] += 1
            return None

        dir_ok, dir_reason, dir_token = risk.direction_allowed(sig.direction)
        if not dir_ok:
            log.info("[%s] Bloqueado correlación: %s", symbol, dir_reason)
            diag["counts"]["correlation_blocked"] += 1
            return None
        dir_reserved = True

        if btc_klines and getattr(C,'BTC_CORR_ENABLED',True) and symbol != "BTC-USDT":
            btc_corr = compute_correlation(k3m, btc_klines)
            btc_guard.threshold  = getattr(C,'BTC_CORR_THRESHOLD',0.5)
            btc_guard.window_sec = getattr(C,'BTC_CORR_WINDOW_SEC',1800)
            btc_guard.max_same   = getattr(C,'BTC_CORR_MAX_SAME',3)
            btc_reserved = abs(btc_corr) >= btc_guard.threshold
            if btc_reserved:
                btc_ok, btc_reason, btc_token = btc_guard.allowed(sig.direction, btc_corr)
                if not btc_ok:
                    diag["counts"]["btc_correlation_blocked"] += 1
                    btc_reserved = False
                    return None

        oi_delta = await _get_oi_delta(client, symbol)
        if getattr(C,'OI_FILTER_ENABLED',False) and oi_delta < -0.05:
            diag["counts"]["oi_declining"] += 1
            return None
        if oi_delta > 0.02:
            sig.score = min(sig.score + 3, 100.0)
            sig.tier  = score_to_tier(sig.score)

        try:
            balance = await client.get_balance()
        except Exception as e:
            log.error("[%s] get_balance: %s", symbol, e)
            return None
        if balance < 5.0: balance = C.CAPITAL

        qty = risk.kelly_position_size(balance, sig.entry, sig.sl, sig.score, sig.tier, symbol=symbol)
        if qty <= 0:
            return None

        await tg.notify_signal(sig)

        entry_resp = {}; used_limit = False
        if getattr(C,'LIMIT_ORDERS_ENABLED',False):
            lmt_resp = await client.place_limit_entry(
                symbol, sig.direction, qty, sig.entry,
                sl_price=sig.sl, tp1_price=sig.tp1, tp2_price=sig.tp2,
                timeout_s=getattr(C,'LIMIT_TIMEOUT_SECS',15),
            )
            if lmt_resp.get("code",-1) == 0:
                entry_resp = lmt_resp; used_limit = True

        if not used_limit:
            try:
                results = await client.open_trade(
                    symbol=symbol, direction=sig.direction, quantity=qty,
                    sl_price=sig.sl, tp1_price=sig.tp1, tp2_price=sig.tp2,
                )
            except Exception as e:
                log.error("[%s] open_trade: %s", symbol, e)
                return None
            entry_resp = results.get("entry", {})

        if entry_resp.get("code",-1) != 0:
            log.error("[%s] Entrada rechazada: %s", symbol, entry_resp)
            return None

        order_id = str(
            entry_resp.get("data",{}).get("order",{}).get("orderId","unknown")
            or entry_resp.get("data",{}).get("orderId","unknown")
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


async def scan_loop(client, risk, pos_mgr, complement=None, journal=None):
    log.info(
        "Scanner JOYFUL-ART v7.7+v6 | Modo=%s | SHORT_ONLY=%s | "
        "Session=%d-%dUTC | RSI15m=%s | EMA55=%s | VWAP_slope_boost=%s | "
        "IBS=%s BB=%s EMA9V=%s",
        C.MODE,
        getattr(C,'SHORT_ONLY',False),
        getattr(C,'TRADE_START_UTC',7), getattr(C,'TRADE_END_UTC',21),
        getattr(C,'RSI15M_FILTER_ENABLED',True),
        getattr(C,'EMA55_BOOST_ENABLED',True),
        getattr(C,'VWAP_SLOPE_BOOST_THR',0.02),
        getattr(C,'IBS_PULLBACK_ENABLED',False),
        getattr(C,'BB_SHORT_ENABLED',False),
        getattr(C,'EMA9_VWAP_ENABLED',False),
    )

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
                await tg.notify_status(
                    risk.status(unrealized_pnl=unrealized), balance, len(symbols))
            except Exception:
                pass

        btc_klines = None
        if getattr(C,'BTC_CORR_ENABLED',True):
            try:
                btc_klines = await client.get_klines("BTC-USDT", C.TIMEFRAME, 80)
            except Exception:
                pass

        BATCH = 20
        signals_found = 0
        for i in range(0, len(symbols), BATCH):
            batch = symbols[i:i+BATCH]
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
        top_str = " | ".join(f"{k}={v}" for k,v in top5) if top5 else "—"

        log.info(
            "Iter %d | %d símbolos | %d señales | %.1fs | "
            "direccionales=%d avg=%.1f max=%.1f(%s %s) | %s",
            iteration, len(symbols), signals_found, elapsed,
            diag["score_n"], avg_sc, diag["score_max"],
            diag["score_max_symbol"], diag["score_max_dir"], top_str,
        )

        if iteration % 5 == 0 and signals_found == 0:
            try:
                await tg.notify_diagnostics(
                    iteration, len(symbols), diag["score_n"], avg_sc,
                    diag["score_max"], diag["score_max_symbol"],
                    diag["score_max_dir"], top5,
                )
            except Exception:
                pass

        if iteration % 8 == 0 and C.MODE == "LIVE":
            try:
                from scanner import _harvest_scan
                await _harvest_scan(symbols[:50], client, risk, pos_mgr, diag, journal)
            except Exception:
                pass

        await asyncio.sleep(max(0.0, C.SCAN_INTERVAL - elapsed))
