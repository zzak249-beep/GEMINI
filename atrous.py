"""
atrous.py — Transformada à trous causal. SOLO MIDE, no decide nada.

═══════════════════════════════════════════════════════════════════════
POR QUÉ ESTÁ AQUÍ Y POR QUÉ NO TOCA NINGUNA DECISIÓN
═══════════════════════════════════════════════════════════════════════
El motor del bot (signal_engine.py) usa una aproximación floja: el
"detalle a escala n" es la diferencia entre dos SMA de n barras. No
reconstruye la serie y su filtro de régimen se enciende el 93% del
tiempo sobre ruido puro — o sea que no filtra.

Esta es la à trous de verdad. Pero activarla como criterio CAMBIARÍA
qué opera el bot, y eso invalida las 39 operaciones de evidencia que ya
están pagadas. Con t=0.71 no sobra muestra para gastarla.

Así que aquí no decide: se calcula en cada señal y se ANOTA en
senales_todas.csv. Dentro de ocho semanas se comparan las señales donde
la à trous estaba de acuerdo contra las que no. Si separa ganadoras de
perdedoras, se activa CON DATOS. Si no separa, se descarta y no se ha
perdido nada.

═══════════════════════════════════════════════════════════════════════
DECISIONES DE DISEÑO, Y POR QUÉ
═══════════════════════════════════════════════════════════════════════
FILTRO HAAR, NO B3. El à trous original es SIMÉTRICO y centrado, y por
eso tiene retardo cero -- pero mira al futuro. Hacerlo unilateral
conserva la causalidad y aparece un retardo que nadie suele mencionar:

    filtro        c1    c2    c3    c4    c5
    B3 Spline    2.0   6.0  14.0  30.0  62.0   barras de retardo
    Haar         0.5   1.5   3.5   7.5  15.5

Con B3 y 5 niveles, la "tendencia macro" va 62 barras por detrás: más
de cinco horas en 5m. Con Haar, 15.5. Cuatro veces menos.

RUIDO POR MAD, NO POR DESVIACIÓN TÍPICA. El estimador estándar
(Donoho-Johnstone) es la desviación absoluta mediana de la escala más
fina dividida por 0.6745. Usar la desviación típica de cada banda mete
la señal DENTRO de la estimación de ruido, así que el umbral se infla
justo cuando hay estructura -- suaviza más precisamente cuando no
debería.

RECONSTRUCCIÓN EXACTA. Sin umbralado, close = c_J + Σ w_j al bit. Eso
no es una virtud: es una identidad. El "retardo cero sin umbralado" que
presumen algunos scripts se consigue no haciendo nada.
"""
from __future__ import annotations

import statistics as st


def descomponer(closes: list[float], niveles: int = 4):
    """
    à trous de Haar, causal. Devuelve (c_J, [w_1 … w_J]).

    c_{j+1}(t) = [c_j(t) + c_j(t - 2^j)] / 2
    w_{j+1}(t) = c_j(t) - c_{j+1}(t)

    Cada coeficiente usa close[t-2^j … t] y nada más. El valor de la
    barra t no cambia nunca al llegar t+1.
    """
    n = len(closes)
    if n < 2 ** niveles + 2:
        return None, None
    c = list(closes)
    detalles: list[list[float]] = []
    for j in range(niveles):
        lag = 2 ** j
        nxt = list(c)
        for t in range(lag, n):
            nxt[t] = (c[t] + c[t - lag]) / 2.0
        detalles.append([c[t] - nxt[t] for t in range(n)])
        c = nxt
    return c, detalles


def _mad_sigma(x: list[float]) -> float:
    """Desviación absoluta mediana / 0.6745. Robusta a valores extremos."""
    if len(x) < 5:
        return 0.0
    med = st.median(x)
    mad = st.median([abs(v - med) for v in x])
    return mad / 0.6745


def denoise(closes: list[float], niveles: int = 4, fuerza: float = 1.0,
            ventana: int = 50):
    """
    Reconstrucción con umbralado suave: w' = sign(w)·max(|w|−λ, 0).

    fuerza=0 devuelve el precio exacto (identidad telescópica).
    Devuelve (serie_denoised, c_J) o (None, None) si faltan datos.
    """
    trend, det = descomponer(closes, niveles)
    if trend is None:
        return None, None
    n = len(closes)
    limpio = list(trend)
    for j, w in enumerate(det):
        seg = w[max(2 ** niveles, n - ventana):]
        sigma = _mad_sigma(seg)
        # Más umbral a las escalas finas, donde se concentra el ruido.
        lam = fuerza * sigma * (1.5 ** ((len(det) - 1 - j) / max(len(det) - 1, 1)))
        for t in range(n):
            v = w[t]
            d = abs(v) - lam
            limpio[t] += (d if v >= 0 else -d) if d > 0 else 0.0
    return limpio, trend


def contexto(closes: list[float], side: str, niveles: int = 4,
             fuerza: float = 1.0, ventana: int = 50) -> dict:
    """
    Lo que se anota en cada señal. NO decide nada.

    side: "LONG" o "SHORT" (el lado que el bot va a operar).

    Devuelve:
      at_pendiente   pendiente de la serie denoised en la última barra
      at_macro       pendiente de c_J (la tendencia lenta)
      at_acuerdo     1 si las dos van a favor del lado, 0 si no
      at_ruido       fracción de energía en las dos escalas finas
      at_retardo     barras de retardo de c_J (informativo, fijo por nivel)
    """
    vacio = {"at_pendiente": None, "at_macro": None, "at_acuerdo": None,
             "at_ruido": None, "at_retardo": None}
    limpio, trend = denoise(closes, niveles, fuerza, ventana)
    if limpio is None or len(limpio) < 3:
        return vacio

    pend = limpio[-1] - limpio[-2]
    macro = trend[-1] - trend[-2]
    signo = 1.0 if side.upper() in ("LONG", "BUY") else -1.0

    _, det = descomponer(closes, niveles)
    warm = 2 ** niveles
    energias = []
    for w in det:
        seg = w[max(warm, len(w) - ventana):]
        energias.append(sum(v * v for v in seg) / max(len(seg), 1))
    total = sum(energias)
    ruido = (energias[0] + energias[1]) / total if total > 0 and len(energias) > 1 else None

    # Retardo de grupo acumulado del filtro de Haar unilateral: Σ 2^j / 2
    retardo = sum(2 ** j for j in range(niveles)) / 2.0

    return {
        "at_pendiente": round(pend, 10),
        "at_macro": round(macro, 10),
        "at_acuerdo": int(pend * signo > 0 and macro * signo > 0),
        "at_ruido": round(ruido, 4) if ruido is not None else None,
        "at_retardo": retardo,
    }
