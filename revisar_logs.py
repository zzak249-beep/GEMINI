"""
revisar_logs.py — ¿qué bots tienen el bug del SHORT invertido?

Contesta en un minuto la pregunta de en cuáles hay que instalar guardas.py,
sin desplegar nada y sin tener el código delante.

CÓMO SE USA
  1. En Railway, pestaña Deploy Logs de cada bot, botón de descarga.
     O filtra por  'positionSide'  y copia lo que salga a un .txt
  2. python revisar_logs.py joyful.txt zesty.txt renewed.txt

QUÉ BUSCA
  Las líneas del tipo
     Señal generada para LYN-USDT: {..., 'side': 'sell',
       'positionSide': 'SHORT', 'price': 0.03559, 'sl': 0.0354119,
       'tp': 0.0356984, ...}
  y comprueba la orientación:
     LONG  -> sl POR DEBAJO de price, tp POR ENCIMA
     SHORT -> sl POR ENCIMA de price, tp POR DEBAJO

  Además calcula el ratio |tp-entrada| / |sl-entrada| de cada señal. Si los
  cortos dan un ratio y los largos el inverso, no es que el stop esté mal
  calculado: es que los campos van intercambiados. Esa fue la prueba que
  destapó el fallo en joyful-art (largos 1.66, cortos 0.61, y 1.64 al
  intercambiarlos).
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict

CAMPO = r"'{k}'\s*:\s*'?([-\w\.]+)'?"
RE_SYM = re.compile(r"Señal generada para\s+([\w\-]+)")


def sacar(linea: str, clave: str):
    m = re.search(CAMPO.format(k=clave), linea)
    if not m:
        return None
    v = m.group(1)
    try:
        return float(v)
    except ValueError:
        return v


def analizar(ruta: str) -> dict:
    res = {"total": 0, "largos": 0, "cortos": 0, "malas": [],
           "ratios": defaultdict(list), "sin_datos": 0}
    try:
        with open(ruta, encoding="utf-8", errors="replace") as f:
            lineas = f.readlines()
    except OSError as e:
        print(f"  no se pudo leer {ruta}: {e}")
        return res

    for ln in lineas:
        if "positionSide" not in ln:
            continue
        ps = sacar(ln, "positionSide")
        px = sacar(ln, "price")
        sl = sacar(ln, "sl")
        tp = sacar(ln, "tp")
        if ps not in ("LONG", "SHORT") or not all(
                isinstance(x, float) and x > 0 for x in (px, sl, tp)):
            res["sin_datos"] += 1
            continue
        res["total"] += 1
        largo = ps == "LONG"
        res["largos" if largo else "cortos"] += 1

        bien = (sl < px < tp) if largo else (tp < px < sl)
        dsl, dtp = abs(px - sl), abs(px - tp)
        if dsl > 0:
            res["ratios"][ps].append(dtp / dsl)
        if not bien:
            m = RE_SYM.search(ln)
            res["malas"].append({
                "symbol": m.group(1) if m else "?",
                "ps": ps, "price": px, "sl": sl, "tp": tp,
                "swap": (tp < px < sl) if largo else (sl < px < tp),
            })
    return res


def media(xs):
    return sum(xs) / len(xs) if xs else 0.0


def main(rutas):
    if not rutas:
        print(__doc__)
        return
    for ruta in rutas:
        print("=" * 66)
        print(ruta)
        print("=" * 66)
        r = analizar(ruta)
        if r["total"] == 0:
            print("  Ninguna señal con positionSide+price+sl+tp.")
            if r["sin_datos"]:
                print(f"  ({r['sin_datos']} líneas con positionSide pero sin los tres precios)")
            print("  Filtra en Railway por  'positionSide'  y vuelve a exportar.\n")
            continue

        print(f"  Señales analizadas: {r['total']}  "
              f"({r['largos']} largos, {r['cortos']} cortos)")
        rl, rs = media(r["ratios"].get("LONG", [])), media(r["ratios"].get("SHORT", []))
        if rl and rs:
            print(f"  Ratio medio |tp|/|sl|:  LONG {rl:.2f}   SHORT {rs:.2f}")
            if rs > 0 and abs(1 / rs - rl) < 0.25:
                print("  ⚠  Los cortos dan el ratio INVERSO de los largos:")
                print("     los campos sl y tp van intercambiados en los SHORT.")

        if not r["malas"]:
            print("  ✅ Orientación correcta en todas.\n")
            continue

        print(f"\n  🚫 {len(r['malas'])} señal(es) con la orientación mal:")
        for m in r["malas"][:12]:
            print(f"     {m['symbol']:<12} {m['ps']:<5} entrada={m['price']:<12.8g} "
                  f"sl={m['sl']:<12.8g} tp={m['tp']:<12.8g}"
                  + ("   <- intercambiados" if m["swap"] else "   <- incoherentes"))
        if len(r["malas"]) > 12:
            print(f"     … y {len(r['malas']) - 12} más")
        n_swap = sum(1 for m in r["malas"] if m["swap"])
        print(f"\n  VEREDICTO: {n_swap} de {len(r['malas'])} se arreglan intercambiando")
        print("  los campos. Instala guardas.py en este bot y corrige el origen.\n")


if __name__ == "__main__":
    main(sys.argv[1:])
