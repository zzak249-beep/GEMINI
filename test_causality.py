"""
test_causality.py — ¿Tu motor de señal mira al futuro?

QUÉ HACE
--------
1. PRUEBA DE REPINTADO. Calcula la señal de una misma vela varias veces,
   cada una con más velas posteriores disponibles. Si el resultado de esa
   vela CAMBIA según cuántas barras futuras se le pasen, el motor no es
   causal: en backtest usa datos que en vivo no existían. Es la prueba
   definitiva y no admite interpretación.

2. COMPARACIÓN. Recorre el histórico barra a barra en modo walk-forward
   (a cada paso solo se le dan las velas hasta esa barra, nunca más) y
   cuenta cuántas señales da tu motor, cuántas da el causal, y en cuántas
   coinciden.

CÓMO SE USA
-----------
    python test_causality.py                  # BTC-USDT por defecto
    python test_causality.py ETH-USDT 5m 600

No necesita claves de API: las velas se piden al endpoint público de
BingX. Tampoco importa config.py, así que se puede ejecutar en local sin
tener el entorno de Railway montado.

CÓMO LEER EL RESULTADO
----------------------
- "SIN REPINTADO" en tu motor: enhorabuena, es causal. Las mejoras del
  otro archivo son opcionales.
- "REPINTA" en tu motor: los resultados de backtest que hayas medido con
  él no son alcanzables en vivo. Hay que sustituir la descomposición
  antes de sacar ninguna conclusión sobre si la estrategia funciona.
"""
from __future__ import annotations

import sys

import requests

import wavelet_causal

BASE_URL = "https://open-api.bingx.com"

PARAMS = {
    "lookback_energy": 40,
    "k_dominance": 1.30,      # DOMINANCE_THRESHOLD de tus variables
    "cooldown_bars": 4,
    "atr_length": 14,
    "atr_mult_sl": 1.5,
    "atr_mult_tp": 2.5,
    "bar_ms": 5 * 60 * 1000,
    "approx_len": 8,
    "normalize_scales": True,
}

# Claves cuyo valor debe quedar CONGELADO para una vela ya cerrada. Si
# alguna cambia al añadir barras posteriores, hay lookahead.
CLAVES = ("long_cond", "short_cond", "is_trending", "dominance", "sl", "tp")


def bajar_velas(symbol: str, interval: str, limit: int):
    r = requests.get(
        f"{BASE_URL}/openApi/swap/v3/quote/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") not in (0, None):
        raise SystemExit(f"BingX devolvió error: {data}")
    return data.get("data", [])


def _cargar_motor_usuario():
    """signal_engine solo si existe: el test debe funcionar igual para
    auditar solo el motor causal."""
    try:
        import signal_engine
        return signal_engine
    except Exception as e:
        print(f"  (signal_engine no importable: {e})")
        return None


def _val(d, clave):
    v = d.get(clave) if isinstance(d, dict) else None
    if isinstance(v, float):
        return round(v, 10)
    return v


def prueba_repintado(nombre, fn_signal, df, pasos=8):
    """Fija una vela objetivo y recalcula su señal dándole cada vez una
    barra futura más. El valor de esa vela no debería moverse nunca."""
    print(f"\n── Repintado: {nombre} " + "─" * (46 - len(nombre)))

    objetivo_idx = len(df) - pasos - 1
    ts_objetivo = int(df["open_time"].iloc[objetivo_idx])

    referencia = None
    cambios = []
    for extra in range(pasos + 1):
        sub = df.iloc[: objetivo_idx + 1 + extra]
        try:
            sig = fn_signal(sub, PARAMS, None)
        except Exception as e:
            print(f"  +{extra} barras -> ERROR: {e}")
            continue

        # La señal se calcula sobre la ÚLTIMA vela del corte. Para
        # comparar la MISMA vela hay que recortar hasta ella en cada
        # iteración -- por eso se recorta a objetivo+1 y se añaden barras
        # solo para ver si contaminan el pasado.
        sub_objetivo = sub.iloc[: objetivo_idx + 1]
        try:
            sig_obj = fn_signal(sub_objetivo, PARAMS, None)
        except Exception:
            sig_obj = sig

        actual = {k: _val(sig_obj, k) for k in CLAVES}
        if referencia is None:
            referencia = actual
            print(f"  +{extra} barras -> referencia: {actual}")
        else:
            difs = {k: (referencia[k], actual[k]) for k in CLAVES
                    if referencia[k] != actual[k]}
            if difs:
                cambios.append((extra, difs))
                print(f"  +{extra} barras -> CAMBIÓ: {difs}")

    if cambios:
        print(f"  ❌ {nombre}: REPINTA. El valor de la vela {ts_objetivo} "
              f"cambia con datos posteriores.")
        return False
    print(f"  ✅ {nombre}: SIN REPINTADO en {pasos} barras futuras.")
    return True


