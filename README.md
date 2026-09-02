## ⚠️ 0. IMPRESCINDIBLE antes de usar dinero real: Volume persistente en Railway

Por defecto Railway **borra el disco del contenedor en cada redeploy**. Si
`state.json` vive en el disco normal del contenedor (como está por defecto),
cada vez que cambies una variable o subas un cambio, el bot **olvida** qué
posiciones tenía abiertas y reinicia el circuit breaker a cero. Esto puede
hacer que abra más posiciones de las que crees que tiene permitidas.

**Antes de poner `AUTO_TRADE=true` con dinero real:**
1. Railway → tu servicio → pestaña **Volumes** → *New Volume*.
2. Móntalo en `/data` (o la ruta que prefieras).
3. En Variables, pon `STATE_FILE=/data/state.json`.
4. Redeploy una vez con esto ya puesto, y verifica con `/status` que el
   estado sobrevive a un redeploy manual de prueba.

Independientemente de esto, el bot ya trae un tope de seguridad absoluto
(`HARD_MAX_TOTAL_POSITIONS`, por defecto 5) que se comprueba contra las
posiciones **reales** en BingX, no contra el JSON local — así que aunque el
estado se pierda, el bot no puede abrir un número descontrolado de
posiciones. Pero el Volume sigue siendo necesario para que el circuit
breaker y el cooldown de señales funcionen bien entre reinicios.

## 0b. Verificación de SL/TP tras cada entrada

Si BingX rechaza en silencio el `stopLoss`/`takeProfit` embebido en la orden
(pasa con algunos símbolos, sobre todo alts ilíquidos), la posición quedaría
abierta sin protección y no se cerraría nunca sola — es lo que parece haber
pasado si viste posiciones acumulándose sin cerrarse. Ahora, justo después
de abrir cada orden, el bot llama a `bx.has_stop_and_take_profit(symbol)`:
si no encuentra las órdenes condicionales de SL y TP abiertas, **cierra la
posición inmediatamente** con una orden de mercado y te avisa por Telegram,
en vez de dejarla huérfana.

# Wavelet MRA Haar 5m — Bot BingX + Telegram (sin TradingView de pago)

Bot que calcula la señal wavelet **él solo**, leyendo velas de BingX cada 5
minutos — no necesitas plan de pago de TradingView. Según configuración:

- **Modo manual** (`AUTO_TRADE=false`, por defecto): solo manda la señal a
  Telegram con precio, SL y TP para que operes tú.
- **Modo automático** (`AUTO_TRADE=true`): además ejecuta la orden en BingX
  (perpetual swap, modo hedge) con sizing por riesgo, circuit breaker y
  reconciliación de posiciones al arrancar y periódica.

Sigue existiendo el modo `SIGNAL_SOURCE=tradingview` con el webhook original
por si en el futuro quieres pasar a Essential y usar tu Pine Script tal cual
— ver la sección 6.

Ver `RESEARCH.md` para el análisis matemático de la estrategia: qué es
realmente (y qué no) el filtro "wavelet", y bajo qué condiciones tiene
alguna ventaja real.

## Estructura

```
wavelet_bot/
├── main.py               # servidor Flask: health, webhook opcional, arranque del scheduler
├── signal_engine.py       # cálculo de la señal wavelet en Python (pandas) sobre velas de BingX
├── poller.py              # scheduler: genera señales cada 5m + reconcilia cierres por SL/TP
├── bingx_client.py        # cliente REST BingX swap v2/v3 (HMAC + klines públicas + income)
├── telegram_notifier.py   # envío de mensajes a Telegram
├── state_manager.py       # persistencia JSON + reconciliación + circuit breaker + cooldown
├── config.py              # lee todo de variables de entorno
├── pinescript/
│   └── wavelet_mra_haar_5m.pine   # estrategia Pine v6 original (solo si usas TradingView)
├── tests/                 # 44 tests (pytest), todo mockeado, sin tocar red real
├── requirements.txt
├── Procfile
├── railway.json
├── .env.example
└── RESEARCH.md
```

