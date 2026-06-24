"""
Market Structure — ChoCh Direction Filter
══════════════════════════════════════════════════════════════════════════════
Adaptación del indicador 'Market Structure Trend Matrix [BigBeluga]' para
Python puro, sin dependencias externas.

Concepto central: Change of Character (ChoCh)
  ChoCh ↑: precio cierra POR ENCIMA de un swing high reciente
           → la estructura del mercado ha cambiado a alcista
           → señal más objetiva que las EMAs (no es un slope, es una ruptura)

  ChoCh ↓: precio cierra POR DEBAJO de un swing low reciente
           → estructura bajista confirmada

Diferencia vs slope_filter (multi_tf_slope_alignment):
  - slope_filter: EMA9 > EMA21 por X barras → tendencia suave y lagging
  - ChoCh: precio > pivot_high reciente → ruptura binaria y más rápida

Diferencia vs BOS de SMC:
  - BOS (Break of Structure) requiere que el swing sea significativo
  - ChoCh es la primera ruptura, antes de que se confirme como tendencia

Uso en zesty-reverence (kotegawa_scanner):
  El ChoCh ↑ en 1H después de un dip confirma que el rebote está empezando
  con evidencia estructural — no solo que el RSI está sobrevendido.

  from market_structure import choch_direction, ms_filter
  direction, ph_val, pl_val = choch_direction(k1h, ms_len=10)
  boost, reason, block = ms_filter(k1h, "LONG", ms_len=10)

Uso en joyful-art (scanner):
  En 3m: ms_len=10 → busca pivots en ±30min → niveles intraday relevantes
  Añadir después del slope filter como confirmación adicional.

Configurable con:
  MS_ENABLED=true/false (default false — validar primero)
  MS_LEN=10             (barras de lookback/lookahead para pivots)
  MS_ATR_LEN=14         (periodo ATR para trailing stop)
  MS_ATR_MULT=4.0       (multiplicador ATR para trailing stop)
  MS_REQUIRE_CHOCH=false (true = bloquea si no hay ChoCh reciente)
══════════════════════════════════════════════════════════════════════════════
"""
import logging

log = logging.getLogger("market_structure")


# ── Helpers matemáticos ───────────────────────────────────────────────────────

def _rma(values: list, period: int) -> list:
    n = len(values)
    out = [0.0] * n
    if n == 0:
        return out
    alpha = 1.0 / period
    for i in range(n):
        out[i] = (sum(values[:i+1]) / (i+1)) if i < period else \
                 (out[i-1] + alpha * (values[i] - out[i-1]))
    return out


def _atr(klines: list, period: int = 14) -> float:
    """ATR de las últimas `period` barras."""
    if len(klines) < period + 1:
        return (klines[-1][2] - klines[-1][3]) if klines else 0.0
    tr = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i][2], klines[i][3], klines[i-1][4]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    rma = _rma(tr, period)
    return rma[-1] if rma else tr[-1]


def _find_pivot_high(klines: list, ms_len: int) -> tuple:
    """
    Encuentra el pivot high más reciente.
    Un pivot high es un bar cuyo high es >= todos los ms_len bars a cada lado.

    Returns (ph_value, ph_bar_index_from_end) o (None, None)

    Nota: igual que Pine ta.pivothigh(msLen, msLen) — el pivot se confirma
    msLen barras después, por eso buscamos desde -(ms_len+1) hacia atrás.
    """
    n = len(klines)
    # El último pivot confirmado está en bar -(ms_len+1) como mínimo
    for i in range(n - ms_len - 1, ms_len - 1, -1):
        candidate = klines[i][2]   # high
        # Verificar que todos los ms_len bars a cada lado son menores
        left_ok  = all(klines[i-j][2] <= candidate for j in range(1, ms_len+1))
        right_ok = all(klines[i+j][2] <= candidate for j in range(1, min(ms_len+1, n-i)))
        if left_ok and right_ok:
            return candidate, i
    return None, None


