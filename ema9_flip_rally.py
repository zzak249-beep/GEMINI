"""
9 EMA Flip & Rally Filter — adaptación de la estrategia de ThiccTeddy
══════════════════════════════════════════════════════════════════════════════
Estrategia original (manual):
  1. Precio rompe EMA9 con VOLUMEN ALTO → Flip confirmado
  2. Precio retestea EMA9 con VOLUMEN BAJO → corrección sana
  3. Precio rebota de EMA9 con VOLUMEN ALTO + vela verde → ENTRADA
  4. SL por debajo de EMA9 | Trail: mover SL con EMA9

Automatización para joyful-art:
  En vez de rastrear el estado completo (requeriría persistencia por símbolo),
  detectamos si el momento actual satisface las condiciones del estado 3
  (RALLY desde EMA9), lo que implícitamente requiere:
    - EMA9 en tendencia alcista (slope positivo)
    - Precio cerca de EMA9 (dentro del 1%)
    - Vela actual verde Y cierra encima de EMA9
    - Volumen actual por encima de su media

  Para el SHORT (versión bajista):
    - EMA9 en tendencia bajista
    - Precio cerca de EMA9 por debajo
    - Vela roja + volumen alto

Integración en scanner.py de joyful-art:
  from ema9_flip_rally import ema9_flip_rally_filter

  # Después del slope filter, usando k3m ya fetcheadas:
  if getattr(C, 'EMA9_RALLY_ENABLED', False):
      e9_boost, e9_reason, e9_block = ema9_flip_rally_filter(k3m, sig.direction)
      if e9_block:
          diag["counts"]["ema9_block"] += 1
          return None
      if e9_boost != 0:
          sig.score = max(0.0, min(sig.score + e9_boost, 100.0))
          sig.tier  = score_to_tier(sig.score)
          filter_tags["ema9_rally"] = e9_reason

SL dinámico (diferencia clave vs ATR fijo):
  El SL óptimo según la estrategia es EMA9, no un múltiplo de ATR.
  Esto se puede pasar al position_manager como sl_price = ema9_val.
  Ventaja: el SL sube con el precio → trail natural.
  Desventaja: en volatilidad alta EMA9 puede estar muy lejos.

Variables Railway:
  EMA9_RALLY_ENABLED=false     (default desactivado)
  EMA9_NEAR_PCT=1.0            (% máximo de distancia al EMA9 para "cerca")
  EMA9_VOL_HIGH_MULT=1.3       (ratio volumen actual / media para "alto")
  EMA9_VOL_LOW_MULT=0.8        (ratio volumen para "bajo" en retest)
  EMA9_SLOPE_BARS=3            (barras para calcular slope de EMA9)
  EMA9_BOOST=7.0               (puntos de boost cuando se cumple el setup)
══════════════════════════════════════════════════════════════════════════════
"""
import logging

log = logging.getLogger("ema9_flip_rally")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ema(values: list, period: int) -> list:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _vol_ma(volumes: list, period: int = 20) -> float:
    if len(volumes) < period:
        return sum(volumes) / len(volumes) if volumes else 1.0
    return sum(volumes[-period:]) / period


# ── Estado completo del 9 EMA Flip & Rally ────────────────────────────────────

