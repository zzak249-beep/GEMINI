"""
wavelet_causal.py — Descomposición wavelet ESTRICTAMENTE CAUSAL.

EL PROBLEMA QUE RESUELVE
------------------------
El MRA (multiresolution analysis), tanto en DWT como en MODWT, NO es
causal. Para calcular el coeficiente en el instante t usa datos desde
t-L+1 hasta t+L-1, donde L es la longitud del filtro. Con un filtro de
longitud 8 eso son 7 barras del futuro. Además, el padding del borde
derecho (simétrico, periódico o reflejado) fabrica datos que aún no
existen.

Consecuencia práctica: en backtest la señal parece excelente, y en vivo
el coeficiente de la última vela CAMBIA cada vez que llega una vela
nueva. La señal que disparó la entrada desaparece al mirarla después.
Eso es repintado, y hace que cualquier métrica de backtest sea ficción.

LA SOLUCIÓN
-----------
La única familia wavelet que permite usar los coeficientes para
predicción sin mirar al futuro es la à trous de Haar:

    S_0(t)     = precio(t)
    S_{j+1}(t) = [S_j(t) + S_j(t - 2^j)] / 2
    w_{j+1}(t) = S_j(t) - S_{j+1}(t)

Cada coeficiente solo mira hacia atrás. No hay diezmado, así que es
invariante a desplazamiento: el valor en la barra t es el mismo hoy que
dentro de un mes.

El precio a pagar: Haar tiene una forma de onda discontinua y peor
selectividad en frecuencia que Daubechies o symlets. Pero esos filtros
más suaves son justo los que rompen la causalidad. En trading en vivo,
causal y tosco gana a elegante con lookahead.

CONTRATO
--------
compute_signal_causal(df, params, last_signal_ts) devuelve un dict con
las mismas claves que signal_engine.compute_signal, para poder
compararlos barra a barra (ver test_causality.py) y, si convence,
sustituirlo sin tocar poller.py ni scanner.py.

AVISO: la regla de DIRECCIÓN de aquí es una reconstrucción razonable,
no la tuya. Compárala contra tu motor antes de sustituir nada. Lo que
sí es objetivo y no opinable es la descomposición causal.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Descomposición
# --------------------------------------------------------------------------- #
def atrous_haar(x, levels: int):
    """Descomposición à trous de Haar, causal.

    Devuelve (details, smooth, primera_barra_valida):
      details[j] = w_{j+1}, array de la misma longitud que x
      smooth     = S_levels
      primera_barra_valida = 2**levels - 1

    Las primeras 2^levels - 1 barras están contaminadas por el borde
    izquierdo (no hay suficiente historia) y salen como NaN. En MODWT
    estándar y en à trous solo el borde IZQUIERDO está afectado, que es
    justo lo que hace utilizable el método: el borde derecho, que es
    donde operamos, está limpio.
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    if levels < 1:
        raise ValueError("levels debe ser >= 1")

    s = x.copy()
    details = []
    for j in range(levels):
        desplazamiento = 2 ** j
        s_next = np.full(n, np.nan)
        # S_{j+1}(t) = [S_j(t) + S_j(t - 2^j)] / 2
        if n > desplazamiento:
            s_next[desplazamiento:] = (s[desplazamiento:] + s[:-desplazamiento]) / 2.0
        details.append(s - s_next)   # w_{j+1}(t) = S_j(t) - S_{j+1}(t)
        s = s_next

    return details, s, min(2 ** levels - 1, n)


def _levels_desde_ventana(approx_len: int) -> int:
    """APPROX_LEN es la ventana efectiva de la aproximación. En à trous,
    el nivel J cubre 2^J barras, así que J = log2(APPROX_LEN)."""
    if approx_len < 2:
        return 1
    return max(1, int(math.floor(math.log2(approx_len))))