def _find_pivot_low(klines: list, ms_len: int) -> tuple:
    """
    Encuentra el pivot low más reciente.
    Returns (pl_value, pl_bar_index_from_end) o (None, None)
    """
    n = len(klines)
    for i in range(n - ms_len - 1, ms_len - 1, -1):
        candidate = klines[i][3]   # low
        left_ok  = all(klines[i-j][3] >= candidate for j in range(1, ms_len+1))
        right_ok = all(klines[i+j][3] >= candidate for j in range(1, min(ms_len+1, n-i)))
        if left_ok and right_ok:
            return candidate, i
    return None, None


# ── ChoCh Detection ───────────────────────────────────────────────────────────

def choch_direction(klines: list, ms_len: int = 10) -> tuple:
    """
    Calcula la dirección actual de Market Structure usando pivots.

    Simula la variable `direction` del indicador Pine — rastrea si el último
    cambio de estructura fue alcista (ChoCh ↑) o bajista (ChoCh ↓).

    Returns:
        (direction: str, ph_value: float, pl_value: float)
        direction = "BULL" | "BEAR" | "NEUTRAL"

    Lógica:
      1. Encuentra el pivot high y pivot low más recientes confirmados
      2. Si el precio actual está POR ENCIMA del ph_val → estructura alcista
      3. Si está POR DEBAJO del pl_val → estructura bajista
      4. Si está entre ambos → sin ruptura estructural → neutral

    Esto es equivalente al estado final de `direction` en el Pine Script,
    aunque sin rastrear el historial de crossovers (stateless = más robusto
    en Python donde cada ciclo es independiente).
    """
    if len(klines) < ms_len * 3:
        return "NEUTRAL", None, None

    closes = [k[4] for k in klines]
    curr   = closes[-1]
    prev   = closes[-2] if len(closes) > 1 else curr

    ph_val, ph_idx = _find_pivot_high(klines, ms_len)
    pl_val, pl_idx = _find_pivot_low(klines, ms_len)

    if ph_val is None or pl_val is None:
        return "NEUTRAL", ph_val, pl_val

    # ChoCh ↑: precio cruzó encima del pivot high (crossover)
    # ChoCh ↓: precio cruzó debajo del pivot low  (crossunder)
    # Estado actual (no solo el momento del cruce):
    if curr > ph_val:
        return "BULL", ph_val, pl_val
    elif curr < pl_val:
        return "BEAR", ph_val, pl_val
    else:
        return "NEUTRAL", ph_val, pl_val


def choch_fresh_crossover(klines: list, ms_len: int = 10) -> tuple:
    """
    Detecta si hay un ChoCh RECIENTE (crossover en las últimas 3 barras).

    Más estricto que choch_direction() — útil como boost de timing de entrada:
    no solo "estamos en estructura alcista" sino "acabamos de romper el pivot
    high hace menos de 3 barras", lo que sugiere momentum inminente.

    Returns:
        (is_fresh: bool, direction: str, description: str)
    """
    if len(klines) < ms_len * 3 + 3:
        return False, "NEUTRAL", "insufficient_data"

    ph_val, _ = _find_pivot_high(klines, ms_len)
    pl_val, _ = _find_pivot_low(klines, ms_len)

    if ph_val is None or pl_val is None:
        return False, "NEUTRAL", "no_pivots"

    closes = [k[4] for k in klines]

    # Buscar crossover en las últimas 3 barras
    for lookback in range(1, 4):
        curr = closes[-lookback]
        prev = closes[-lookback - 1] if lookback + 1 <= len(closes) else curr
        if prev <= ph_val < curr:
            return True, "BULL", f"ChoCh↑ hace {lookback}bar @ ph={ph_val:.6f}"
        if prev >= pl_val > curr:
            return True, "BEAR", f"ChoCh↓ hace {lookback}bar @ pl={pl_val:.6f}"

    return False, "NEUTRAL", "no_fresh_cross"


def atr_trailing_stop(klines: list,
                       atr_period: int  = 14,
                       atr_mult: float  = 4.0,
                       direction: str   = "BULL") -> float:
    """
    Calcula el ATR Trailing Stop del indicador.
    Con atrMult=4.0 es más conservador que el trail del bot (TRAIL_DISTANCE_ATR=2.0)
    — útil para comparar si el stop del bot está razonablemente colocado.
    """
    if not klines:
        return 0.0
    atr_val  = _atr(klines, atr_period)
    curr_close = klines[-1][4]
    if direction == "BULL":
        return curr_close - atr_val * atr_mult
    else:
        return curr_close + atr_val * atr_mult


