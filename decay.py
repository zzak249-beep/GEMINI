"""
decay.py — mide a qué velocidad se desvanece la señal de basis, y si alguna
vez llega a pagar el peaje.

═══════════════════════════════════════════════════════════════════════
LA PREGUNTA
═══════════════════════════════════════════════════════════════════════
El panel de 15 minutos dejó dos cosas claras:

    IC(basis_z -> retorno futuro, transversal)   +0.079 a 15 min
    el mismo IC esperando una sola vela          +0.005

O sea: toda la ventaja vive DENTRO del primer intervalo de captura, y con
capturas cada 15 min no se puede ver dónde muere. Puede ser a los 30
segundos (entonces es precio rancio y no hay nada que hacer) o a los 8
minutos (entonces hay un bot posible, con ejecución maker).

La diferencia entre esas dos respuestas es la diferencia entre cerrar la
línea y abrir otra. Este módulo la contesta capturando cada 60 s en vez de
cada 900, y midiendo el IC en horizontes de 1, 2, 3, 5, 10, 15, 30 y 60 min.

═══════════════════════════════════════════════════════════════════════
POR QUÉ CUESTA CASI NADA
═══════════════════════════════════════════════════════════════════════
/quote/premiumIndex devuelve TODOS los símbolos en UNA llamada. No hacen
falta klines ni openInterest por símbolo. Una captura completa del universo
son 1-2 peticiones, así que 60 s de cadencia son ~2.900 peticiones al día:
menos de lo que el bot de crowding gasta ahora en un solo ciclo.

Se guarda el basis CRUDO, no el z. El z depende de la ventana y la ventana
es justo uno de los parámetros que hay que probar; guardando el crudo se
puede recalcular con cualquier ventana sin volver a capturar.

═══════════════════════════════════════════════════════════════════════
EL COSTE NO SE ESTIMA, SE MIDE
═══════════════════════════════════════════════════════════════════════
En el análisis anterior puse 0,11% de coste de ida y vuelta a ojo. Aquí se
apunta el bid y el ask reales de cada símbolo en cada captura, así que el
umbral que la señal tiene que superar sale del libro, no de mi suposición.
Se informan las dos ejecuciones por separado:

    taker:  spread completo + 2 x comisión taker
    maker:  0 x spread + 2 x comisión maker   (asume que te llenan; no
            siempre te llenan, y justo cuando la señal es buena es cuando
            menos te llenan — ese sesgo NO está medido aquí)

═══════════════════════════════════════════════════════════════════════
EL CONTROL
═══════════════════════════════════════════════════════════════════════
Ya nos ha pasado tres veces en este proyecto que una "mejora" daba beneficio
también sobre paseos aleatorios. Así que cada IC se acompaña de su nulo
empírico: se baraja basis_z DENTRO de cada instantánea (lo que destruye la
relación señal-retorno pero conserva la estructura transversal de los
retornos) y se recalcula todo DECAY_BARAJAS veces. El p empírico es la
fracción de barajas que iguala o supera lo observado. Si el IC real no sale
de su propio nulo, no hay nada, por mucho que el t parezca grande.

Además el t-Student se calcula SOLO sobre instantáneas no solapadas (una de
cada h), porque con capturas cada minuto y horizonte de 60 min hay 60
ventanas compartiendo las mismas velas y el t normal saldría inflado ~8x.

NO OPERA. NO PIDE CLAVES DE API. Solo endpoints públicos de BingX.
"""
from __future__ import annotations

import csv
import glob
import logging
import math
import os
import random
import signal
import statistics as st
import subprocess
import sys
import time
from collections import defaultdict, deque
from operator import mul

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("decay")

BASE = "https://open-api.bingx.com"
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(
    pool_connections=8, pool_maxsize=8,
    max_retries=Retry(total=2, backoff_factor=0.4,
                      status_forcelist=(429, 500, 502, 503, 504))))