## 1. Subir a GitHub

```bash
cd wavelet_bot
git init
git add .
git commit -m "Wavelet MRA Haar 5m bot — señal en Python, sin TradingView de pago"
git branch -M main
git remote add origin git@github.com:<tu-usuario>/wavelet-mra-bot.git
git push -u origin main
```

`.env` y `state.json` están en `.gitignore` — nunca subas tus claves.

## 2. Desplegar en Railway

1. Railway → New Project → Deploy from GitHub repo → selecciona el repo.
2. Railway detecta `Procfile`/`railway.json` automáticamente (Nixpacks + Python).
3. En **Variables**, copia todo lo de `.env.example` y rellena:
   - `BINGX_API_KEY` / `BINGX_API_SECRET` (API key de BingX con permisos de
     **futuros/trading**, sin permiso de retiro). Pégalas con cuidado: un
     salto de línea al final rompe la cabecera HTTP (el bot ya las limpia
     solo con `.strip()`, pero mejor pegarlas bien).
   - `TELEGRAM_BOT_TOKEN` (de @BotFather) y `TELEGRAM_CHAT_ID` (de @userinfobot
     o tu chat con el bot).
   - `WEBHOOK_SECRET`: genera uno con `openssl rand -hex 24` (solo hace
     falta si algún día usas el webhook de TradingView; con `SIGNAL_SOURCE=python`
     no se usa, pero déjalo puesto por si acaso).
   - `SYMBOLS=BTC-USDT` (o varios separados por coma: `BTC-USDT,ETH-USDT`).
   - Deja `AUTO_TRADE=false` la primera semana. Solo señales, cero riesgo.
4. Deploy. Railway te da una URL tipo `https://tu-app.up.railway.app`.
5. Prueba: `curl https://tu-app.up.railway.app/` debe devolver
   `{"status":"ok","signal_source":"python","symbols":["BTC-USDT"],...}`.
6. Comprueba que el motor de señales lee bien BingX (esto SÍ necesita que
   Railway pueda salir a internet, cosa que ya hace):
   ```bash
   curl https://tu-app.up.railway.app/signal-check/BTC-USDT
   ```
   Debería devolver el JSON con `is_trending`, `long_cond`, `short_cond`,
   `close`, `approx`, `atr`, etc. de la última vela cerrada.

## 3. Nada que configurar en TradingView

Con `SIGNAL_SOURCE=python` (por defecto) el bot ya no depende de alertas de
TradingView para nada — calcula la señal él mismo cada 5 minutos con las
velas públicas de BingX. Puedes seguir usando el Pine Script en TradingView
solo como referencia visual si quieres, pero no hace falta ninguna alerta.

## 3b. Analizar TODAS las monedas de BingX

Por defecto el bot vigila solo lo que pongas en `SYMBOLS`. Si quieres que
vigile **todo el universo de perpetuos USDT de BingX** en vez de una lista
fija, pon en Railway:

```
SYMBOLS=ALL
SCAN_ALL_MAX_SYMBOLS=150      # tope de símbolos por ciclo (rate limit)
SCAN_ALL_REFRESH_HOURS=6      # cada cuánto refresca la lista de símbolos
SCAN_REPORT_ENABLED=true      # resumen periódico por Telegram
SCAN_REPORT_INTERVAL_HOURS=4
```

En este modo, cada 5 minutos el bot calcula la señal en todos los símbolos
descubiertos (con una pequeña pausa entre cada uno para no pasarse del
límite de 500 peticiones/10s de BingX) y trata cada señal exactamente igual
que si viniera de un solo símbolo — en modo manual, avisa por Telegram; en
`AUTO_TRADE=true`, ejecuta, siempre respetando `MAX_CONCURRENT_POSITIONS`.