# ── Filtro principal para scanner / kotegawa_scanner ─────────────────────────

def ms_filter(
    klines:    list,
    direction: str  = "LONG",
    ms_len:    int  = 10,
    atr_period: int = 14,
    require_fresh: bool = False,
) -> tuple:
    """
    Filtro de Market Structure para scanner.py y kotegawa_scanner.py.

    Returns: (boost: float, reason: str, block: bool)

    Lógica:
      - Si estructura alcista (ChoCh ↑) y señal LONG  → boost +6
      - Si estructura bajista (ChoCh ↓) y señal SHORT → boost +6
      - Si estructura contradice la señal             → penalización -4
      - Si require_fresh=True y no hay ChoCh reciente → sin boost

    Para zesty (1H klines, ms_len=10):
      10 barras × 1H = ±10H de lookback para pivots
      Detecta cambios de estructura diarios/semanales

    Para joyful-art (3m klines, ms_len=10):
      10 barras × 3m = ±30min de lookback para pivots
      Detecta cambios de estructura intraday
    """
    ms_direction, ph_val, pl_val = choch_direction(klines, ms_len)

    # Boost por alineación
    boost = 0.0
    block = False

    if ms_direction == "BULL":
        if direction == "LONG":
            if require_fresh:
                is_fresh, _, fresh_desc = choch_fresh_crossover(klines, ms_len)
                boost = 6.0 if is_fresh else 2.0
                label = f"ChoCh↑ {'FRESH' if is_fresh else 'viejo'} {fresh_desc}"
            else:
                boost = 4.0
                label = f"ChoCh↑ struct BULL ph={ph_val:.6f}"
        else:  # SHORT contra estructura alcista
            boost = -4.0
            label = f"ChoCh↑ CONTRA SHORT — struct alcista"

    elif ms_direction == "BEAR":
        if direction == "SHORT":
            if require_fresh:
                is_fresh, _, fresh_desc = choch_fresh_crossover(klines, ms_len)
                boost = 6.0 if is_fresh else 2.0
                label = f"ChoCh↓ {'FRESH' if is_fresh else 'viejo'} {fresh_desc}"
            else:
                boost = 4.0
                label = f"ChoCh↓ struct BEAR pl={pl_val:.6f}"
        else:  # LONG contra estructura bajista
            boost = -4.0
            label = f"ChoCh↓ CONTRA LONG — struct bajista"

    else:
        # NEUTRAL: sin ruptura estructural → sin boost, sin penalización
        boost = 0.0
        label = f"MS neutral (precio entre ph={ph_val} y pl={pl_val})"

    log.debug("[ms_filter] %s | dir=%s boost=%+.0f", label, direction, boost)
    return round(boost, 1), label, block


# ── Test rápido ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Simular: precio sube, rompe un pivot high reciente
    klines = []
    price = 100.0
    for i in range(80):
        if i < 30:
            price += 0.5   # sube
        elif i < 45:
            price -= 0.2   # forma pivot high y baja
        else:
            price += 0.6   # sube y rompe el pivot
        o = price - 0.3
        c = price + 0.3 if i % 3 != 0 else price - 0.1
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        klines.append([i, o, h, l, c, 1000.0])

    direction, ph, pl = choch_direction(klines, ms_len=10)
    print(f"Market structure direction: {direction}")
    print(f"Pivot high: {ph}, Pivot low: {pl}")
    print(f"Current price: {klines[-1][4]:.2f}")

    is_fresh, fd, fdesc = choch_fresh_crossover(klines, ms_len=10)
    print(f"Fresh ChoCh: {is_fresh} ({fdesc})")

    boost, reason, block = ms_filter(klines, "LONG", ms_len=10)
    print(f"\nFiltro LONG: boost={boost} block={block}")
    print(f"Reason: {reason}")

    boost2, reason2, block2 = ms_filter(klines, "SHORT", ms_len=10)
    print(f"\nFiltro SHORT: boost={boost2} block={block2}")
    print(f"Reason: {reason2}")