def env(k, d):
    v = os.getenv(k)
    if v is None or v == "":
        return d
    if isinstance(d, bool):
        return str(v).strip().lower() in ("1", "true", "yes", "si", "sí", "on")
    if isinstance(d, int) and not isinstance(d, bool):
        try:
            return int(float(v))
        except ValueError:
            return d
    if isinstance(d, float):
        try:
            return float(v)
        except ValueError:
            return d
    return v


CFG = {
    "DIR": env("DECAY_DIR", "/data/decay"),
    "CADA_SEG": env("DECAY_CADA_SEG", 60.0),
    "DIAS": env("DECAY_DIAS", 7),              # días de CSV que se conservan
    "MIN_SIMBOLOS": env("DECAY_MIN_SIMBOLOS", 40),
    "MIN_VOL_USDT": env("DECAY_MIN_VOL_USDT", 2_000_000.0),
    "Z_VENTANA": env("DECAY_Z_VENTANA", 240),  # capturas para el z (240 = 4 h a 60 s)
    "Z_MIN": env("DECAY_Z_MIN", 60),           # mínimo para que el z valga algo
    "INFORME_H": env("DECAY_INFORME_H", 6.0),  # cada cuánto se manda el informe
    "ANALISIS_H": env("DECAY_ANALISIS_H", 48.0),  # historia que entra en el informe
    "MAX_PARES": env("DECAY_MAX_PARES", 1200),    # tope de ventanas por horizonte
    "BARAJAS": env("DECAY_BARAJAS", 200),
    "COM_TAKER": env("DECAY_COM_TAKER", 0.045),  # % por lado
    "COM_MAKER": env("DECAY_COM_MAKER", 0.020),  # % por lado
    "TG_TOKEN": env("TG_TOKEN", ""),
    "TG_CHAT": env("TG_CHAT", ""),
}

# Horizontes en MINUTOS. Se traducen a capturas dividiendo por la cadencia.
HORIZONTES = [1, 2, 3, 5, 10, 15, 30, 60]
# Esperas antes de entrar, en minutos. La prueba que mata el precio rancio.
ESPERAS = [0, 1, 2, 5]

COLS = ["ts", "symbol", "px", "basis", "funding", "bid", "ask"]

_parar = False


def _senal(*_):
    global _parar
    _parar = True


# ──────────────────────────────────────────────────────── API pública
def _get(path: str, params: dict | None = None, intentos: int = 3):
    for i in range(intentos):
        try:
            r = SESSION.get(BASE + path, params=params or {}, timeout=12)
            r.raise_for_status()
            j = r.json()
            if str(j.get("code", 0)) not in ("0", "None"):
                return None
            return j.get("data")
        except Exception as e:
            if i == intentos - 1:
                log.debug("%s falló: %s", path, e)
            time.sleep(0.4 * (i + 1))
    return None


def universo() -> set[str]:
    """Símbolos -USDT activos con volumen suficiente. Se refresca cada hora:
    filtrar por volumen evita que el decil corto se llene de símbolos que no
    se pueden operar y que arrastran el resultado con ruido de tick."""
    d = _get("/openApi/swap/v2/quote/ticker")
    if not d:
        return set()
    out = set()
    for t in d:
        try:
            s = t.get("symbol", "")
            if s.endswith("-USDT") and float(t.get("quoteVolume") or 0) >= CFG["MIN_VOL_USDT"]:
                out.add(s)
        except (TypeError, ValueError):
            continue
    return out


def premium() -> dict[str, dict]:
    d = _get("/openApi/swap/v2/quote/premiumIndex")
    if not d:
        return {}
    if isinstance(d, dict):
        d = [d]
    out = {}
    for it in d:
        try:
            s = it.get("symbol")
            mark = float(it.get("markPrice") or 0)
            idx = float(it.get("indexPrice") or 0)
            if not s or mark <= 0 or idx <= 0:
                continue
            out[s] = {"px": mark,
                      "basis": (mark - idx) / idx * 100.0,
                      "funding": float(it.get("lastFundingRate") or 0) * 100.0}
        except (TypeError, ValueError):
            continue
    return out


