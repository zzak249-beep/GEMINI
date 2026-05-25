"""
strategy.py — Lógica de entrada: Score + Decaimiento + CVD

Reglas exactas:
  LONG  = score > +THR  AND decay_alive AND cvd_rising AND NOT bear_div
  SHORT = score < -THR  AND decay_alive AND NOT cvd_rising AND NOT bull_div

Calidad de la señal:
  FUERTE → score extremo + bull/bear_div confirmando
  NORMAL → condiciones base cumplidas
"""
import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass, field
from typing import Optional

import config as C
import indicators as ind

log = logging.getLogger(__name__)


@dataclass
class Signal:
    direction:  str   = "NONE"     # LONG | SHORT | NONE
    quality:    str   = "NONE"     # FUERTE | NORMAL | NONE
    entry:      float = 0.0
    sl:         float = 0.0
    tp:         float = 0.0
    atr_val:    float = 0.0
    # Valores de indicadores para Telegram
    score:      float = 0.0
    decay_pct:  float = 0.0
    cvd_rising: bool  = False
    bull_div:   bool  = False
    bear_div:   bool  = False
    htf_bull:   bool  = False
    htf_bear:   bool  = False
    f_mom:      float = 0.0
    f_rev:      float = 0.0
    f_vol:      float = 0.0
    reasons:    list  = field(default_factory=list)


