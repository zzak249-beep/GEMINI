"""
QF Machine × JP Fusion — Signal Engine v3.1
FIXES: 
  - Todos los bool/scalar correctos (no más Series ambiguas)
  - Modo HUNT: opera con Score>threshold Y Decay>threshold (sin exigir 6 capas)
  - Klines fix: acepta dict o list desde BingX
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    direction: str          # "LONG" | "SHORT" | "FLAT"
    tier: str               # "SUPREMA" | "FUEL" | "STD" | "HUNT_LONG" | "HUNT_SHORT" | "NONE"
    conviction: int         # 0-10
    norm_score: float
    sl_price: float
    entry_price: float
    details: dict


class QFSignalEngine:
    def __init__(self, cfg: dict):
        self.c = cfg

    @staticmethod
    def _tanh(x):
        x = np.clip(2.0 * x, -20, 20)
        e2x = np.exp(x)
        return (e2x - 1.0) / (e2x + 1.0)

    @staticmethod
    def _ema(s: pd.Series, span: int) -> pd.Series:
        return s.ewm(span=span, adjust=False).mean()

    @staticmethod
    def _sma(s: pd.Series, w: int) -> pd.Series:
        return s.rolling(w).mean()

    @staticmethod
    def _stdev(s: pd.Series, w: int) -> pd.Series:
        return s.rolling(w).std()

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        hl = df['high'] - df['low']
        hc = (df['high'] - df['close'].shift(1)).abs()
        lc = (df['low']  - df['close'].shift(1)).abs()
        return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean()

    @staticmethod
    def _pivothigh(s: pd.Series, left: int, right: int) -> pd.Series:
        result = pd.Series(np.nan, index=s.index)
        for i in range(left, len(s) - right):
            w = s.iloc[i - left:i + right + 1]
            if s.iloc[i] == w.max():
                result.iloc[i] = s.iloc[i]
        return result

    @staticmethod
    def _pivotlow(s: pd.Series, left: int, right: int) -> pd.Series:
        result = pd.Series(np.nan, index=s.index)
        for i in range(left, len(s) - right):
            w = s.iloc[i - left:i + right + 1]
            if s.iloc[i] == w.min():
                result.iloc[i] = s.iloc[i]
        return result

    # ── L2 ───────────────────────────────────────────────────
    def _l2_factors(self, df):
        c = self.c
        close = df['close']
        roc      = (close - close.shift(c['mom'])) / close.shift(c['mom'])
        vol_norm = self._stdev(close, c['mom']) / self._sma(close, c['mom'])
        f_mom    = (roc / vol_norm.replace(0, np.nan)).fillna(0)

        basis     = self._sma(close, c['rev'])
        basis_std = self._stdev(close, c['rev'])
        f_rev     = (-(close - basis) / basis_std.replace(0, np.nan)).fillna(0)

        obv     = (np.sign(close.diff()) * df['volume']).cumsum()
        obv_ma  = self._ema(obv, c['vol_len'])
        obv_std = self._stdev(obv, c['vol_len'])
        f_vol   = ((obv - obv_ma) / obv_std.replace(0, np.nan)).fillna(0)

        raw    = c['w1']*f_mom + c['w2']*f_rev + c['w3']*f_vol
        comp   = self._ema(raw, c['smo'])
        sc_std = self._stdev(comp, c['dlen'])
        norm   = self._tanh(comp / sc_std.replace(0, np.nan)).fillna(0)

        return {'f_mom': f_mom, 'f_rev': f_rev, 'f_vol': f_vol,
                'norm_score': norm, 'basis': basis}

    # ── L3 Decay ─────────────────────────────────────────────
    def _l3_decay(self, df, norm_score):
        c = self.c
        fwd   = df['close'].pct_change()
        ic    = norm_score.shift(1).rolling(c['dlen']).corr(fwd)
        roll  = self._ema(ic.abs(), c['smo'])
        peak  = roll.rolling(c['dlen']).max()
        decay = (roll / peak.replace(0, np.nan)).fillna(0.5)
        val   = float(decay.iloc[-1])
        return val, bool(val >= c['dthr'])

    # ── L4 Dark Pool ─────────────────────────────────────────
    def _l4_darkpool(self, df):
        c = self.c
        atr       = self._atr(df, c['atr_len'])
        vol_base  = self._sma(df['volume'], c['dpb'])
        vol_spike = df['volume'] > vol_base * c['dpm']
        narrow    = (df['high'] - df['low']) < atr * 0.6
        dp_buy    = bool((vol_spike & narrow & (df['close'] > df['open'])).iloc[-1])
        dp_sell   = bool((vol_spike & narrow & (df['close'] < df['open'])).iloc[-1])
        atr_v     = float(atr.iloc[-1])
        vb        = float(vol_base.iloc[-1])
        rng       = float(df['high'].iloc[-1] - df['low'].iloc[-1])
        vol       = float(df['volume'].iloc[-1])
        co        = df['close'].iloc[-1] > df['open'].iloc[-1]
        vac_up    = bool(rng > atr_v*1.8 and vol < vb*0.6 and co)
        vac_dn    = bool(rng > atr_v*1.8 and vol < vb*0.6 and not co)
        return {'dp_buy': dp_buy, 'dp_sell': dp_sell,
                'vac_up': vac_up, 'vac_dn': vac_dn, 'atr': atr}

    # ── L5 Exec ──────────────────────────────────────────────
    def _l5_exec(self, df):
        c = self.c
        hi_lo = np.log(df['high'] / df['low'])
        spread = self._sma(hi_lo, c['spl']) * df['close']
        bp = (spread / df['close']) * 100
        return bool(float(bp.iloc[-1]) < c['bpt'])

    # ── L6 Asym ──────────────────────────────────────────────
    def _l6_asym(self, df):
        c = self.c
        is_up  = df['close'] > df['open']
        is_dn  = df['close'] < df['open']
        up_rng = (df['high'] - df['low']).where(is_up, 0.0)
        dn_rng = (df['high'] - df['low']).where(is_dn, 0.0)
        avg_up = float(self._sma(up_rng, c['asl']).iloc[-1])
        avg_dn = float(self._sma(dn_rng, c['asl']).iloc[-1])
        rb = avg_up / avg_dn if avg_dn > 0 else 1.0
        rs = avg_dn / avg_up if avg_up > 0 else 1.0
        return {
            'asym_bull': bool(rb >= c['arr']),
            'asym_bear': bool(rs >= c['abr']),
            'rng_bull': rb, 'rng_bear': rs,
        }

    # ── L7 Trendline ─────────────────────────────────────────
    def _l7_trendline(self, df):
        c   = self.c
        atr = self._atr(df, c['atr_len'])
        ph  = self._pivothigh(df['high'], c['tll'], c['tlr'])
        pl  = self._pivotlow(df['low'],   c['pll'], c['plr'])

        tl_break_long = tl_break_short = False
        ph_vals = ph.dropna()
        pl_vals = pl.dropna()

        if len(ph_vals) >= 2:
            i1, i2 = ph_vals.index[-1], ph_vals.index[-2]
            p1, p2 = float(ph_vals.iloc[-1]), float(ph_vals.iloc[-2])
            n1 = df.index.get_loc(i1)
            n2 = df.index.get_loc(i2)
            if p2 > p1 and (len(df)-1-n2) <= c['tlb']:
                slope = (p1-p2)/max(n1-n2,1)
                ctl   = p1 + slope*(len(df)-1-n1)
                ptl   = p1 + slope*(len(df)-2-n1)
                buf   = float(atr.iloc[-1])*c['tlm']
                if float(df['close'].iloc[-1]) > ctl+buf and float(df['close'].iloc[-2]) <= ptl+buf:
                    tl_break_long = True

        if len(pl_vals) >= 2:
            i1, i2 = pl_vals.index[-1], pl_vals.index[-2]
            p1, p2 = float(pl_vals.iloc[-1]), float(pl_vals.iloc[-2])
            n1 = df.index.get_loc(i1)
            n2 = df.index.get_loc(i2)
            if p2 < p1 and (len(df)-1-n2) <= c['tlb']:
                slope = (p1-p2)/max(n1-n2,1)
                ctl   = p1 + slope*(len(df)-1-n1)
                ptl   = p1 + slope*(len(df)-2-n1)
                buf   = float(atr.iloc[-1])*c['tlm']
                if float(df['close'].iloc[-1]) < ctl-buf and float(df['close'].iloc[-2]) >= ptl-buf:
                    tl_break_short = True

        return {'tl_break_long': tl_break_long, 'tl_break_short': tl_break_short}

    # ── L8 Swings ────────────────────────────────────────────
    def _l8_swings(self, df):
        c  = self.c
        pl = self._pivotlow(df['low'],   c['pll'], c['plr'])
        ph = self._pivothigh(df['high'], c['phl'], c['phr'])
        rpl = pl.dropna().tail(c['hlw'])
        rph = ph.dropna().tail(c['hlw'])
        hl_count = int(sum(float(rpl.iloc[i])>float(rpl.iloc[i-1]) for i in range(1,len(rpl))))
        lh_count = int(sum(float(rph.iloc[i])<float(rph.iloc[i-1]) for i in range(1,len(rph))))
        last_sl  = float(rpl.iloc[-1]) if len(rpl)>0 else np.nan
        last_sh  = float(rph.iloc[-1]) if len(rph)>0 else np.nan
        return {
            'sell_exhausted': bool(hl_count>=c['hlc']),
            'buy_exhausted':  bool(lh_count>=c['hhc']),
            'last_sl': last_sl, 'last_sh': last_sh,
            'hl_count': hl_count, 'lh_count': lh_count,
        }

    # ── L9 FVG ───────────────────────────────────────────────
    def _l9_fvg(self, df, atr):
        c = self.c
        bull = (df['low'] > df['high'].shift(2)) & \
               ((df['low']-df['high'].shift(2)) > atr*c['fvg_min'])
        bear = (df['high'] < df['low'].shift(2)) & \
               ((df['low'].shift(2)-df['high']) > atr*c['fvg_min'])
        n = min(c['fvg_bars'], len(df))
        in_bull = in_bear = False
        rb = bull.iloc[-n:]
        if rb.any():
            idx = rb[rb].index[-1]
            top = float(df.loc[idx,'low'])
            loc = df.index.get_loc(idx)
            if loc >= 2:
                bot = float(df.iloc[loc-2]['high'])
                cp  = float(df['close'].iloc[-1])
                in_bull = bool(bot <= cp <= top)
        rb2 = bear.iloc[-n:]
        if rb2.any():
            idx = rb2[rb2].index[-1]
            top = float(df.loc[idx,'low'])
            bot = float(df.loc[idx,'high'])
            cp  = float(df['close'].iloc[-1])
            in_bear = bool(bot <= cp <= top)
        return {'bull_fvg_raw': bool(bull.iloc[-1]), 'bear_fvg_raw': bool(bear.iloc[-1]),
                'in_bull_fvg': in_bull, 'in_bear_fvg': in_bear}

    # ── L10 OB ───────────────────────────────────────────────
    def _l10_ob(self, df, atr):
        c = self.c
        sb = ((df['close']-df['open'])>atr*c['ob_imp'])&(df['close']>df['close'].shift(1))
        ss = ((df['open']-df['close'])>atr*c['ob_imp'])&(df['close']<df['close'].shift(1))
        bull = sb & (df['close'].shift(1)<df['open'].shift(1))
        bear = ss & (df['close'].shift(1)>df['open'].shift(1))
        n = min(c['ob_bars'], len(df))
        in_bull = in_bear = False
        rb = bull.iloc[-n:]
        if rb.any():
            idx = rb[rb].index[-1]
            hi = float(df.loc[idx,'open']); lo = float(df.loc[idx,'close'])
            cp = float(df['close'].iloc[-1])
            in_bull = bool(lo <= cp <= hi)
        rb2 = bear.iloc[-n:]
        if rb2.any():
            idx = rb2[rb2].index[-1]
            hi = float(df.loc[idx,'close']); lo = float(df.loc[idx,'open'])
            cp = float(df['close'].iloc[-1])
            in_bear = bool(lo <= cp <= hi)
        return {'bull_ob_raw': bool(bull.iloc[-1]), 'bear_ob_raw': bool(bear.iloc[-1]),
                'in_bull_ob': in_bull, 'in_bear_ob': in_bear}

    # ── L11 CVD ──────────────────────────────────────────────
    def _l11_cvd(self, df):
        c = self.c
        hl   = df['high']-df['low']
        bvol = ((df['close']-df['low'])/hl.replace(0,np.nan)).fillna(0.5)*df['volume']
        svol = ((df['high']-df['close'])/hl.replace(0,np.nan)).fillna(0.5)*df['volume']
        cvd  = (bvol-svol).cumsum()
        cema = self._ema(cvd, c['cvd_len'])
        cv   = float(cvd.iloc[-1]); ce = float(cema.iloc[-1])
        cp   = float(df['close'].iloc[-1])
        cpp  = float(df['close'].iloc[-c['cvd_div']])
        cvp  = float(cvd.iloc[-c['cvd_div']])
        return {
            'cvd_rising':   bool(cv > ce),
            'cvd_bull_div': bool(cp < cpp and cv > cvp),
            'cvd_bear_div': bool(cp > cpp and cv < cvp),
        }

    # ── L12 Squeeze ──────────────────────────────────────────
    def _l12_squeeze(self, df):
        c = self.c; n = c['sq_len']
        basis  = self._sma(df['close'], n)
        dev    = self._stdev(df['close'], n)
        bb_hi  = basis + c['sq_bbm']*dev
        bb_lo  = basis - c['sq_bbm']*dev
        ka     = self._atr(df, n)
        ke     = self._ema(df['close'], n)
        kc_hi  = ke + c['sq_kcm']*ka
        kc_lo  = ke - c['sq_kcm']*ka
        sq_on  = (bb_hi < kc_hi) & (bb_lo > kc_lo)
        sq_fire= ~sq_on & sq_on.shift(1).fillna(False)
        high_r = df['high'].rolling(n).max()
        low_r  = df['low'].rolling(n).min()
        mid    = ((high_r+low_r)/2 + basis)/2
        val    = df['close'] - mid
        fired  = bool(sq_fire.iloc[-1])
        v      = float(val.iloc[-1])
        return {'sq_bull': bool(fired and v>0), 'sq_bear': bool(fired and v<0),
                'sq_on': bool(sq_on.iloc[-1])}

    # ── HTF ──────────────────────────────────────────────────
    def _htf_regime(self, df_htf):
        e9  = self._ema(df_htf['close'], 9)
        e21 = self._ema(df_htf['close'], 21)
        return {
            'htf_bull': bool(float(e9.iloc[-1]) > float(e21.iloc[-1])),
            'htf_bear': bool(float(e9.iloc[-1]) < float(e21.iloc[-1])),
        }

    # ── MAIN COMPUTE ─────────────────────────────────────────
    def compute(self, df: pd.DataFrame, df_htf: pd.DataFrame) -> SignalResult:
        if len(df) < 100:
            return SignalResult("FLAT","NONE",0,0.0,np.nan,float(df['close'].iloc[-1]),{})

        c = self.c

        l2   = self._l2_factors(df)
        atr  = self._atr(df, c['atr_len'])
        l4   = self._l4_darkpool(df)
        l5   = self._l5_exec(df)
        l6   = self._l6_asym(df)
        l7   = self._l7_trendline(df)
        l8   = self._l8_swings(df)
        l9   = self._l9_fvg(df, atr)
        l10  = self._l10_ob(df, atr)
        l11  = self._l11_cvd(df)
        l12  = self._l12_squeeze(df)
        htf  = self._htf_regime(df_htf)

        decay_val, alive = self._l3_decay(df, l2['norm_score'])

        # — scalars puros —
        ns       = float(l2['norm_score'].iloc[-1])
        exec_ok  = bool(l5)
        dp_buy   = bool(l4['dp_buy']); dp_sell = bool(l4['dp_sell'])
        ab       = bool(l6['asym_bull']); as_ = bool(l6['asym_bear'])
        tl_l     = bool(l7['tl_break_long']); tl_s = bool(l7['tl_break_short'])
        sell_ex  = bool(l8['sell_exhausted']); buy_ex = bool(l8['buy_exhausted'])
        hb       = bool(htf['htf_bull']); hs = bool(htf['htf_bear'])
        cvd_up   = bool(l11['cvd_rising'])
        cvd_bd   = bool(l11['cvd_bull_div']); cvd_sd = bool(l11['cvd_bear_div'])
        sq_b     = bool(l12['sq_bull']); sq_s = bool(l12['sq_bear'])
        bfvg     = bool(l9['in_bull_fvg']); sfvg = bool(l9['in_bear_fvg'])
        bob      = bool(l10['in_bull_ob']); sob = bool(l10['in_bear_ob'])

        entry = float(df['close'].iloc[-1])
        atr_v = float(atr.iloc[-1])
        sl_l  = float(l8['last_sl']) if not np.isnan(l8['last_sl']) else entry - atr_v*2
        sl_s_ = float(l8['last_sh']) if not np.isnan(l8['last_sh']) else entry + atr_v*2

        # ── Señales completas (requieren muchas capas) ────────
        long_std  = ns>0.15 and alive and exec_ok and hb and ab and sell_ex
        long_fuel = long_std and (tl_l or sq_b or ((bfvg or bob) and cvd_up))
        long_sup  = long_fuel and (dp_buy or cvd_bd)

        short_std  = ns<-0.15 and alive and exec_ok and hs and as_ and buy_ex
        short_fuel = short_std and (tl_s or sq_s or ((sfvg or sob) and not cvd_up))
        short_sup  = short_fuel and (dp_sell or cvd_sd)

        # ── Conviction ────────────────────────────────────────
        lc = int(sum([ns>0.15,alive,exec_ok,hb,ab,sell_ex,tl_l,dp_buy,cvd_up,(sq_b or bfvg or bob)]))
        sc = int(sum([ns<-0.15,alive,exec_ok,hs,as_,buy_ex,tl_s,dp_sell,not cvd_up,(sq_s or sfvg or sob)]))

        det = {
            'norm_score': round(ns*100,1), 'decay_pct': round(decay_val*100,1),
            'f_mom': round(float(l2['f_mom'].iloc[-1])*100,1),
            'f_rev': round(float(l2['f_rev'].iloc[-1])*100,1),
            'f_vol': round(float(l2['f_vol'].iloc[-1])*100,1),
            'htf_bull': hb, 'htf_bear': hs, 'sig_alive': alive,
            'exec_ok': exec_ok, 'asym_bull': ab, 'asym_bear': as_,
            'tl_long': tl_l, 'tl_short': tl_s,
            'sell_exhausted': sell_ex, 'buy_exhausted': buy_ex,
            'hl_count': l8['hl_count'],
            'in_bull_fvg': bfvg, 'in_bear_fvg': sfvg,
            'in_bull_ob': bob, 'in_bear_ob': sob,
            'cvd_rising': cvd_up, 'cvd_bull_div': cvd_bd, 'cvd_bear_div': cvd_sd,
            'sq_bull': sq_b, 'sq_bear': sq_s, 'sq_on': bool(l12['sq_on']),
            'dp_buy': dp_buy, 'dp_sell': dp_sell,
            'atr': round(atr_v, 6),
        }

        # ── Señales completas primero ─────────────────────────
        if long_sup:   return SignalResult("LONG",  "SUPREMA", lc, ns, sl_l,  entry, det)
        if long_fuel:  return SignalResult("LONG",  "FUEL",    lc, ns, sl_l,  entry, det)
        if long_std:   return SignalResult("LONG",  "STD",     lc, ns, sl_l,  entry, det)
        if short_sup:  return SignalResult("SHORT", "SUPREMA", sc, ns, sl_s_, entry, det)
        if short_fuel: return SignalResult("SHORT", "FUEL",    sc, ns, sl_s_, entry, det)
        if short_std:  return SignalResult("SHORT", "STD",     sc, ns, sl_s_, entry, det)

        # ── HUNT MODE: Score + Decay suficientes ──────────────
        # Opera cuando Score y Decay superan umbrales aunque no se cumplan todas las capas
        hunt_score_thr = float(c.get('hunt_score_thr', 0.08))   # ~8/100
        hunt_decay_thr = float(c.get('hunt_decay_thr', 0.35))   # 35%

        if decay_val >= hunt_decay_thr:
            if ns > hunt_score_thr:
                return SignalResult("LONG",  "HUNT_LONG",  lc, ns, sl_l,  entry, det)
            if ns < -hunt_score_thr:
                return SignalResult("SHORT", "HUNT_SHORT", sc, ns, sl_s_, entry, det)

        return SignalResult("FLAT","NONE", max(lc,sc), ns, np.nan, entry, {})