def libro() -> dict[str, tuple]:
    """Mejor bid/ask de todos los símbolos. Si el endpoint no existe en esta
    cuenta, se devuelve vacío y el informe lo dice en vez de inventarse un
    spread."""
    for path in ("/openApi/swap/v2/quote/bookTicker", "/openApi/swap/v1/ticker/bookTicker"):
        d = _get(path, intentos=1)
        if not d:
            continue
        if isinstance(d, dict):
            d = [d]
        out = {}
        for it in d:
            try:
                s = it.get("symbol")
                b = float(it.get("bidPrice") or 0)
                a = float(it.get("askPrice") or 0)
                if s and b > 0 and a > b:
                    out[s] = (b, a)
            except (TypeError, ValueError):
                continue
        if out:
            return out
    return {}


# ──────────────────────────────────────────────────────── captura
def ruta_dia(ts: float) -> str:
    return os.path.join(CFG["DIR"], "decay_%s.csv" % time.strftime("%Y%m%d", time.gmtime(ts)))


def podar():
    """Borra los CSV más viejos que DIAS. Sin esto el volumen se llena y el
    contenedor muere sin avisar."""
    try:
        corte = time.time() - CFG["DIAS"] * 86400
        for f in glob.glob(os.path.join(CFG["DIR"], "decay_*.csv")):
            if os.path.getmtime(f) < corte:
                os.remove(f)
                log.info("podado %s", os.path.basename(f))
    except Exception:
        log.exception("podar falló")


def capturar(uni: set[str]) -> int:
    pr = premium()
    if not pr:
        return 0
    lb = libro()
    filas = []
    ts = int(time.time())
    for s, v in pr.items():
        if uni and s not in uni:
            continue
        b, a = lb.get(s, (0.0, 0.0))
        filas.append({"ts": ts, "symbol": s, "px": "%.10g" % v["px"],
                      "basis": round(v["basis"], 6),
                      "funding": round(v["funding"], 6),
                      "bid": "%.10g" % b, "ask": "%.10g" % a})
    if len(filas) < CFG["MIN_SIMBOLOS"]:
        return 0
    ruta = ruta_dia(ts)
    os.makedirs(CFG["DIR"], exist_ok=True)
    nuevo = not os.path.exists(ruta) or os.path.getsize(ruta) == 0
    with open(ruta, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerows(filas)
    return len(filas)


# ──────────────────────────────────────────────────────── estadística
def rangos(v: list[float]) -> list[float]:
    """Rangos con empates promediados."""
    n = len(v)
    o = sorted(range(n), key=lambda i: v[i])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and v[o[j + 1]] == v[o[i]]:
            j += 1
        a = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[o[k]] = a
        i = j + 1
    return r


def spearman_rangos(rx: list[float], ry: list[float]):
    n = len(rx)
    if n < 8:
        return None
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else None


def cargar() -> tuple[list[int], dict]:
    """Devuelve (ts ordenados, {ts: {symbol: fila}}) de las últimas
    ANALISIS_H horas. Más historia no mejora el informe y sí lo hace lento:
    lo que se mide aquí es un efecto de minutos, no de semanas."""
    corte = time.time() - CFG["ANALISIS_H"] * 3600
    snaps: dict[int, dict] = defaultdict(dict)
    for f in sorted(glob.glob(os.path.join(CFG["DIR"], "decay_*.csv"))):
        if os.path.getmtime(f) < corte - 86400:
            continue
        try:
            with open(f, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        if int(r["ts"]) < corte:
                            continue
                        px = float(r["px"])
                        if px <= 0:
                            continue
                        b = float(r["bid"] or 0)
                        a = float(r["ask"] or 0)
                        snaps[int(r["ts"])][r["symbol"]] = {
                            "px": px, "basis": float(r["basis"]),
                            "spread": (a - b) / px * 100.0 if a > b > 0 else None}
                    except (ValueError, KeyError, TypeError):
                        continue
        except Exception:
            log.exception("no pude leer %s", f)
    return sorted(snaps), snaps


def calcular_z(ts: list[int], snaps: dict) -> dict:
    """basis_z de cada símbolo en cada instantánea, usando SOLO las capturas
    anteriores. Ventana deslizante, sin mirar al futuro."""
    vent, zmin = int(CFG["Z_VENTANA"]), int(CFG["Z_MIN"])
    # Sumas deslizantes. Recalcular media y desviación sobre la ventana entera
    # en cada símbolo y cada captura cuesta O(ventana) y con capturas de 60 s
    # son ~124 millones de operaciones por informe: dos minutos largos. Con
    # suma y suma de cuadrados incrementales es O(1) y baja a segundos.
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=vent))
    acum: dict[str, list] = defaultdict(lambda: [0.0, 0.0])   # [suma, suma²]
    z: dict[int, dict] = {}
    for t in ts:
        fila = {}
        for s, v in snaps[t].items():
            h, ac = hist[s], acum[s]
            n = len(h)
            if n >= zmin:
                m = ac[0] / n
                var = ac[1] / n - m * m
                if var > 1e-18:                  # el redondeo puede darla <0
                    fila[s] = (v["basis"] - m) / math.sqrt(var)
            b = v["basis"]
            if n == vent:                        # la deque va a expulsar el más viejo
                viejo = h[0]
                ac[0] -= viejo
                ac[1] -= viejo * viejo
            h.append(b)
            ac[0] += b
            ac[1] += b * b
        z[t] = fila
    return z