def analyze_ema9_state(klines: list,
                       ema_period:    int   = 9,
                       vol_period:    int   = 20,
                       near_pct:      float = 1.0,
                       vol_high_mult: float = 1.3,
                       vol_low_mult:  float = 0.8,
                       slope_bars:    int   = 3) -> dict:
    """
    Analiza el estado actual del precio respecto a la EMA9.

    Detecta los 3 estados del sistema Flip & Rally:
      FLIP    → precio cruzó encima de EMA9 con volumen alto (alcista)
               o cruzó DEBAJO con volumen alto (bajista)
      RETEST  → precio volvió a tocar EMA9 con volumen bajo después del flip
      RALLY   → precio rebotó de EMA9 con volumen alto (señal de entrada)

    El análisis es histórico (lookback de las últimas barras) para detectar
    si estamos en el punto óptimo de entrada.

    Returns dict con:
      ema9:          valor actual de EMA9
      ema9_slope:    pendiente de EMA9 (positiva = alcista)
      price_vs_ema9: % de distancia del precio al EMA9
      near_ema9:     bool — precio dentro del `near_pct`% del EMA9
      above_ema9:    bool — precio encima de EMA9
      vol_ratio:     ratio volumen actual / media
      flip_bull:     bool — flip alcista en las últimas 5 barras
      flip_bear:     bool — flip bajista en las últimas 5 barras
      retest_bull:   bool — retest del EMA9 con vol bajo tras flip alcista
      rally_bull:    bool — rebote alcista desde EMA9 con vol alto
      rally_bear:    bool — rebote bajista desde EMA9 con vol alto
      die_signal:    bool — precio cruzó debajo después de estar encima
    """
    if len(klines) < ema_period + vol_period + 5:
        return {"error": "insufficient_data", "ema9": 0.0}

    closes  = [k[4] for k in klines]
    opens   = [k[1] for k in klines]
    highs   = [k[2] for k in klines]
    lows    = [k[3] for k in klines]
    volumes = [k[5] for k in klines]

    ema9_series = _ema(closes, ema_period)
    ema9_now    = ema9_series[-1]
    ema9_prev   = ema9_series[-slope_bars] if len(ema9_series) > slope_bars else ema9_now

    curr_close  = closes[-1]
    curr_open   = opens[-1]
    curr_vol    = volumes[-1]
    vol_avg     = _vol_ma(volumes[:-1], vol_period)

    # Slope de EMA9 — positivo = alcista
    ema9_slope = (ema9_now - ema9_prev) / ema9_prev * 100 if ema9_prev > 0 else 0.0

    # Distancia del precio al EMA9
    price_vs_ema9 = (curr_close - ema9_now) / ema9_now * 100
    near_ema9     = abs(price_vs_ema9) <= near_pct
    above_ema9    = curr_close > ema9_now
    vol_ratio     = curr_vol / vol_avg if vol_avg > 0 else 1.0
    green_candle  = curr_close > curr_open
    red_candle    = curr_close < curr_open

    # ── Detectar FLIP (cruce de EMA9 con volumen alto) ────────────────────────
    # Buscamos en las últimas 5 barras un cruce de EMA9
    flip_bull = False
    flip_bear = False
    flip_bar  = None

    for i in range(len(closes) - 5, len(closes) - 1):
        if i <= 0:
            continue
        prev_c = closes[i-1]
        curr_c = closes[i]
        ema_at = ema9_series[i]
        vol_at = volumes[i]
        vol_avg_at = _vol_ma(volumes[:i], vol_period)

        # Cruce alcista con volumen alto
        if prev_c <= ema9_series[i-1] and curr_c > ema_at:
            if vol_at >= vol_avg_at * vol_high_mult:
                flip_bull = True
                flip_bar  = i
                break

        # Cruce bajista con volumen alto
        if prev_c >= ema9_series[i-1] and curr_c < ema_at:
            if vol_at >= vol_avg_at * vol_high_mult:
                flip_bear = True
                flip_bar  = i
                break

    # ── Detectar RETEST (precio vuelve a EMA9 con vol bajo) ──────────────────
    retest_bull = False
    if flip_bull and flip_bar is not None:
        # Buscar una barra después del flip donde precio toque EMA9 con vol bajo
        for i in range(flip_bar + 1, len(closes)):
            dist = abs(closes[i] - ema9_series[i]) / ema9_series[i] * 100
            vol_at     = volumes[i]
            vol_avg_at = _vol_ma(volumes[:i], vol_period)
            if dist <= near_pct and vol_at < vol_avg_at * vol_low_mult:
                retest_bull = True
                break

    # ── Detectar RALLY (rebote alcista de EMA9 con vol alto) ─────────────────
    # Condición simplificada: precio cerca de EMA9 por encima,
    # vela verde, volumen alto, EMA9 alcista
    rally_bull = (
        above_ema9      and
        near_ema9       and
        green_candle    and
        vol_ratio >= vol_high_mult  and
        ema9_slope > 0
    )

    # Versión bajista: precio cerca de EMA9 por debajo, vela roja, vol alto
    rally_bear = (
        not above_ema9  and
        near_ema9       and
        red_candle      and
        vol_ratio >= vol_high_mult  and
        ema9_slope < 0
    )

    # ── Die signal (precio cruzó debajo de EMA9 tras estar encima) ────────────
    die_signal = (
        closes[-2] > ema9_series[-2] and
        closes[-1] < ema9_series[-1] and
        ema9_slope < 0
    )

    return {
        "ema9":          round(ema9_now, 8),
        "ema9_slope":    round(ema9_slope, 3),
        "price_vs_ema9": round(price_vs_ema9, 3),
        "near_ema9":     near_ema9,
        "above_ema9":    above_ema9,
        "vol_ratio":     round(vol_ratio, 2),
        "flip_bull":     flip_bull,
        "flip_bear":     flip_bear,
        "retest_bull":   retest_bull,
        "rally_bull":    rally_bull,
        "rally_bear":    rally_bear,
        "die_signal":    die_signal,
        "green_candle":  green_candle,
    }


# ── Filtro para scanner.py ────────────────────────────────────────────────────

