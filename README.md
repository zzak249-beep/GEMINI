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

## 5. Pasar a real

Cuando confíes en las señales:
1. Cambia `AUTO_TRADE=true` en Railway (redeploy automático).
2. Empieza con `RISK_PCT_PER_TRADE` bajo (1% o menos) y `LEVERAGE` moderado.
3. Vigila el circuit breaker: se activa solo tras `MAX_CONSECUTIVE_LOSSES`
   pérdidas seguidas o `MAX_DAILY_DRAWDOWN_PCT`% de drawdown diario, y te
   avisa por Telegram. Para reactivarlo manualmente:
   ```bash
   curl -X POST https://tu-app.up.railway.app/reset-breaker/<WEBHOOK_SECRET>
   ```
4. El cierre (SL/TP) lo gestiona BingX solo (va embebido en la orden). El
   bot detecta que la posición desapareció cada 2 minutos (`job_reconcile_closed_positions`),
   calcula el PnL real vía el endpoint de income, actualiza el circuit
   breaker y te avisa por Telegram.

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