def pares(ts, snaps, z, paso_h: int, espera: int):
    """Prepara, UNA sola vez, los rangos de basis_z y del retorno futuro de
    cada instantánea. Barajar después es permutar una lista ya ordenada, no
    volver a leer y reordenar el panel entero: es lo que hace que 200 barajas
    tarden segundos en vez de minutos.

    Devuelve [(rx, ry, i)] con i = índice de la instantánea, que hace falta
    luego para quedarse solo con ventanas que no comparten velas."""
    out = []
    tol = (espera + paso_h) * CFG["CADA_SEG"] * 2.0
    for i in range(len(ts) - espera - paso_h):
        t0, ta, tb = ts[i], ts[i + espera], ts[i + espera + paso_h]
        if tb - t0 > tol:
            continue
        zz = z.get(t0, {})
        a, b = snaps[ta], snaps[tb]
        com = [s for s in zz if s in a and s in b]
        if len(com) < CFG["MIN_SIMBOLOS"]:
            continue
        rets = [(b[s]["px"] / a[s]["px"] - 1.0) * 100.0 for s in com]
        m = sum(rets) / len(rets)
        rets = [x - m for x in rets]              # neutral al mercado
        rx, ry = rangos([zz[s] for s in com]), rangos(rets)
        n = len(rx)
        mx, my = sum(rx) / n, sum(ry) / n
        dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
        dy = math.sqrt(sum((b - my) ** 2 for b in ry))
        if dx <= 0 or dy <= 0:
            continue
        # Barajar rx es una permutación del MISMO multiconjunto, así que mx y
        # dx no cambian. Precalculados aquí, cada baraja se reduce a un
        # producto escalar: cinco veces más rápido que recalcular Spearman.
        out.append((rx, ry, i, n * mx * my, dx * dy))
    # Tope de ventanas. El nulo cuesta BARAJAS x len(out) x n log n, así que
    # sin tope una semana de capturas tarda media hora y el bucle se queda sin
    # capturar. Se submuestrea REGULARMENTE (no al azar) para no romper la
    # alternancia par/impar de la que depende el conteo de no solapadas.
    tope = int(CFG["MAX_PARES"])
    if tope > 0 and len(out) > tope:
        paso = len(out) / tope
        out = [out[int(k * paso)] for k in range(tope)]
    return out


def ic_de_pares(ps, paso_h: int, barajar=False, rnd=None):
    """(todos los IC, los de ventanas no solapadas). Permutar los rangos de x
    es exactamente permutar basis_z: el ranking es biyectivo."""
    todos, indep = [], []
    sh = rnd.shuffle if barajar else None
    for rx, ry, i, corr, den in ps:
        if sh is not None:
            rx = rx[:]
            sh(rx)
        v = (sum(map(mul, rx, ry)) - corr) / den
        todos.append(v)
        if i % max(paso_h, 1) == 0:               # ventanas que no comparten velas
            indep.append(v)
    return todos, indep


