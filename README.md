# RSI + SuperTrend "Doble Dip" — Bot BingX Futures

Bot Python que replica en 15m la estrategia del Pine Script v6 subido
("RSI & SuperTrend Özel Dip Stratejisi"):

- **Entrada (long):** 2º cruce alcista de RSI(10) sobre su media SMA(10),
  con RSI por debajo de 50, sin que RSI haya vuelto a superar 50 entre
  cruces ("Doble Dip" / formación en W).
- **Salida:** SuperTrend(ATR 10, factor 2.5) cambia de alcista a bajista.
- Solo opera en largo (igual que el script original, que no tiene lógica
  de cortos).

Añadido sobre el script original (no viene en el Pine, se necesita para
operar con dinero real): tamaño de posición configurable, apalancamiento
configurable y un stop-loss de seguridad como orden real en el exchange.

**Mejoras de fiabilidad (v2):**
- Comprobación de credenciales/conexión con BingX al arrancar — falla
  rápido y avisa por Telegram si `BINGX_API_KEY`/`SECRET` son inválidas,
  en vez de esperar a la primera señal real para descubrirlo.
- Detección de cierre externo: si el stop-loss salta solo entre ciclos
  (o alguien cierra la posición a mano en BingX), el bot lo detecta y
  avisa por Telegram — antes se quedaba en silencio.
- Al cerrar por señal de SuperTrend, cancela **todas** las órdenes
  abiertas del símbolo (no solo el ID de stop-loss que recuerda en
  memoria), así un reinicio de Railway nunca deja un stop huérfano.
- Si el bot se reinicia con una posición ya abierta, recupera el
  stop-loss existente en vez de perder su referencia.

**v3 — escaneo de todas las monedas (`SYMBOLS=ALL`):**
El bot ya no opera un solo símbolo: descubre todos los perpetuos USDT-M
de BingX (o la lista que le pases), evalúa la estrategia en **paralelo**
en todos a la vez (solo lectura) y abre/cierra posiciones de forma
**secuencial**, para que el dimensionamiento de cada entrada lea el
balance ya actualizado por la entrada anterior del mismo ciclo — así no
se compromete más margen del que en realidad hay.

Controles de riesgo nuevos, específicos de operar muchos símbolos a la
vez (no existen en el Pine original):
- `MAX_CONCURRENT_POSITIONS` — techo duro de posiciones abiertas
  simultáneas, cueste lo que cueste el resto de señales.
- `SYMBOL_COOLDOWN_MINUTES` — tras cerrar un símbolo, no se reabre antes
  de X minutos.
- Circuit breaker global — tras `MAX_CONSECUTIVE_LOSSES` cierres en
  pérdida seguidos (en cualquier símbolo, incluidos los que se cierran
  solos por el stop-loss), se pausan **nuevas entradas**
  `CIRCUIT_BREAKER_COOLDOWN_MINUTES`. Las salidas de posiciones ya
  abiertas nunca se pausan por esto.
- `POSITION_SIZE_PCT` por defecto baja de 20% a 5% respecto a la versión
  de un solo símbolo, precisamente porque ahora se apila: con los valores
  por defecto, `MAX_CONCURRENT_POSITIONS x POSITION_SIZE_PCT` = 5 x 5% =
  25% del balance en margen como máximo teórico simultáneo.

**v4 — filtro de tendencia (`TREND_FILTER_ENABLED=true`, activo por
defecto):** tomado del script "Higher-Low tras Ruptura de Base + EMA50"
(a partir de una imagen de patrón chartista): no toma el Doble Dip si el
precio sigue dentro de una base sin confirmar. Exige que la EMA de
tendencia (`TREND_EMA_LENGTH`, 50 por defecto) esté subiendo Y que el
precio ya haya roto la directriz bajista que conectaba los últimos dos
pivote-altos de la base previa - la ruptura se invalida sola si aparece un
nuevo mínimo por debajo del suelo de la ruptura, o si pasa
`TREND_MAX_BARS_AFTER_BREAK` velas sin confirmarse. A diferencia del bot
Higher-Low+EMA50 (donde esto ES la señal), aquí es solo un FILTRO sobre la
señal de RSI: no exige que haya un higher-low exacto en la misma vela.

**Hallazgo del backtest al construirlo:** en una serie sintética de 3000
velas, el filtro pasó de 29 operaciones (Doble Dip puro) a solo 1 - un
filtro de confirmación de tendencia rechaza casi todo en datos sin
tendencias sostenidas (random walk puro). No sé si tus símbolos reales se
comportan igual; **corre `backtest.py` con y sin `--no-trend-filter` sobre
tu histórico real antes de asumir que el filtro por defecto (activado) es
mejor** - podría estar dejando pasar muy pocas señales para tu gusto.

## Archivos

| Archivo | Función |
|---|---|
| `main.py` | Arranque: servidor Flask de salud + hilo del bot |
| `trading_bot.py` | Bucle principal, ejecución de entradas/salidas |
| `indicators.py` | RSI, SuperTrend y lógica del contador Doble Dip |
| `bingx_client.py` | Cliente REST de BingX (firma, klines, órdenes) |
| `telegram_notifier.py` | Notificaciones a Telegram |
| `config.py` | Carga de variables de entorno |

