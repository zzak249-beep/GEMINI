"""
comparar_senales.py — ¿Habrían ido mejor las señales que el bot descartó?

Con MAX_CONCURRENT_POSITIONS=1 y varias señales por ciclo, el bot opera
la primera que encuentra hueco. El orden lo fija el volumen de 24h.
Nadie eligió ese criterio: salió de cómo está escrito el bucle.

Este script responde si ese criterio es bueno, malo o indiferente —con
datos, no con opinión. Lee senales_todas.csv (lo genera poller.py),
simula QUÉ HABRÍA PASADO con cada señal usando su propio SL/TP, y
compara ejecutadas contra descartadas.

    python comparar_senales.py senales_todas.csv

Si las descartadas van mejor, hay un ranking que aprender. Si van igual,
el orden da igual y puedes dejar de pensar en esto. Las dos respuestas
valen.
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
import time

import requests

BINANCE = "https://fapi.binance.com/fapi/v1/klines"
MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000}


def velas(symbol: str, desde_ms: int, barras: int, interval: str):
    """Velas DESDE la señal hacia adelante, para ver cómo se resolvió."""
    sym = symbol.replace("-", "").upper()
    try:
        r = requests.get(BINANCE, timeout=20, params={
            "symbol": sym, "interval": interval,
            "startTime": desde_ms, "limit": min(barras, 1000)})
        if r.status_code != 200:
            return []
        return [{"h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
                for k in r.json()]
    except requests.RequestException:
        return []


def resolver(s: dict, interval: str, max_barras: int = 288):
    """
    Simula la operación: ¿tocó antes el SL o el TP?

    Si en la misma vela se tocan los dos, se supone el PEOR caso (stop).
    Suponer el mejor es la forma más común de inflar un resultado sin
    darse cuenta.
    """
    v = velas(s["symbol"], s["ts"] + MS.get(interval, 300_000), max_barras, interval)
    if not v:
        return None, None
    largo = s["side"] == "LONG"
    riesgo = abs(s["price"] - s["sl"])
    if riesgo <= 0:
        return None, None
    for i, k in enumerate(v):
        toca_sl = k["l"] <= s["sl"] if largo else k["h"] >= s["sl"]
        toca_tp = k["h"] >= s["tp"] if largo else k["l"] <= s["tp"]
        if toca_sl:
            return -1.0, i + 1
        if toca_tp:
            bruto = (s["tp"] - s["price"]) if largo else (s["price"] - s["tp"])
            return bruto / riesgo, i + 1
    # no resolvió en la ventana: se cierra a mercado al final
    ult = v[-1]["c"]
    bruto = (ult - s["price"]) if largo else (s["price"] - ult)
    return bruto / riesgo, len(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--max-barras", type=int, default=288,
                    help="cuántas barras seguir cada señal (288 = 24h en 5m)")
    a = ap.parse_args()

    filas = []
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        for x in csv.DictReader(f):
            try:
                filas.append({
                    "symbol": x["symbol"], "side": x["side"],
                    "ts": int(float(x["ts_señal"])),
                    "price": float(x["price"]), "sl": float(x["sl"]),
                    "tp": float(x["tp"]),
                    "ejecutada": x.get("ejecutada") in ("1", "True", "true"),
                })
            except (KeyError, ValueError, TypeError):
                continue
    if not filas:
        print("Sin señales legibles en el CSV.")
        return 1

    ej = sum(1 for x in filas if x["ejecutada"])
    print(f"{len(filas)} señales · {ej} ejecutadas · {len(filas)-ej} descartadas")
    print("Simulando cada una con su propio SL/TP...\n")

    for i, s in enumerate(filas, 1):
        s["r"], s["barras"] = resolver(s, a.tf, a.max_barras)
        marca = "EJEC" if s["ejecutada"] else "    "
        r = f"{s['r']:+.2f}R" if s["r"] is not None else "sin datos"
        print(f"  [{i}/{len(filas)}] {marca} {s['symbol']:16} {r}")
        time.sleep(0.12)

    con = [x for x in filas if x["r"] is not None]
    ej = [x for x in con if x["ejecutada"]]
    de = [x for x in con if not x["ejecutada"]]

    print("\n" + "=" * 60)
    print("RESULTADO")
    print("=" * 60)
    for nombre, g in (("EJECUTADAS", ej), ("DESCARTADAS", de)):
        if len(g) < 5:
            print(f"{nombre}: solo {len(g)}, muestra insuficiente")
            continue
        rs = [x["r"] for x in g]
        print(f"{nombre:12} n={len(rs):>4} · acierto "
              f"{sum(1 for x in rs if x>0)/len(rs):>4.0%} · "
              f"media {st.mean(rs):+.3f}R")

    if len(ej) >= 10 and len(de) >= 10:
        dif = st.mean([x["r"] for x in de]) - st.mean([x["r"] for x in ej])
        print(f"\nDiferencia (descartadas - ejecutadas): {dif:+.3f}R")
        if dif > 0.15:
            print("-> Las que DESCARTAS van MEJOR. El criterio actual")
            print("   (volumen de 24h descendente) te está eligiendo las peores.")
            print("   Merece la pena buscar un ranking con features.py.")
        elif dif < -0.15:
            print("-> Las ejecutadas van MEJOR. El orden por volumen está")
            print("   funcionando: más liquidez = menos coste. No lo toques.")
        else:
            print("-> Van IGUAL. El orden no importa; con un solo hueco estás")
            print("   tomando una muestra aleatoria de tus propias señales.")
            print("   Si quieres más resultado, la palanca es MAX_CONCURRENT,")
            print("   no el ranking -- y eso sube el riesgo, no lo baja.")
    else:
        print("\nHacen falta 10+ de cada grupo para comparar.")

    salida = a.csv.replace(".csv", "") + "_resuelto.csv"
    with open(salida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "side", "ejecutada", "r", "barras"])
        w.writeheader()
        for x in filas:
            w.writerow({k: x.get(k) for k in w.fieldnames})
    print(f"\nDetalle en: {salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