def ic_serie(ts, snaps, z, paso_h: int, espera: int):
    return ic_de_pares(pares(ts, snaps, z, paso_h, espera), paso_h)


def decil(ts, snaps, z, paso_h: int):
    """Rendimiento POR PATA de la cartera larga-corta por deciles de basis_z,
    y spread mediano de los símbolos que entran en las patas."""
    rr, sp = [], []
    tol = paso_h * CFG["CADA_SEG"] * 2.0
    for i in range(len(ts) - paso_h):
        t0, t1 = ts[i], ts[i + paso_h]
        if t1 - t0 > tol:
            continue
        zz = z.get(t0, {})
        a, b = snaps[t0], snaps[t1]
        com = [s for s in zz if s in a and s in b]
        if len(com) < CFG["MIN_SIMBOLOS"]:
            continue
        com.sort(key=lambda s: zz[s])
        k = max(3, len(com) // 10)
        rt = {s: (b[s]["px"] / a[s]["px"] - 1.0) * 100.0 for s in com}
        m = sum(rt.values()) / len(rt)
        hi = sum(rt[s] - m for s in com[-k:]) / k
        lo = sum(rt[s] - m for s in com[:k]) / k
        rr.append((hi - lo) / 2.0)                # por pata, no por par
        for s in com[:k] + com[-k:]:
            if a[s]["spread"] is not None:
                sp.append(a[s]["spread"])
    return rr, sp


def informe() -> str:
    ts, snaps = cargar()
    if len(ts) < 120:
        return ("decay · aún no hay suficiente historia\n"
                "capturas: %d (hacen falta 120 y una ventana de z de %d)"
                % (len(ts), CFG["Z_MIN"]))

    cad = CFG["CADA_SEG"]
    horas = (ts[-1] - ts[0]) / 3600.0
    z = calcular_z(ts, snaps)
    con_z = sum(1 for t in ts if z.get(t))
    rnd = random.Random(12345)

    L = []
    L.append("DECAY · dónde muere la señal de basis")
    L.append("%d capturas · %.1f h · cadencia %.0fs · z sobre %d capturas"
             % (len(ts), horas, cad, CFG["Z_VENTANA"]))
    L.append("universo mediano %d símbolos · %d capturas con z válido"
             % (int(st.median([len(snaps[t]) for t in ts])), con_z))
    L.append("")

    # ── coste medido, no supuesto
    sp_todos = [v["spread"] for t in ts for v in snaps[t].values()
                if v["spread"] is not None]
    if sp_todos:
        sp_med = st.median(sp_todos)
        coste_taker = sp_med + 2 * CFG["COM_TAKER"]
        coste_maker = 2 * CFG["COM_MAKER"]
        L.append("COSTE MEDIDO (spread mediano del libro %.4f%%)" % sp_med)
    else:
        sp_med = float("nan")
        coste_taker = 0.11
        coste_maker = 2 * CFG["COM_MAKER"]
        L.append("COSTE (sin bid/ask del endpoint — taker es una SUPOSICIÓN)")
    L.append("  taker %.4f%% ida+vuelta   ·   maker %.4f%% ida+vuelta"
             % (coste_taker, coste_maker))
    L.append("")

    # ── curva de decaimiento
    L.append("IC transversal por horizonte  (+ = basis_z alto sube)")
    L.append("%6s %8s %8s %7s %8s" % ("horiz", "IC", "t(indep)", "p nulo", "n"))
    mejor = None
    for hm in HORIZONTES:
        ph = max(1, int(round(hm * 60 / cad)))
        ps = pares(ts, snaps, z, ph, 0)
        todos, indep = ic_de_pares(ps, ph)
        if len(todos) < 30:
            continue
        mic = sum(todos) / len(todos)
        if len(indep) > 3:
            sd = st.stdev(indep)
            t = mic / (sd / math.sqrt(len(indep))) if sd > 0 else float("nan")
        else:
            t = float("nan")
        # nulo empírico: mismo panel, misma estructura, señal barajada
        peor = 0
        for _ in range(CFG["BARAJAS"]):
            nn, _u = ic_de_pares(ps, ph, barajar=True, rnd=rnd)
            if nn and abs(sum(nn) / len(nn)) >= abs(mic):
                peor += 1
        p = (peor + 1) / (CFG["BARAJAS"] + 1)
        L.append("%5dm %+8.4f %8.2f %7.3f %8d" % (hm, mic, t, p, len(todos)))
        if mejor is None or abs(mic) > abs(mejor[1]):
            mejor = (hm, mic, p)
    umbral_b = 0.05 / len(HORIZONTES)
    L.append("  p nulo = fracción de %d barajas que iguala el IC observado."
             % CFG["BARAJAS"])
    L.append("  Bonferroni por %d horizontes: exige p < %.4f"
             % (len(HORIZONTES), umbral_b))
    if 1.0 / (CFG["BARAJAS"] + 1) > umbral_b:
        # Con pocas barajas el p mínimo posible ya está por encima del umbral
        # y NINGÚN resultado podría pasar nunca: el test parecería funcionar
        # mientras rechaza todo por construcción.
        L.append("  AVISO: con %d barajas el p mínimo es %.4f y nada puede"
                 % (CFG["BARAJAS"], 1.0 / (CFG["BARAJAS"] + 1)))
        L.append("  pasar el umbral. Sube DECAY_BARAJAS a %d o más."
                 % int(math.ceil(1.0 / umbral_b)))
    L.append("")

    # ── la prueba que mata el precio rancio
    L.append("MISMA SEÑAL, ENTRANDO TARDE  (horizonte fijo 5 min)")
    ph5 = max(1, int(round(5 * 60 / cad)))
    for em in ESPERAS:
        pe = int(round(em * 60 / cad))
        todos, _ = ic_serie(ts, snaps, z, ph5, pe)
        if todos:
            L.append("  esperar %2d min   IC %+.4f   (n=%d)"
                     % (em, sum(todos) / len(todos), len(todos)))
    L.append("  Si cae a cero con 1 min de espera es precio rancio, no señal.")
    L.append("")

    # ── ¿paga el peaje?
    L.append("CARTERA DECIL · ¿el bruto supera el coste?")
    L.append("%6s %9s %10s %10s" % ("horiz", "bruto%", "neto taker", "neto maker"))
    viable = []
    for hm in HORIZONTES:
        ph = max(1, int(round(hm * 60 / cad)))
        rr, sp = decil(ts, snaps, z, ph)
        if len(rr) < 30:
            continue
        br = sum(rr) / len(rr)
        ct = (st.median(sp) + 2 * CFG["COM_TAKER"]) if sp else coste_taker
        L.append("%5dm %+9.4f %+10.4f %+10.4f" % (hm, br, br - ct, br - coste_maker))
        if br - coste_maker > 0:
            viable.append(hm)
    L.append("")

    # ── veredicto
    L.append("VEREDICTO")
    if mejor is None:
        L.append("  Sin IC medible en ningún horizonte. No hay señal que perseguir.")
    else:
        hm, mic, p = mejor
        umbral = umbral_b
        if p > umbral:
            L.append("  IC máximo %+.4f a %d min, pero p=%.3f > %.4f (Bonferroni)."
                     % (mic, hm, p, umbral))
            L.append("  No se distingue de barajar la señal al azar. Cerrar la línea.")
        elif not viable:
            L.append("  IC real (%+.4f a %d min, p=%.3f) pero NINGÚN horizonte"
                     % (mic, hm, p))
            L.append("  cubre ni siquiera el coste maker. Hay señal, no hay negocio.")
        else:
            L.append("  IC %+.4f a %d min, p=%.3f, y rentable en maker a: %s"
                     % (mic, hm, p, ", ".join("%dm" % h for h in viable)))
            L.append("  Siguiente paso NO es operar: es medir el llenado maker.")
            L.append("  El sesgo de selección adverso en las órdenes pasivas no")
            L.append("  está medido aquí y suele comerse la mitad del bruto.")
    return "\n".join(L)


# ──────────────────────────────────────────────────────── telegram
def avisar(texto: str):
    if not CFG["TG_TOKEN"] or not CFG["TG_CHAT"]:
        print(texto)
        return
    for trozo in [texto[i:i + 3800] for i in range(0, len(texto), 3800)]:
        try:
            requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG["TG_TOKEN"],
                          json={"chat_id": CFG["TG_CHAT"],
                                "text": "<pre>%s</pre>" % trozo,
                                "parse_mode": "HTML"}, timeout=15)
        except Exception:
            log.exception("telegram falló")


