"""
bot/strategy.py
═══════════════════════════════════════════════════════════════
NEXUS v1.0 — Motor de señales de 6 capas independientes

ARQUITECTURA DE SEÑAL:
  Capa 1 — SAMA Adaptativa        → Filtro de tendencia (20 pts)
  Capa 2 — Markov + ADX Régimen   → Probabilidad estadística (20 pts)
  Capa 3 — CVD Sintético          → Presión institucional (15 pts)
  Capa 4 — Liquidity Sweep ★      → Caza de stops detectada (20 pts)
  Capa 5 — Funding Rate           → Temperatura del mercado (10 pts)
  Capa 6 — Kotegawa (LONG) / STC  → Confirmación reversión/tendencia (15 pts)

VETOS AUTOMÁTICOS (anulan la señal):
  ✗ Divergencia CVD-Precio (precio nuevo extremo sin confirmación volumen)
  ✗ ADX en rango y RVOL < mínimo (mercado dormido)
  ✗ Señal contraria en timeframe superior (implícito por SAMA lenta)

Mínimo para entrada: 55 puntos (≥3 capas convergiendo)
═══════════════════════════════════════════════════════════════
"""
import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import Config
from bot import indicators as ind
from bot.markov import MarkovEngine

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    symbol:          str
    long:            bool  = False
    short:           bool  = False
    vetoed:          bool  = False
    veto_reason:     str   = ""

    # Capas
    sama_slope:      float = 0.0
    adaptive_thr:    float = 0.0
    sama_bullish:    bool  = False
    sama_bearish:    bool  = False

    prob_bull:       float = 0.0
    prob_bear:       float = 0.0
    adx:             float = 0.0
    regime:          str   = "UNKNOWN"

    cvd_delta:       float = 0.0
    cvd_bull:        bool  = False
    cvd_bear:        bool  = False
    bull_div:        bool  = False
    bear_div:        bool  = False

    sweep_long:      bool  = False
    sweep_short:     bool  = False
    sweep_str:       float = 0.0

    funding_rate:    float = 0.0
    funding_bull:    bool  = False
    funding_bear:    bool  = False

    kotegawa_bull:   bool  = False
    stc_val:         float = 50.0
    rvol:            float = 0.0
    vwap:            float = 0.0
    poc:             float = 0.0
    rsi_val:         float = 50.0
    pct_below_ma:    float = 0.0

    entry_price:     float = 0.0
    atr14:           float = 0.0
    score:           float = 0.0
    score_long:      float = 0.0
    score_short:     float = 0.0
    reasons:         list  = field(default_factory=list)