# --------------------------------------------------------------------------- #
# Energía por escala
# --------------------------------------------------------------------------- #
def energias_por_escala(details, lookback: int, hasta: int = None,
                        normalizar: bool = True):
    """Energía (suma de cuadrados) de cada nivel en la ventana móvil que
    termina en `hasta` (inclusive).

    normalizar=True divide cada energía por el número de coeficientes
    válidos. Sin normalizar, los niveles altos parecen sistemáticamente
    más energéticos solo por cómo se acumula la varianza con la escala,
    y la dominancia mide entonces la escala, no el régimen.
    """
    if hasta is None:
        hasta = len(details[0]) - 1
    inicio = max(0, hasta - lookback + 1)

    salida = []
    for d in details:
        trozo = d[inicio:hasta + 1]
        trozo = trozo[~np.isnan(trozo)]
        if len(trozo) == 0:
            salida.append(0.0)
            continue
        e = float(np.sum(trozo ** 2))
        if normalizar:
            e /= len(trozo)
        salida.append(e)
    return salida


def dominancia(energias):
    """Cuánto destaca la escala más energética sobre la media del resto.

    1.0 = ninguna escala destaca (mercado sin estructura dominante,
    ruido repartido por igual). Cuanto más alto, más concentrada está la
    energía en una escala concreta -- que es la lectura de "régimen
    tendencial" frente a "rango".

    Devuelve (ratio, indice_escala_dominante).
    """
    if not energias:
        return 0.0, -1
    idx = int(np.argmax(energias))
    resto = [e for i, e in enumerate(energias) if i != idx]
    if not resto:
        return 1.0, idx
    media_resto = float(np.mean(resto))
    if media_resto <= 0:
        return float("inf") if energias[idx] > 0 else 1.0, idx
    return energias[idx] / media_resto, idx


