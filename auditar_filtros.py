"""
auditar_filtros.py — ¿cuánto te cuesta cada uno de tus filtros?

═══════════════════════════════════════════════════════════════════════
POR QUÉ ESTO CASI NADIE PUEDE HACERLO
═══════════════════════════════════════════════════════════════════════
Todo el mundo tiene el historial de las operaciones que HIZO. Nadie tiene
el de las que descartó, porque nunca ocurrieron. Sin ese contrafactual no
se puede responder la pregunta que decide si un filtro sirve:

    ¿lo que este filtro bloqueó habría ganado o perdido?

Un filtro que rechaza perdedoras te ahorra dinero. Uno que rechaza
ganadoras te lo cuesta, y no te enteras nunca: no aparece en ninguna
métrica, porque esas operaciones no están en tu cuenta.

Tú SÍ puedes: poller.py escribe TODAS las señales en senales_todas.csv,
ejecutadas o no, con el motivo del rechazo y con su precio, SL y TP. Esas
tres cifras bastan para resolver qué habría pasado contra velas reales.

═══════════════════════════════════════════════════════════════════════
QUÉ HACE
═══════════════════════════════════════════════════════════════════════
Agrupa las señales por MOTIVO de rechazo, resuelve cada una con velas
reales de BingX (su propio SL/TP, coste descontado, stop si se tocan los
dos) y devuelve, por cada filtro:

    n · media R · t · veredicto

    AHORRA     lo que bloqueó perdía   -> el filtro se queda
    CUESTA     lo que bloqueó ganaba   -> el filtro te quita dinero
    INDIFERENTE  no distingue          -> quítalo y simplifica

═══════════════════════════════════════════════════════════════════════
LO QUE NO PUEDE
═══════════════════════════════════════════════════════════════════════
- Las rechazadas nunca se ejecutaron, así que no hay deslizamiento real:
  la simulación es idealizada y algo optimista para ellas. Una diferencia
  pequeña a favor de las descartadas NO es concluyente.
- "Sin hueco" no es un filtro, es una limitación de capital. Sale en el
  informe pero su lectura es otra: mide qué te cuesta operar con una
  posición a la vez.
- Estás mirando VARIOS filtros a la vez. Con 6 motivos distintos, el
  umbral de significación sube: |t| >= 2 ya no vale, hace falta ~2.64.
  El script lo aplica y lo dice.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import statistics as st
import sys
import time

import requests

BASE = "https://open-api.bingx.com"
MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
      "1h": 3_600_000, "4h": 14_400_000}
COSTE_PCT = 0.25
HORAS = 24.0
PACING = 0.12


def velas(symbol: str, desde_ms: int, barras: int, interval: str):
    try:
        r = requests.get(f"{BASE}/openApi/swap/v3/quote/klines", timeout=20,
                         params={"symbol": symbol, "interval": interval,
                                 "startTime": desde_ms, "limit": min(barras, 1000)})
        if r.status_code != 200:
            return []
        d = r.json()
        if d.get("code") not in (0, None):
            return []
        filas = d.get("data", [])
    except requests.RequestException:
        return []
    out = []
    for k in filas:
        try:
            if isinstance(k, dict):
                out.append({"t": int(k.get("time", 0)), "h": float(k["high"]),
                            "l": float(k["low"]), "c": float(k["close"])})
            else:
                out.append({"t": int(k[0]), "h": float(k[2]),
                            "l": float(k[3]), "c": float(k[4])})
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    out.sort(key=lambda x: x["t"])
    return [v for v in out if v["t"] >= desde_ms]


def resolver(s: dict, horas: float, coste_pct: float):
    """R neta de la señal. Si se tocan stop y objetivo en la misma vela,
    manda el STOP: no sabemos el orden intravela y suponer lo contrario
    es la forma más común de inflar un resultado sin darse cuenta."""
    interval = s.get("timeframe") or "5m"
    barra = MS.get(interval, 300_000)
    n = max(int(horas * 3_600_000 / barra), 2)
    v = velas(s["symbol"], s["ts"] + barra, n, interval)
    if not v:
        return None
    largo = s["side"] == "LONG"
    riesgo = abs(s["price"] - s["sl"])
    if riesgo <= 0:
        return None
    coste_r = (coste_pct / 100.0 * s["price"]) / riesgo
    for k in v:
        if (k["l"] <= s["sl"]) if largo else (k["h"] >= s["sl"]):
            return -1.0 - coste_r
        if (k["h"] >= s["tp"]) if largo else (k["l"] <= s["tp"]):
            bruto = (s["tp"] - s["price"]) if largo else (s["price"] - s["tp"])
            return bruto / riesgo - coste_r
    ult = v[-1]["c"]
    bruto = (ult - s["price"]) if largo else (s["price"] - ult)
    return bruto / riesgo - coste_r


def clase(motivo: str) -> str:
    """Agrupa por CLASE de motivo, no por el texto exacto.

    'margen insuficiente: necesita 10.00 y hay 3.20' y '...necesita 12.00
    y hay 1.10' son el MISMO filtro. Sin agrupar, cada rechazo sería su
    propia categoría de n=1 y no se podría medir nada.
    """
    m = (motivo or "").strip()
    if not m:
        return "EJECUTADA"
    m = re.sub(r"[\d.,]+", "N", m.lower())
    tabla = [
        ("sin hueco", "sin hueco (capacidad, no filtro)"),
        ("modo manual", "modo manual"),
        ("margen insuficiente", "margen insuficiente"),
        ("sin fondos", "sin fondos"),
        ("circuit breaker", "circuit breaker"),
        ("orientación", "orientación SL/TP"),
        ("nocional", "tope de nocional"),
        ("mínimo de", "suelo de nocional"),
        ("riesgo", "techo de riesgo"),
        ("stop demasiado estrecho", "stop estrecho"),
        ("leverage", "apalancamiento aplicado"),
        # El direccional ANTES que el de posiciones: su texto contiene las
        # dos palabras y el orden de esta tabla decide cuál gana.
        ("misma apuesta", "tope direccional"),
        ("posiciones", "tope de posiciones"),
        ("redondeo", "cantidad redondea a 0"),
    ]
    for clave, nombre in tabla:
        if clave in m:
            return nombre
    return m[:40]


def resumen(nombre: str, rs: list, tumbral: float) -> str:
    n = len(rs)
    if n < 5:
        return f"  {nombre:<34} n={n:<4} muestra insuficiente"
    m = st.mean(rs)
    sd = st.pstdev(rs) if n > 1 else 1.0
    t = m * math.sqrt(n) / sd if sd > 1e-9 else 0.0
    if abs(t) < tumbral:
        ver = "INDIFERENTE"
    elif m < 0:
        ver = "AHORRA"
    else:
        ver = "CUESTA"
    aviso = "  ⚠" if n < 87 else ""
    return (f"  {nombre:<34} n={n:<4} {m:+.3f}R  t={t:+5.2f}  {ver}{aviso}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--horas", type=float, default=HORAS)
    ap.add_argument("--coste", type=float, default=COSTE_PCT)
    a = ap.parse_args()

    filas = []
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with open(a.csv, newline="", encoding=enc) as f:
                for x in csv.DictReader(f):
                    try:
                        filas.append({
                            "symbol": x["symbol"], "side": x["side"],
                            "ts": int(float(x["ts_señal"])),
                            "timeframe": (x.get("timeframe") or "5m").strip(),
                            "price": float(x["price"]), "sl": float(x["sl"]),
                            "tp": float(x["tp"]),
                            "motivo": clase(x.get("motivo_no_ejecutada", "")),
                        })
                    except (KeyError, ValueError, TypeError):
                        continue
            break
        except UnicodeDecodeError:
            filas = []
    if not filas:
        print("Sin señales legibles en el CSV.")
        return 1

    grupos = {}
    for x in filas:
        grupos.setdefault(x["motivo"], []).append(x)

    print(f"{len(filas)} señales · {len(grupos)} motivos distintos")
    print(f"Seguimiento {a.horas:.0f} h · coste {a.coste}%\n")
    for k, v in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(v):>5}  {k}")

    print("\nResolviendo contra velas reales de BingX...\n")
    for i, s in enumerate(filas, 1):
        s["r"] = resolver(s, a.horas, a.coste)
        if i % 25 == 0:
            print(f"  ... {i}/{len(filas)}", flush=True)
        time.sleep(PACING)

    con = [x for x in filas if x["r"] is not None]
    grupos = {}
    for x in con:
        grupos.setdefault(x["motivo"], []).append(x["r"])

    # Umbral corregido por número de filtros mirados a la vez. Elegir el
    # peor de 6 no es una prueba, son 6.
    k = max(sum(1 for v in grupos.values() if len(v) >= 5), 1)
    p = 0.05 / (2.0 * k)
    tq = math.sqrt(-2.0 * math.log(p))
    tumbral = tq - ((2.515517 + 0.802853 * tq + 0.010328 * tq * tq) /
                    (1 + 1.432788 * tq + 0.189269 * tq * tq + 0.001308 * tq ** 3))

    print("\n" + "=" * 70)
    print(f"QUÉ BLOQUEÓ CADA FILTRO  ({len(con)} resueltas de {len(filas)})")
    print(f"Umbral |t| >= {tumbral:.2f}  ({k} filtros con muestra, Bonferroni)")
    print("=" * 70)
    ejec = grupos.pop("EJECUTADA", [])
    if ejec:
        print(resumen("EJECUTADAS (referencia)", ejec, tumbral))
        print()
    for nombre, rs in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
        print(resumen(nombre, rs, tumbral))

    print("\n" + "-" * 70)
    print("AHORRA      = lo que bloqueó perdía. El filtro se queda.")
    print("CUESTA      = lo que bloqueó ganaba. Te está quitando dinero.")
    print("INDIFERENTE = no distingue. Quítalo y simplifica.")
    print("⚠ = menos de 87 operaciones: no detecta ventajas por debajo de 0.30 R.")
    print("\nOJO: las rechazadas nunca se ejecutaron, así que su simulación no")
    print("lleva deslizamiento y sale algo optimista. Una diferencia pequeña a")
    print("su favor NO es concluyente.")

    salida = a.csv.replace(".csv", "") + "_filtros.csv"
    with open(salida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "side", "timeframe", "motivo", "r"])
        w.writeheader()
        for x in filas:
            w.writerow({kk: x.get(kk) for kk in w.fieldnames})
    print(f"\nDetalle en: {salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