class Strategy:

    def compute(self,
                df: pd.DataFrame,
                df_htf: Optional[pd.DataFrame] = None) -> Signal:
        """
        df     : OHLCV 3min con columnas open, high, low, close, volume
        df_htf : OHLCV 15min (opcional, para filtro HTF)
        """
        if df is None or len(df) < 60:
            return Signal()

        h, l, c, o, v = df["high"], df["low"], df["close"], df["open"], df["volume"]

        # ── 1. ATR ────────────────────────────────────────────
        atr_s = ind.atr(h, l, c, C.ATR_PERIOD)

        # ── 2. SCORE COMPUESTO ────────────────────────────────
        score, fm, fr, fv = ind.composite_score(
            c, v,
            C.MOM_LOOKBACK, C.REV_LOOKBACK, C.VOL_LOOKBACK,
            C.W_MOM, C.W_REV, C.W_VOL,
            C.SMOOTH, C.DECAY_LEN,
        )

        # ── 3. DECAIMIENTO ────────────────────────────────────
        sig_alive, decay_r = ind.signal_decay(
            score, c, C.DECAY_LEN, C.SMOOTH, C.DECAY_THR
        )

        # ── 4. CVD DELTA ──────────────────────────────────────
        cvd_rising, bull_div, bear_div, cvd_raw, cvd_ema_s = ind.cvd_delta(
            h, l, c, v, C.CVD_EMA_LEN, C.CVD_DIV_BARS
        )

        # ── 5. HTF ────────────────────────────────────────────
        htf_bull = htf_bear = True  # por defecto no filtra
        if C.REQUIRE_HTF and df_htf is not None and len(df_htf) > C.HTF_SLOW + 2:
            htf_bull, htf_bear = ind.htf_regime(
                df_htf["close"], C.HTF_FAST, C.HTF_SLOW
            )

        # ── Valores finales (última vela cerrada) ─────────────
        def b(s):
            try: return bool(s.iloc[-1])
            except: return False
        def f(s):
            try:
                val = float(s.iloc[-1])
                return 0.0 if np.isnan(val) else val
            except: return 0.0

        sc    = f(score)
        alive = b(sig_alive)
        dec   = f(decay_r)
        cvdr  = b(cvd_rising)
        bdiv  = b(bull_div)
        brdiv = b(bear_div)
        atr_v = f(atr_s)
        price = f(c)
        fm_v  = f(fm)
        fr_v  = f(fr)
        fv_v  = f(fv)

        # ══════════════════════════════════════════════════════
        #  REGLAS DE ENTRADA
        # ══════════════════════════════════════════════════════

        #  LONG:
        #   ✅ Score > umbral (alcista)
        #   ✅ Señal viva (decaimiento >= threshold)
        #   ✅ CVD rising (presión compradora)
        #   ❌ Sin divergencia bajista (distribución)
        #   ✅ HTF alcista (si activado)
        long_base   = sc > C.SCORE_THR and alive and cvdr and not brdiv
        long_htf_ok = htf_bull if C.REQUIRE_HTF else True
        long_valid  = long_base and long_htf_ok

        #  LONG FUERTE: además hay divergencia alcista (acumulación oculta)
        long_strong = long_valid and bdiv

        #  SHORT:
        #   ✅ Score < -umbral (bajista)
        #   ✅ Señal viva
        #   ❌ CVD no rising (presión vendedora)
        #   ❌ Sin divergencia alcista
        #   ✅ HTF bajista (si activado)
        short_base   = sc < -C.SCORE_THR and alive and not cvdr and not bdiv
        short_htf_ok = htf_bear if C.REQUIRE_HTF else True
        short_valid  = short_base and short_htf_ok

        short_strong = short_valid and brdiv

        # ── Construir señal ───────────────────────────────────
        sig = Signal()
        sig.score      = sc
        sig.decay_pct  = dec * 100
        sig.cvd_rising = cvdr
        sig.bull_div   = bdiv
        sig.bear_div   = brdiv
        sig.htf_bull   = htf_bull
        sig.htf_bear   = htf_bear
        sig.atr_val    = atr_v
        sig.f_mom      = fm_v
        sig.f_rev      = fr_v
        sig.f_vol      = fv_v

        if long_valid:
            sig.direction = "LONG"
            sig.quality   = "FUERTE" if long_strong else "NORMAL"
            sig.entry     = price
            sig.sl        = price - atr_v * C.SL_ATR_MULT
            risk          = sig.entry - sig.sl
            sig.tp        = price + risk * C.TP_RR
            sig.reasons   = self._reasons_long(sc, alive, dec, cvdr, bdiv, htf_bull)

        elif short_valid:
            sig.direction = "SHORT"
            sig.quality   = "FUERTE" if short_strong else "NORMAL"
            sig.entry     = price
            sig.sl        = price + atr_v * C.SL_ATR_MULT
            risk          = sig.sl - sig.entry
            sig.tp        = price - risk * C.TP_RR
            sig.reasons   = self._reasons_short(sc, alive, dec, cvdr, brdiv, htf_bear)

        if sig.direction != "NONE":
            log.info(
                f"SEÑAL {sig.direction} {sig.quality} | "
                f"score={sc:+.3f} decay={dec*100:.0f}% cvd={'↑' if cvdr else '↓'} "
                f"entry={sig.entry:.4f} sl={sig.sl:.4f} tp={sig.tp:.4f}"
            )

        return sig

    # ── Razones ───────────────────────────────────────────────

    @staticmethod
    def _reasons_long(sc, alive, dec, cvdr, bdiv, htf_bull):
        r = [f"✅ Score {sc:+.3f} (alcista)"]
        r.append(f"✅ Decaimiento vivo {dec*100:.0f}%")
        if cvdr: r.append("✅ CVD rising — presión compradora")
        if bdiv: r.append("💎 CVD Bull Div — acumulación oculta")
        if htf_bull: r.append("✅ HTF alcista (15min)")
        return r

    @staticmethod
    def _reasons_short(sc, alive, dec, cvdr, brdiv, htf_bear):
        r = [f"✅ Score {sc:+.3f} (bajista)"]
        r.append(f"✅ Decaimiento vivo {dec*100:.0f}%")
        if not cvdr: r.append("✅ CVD falling — presión vendedora")
        if brdiv: r.append("💎 CVD Bear Div — distribución oculta")
        if htf_bear: r.append("✅ HTF bajista (15min)")
        return r