# --------------------------------------------------------------------------- #
# ATR causal
# --------------------------------------------------------------------------- #
def atr_wilder(high, low, close, length: int):
    """ATR de Wilder. Causal por construcción (media exponencial de los
    true ranges pasados). Devuelve un array de la misma longitud."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    tr = np.full(n, np.nan)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    atr = np.full(n, np.nan)
    if n <= length:
        return atr
    atr[length] = np.mean(tr[1:length + 1])
    for i in range(length + 1, n):
        atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


# --------------------------------------------------------------------------- #
# Señal de referencia
# --------------------------------------------------------------------------- #
def compute_signal_causal(df: pd.DataFrame, params: dict, last_signal_ts=None) -> dict:
    """Señal sobre la ÚLTIMA vela del DataFrame (que debe estar cerrada).

    Mismas claves de salida que signal_engine.compute_signal, para poder
    compararlos barra a barra sin adaptadores.

    Regla de dirección: la pendiente de la aproximación S_J entre la
    barra anterior y la actual. Es la componente de baja frecuencia que
    queda tras quitar el ruido de las escalas finas, así que su signo es
    la dirección estructural del tramo -- no un cruce de medias
    disfrazado, porque S_J procede de la misma descomposición que la
    dominancia.
    """
    lookback = int(params.get("lookback_energy", 40))
    k_dom = float(params.get("k_dominance", params.get("dominance_threshold", 1.5)))
    atr_len = int(params.get("atr_length", 14))
    mult_sl = float(params.get("atr_mult_sl", 1.5))
    mult_tp = float(params.get("atr_mult_tp", 2.5))
    cooldown_bars = int(params.get("cooldown_bars", 0))
    bar_ms = int(params.get("bar_ms", 5 * 60 * 1000))
    approx_len = int(params.get("approx_len", 8))
    normalizar = bool(params.get("normalize_scales", True))
    permitir_long = bool(params.get("allow_long", True))
    permitir_short = bool(params.get("allow_short", True))

    niveles = _levels_desde_ventana(approx_len)

    cierre = df["close"].to_numpy(dtype=float)
    n = len(cierre)

    ts_col = "open_time" if "open_time" in df.columns else df.columns[0]
    timestamp = int(df[ts_col].iloc[-1])

    vacio = {
        "long_cond": False, "short_cond": False, "is_trending": False,
        "close": float(cierre[-1]) if n else None,
        "sl": None, "tp": None, "timestamp": timestamp,
        "dominance": 0.0, "dominant_scale": -1, "atr": None,
        "levels": niveles, "reason": None,
    }

    minimo = max(2 ** niveles + lookback, atr_len + 2)
    if n < minimo:
        vacio["reason"] = f"faltan velas ({n} < {minimo})"
        return vacio

    details, smooth, primera_valida = atrous_haar(cierre, niveles)
    t = n - 1
    if t < primera_valida + 1:
        vacio["reason"] = "barra dentro de la zona de borde izquierdo"
        return vacio

    energias = energias_por_escala(details, lookback, hasta=t, normalizar=normalizar)
    ratio, idx_dom = dominancia(energias)
    en_regimen = ratio >= k_dom

    atr_serie = atr_wilder(df["high"], df["low"], df["close"], atr_len)
    atr = atr_serie[t]
    if atr is None or np.isnan(atr) or atr <= 0:
        vacio["reason"] = "ATR no disponible"
        vacio["dominance"] = ratio
        return vacio

    pendiente = smooth[t] - smooth[t - 1]
    if np.isnan(pendiente):
        vacio["reason"] = "aproximación no disponible en esta barra"
        return vacio

    # Cooldown por barras desde la última señal (mismo criterio que el
    # Pine original: evita reentrar en la misma estructura).
    if last_signal_ts and cooldown_bars > 0:
        barras = (timestamp - int(last_signal_ts)) / bar_ms
        if barras < cooldown_bars:
            vacio["dominance"] = ratio
            vacio["is_trending"] = en_regimen
            vacio["atr"] = float(atr)
            vacio["reason"] = f"cooldown ({barras:.0f}/{cooldown_bars} barras)"
            return vacio

    long_cond = bool(en_regimen and pendiente > 0 and permitir_long)
    short_cond = bool(en_regimen and pendiente < 0 and permitir_short)

    precio = float(cierre[t])
    sl = tp = None
    if long_cond:
        sl = precio - mult_sl * atr
        tp = precio + mult_tp * atr
    elif short_cond:
        sl = precio + mult_sl * atr
        tp = precio - mult_tp * atr

    # Red de seguridad: si el redondeo o un ATR degenerado dejan SL/TP
    # inválidos, se descarta la señal en vez de mandar una orden con un
    # stop que se dispara al abrir.
    if long_cond and not (sl < precio < tp):
        long_cond = False
        sl = tp = None
    if short_cond and not (tp < precio < sl):
        short_cond = False
        sl = tp = None

    return {
        "long_cond": long_cond,
        "short_cond": short_cond,
        "is_trending": bool(en_regimen),
        "close": precio,
        "sl": float(sl) if sl is not None else None,
        "tp": float(tp) if tp is not None else None,
        "timestamp": timestamp,
        "dominance": float(ratio),
        "dominant_scale": int(idx_dom),
        "atr": float(atr),
        "levels": niveles,
        "slope": float(pendiente),
        "energies": [float(e) for e in energias],
        "reason": None,
    }


def klines_to_df(rows) -> pd.DataFrame:
    """Mismo papel que signal_engine.klines_to_df, para poder usar este
    módulo sin depender del otro. BingX devuelve dicts con claves
    nombradas -- ojo, 'close' va ANTES que 'high' en la respuesta real,
    así que se parsea por nombre y nunca por posición."""
    filas = []
    for k in rows or []:
        if isinstance(k, dict):
            filas.append({
                "open_time": int(k.get("time", k.get("open_time", 0))),
                "open": float(k["open"]), "high": float(k["high"]),
                "low": float(k["low"]), "close": float(k["close"]),
                "volume": float(k.get("volume", 0)),
            })
        else:
            filas.append({
                "open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            })
    df = pd.DataFrame(filas)
    if not df.empty:
        df = df.sort_values("open_time").reset_index(drop=True)
    return df