def ema9_flip_rally_filter(
    klines:        list,
    direction:     str   = "LONG",
    near_pct:      float = 1.0,
    vol_high_mult: float = 1.3,
    slope_bars:    int   = 3,
    boost_amount:  float = 7.0,
) -> tuple:
    """
    Filtro 9 EMA Flip & Rally para scanner.py.

    Returns: (boost: float, reason: str, block: bool)

    Lógica de boost/block:
      LONG + rally_bull detectado → boost +7 (setup óptimo)
      LONG + flip_bull reciente   → boost +3 (tendencia recién girada)
      LONG + die_signal           → block   (precio cruzó debajo de EMA9)
      LONG + ema9_slope < 0       → penalización -4

      SHORT: lógica inversa
    """
    state = analyze_ema9_state(
        klines,
        near_pct=near_pct,
        vol_high_mult=vol_high_mult,
        slope_bars=slope_bars,
    )

    if "error" in state:
        return 0.0, "ema9_no_data", False

    boost  = 0.0
    block  = False

    if direction == "LONG":
        if state["die_signal"]:
            block  = True
            reason = f"EMA9 DIE SIGNAL — precio cruzó debajo ema9={state['ema9']:.6f}"
        elif state["rally_bull"]:
            boost  = boost_amount
            reason = (
                f"EMA9 RALLY ↑ vol={state['vol_ratio']:.2f}× "
                f"slope={state['ema9_slope']:+.2f}% "
                f"price_vs_ema9={state['price_vs_ema9']:+.2f}%"
            )
        elif state["flip_bull"]:
            boost  = boost_amount * 0.4
            reason = f"EMA9 FLIP ↑ reciente slope={state['ema9_slope']:+.2f}%"
        elif state["ema9_slope"] < -0.1:
            boost  = -4.0
            reason = f"EMA9 bajista (slope={state['ema9_slope']:+.2f}%) — penalización LONG"
        else:
            boost  = 0.0
            reason = f"EMA9 neutral slope={state['ema9_slope']:+.2f}%"

    elif direction == "SHORT":
        if state["rally_bear"]:
            boost  = boost_amount
            reason = (
                f"EMA9 RALLY ↓ vol={state['vol_ratio']:.2f}× "
                f"slope={state['ema9_slope']:+.2f}% "
                f"price_vs_ema9={state['price_vs_ema9']:+.2f}%"
            )
        elif state["flip_bear"]:
            boost  = boost_amount * 0.4
            reason = f"EMA9 FLIP ↓ reciente slope={state['ema9_slope']:+.2f}%"
        elif state["ema9_slope"] > 0.1:
            boost  = -4.0
            reason = f"EMA9 alcista (slope={state['ema9_slope']:+.2f}%) — penalización SHORT"
        else:
            boost  = 0.0
            reason = f"EMA9 neutral slope={state['ema9_slope']:+.2f}%"
    else:
        reason = "direction_none"

    log.debug("[ema9_rally] dir=%s boost=%+.1f block=%s | %s", direction, boost, block, reason)
    return round(boost, 1), reason, block


# ── SL dinámico basado en EMA9 ────────────────────────────────────────────────

def ema9_sl_price(klines: list, direction: str = "LONG", margin_pct: float = 0.3) -> float:
    """
    Calcula el SL dinámico basado en EMA9 (en vez de ATR fijo).

    Para LONG: SL = EMA9 × (1 - margin_pct/100)
    Para SHORT: SL = EMA9 × (1 + margin_pct/100)

    El margin_pct da algo de margen para evitar whipsaws en EMA9.
    Default 0.3% de margen.

    Uso en scanner.py (opcional — reemplaza el SL de ATR):
      if getattr(C, 'EMA9_SL_ENABLED', False):
          ema9_sl = ema9_sl_price(k3m, sig.direction, margin_pct=0.3)
          if ema9_sl > 0:
              sig.sl = ema9_sl
    """
    if len(klines) < 15:
        return 0.0
    closes      = [k[4] for k in klines]
    ema9_series = _ema(closes, 9)
    ema9_val    = ema9_series[-1]
    if ema9_val <= 0:
        return 0.0
    if direction == "LONG":
        return ema9_val * (1 - margin_pct / 100)
    else:
        return ema9_val * (1 + margin_pct / 100)


# ── Test ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import math

    # Simular un Flip & Rally alcista
    klines = []
    price = 100.0
    for i in range(80):
        if i < 20:
            price -= 0.3                           # tendencia bajista
        elif i == 20:
            price += 3.0                           # FLIP: ruptura fuerte de EMA9
        elif i < 30:
            price -= 0.1                           # RETEST: corrección suave
        else:
            price += 0.4 + math.sin(i/5) * 0.2   # RALLY: tendencia alcista

        o = price - 0.2 if i > 20 else price + 0.2
        c = price + 0.2 if i > 20 else price - 0.2
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        # Volumen alto en el flip y en el rally
        vol = 2000 if i == 20 else (1500 if i > 30 and i % 5 == 0 else 800)
        klines.append([i, o, h, l, c, float(vol)])

    state = analyze_ema9_state(klines)
    print("Estado actual 9 EMA Flip & Rally:")
    for k, v in state.items():
        print(f"  {k}: {v}")

    boost, reason, block = ema9_flip_rally_filter(klines, "LONG")
    print(f"\nFiltro LONG: boost={boost} block={block}")
    print(f"Reason: {reason}")

    sl = ema9_sl_price(klines, "LONG")
    print(f"SL dinámico EMA9: {sl:.4f}")