**Importante si vas a poner `AUTO_TRADE=true` con `SYMBOLS=ALL`**: baja
`MAX_CONCURRENT_POSITIONS` y `RISK_PCT_PER_TRADE` — con cientos de símbolos
vigilados a la vez, varias señales pueden dispararse en el mismo ciclo de 5
minutos, y sin ese límite el bot podría abrir muchas posiciones de golpe.

Para **analizar sin arriesgar nada**, en cualquier momento puedes pedir un
análisis puntual (útil para investigar el filtro, no solo para operar):

```bash
# Analiza todos los perpetuos USDT ahora mismo, sin ejecutar ni avisar
curl "https://tu-app.up.railway.app/scan?quote=USDT&limit=150"

# Igual, pero además manda el resumen a tu Telegram
curl "https://tu-app.up.railway.app/scan?quote=USDT&limit=150&notify=true"
```

Devuelve qué símbolos tienen una señal activa ahora mismo, cuáles están en
régimen tendencial sin haber cruzado todavía, y cuántos fallaron al leer
(símbolos ilíquidos o recién listados con pocas velas).

## 4. Verificar en modo manual antes de arriesgar dinero

Con `AUTO_TRADE=false`, cada señal solo llega a Telegram. Corre así **al
menos 1-2 semanas** y compara las señales contra lo que habría pasado.
Usa `/signal-check/<symbol>` cuando quieras para ver el estado actual del
filtro sin esperar a que dispare.

## 5. Pasar a real — checklist completo

Antes de `AUTO_TRADE=true` con dinero real, en este orden:

1. **Volume persistente en Railway montado** (sección 0). Sin esto, el
   circuit breaker y el conteo de posiciones pueden perderse en un redeploy.
2. **`BINGX_DEMO=false`** cuando estés listo — antes de eso, corre al menos
   unos días con `BINGX_DEMO=true` para confirmar que las entradas,
   SL/TP y cierres funcionan de principio a fin contra la cuenta VST. El
   log de arranque te dice claramente contra qué entorno está pegando
   (`bingx_env` en `/`).
3. **`SYMBOLS` a pares líquidos**, no `ALL` al principio — `BTC-USDT,ETH-USDT,SOL-USDT`
   o similares. `MIN_24H_VOLUME_USDT` ya filtra ilíquidos si usas `ALL`,
   pero para tu primera vez en real, mejor una lista corta y conocida.
4. **`RISK_PCT_PER_TRADE` bajo** (1% o menos) y **`LEVERAGE` moderado**
   (5-10x) — no lo que uses en un experimento, lo que estés dispuesto a
   perder mientras confirmas que todo funciona como esperas.
5. **`HARD_MAX_TOTAL_POSITIONS` bajo** (3-5) las primeras semanas.
6. Cambia `AUTO_TRADE=true` en Railway (redeploy automático).
7. Verifica con `curl https://tu-app.up.railway.app/positions` que lo que
   ves ahí coincide con lo que ves en la web de BingX.
8. Vigila el circuit breaker: se activa solo tras `MAX_CONSECUTIVE_LOSSES`
   pérdidas seguidas o `MAX_DAILY_DRAWDOWN_PCT`% de drawdown diario, y te
   avisa por Telegram. Para reactivarlo manualmente:
   ```bash
   curl -X POST https://tu-app.up.railway.app/reset-breaker/<WEBHOOK_SECRET>
   ```
9. **Si algo se ve mal y quieres parar todo YA**, sin esperar a diagnosticar:
   ```bash
   curl -X POST https://tu-app.up.railway.app/emergency-stop/<WEBHOOK_SECRET>
   ```
   Esto pausa el trading y cierra TODAS las posiciones reales abiertas en
   BingX (consultadas directamente al exchange), no solo las que el bot
   creía tener localmente.
10. El cierre normal (SL/TP) lo gestiona BingX solo. El bot verifica
    justo tras cada entrada que el SL/TP se confirmó de verdad
    (`has_stop_and_take_profit`) — si BingX lo rechazó en silencio, cierra
    la posición al instante en vez de dejarla desprotegida. Y cada 2
    minutos reconcilia por si una posición se cerró sola, calculando el
    PnL real vía el endpoint de income.

