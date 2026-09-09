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

CORRECCIONES respecto a la primera versión:
  - Las velas se piden a BINGX (endpoint público, sin firma -- el mismo
    que usa test_causality.py), no a Binance. Las señales se generaron
    con precios de BingX; resolverlas con velas de otro exchange mete
    basis y, sobre todo, HUECOS: con SYMBOLS=ALL la mayoría de señales
    son altcoins, y las que solo cotizan en BingX no existen en Binance
    -- se descartaban en silencio como "sin datos". Eso sesgaba la
    comparación hacia las monedas grandes, justo lo contrario de lo que
    hace falta para juzgar el universo ALL.
  - El timeframe se lee de la propia fila del CSV (columna 'timeframe',
    que poller.py ya escribe en cada señal), no de un --tf global. Con
    --tf por defecto en "5m" y el bot corriendo en 15m, el punto de
    arranque para buscar el desenlace (ts_señal + 5 min) caía DENTRO de
    la misma vela que generó la señal, no después -- lookahead en el
    propio script que se supone que audita el bot.
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
import time

import requests

BINGX_BASE_URL = "https://open-api.bingx.com"
MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
     "1h": 3_600_000, "4h": 14_400_000}


def velas(symbol: str, desde_ms: int, barras: int, interval: str):
    """Velas DESDE la señal hacia adelante, para ver cómo se resolvió.

    Endpoint público de BingX (sin firma), el mismo que usa
    test_causality.py -- así el precio de resolución es del MISMO
    exchange donde se habría ejecutado la orden, y cubre los símbolos
    de cola larga que Binance no lista."""
    try:
        r = requests.get(
            f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines",
            timeout=20,
            params={
                "symbol": symbol, "interval": interval,
                "startTime": desde_ms, "limit": min(barras, 1000),
            },
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if data.get("code") not in (0, None):
            return []
        filas = data.get("data", [])
    except requests.RequestException:
        return []

    salida = []
    for k in filas:
        try:
            if isinstance(k, dict):
                t = int(k.get("time", k.get("open_time", 0)))
                salida.append({"t": t, "h": float(k["high"]),
                               "l": float(k["low"]), "c": float(k["close"])})
            else:
                salida.append({"t": int(k[0]), "h": float(k[2]),
                               "l": float(k[3]), "c": float(k[4])})
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    salida.sort(key=lambda x: x["t"])
    # Descarta cualquier vela que empiece ANTES de desde_ms: BingX puede
    # devolver la vela en curso en el momento de pedirla si startTime cae
    # dentro de ella. Sin este filtro, esa vela contaminaría el resultado
    # con precios de ANTES del punto de partida real.
    return [v for v in salida if v["t"] >= desde_ms]


def resolver(s: dict, max_barras: int = 288):
    """
    Simula la operación: ¿tocó antes el SL o el TP?

    Si en la misma vela se tocan los dos, se supone el PEOR caso (stop).
    Suponer el mejor es la forma más común de inflar un resultado sin
    darse cuenta.

    El intervalo sale de s['timeframe'] (columna propia de cada señal en
    el CSV), no de un flag global -- si el bot cambia de temporalidad
    entre despliegues, cada fila se resuelve con la suya.
    """
    interval = s.get("timeframe") or "5m"
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
    ap.add_argument("--tf", default="5m",
                    help="temporalidad de RESPALDO solo para filas antiguas "
                         "sin columna 'timeframe'. Si el CSV ya la tiene "
                         "(poller.py la escribe desde siempre), se ignora.")
    ap.add_argument("--max-barras", type=int, default=288,
                    help="cuántas barras seguir cada señal (288 = 24h en 5m)")
    a = ap.parse_args()

    filas = []
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with open(a.csv, newline="", encoding=encoding) as f:
                for x in csv.DictReader(f):
                    try:
                        filas.append({
                            "symbol": x["symbol"], "side": x["side"],
                            "ts": int(float(x["ts_señal"])),
                            "timeframe": (x.get("timeframe") or a.tf).strip(),
                            "price": float(x["price"]), "sl": float(x["sl"]),
                            "tp": float(x["tp"]),
                            "ejecutada": x.get("ejecutada") in ("1", "True", "true"),
                        })
                    except (KeyError, ValueError, TypeError):
                        continue
            break
        except UnicodeDecodeError:
            filas = []
            continue
    if not filas:
        print("Sin señales legibles en el CSV.")
        return 1

    ej = sum(1 for x in filas if x["ejecutada"])
    print(f"{len(filas)} señales · {ej} ejecutadas · {len(filas)-ej} descartadas")
    print("Simulando cada una con su propio SL/TP (velas de BingX)...\n")

    for i, s in enumerate(filas, 1):
        s["r"], s["barras"] = resolver(s, a.max_barras)
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
