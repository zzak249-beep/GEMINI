# Qué se ha cambiado y por qué

No he tocado `signal_engine.py` (no lo has subido, y además NOTAS_MOTOR.md
ya deja escrito por qué no conviene tocar el motor de decisión todavía:
invalidaría la muestra de 39 operaciones). Todo lo de abajo es séptica
alrededor del motor: gestión de margen, guardas antes de operar, y registro
para medir — cero cambios a qué opera el bot hoy.

## 1. `bingx_client.py` — margen real en vez de adivinado

`get_balance()` solo leía el campo `equity` de la respuesta de BingX y
tiraba el resto. Pero esa misma respuesta ya trae `availableMargin` y
`usedMargin`. Nadie los usaba, y en su lugar `main.py` y `guardas.py`
adivinaban por separado el nombre de un getter que no existía
(`get_available_margin`, `get_free_margin`...) y siempre acababan
comparando contra `equity` — que en una cuenta compartida por varios bots
INCLUYE el margen que ya tienen bloqueado los demás. Ese es justo el
origen documentado del rechazo 101204 "Insufficient margin".

Nuevo método `get_balance_detail()` que devuelve
`{equity, available_margin, used_margin, unrealized_profit}` tal cual los
da BingX. `get_balance()` sigue existiendo igual (devuelve solo equity)
pero ahora está construido sobre `get_balance_detail()`.

## 2. `main.py` — `_margen_disponible()` ya no adivina

Usa `bx.get_balance_detail()['available_margin']` directamente. Si esa
llamada falla, cae a `equity` como antes, pero ahora el motivo del
fallback es "no se pudo leer availableMargin de BingX", no "no existe el
getter" — porque ahora sí existe.

## 3. `main.py` — filtro final unificado con `guardas.revisar()`

`guardas.py` y `risk_manager.py` estaban escritos, documentados y
**completamente sin usar**: ni un `import risk_manager` en todo el repo,
y de `guardas.py` solo se llamaba a `revisar_sl_tp()` (la comprobación de
orientación), nunca a `revisar()` (la función que junta orientación +
margen real + riesgo real + ratio tp/sl en una sola llamada).

Mientras tanto, `_handle_entry` reimplementaba a mano su propio chequeo
de margen (el que adivinaba el getter, ver punto 1) y no comprobaba nunca
el ratio tp/sl mínimo (`GUARD_MIN_RATIO`).

Ahora, justo antes de `open_protected_position`, se llama a
`gd.revisar(...)` con la qty/leverage ya decididos. Si bloquea, se avisa
por Telegram (con el mismo enfriamiento de 30 min que ya tenía la guarda
de orientación, para no espamear) y no se manda la orden. Si pasa con
avisos (p. ej. ratio bajo), se registran en el log pero no bloquean.

No cambia la lógica de sizing (margen fijo / por riesgo / suelo de
nocional / tope de nocional / rechazo de leverage superior) — esa sigue
igual, tal cual la dejaste. Solo se sustituye el chequeo de margen final.

## 4. `config.py` — las perillas `GUARD_*` ahora usan tus valores reales

`guardas.py` tiene sus propios defaults (`GUARD_MAX_RIESGO_PCT=3.0`,
`GUARD_MIN_RATIO=1.0`...). Como nunca se llamaba a `revisar()`, esto no
importaba. Ahora que sí se llama, `config.py` fija
`GUARD_MAX_RIESGO_PCT = MAX_RISK_PCT_ABS` (tu variable ya configurada en
Railway, ahora en 4.0) en vez de dejar que se cuele el 3.0 de fábrica de
guardas.py por debajo de lo que ya habías decidido. El resto
(`GUARD_COLCHON`, `GUARD_MIN_RATIO`, `GUARD_BLOQUEAR_SLTP`,
`GUARD_AVISO_MIN`) quedan como variables de entorno normales, con los
mismos valores por defecto que ya tenía `guardas.py`, por si quieres
tocarlos en Railway sin redeploy de código.

No hace falta añadir nada a `railway_vars_15m.txt`: si no defines esas
variables, se comportan exactamente igual que antes de este cambio.

## 5. `poller.py` — `atrous.py` ahora se ejecuta de verdad

`atrous.py` decía en su propio docstring: *"se calcula en cada señal y se
ANOTA en senales_todas.csv"* — pero no había ni un `import atrous` en el
repo. Ahora `job_generate_signals` llama a `atrous.contexto(closes, side)`
en cada señal generada (ejecutada o no) y añade cuatro columnas nuevas al
CSV: `at_pendiente`, `at_macro`, `at_acuerdo`, `at_ruido`.

Es puramente observacional — igual que decía el propio archivo: no entra
en ninguna decisión de si se opera o no. Es lo que hace falta para que,
dentro de unas semanas, se pueda comparar si las señales donde la à
trous causal estaba de acuerdo salieron mejor que las que no, ANTES de
plantearse activarla como filtro de verdad (que es exactamente el plan
que ya tenías escrito en el propio `atrous.py`).

Si `atrous.contexto()` falla por lo que sea, se captura y se registra con
columnas vacías — un fallo aquí nunca puede tumbar ni bloquear una señal.

## Lo que NO se ha tocado, y por qué

- **`signal_engine.py`**: no está subido, y aunque lo estuviera, tocar el
  filtro de régimen ahora mismo tira por la borda la muestra de 39
  operaciones (ver NOTAS_MOTOR.md, sección "Qué hacer con esto").
- **`wavelet_causal.py`**: sigue siendo la herramienta de auditoría
  (`test_causality.py`) para el día que decidas comparar tu motor contra
  uno causal y bien normalizado. No se ha promovido a producción — esa es
  una decisión con datos, no de código.
- **`risk_manager.py`**: sigue sin usarse. Es un paradigma de sizing
  distinto (% de equity como nocional fijo, vía `qty_pct`) al que ya usa
  `main.py` (por riesgo/distancia al stop, o margen fijo). Integrarlo
  significaría añadir un tercer modo de sizing nuevo, no un fix — lo dejo
  fuera de este cambio para no mezclar "arreglar lo que hay" con "añadir
  algo nuevo" en el mismo despliegue. Si quieres ese tercer modo, dímelo
  y lo hacemos aparte.

## Cómo desplegarlo

Es el mismo `railway.json` / `requirements.txt` / arranque de siempre.
Sustituye los archivos de tu repo por estos y redeploy. No hace falta
tocar variables de Railway — todo lo nuevo tiene defaults que reproducen
el comportamiento anterior salvo el fix de margen (que solo puede dejar
pasar entradas que antes se rechazaban por error, nunca al revés).