# ──────────────────────────────────────────────────────── bucle
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _senal)
    signal.signal(signal.SIGINT, _senal)

    # El volumen, antes de nada. Sin él el contenedor escribe en su disco
    # efímero, todo parece ir bien durante horas y el primer redespliegue se
    # lleva la captura entera. Es el fallo más caro posible aquí: no da error,
    # solo borra el trabajo de dos días.
    try:
        os.makedirs(CFG["DIR"], exist_ok=True)
        prueba = os.path.join(CFG["DIR"], ".escritura")
        with open(prueba, "w") as fh:
            fh.write("ok")
        os.remove(prueba)
    except OSError as e:
        msg = ("decay NO arranca: no puedo escribir en %s (%s).\n"
               "Monta el volumen en /data o cambia DECAY_DIR." % (CFG["DIR"], e))
        avisar(msg)
        log.error(msg)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "informe":
        avisar(informe())
        return

    # Sin universo, capturar() no filtra por volumen y el panel se llena de
    # símbolos ilíquidos cuyo ruido de tick domina los deciles. Es un fallo
    # silencioso: el CSV crece igual y el informe sale mal. Se insiste.
    uni = set()
    for intento in range(6):
        uni = universo()
        if uni:
            break
        log.warning("universo vacío (intento %d/6); reintento", intento + 1)
        time.sleep(min(30, 5 * (intento + 1)))
    t_uni = time.time()
    if not uni:
        avisar("decay NO arranca: no consigo la lista de símbolos con volumen.\n"
               "Sin ella capturaría todo el mercado sin filtrar y el informe\n"
               "saldría contaminado. Revisa la salida de red del servicio.")
        log.error("sin universo tras 6 intentos; salgo")
        return
    log.info("universo inicial: %d símbolos (vol >= %.0f USDT)",
             len(uni), CFG["MIN_VOL_USDT"])
    avisar("decay arrancado · %d símbolos · captura cada %.0fs · informe cada %.1fh"
           % (len(uni), CFG["CADA_SEG"], CFG["INFORME_H"]))

    t_inf = time.time()
    t_poda = 0.0
    n_ok = n_fallo = 0
    hijo = None

    while not _parar:
        ini = time.time()
        try:
            if ini - t_uni > 3600:
                nu = universo()
                if nu:
                    uni, t_uni = nu, ini
            if capturar(uni):
                n_ok += 1
            else:
                n_fallo += 1
            if ini - t_poda > 3600:
                podar()
                t_poda = ini
            if ini - t_inf >= CFG["INFORME_H"] * 3600:
                t_inf = ini
                if hijo is None or hijo.poll() is not None:
                    # En subproceso a propósito. El informe tarda minutos y en
                    # este mismo proceso se comería las capturas de esos
                    # minutos, que es justo la resolución que vinimos a medir.
                    log.info("lanzando informe (%d ok, %d fallidas)", n_ok, n_fallo)
                    hijo = subprocess.Popen([sys.executable, os.path.abspath(__file__),
                                             "informe"], env=os.environ.copy())
                else:
                    log.warning("el informe anterior sigue corriendo; me lo salto")
        except Exception:
            log.exception("ciclo falló")
            n_fallo += 1
        resto = CFG["CADA_SEG"] - (time.time() - ini)
        # Se duerme a trocitos para que SIGTERM de Railway no tarde un minuto.
        while resto > 0 and not _parar:
            time.sleep(min(2.0, resto))
            resto -= 2.0

    log.info("parando · %d capturas ok, %d fallidas", n_ok, n_fallo)


if __name__ == "__main__":
    main()