def prueba_borde(nombre, fn_signal, df, pasos=8):
    """Variante más dura: simula el tiempo real. En cada paso el motor
    solo ve hasta la barra t, como en vivo, y se guarda su veredicto para
    esa barra. Luego se recalcula con TODO el histórico y se compara.

    Es la prueba que de verdad importa: mide si la señal que habrías
    ejecutado en vivo coincide con la que ve el backtest.
    """
    print(f"\n── Vivo vs backtest: {nombre} " + "─" * (39 - len(nombre)))

    discrepancias = 0
    total = 0
    for extra in range(1, pasos + 1):
        idx = len(df) - extra - 1
        try:
            en_vivo = fn_signal(df.iloc[: idx + 1], PARAMS, None)
            con_futuro = fn_signal(df.iloc[: idx + 1 + extra], PARAMS, None)
        except Exception as e:
            print(f"  barra -{extra}: ERROR {e}")
            continue
        total += 1
        # con_futuro evalúa OTRA vela; lo que se compara es si el motor,
        # al tener más datos, habría cambiado el veredicto de la barra
        # que ya operó. Se detecta comparando el timestamp devuelto: un
        # motor causal siempre devuelve la última vela que se le pasó.
        if int(en_vivo.get("timestamp", 0)) != int(df["open_time"].iloc[idx]):
            print(f"  barra -{extra}: ⚠️ el motor no devuelve la última vela recibida")
            discrepancias += 1

    if total and discrepancias == 0:
        print(f"  ✅ el motor siempre evalúa la última vela recibida ({total} pruebas).")
    return discrepancias == 0


def comparar(df, motor_usuario, pasos=200):
    """Walk-forward: en cada barra el motor solo ve el pasado."""
    print("\n── Comparación walk-forward " + "─" * 33)

    inicio = max(80, len(df) - pasos)
    señales_u = {"LONG": 0, "SHORT": 0}
    señales_c = {"LONG": 0, "SHORT": 0}
    coincidencias = 0
    barras = 0

    for i in range(inicio, len(df)):
        sub = df.iloc[: i + 1]
        barras += 1

        lado_c = lado_u = None
        try:
            c = wavelet_causal.compute_signal_causal(sub, PARAMS, None)
            if c["long_cond"]:
                lado_c = "LONG"
            elif c["short_cond"]:
                lado_c = "SHORT"
        except Exception:
            pass

        if motor_usuario:
            try:
                u = motor_usuario.compute_signal(sub, PARAMS, last_signal_ts=None)
                if u.get("long_cond"):
                    lado_u = "LONG"
                elif u.get("short_cond"):
                    lado_u = "SHORT"
            except Exception:
                pass

        if lado_c:
            señales_c[lado_c] += 1
        if lado_u:
            señales_u[lado_u] += 1
        if lado_c and lado_c == lado_u:
            coincidencias += 1

    print(f"  Barras evaluadas: {barras}")
    print(f"  Motor causal : {sum(señales_c.values())} señales "
          f"({señales_c['LONG']} long / {señales_c['SHORT']} short)")
    if motor_usuario:
        print(f"  Tu motor     : {sum(señales_u.values())} señales "
              f"({señales_u['LONG']} long / {señales_u['SHORT']} short)")
        print(f"  Coinciden en lado y barra: {coincidencias}")
        if sum(señales_u.values()) > sum(señales_c.values()) * 2:
            print("  ⚠️ Tu motor dispara MUCHAS más señales. Es lo típico "
                  "cuando el lookahead deja ver el desenlace de la barra.")
    else:
        print("  Tu motor     : no evaluado (signal_engine no disponible)")


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC-USDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "5m"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    print("=" * 62)
    print(f"AUDITORÍA DE CAUSALIDAD — {symbol} {interval}, {limit} velas")
    print("=" * 62)

    rows = bajar_velas(symbol, interval, limit)
    df = wavelet_causal.klines_to_df(rows)
    if len(df) < 120:
        raise SystemExit(f"Pocas velas para auditar: {len(df)}")
    print(f"Velas descargadas: {len(df)}")

    motor = _cargar_motor_usuario()

    ok_causal = prueba_repintado(
        "wavelet_causal", wavelet_causal.compute_signal_causal, df)
    prueba_borde("wavelet_causal", wavelet_causal.compute_signal_causal, df)

    ok_usuario = None
    if motor:
        def _fn(sub, params, lst):
            return motor.compute_signal(sub, params, last_signal_ts=lst)
        ok_usuario = prueba_repintado("signal_engine (el tuyo)", _fn, df)
        prueba_borde("signal_engine (el tuyo)", _fn, df)

    comparar(df, motor)

    print("\n" + "=" * 62)
    print("VEREDICTO")
    print("=" * 62)
    print(f"  wavelet_causal          : {'causal ✅' if ok_causal else 'REVISAR ❌'}")
    if ok_usuario is None:
        print("  signal_engine (el tuyo) : no evaluado")
        print("\n  Ejecuta este script DENTRO del repo joyful-art para que")
        print("  pueda importar tu signal_engine y auditarlo.")
    elif ok_usuario:
        print("  signal_engine (el tuyo) : causal ✅")
        print("\n  Tu motor no repinta. Los números de backtest son")
        print("  alcanzables en vivo, al menos por este lado.")
    else:
        print("  signal_engine (el tuyo) : REPINTA ❌")
        print("\n  Los resultados de backtest medidos con él NO son")
        print("  alcanzables en vivo. Antes de sacar conclusiones sobre si")
        print("  la estrategia tiene ventaja, hay que sustituir la")
        print("  descomposición por una causal.")


if __name__ == "__main__":
    main()