class NexusStrategy:
    """
    Genera señales de entrada para BingX perpetual futures.
    Mantiene estado persistente (Markov + Kotegawa pending) por símbolo.
    """

    def __init__(self, config: Config):
        self.cfg = config
        self._markov:            dict[str, MarkovEngine] = {}
        self._kotegawa_pending:  dict[str, bool]         = {}

    def _get_markov(self, symbol: str) -> MarkovEngine:
        if symbol not in self._markov:
            self._markov[symbol] = MarkovEngine(self.cfg.LOOKBACK_MARKOV)
        return self._markov[symbol]

    # ─────────────────────────────────────────────────────────
    # ENTRADA PÚBLICA
    # ─────────────────────────────────────────────────────────

    def analyze(self, df: pd.DataFrame, symbol: str,
                funding_rate: float = 0.0) -> SignalResult:
        """
        df: open, high, low, close, volume — DatetimeIndex — min 250 filas.
        funding_rate: tasa de financiación actual del par.
        """
        result = SignalResult(symbol=symbol)
        if len(df) < 120:
            logger.warning(f"{symbol}: datos insuficientes ({len(df)} velas)")
            return result

        try:
            result = self._layer_sama(df, symbol, result)
            result = self._layer_markov(df, symbol, result)
            result = self._layer_cvd(df, result)
            result = self._layer_sweeps(df, result)
            result = self._layer_funding(funding_rate, result)
            result = self._layer_kotegawa_stc(df, symbol, result)
            result = self._apply_vetos(result)
            result = self._combine(result)
        except Exception as e:
            logger.error(f"{symbol} strategy error: {e}", exc_info=True)

        return result

    # ─────────────────────────────────────────────────────────
    # CAPA 1 — SAMA + ADX Régimen
    # ─────────────────────────────────────────────────────────

    def _layer_sama(self, df: pd.DataFrame, symbol: str,
                    r: SignalResult) -> SignalResult:
        cfg = self.cfg

        sama_s  = ind.sama(df["close"], df["high"], df["low"],
                           cfg.SAMA_LENGTH, cfg.SAMA_MAJ_LENGTH, cfg.SAMA_MIN_LENGTH)
        slope_s = ind.sama_slope(sama_s, df["close"], df["high"], df["low"],
                                  cfg.SLOPE_PERIOD, cfg.SLOPE_RANGE)
        _, _, adx_s   = ind.adx_dmi(df["high"], df["low"], df["close"], cfg.ADX_LEN)
        adap_thr      = ind.adaptive_slope_threshold(adx_s, cfg.SLOPE_MIN,
                                                     cfg.ADX_TREND, cfg.ADX_RANGE)
        atr14_s = ind.atr(df["high"], df["low"], df["close"], 14)

        cur_slope = float(slope_s.iloc[-1])  if not np.isnan(slope_s.iloc[-1]) else 0.0
        cur_thr   = float(adap_thr.iloc[-1]) if not np.isnan(adap_thr.iloc[-1]) else cfg.SLOPE_MIN
        cur_adx   = float(adx_s.iloc[-1])    if not np.isnan(adx_s.iloc[-1])   else 0.0

        is_trending = cur_adx > cfg.ADX_TREND
        is_ranging  = cur_adx < cfg.ADX_RANGE

        r.sama_slope   = round(cur_slope, 2)
        r.adaptive_thr = round(cur_thr, 2)
        r.adx          = round(cur_adx, 2)
        r.regime       = "TENDENCIA" if is_trending else ("RANGO" if is_ranging else "TRANSICION")
        r.sama_bullish = cur_slope >  cur_thr
        r.sama_bearish = cur_slope < -cur_thr
        r.entry_price  = float(df["close"].iloc[-1])
        r.atr14        = float(atr14_s.iloc[-1]) if not np.isnan(atr14_s.iloc[-1]) else 0.0

        # También guardamos VWAP/RVOL/POC aquí (usan los mismos datos)
        vwap_s = ind.vwap(df["high"], df["low"], df["close"], df["volume"])
        rvol_s = ind.rvol(df["volume"], 50)
        poc_s  = ind.poc(df["close"], df["volume"], self.cfg.POC_LOOKBACK)

        r.vwap = float(vwap_s.iloc[-1]) if not np.isnan(vwap_s.iloc[-1]) else 0.0
        r.rvol = float(rvol_s.iloc[-1]) if not np.isnan(rvol_s.iloc[-1]) else 0.0
        r.poc  = float(poc_s.iloc[-1])  if not np.isnan(poc_s.iloc[-1])  else 0.0

        # Guardamos slope series para Markov
        r._slope_s   = slope_s    # type: ignore[attr-defined]
        r._adap_thr  = adap_thr   # type: ignore[attr-defined]
        return r

    # ─────────────────────────────────────────────────────────
    # CAPA 2 — MARKOV (probabilidades de transición de estado)
    # ─────────────────────────────────────────────────────────

    def _layer_markov(self, df: pd.DataFrame, symbol: str,
                      r: SignalResult) -> SignalResult:
        slope_s  = r._slope_s   # type: ignore[attr-defined]
        adap_thr = r._adap_thr  # type: ignore[attr-defined]

        cur_slope  = float(slope_s.iloc[-1])  if not np.isnan(slope_s.iloc[-1]) else 0.0
        prev_slope = float(slope_s.iloc[-2])  if not np.isnan(slope_s.iloc[-2]) else 0.0
        cur_thr    = float(adap_thr.iloc[-1]) if not np.isnan(adap_thr.iloc[-1]) else self.cfg.SLOPE_MIN

        markov = self._get_markov(symbol)
        pb, pr = markov.update(cur_slope, prev_slope, cur_thr)

        r.prob_bull = pb
        r.prob_bear = pr
        return r

    # ─────────────────────────────────────────────────────────
    # CAPA 3 — CVD SINTÉTICO + Divergencia (★ EDGE ESPECIAL)
    # ─────────────────────────────────────────────────────────

    def _layer_cvd(self, df: pd.DataFrame, r: SignalResult) -> SignalResult:
        cfg    = self.cfg
        cvd_s  = ind.synthetic_cvd(df["high"], df["low"], df["close"], df["volume"])
        delta  = ind.cvd_slope(cvd_s, cfg.CVD_SLOPE_PERIOD)
        bull_d, bear_d = ind.cvd_divergence(df["close"], cvd_s, cfg.CVD_DIVERGENCE_LOOKBACK)

        cur_delta  = float(delta.iloc[-1])     if not np.isnan(delta.iloc[-1])  else 0.0
        cur_bull_d = bool(bull_d.iloc[-1])     if not pd.isna(bull_d.iloc[-1])  else False
        cur_bear_d = bool(bear_d.iloc[-1])     if not pd.isna(bear_d.iloc[-1])  else False

        r.cvd_delta = round(cur_delta, 4)
        r.cvd_bull  = cur_delta > 0
        r.cvd_bear  = cur_delta < 0
        r.bull_div  = cur_bull_d   # precio baja pero CVD no confirma → bullish hidden strength
        r.bear_div  = cur_bear_d   # precio sube pero CVD no confirma → bearish hidden weakness
        return r

    # ─────────────────────────────────────────────────────────
    # CAPA 4 — LIQUIDITY SWEEPS (★ EDGE ESPECIAL)
    # ─────────────────────────────────────────────────────────

    def _layer_sweeps(self, df: pd.DataFrame, r: SignalResult) -> SignalResult:
        lb      = self.cfg.LIQUIDITY_LOOKBACK
        sw_long  = ind.liquidity_sweep_long(df["high"], df["low"], df["close"], lb)
        sw_short = ind.liquidity_sweep_short(df["high"], df["low"], df["close"], lb)
        sw_str   = ind.sweep_strength(df["high"], df["low"], df["close"], lb)

        # Detectar sweep en las últimas 3 velas (fresh sweep)
        recent_long  = bool(sw_long.iloc[-3:].any())
        recent_short = bool(sw_short.iloc[-3:].any())
        cur_strength = float(sw_str.iloc[-1]) if not np.isnan(sw_str.iloc[-1]) else 0.0

        r.sweep_long  = recent_long
        r.sweep_short = recent_short
        r.sweep_str   = round(cur_strength, 3)
        return r

    # ─────────────────────────────────────────────────────────
    # CAPA 5 — FUNDING RATE (temperatura del mercado)
    # ─────────────────────────────────────────────────────────

    def _layer_funding(self, funding_rate: float, r: SignalResult) -> SignalResult:
        cfg = self.cfg
        r.funding_rate = funding_rate
        # Funding muy negativo → shorts sobrecargados → favorable para LONG
        r.funding_bull = funding_rate <= cfg.FUNDING_BULL_THRESHOLD
        # Funding muy positivo → longs sobrecargados → favorable para SHORT
        r.funding_bear = funding_rate >= cfg.FUNDING_BEAR_THRESHOLD
        return r

    # ─────────────────────────────────────────────────────────
    # CAPA 6 — KOTEGAWA DIP (LONG) + STC (SHORT)
    # ─────────────────────────────────────────────────────────

    def _layer_kotegawa_stc(self, df: pd.DataFrame, symbol: str,
                             r: SignalResult) -> SignalResult:
        cfg = self.cfg
        ma25       = ind.sma(df["close"], cfg.MA_LEN)
        rsi_s      = ind.rsi(df["close"], cfg.RSI_LEN)
        _, _, bb_l = ind.bollinger_bands(df["close"], cfg.BB_LEN, cfg.BB_MULT)
        stc_s      = ind.stc(df["close"])

        cur_close  = float(df["close"].iloc[-1])
        cur_open   = float(df["open"].iloc[-1])
        cur_ma25   = float(ma25.iloc[-1])  if not np.isnan(ma25.iloc[-1])  else cur_close
        cur_rsi    = float(rsi_s.iloc[-1]) if not np.isnan(rsi_s.iloc[-1]) else 50.0
        cur_bb_l   = float(bb_l.iloc[-1])  if not np.isnan(bb_l.iloc[-1])  else 0.0
        cur_stc    = float(stc_s.iloc[-1]) if not np.isnan(stc_s.iloc[-1]) else 50.0

        dip_level  = cur_ma25 * (1.0 - cfg.DIP_PCT / 100.0)
        pct_below  = (cur_ma25 - cur_close) / cur_ma25 * 100.0 if cur_ma25 > 0 else 0.0
        setup      = (cur_close <= dip_level) and (cur_rsi <= cfg.RSI_OVERSOLD) and (cur_close <= cur_bb_l)

        if setup:
            self._kotegawa_pending[symbol] = True

        pending      = self._kotegawa_pending.get(symbol, False)
        bull_candle  = cur_close > cur_open
        kote_entry   = pending and bull_candle
        if kote_entry:
            self._kotegawa_pending[symbol] = False

        r.kotegawa_bull = kote_entry
        r.stc_val       = round(cur_stc, 2)
        r.rsi_val       = round(cur_rsi, 2)
        r.pct_below_ma  = round(pct_below, 2)
        return r

    # ─────────────────────────────────────────────────────────
    # VETOS AUTOMÁTICOS
    # ─────────────────────────────────────────────────────────

    def _apply_vetos(self, r: SignalResult) -> SignalResult:
        """
        Vetos que anulan la señal independientemente del score:
        1. Divergencia CVD opuesta activa
        2. Mercado en rango sin volumen institucional
        """
        # Divergencia activa en la dirección equivocada
        if r.bear_div and not r.sama_bearish:
            # Precio en nuevo máximo sin CVD confirmando → posible techo
            r.vetoed     = True
            r.veto_reason = "CVD bear divergence activa"
            return r
        if r.bull_div and not r.sama_bullish:
            # Precio en nuevo mínimo sin CVD confirmando → posible suelo
            # Este es FAVORABLE para long, no es veto de long
            pass

        # Rango sin volumen
        if r.regime == "RANGO" and r.rvol < self.cfg.RVOL_MIN:
            r.vetoed     = True
            r.veto_reason = "Mercado en rango sin volumen institucional"
            return r

        return r

    # ─────────────────────────────────────────────────────────
    # FUSIÓN DE SEÑALES — SCORING
    # ─────────────────────────────────────────────────────────

    def _combine(self, r: SignalResult) -> SignalResult:
        if r.vetoed:
            return r

        cfg    = self.cfg
        sl     = 0.0   # score long
        ss     = 0.0   # score short
        reasons: list  = []

        # ── LONG ──────────────────────────────────────────
        if r.sama_bullish:
            sl += 20.0
            reasons.append(f"✅ SAMA alcista ({r.sama_slope:.1f}°)")

        if r.prob_bull > cfg.PROB_THRESHOLD:
            sl += 20.0
            reasons.append(f"✅ Markov bull {r.prob_bull:.1f}%")

        if r.cvd_bull:
            sl += 15.0
            reasons.append(f"✅ CVD ↑ presión compradora")

        if r.sweep_long:
            bonus = min(5.0, r.sweep_str * 10)  # hasta +5 extra según fuerza
            sl += 20.0 + bonus
            reasons.append(f"✅ Sweep LONG detectado (fuerza {r.sweep_str:.2f})")

        if r.funding_bull:
            sl += 10.0
            reasons.append(f"✅ Funding negativo {r.funding_rate:.4%}")

        if r.kotegawa_bull:
            sl += 15.0
            reasons.append(f"✅ Kotegawa dip {r.pct_below_ma:.1f}% bajo MA")

        # Bonus por divergencia bullish hidden (precio bajo, CVD no confirma caída)
        if r.bull_div:
            sl += 8.0
            reasons.append("✅ Bull divergence CVD oculta")

        # ── SHORT ─────────────────────────────────────────
        if r.sama_bearish:
            ss += 20.0
            reasons.append(f"📉 SAMA bajista ({r.sama_slope:.1f}°)")

        if r.prob_bear > cfg.PROB_THRESHOLD:
            ss += 20.0
            reasons.append(f"📉 Markov bear {r.prob_bear:.1f}%")

        if r.cvd_bear:
            ss += 15.0
            reasons.append("📉 CVD ↓ presión vendedora")

        if r.sweep_short:
            bonus = min(5.0, r.sweep_str * 10)
            ss += 20.0 + bonus
            reasons.append(f"📉 Sweep SHORT detectado (fuerza {r.sweep_str:.2f})")

        if r.funding_bear:
            ss += 10.0
            reasons.append(f"📉 Funding positivo {r.funding_rate:.4%}")

        if r.stc_val > 75:
            ss += 15.0
            reasons.append(f"📉 STC sobrecompra {r.stc_val:.1f}")

        if r.bear_div:
            ss += 8.0
            reasons.append("📉 Bear divergence CVD oculta")

        # ── APLICAR UMBRAL ─────────────────────────────────
        MIN = cfg.MIN_SCORE
        r.long        = sl >= MIN
        r.short       = ss >= MIN and not r.long  # no abrir ambas a la vez
        r.score_long  = round(sl, 1)
        r.score_short = round(ss, 1)
        r.score       = round(max(sl, ss), 1)
        r.reasons     = reasons

        if r.long:
            logger.info(f"[{r.symbol}] ★ LONG score={sl:.0f} — {reasons}")
        if r.short:
            logger.info(f"[{r.symbol}] ★ SHORT score={ss:.0f} — {reasons}")

        return r