## 1. BingX — crear API key

1. BingX → Cuenta → API Management → Crear API key.
2. Permisos: **lectura** + **Perpetual Futures trading**. NO actives
   permiso de retiros.
3. Si restringes por IP, en Railway la IP saliente puede cambiar salvo
   que uses un plan con IP estática; si no, deja la key sin restricción
   de IP (los permisos de "sin retiros" ya limitan el daño posible).

## 2. Telegram — crear bot

1. Habla con `@BotFather` → `/newbot` → copia el token → `TELEGRAM_BOT_TOKEN`.
2. Habla con `@userinfobot` (o `@RawDataBot`) para obtener tu `chat_id`
   → `TELEGRAM_CHAT_ID`.

## 3. Desplegar en Railway

```bash
git init
git add .
git commit -m "RSI+SuperTrend Doble Dip bot"
git remote add origin <tu-repo-github>
git push -u origin main
```

En Railway: **New Project → Deploy from GitHub repo** → selecciona el
repo. Railway detecta `Procfile`/`railway.json` automáticamente.

En **Variables**, copia todas las claves de `.env.example` y rellena
los valores (`BINGX_API_KEY`, `BINGX_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID` son obligatorias; el resto tiene valores por defecto
razonables). No definas `PORT` a mano, Railway lo inyecta solo.

## 4. Antes de operar con dinero real

El bot arranca con `DRY_RUN=true`: escanea todos los símbolos, calcula
las señales sobre velas reales y las notifica por Telegram, pero **no
envía ninguna orden**. Con `SYMBOLS=ALL` vas a recibir bastantes más
avisos que con un solo símbolo — es la muestra más realista de con qué
frecuencia va a operar de verdad. Déjalo así unos días y solo entonces
cambia `DRY_RUN=false` en Railway (redeploy automático).

Revisa `LEVERAGE`, `POSITION_SIZE_PCT`, `MAX_CONCURRENT_POSITIONS` y
`STOP_LOSS_PCT` antes de pasar a real — los valores por defecto son
conservadores a propósito (exposición máxima teórica ≈ 25% del balance
en margen), no una recomendación de cuánto
arriesgar.

## 5. Verificar que sigue vivo

Railway te da una URL pública (Settings → Networking → Generate
Domain). `GET /` confirma que el proceso responde; `GET /health`
devuelve el estado de la última vela evaluada (precio, RSI, dirección
de SuperTrend, si hay posición abierta).

## 6. Backtest multi-temporalidad (`backtest.py`)

Descarga histórico real de BingX (paginando hacia atrás) y simula la
MISMA lógica de `indicators.py` que usa el bot en vivo, en varias
temporalidades a la vez, con el mismo dimensionamiento
(`POSITION_SIZE_PCT` × `LEVERAGE`) y comisiones. Solo necesita
`BINGX_API_KEY`/`BINGX_API_SECRET` en el entorno (no hace falta
Telegram ni permisos de trading en la key, son endpoints públicos):

```bash
python backtest.py --symbol BTC-USDT --timeframes 5m,15m,30m,1h,2h,4h,1d --days 120

# comparar con y sin el filtro de tendencia sobre el mismo símbolo/periodo
python backtest.py --symbol BTC-USDT --timeframes 1h --days 120
python backtest.py --symbol BTC-USDT --timeframes 1h --days 120 --no-trend-filter
```

Argumentos útiles: `--fee` (comisión taker por lado, %, por defecto
0.05 = tarifa estándar BingX), `--leverage`, `--position-size`,
`--stop-loss` (si no los pasas, toma los de tu `.env`), `--no-trend-filter`
(Doble Dip puro, sin el filtro EMA50+ruptura), y `--save-trades` para
volcar cada operación individual a `backtest_trades/trades_<tf>.csv`.

Corre esto donde SÍ tengas salida a internet hacia BingX (tu propio
PC, o como comando de un servicio Railway) — no es parte del bot que
corre 24/7, es una herramienta de análisis aparte.

Lee `num_trades` antes que nada: con menos de ~20-30 operaciones en el
periodo probado, cualquier `win_rate`/`profit_factor` bonito no es
estadísticamente fiable, es ruido.

## Notas técnicas / supuestos de la API de BingX

- Firma HMAC-SHA256 sobre query string, cabecera `X-BX-APIKEY`, igual
  en GET y en POST (patrón estándar de la familia de APIs tipo
  Binance). Si un envío de orden da error **100001** (firma inválida),
  revisa primero las credenciales y el `recvWindow`.
- Cuenta en **modo Hedge**: las órdenes usan `positionSide=LONG`
  explícito, no `reduceOnly`+`BOTH`.
- Si al arrancar el log muestra velas con OHLC que no cuadran con el
  gráfico real del símbolo en BingX, compara antes de operar en real —
  el parseo de klines incluye una ruta de reserva por si la API
  devolviera arrays en vez de objetos.