## 6. (Opcional) Volver al webhook de TradingView

Si en el futuro tienes plan Essential+ y prefieres usar el Pine Script
directamente:
1. `SIGNAL_SOURCE=tradingview` y `ENABLE_SCHEDULER=false` en Railway.
2. Configura la alerta en TradingView con condición **"Any alert() function
   call"** sobre `pinescript/wavelet_mra_haar_5m.pine`, webhook URL
   `https://tu-app.up.railway.app/webhook/<WEBHOOK_SECRET>`.

## Notas de arquitectura (para que encaje con el resto de tu flota)

- **Un solo worker** (`--workers 1` en Procfile/railway.json): el estado se
  guarda en un JSON local, no hay lock distribuido. El scheduler corre en un
  hilo de background dentro del mismo proceso — no necesitas un segundo
  servicio en Railway.
- **Firma HMAC**: se construye el query string ordenado UNA vez y se usa
  igual para firmar y transmitir — evita el bug de mismatch orden-firma/
  orden-transmisión que ya diste con `renewed-love`/`joyful-art`/`bot22`.
- **Reconciliación**: al arrancar y cada 2 minutos, compara posiciones
  locales vs. `/openApi/swap/v2/user/positions` y prioriza siempre al
  exchange como fuente de verdad; cuando detecta que una posición se cerró
  sola (SL/TP), consulta `/openApi/swap/v2/user/income` para el PnL real.
- **positionSide**: el bot asume cuenta en modo **hedge** (LONG/SHORT
  simultáneos posibles). Si tu cuenta BingX está en modo one-way, cambia
  `positionSide` a `"BOTH"` en `bingx_client.place_market_order`.
- Los endpoints de BingX (`stopLoss`/`takeProfit` embebidos, `/quote/klines`
  v3, `/user/income`) están confirmados por documentación pública y SDKs de
  terceros, pero **verifica en modo demo (`BINGX_DEMO=true` + endpoint VST)
  antes de tocar dinero real**, porque BingX cambia parámetros de su API sin
  previo aviso frecuentemente.
- El motor de señales en Python (`signal_engine.py`) replica la fórmula
  exacta del Pine (`haar_detail`, energía por escala, cruce sobre SMA(8),
  ATR con RMA de Wilder) — está cubierto por tests que verifican el cálculo
  contra ejemplos resueltos a mano.
- **`scanner.py`** analiza N símbolos de una tirada (usado tanto por
  `SYMBOLS=ALL` como por el endpoint `/scan`) con pausa entre peticiones
  para respetar el límite compartido de datos de mercado de BingX (500
  peticiones/10s por IP).
- **Filtro de liquidez** (`MIN_24H_VOLUME_USDT`): en modo `SYMBOLS=ALL`, el
  bot descarta símbolos con menos volumen de 24h que el umbral y prioriza
  los más líquidos primero, en vez de vigilar alfabéticamente los primeros
  N — el spread/slippage en alts ilíquidos puede comerse cualquier ventaja
  del filtro antes de que se mueva el precio (ver `RESEARCH.md` sección 5).
- **Verificación de SL/TP post-orden**: tras cada entrada, `has_stop_and_take_profit()`
  confirma que BingX aceptó de verdad las órdenes condicionales. Si no,
  cierra la posición al instante en vez de dejarla desprotegida.
- **Tope duro de posiciones** (`HARD_MAX_TOTAL_POSITIONS`): se comprueba
  contra las posiciones reales en BingX, no el JSON local, así que protege
  incluso si el estado se perdió en un redeploy sin Volume.
- **`/emergency-stop/<secret>`**: pausa el trading y cierra TODAS las
  posiciones reales de golpe, consultadas directamente a BingX.
- **`/positions`**: muestra las posiciones reales en BingX ahora mismo, sin
  pasar por el estado local — para verificar rápido sin depender de que el
  bot tenga razón sobre lo que cree tener abierto.
