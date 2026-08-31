# Bot RSI + SuperTrend "Doble Suelo" — BingX / Railway / Telegram

Bot de trading automático en Python que replica la estrategia del Pine Script
**"ProBorsa: RSI & SuperTrend Özel Dip Stratejisi"**, pensado para operar en
**BingX** (spot), desplegarse en **Railway** y enviar avisos por **Telegram**.

Temporalidad por defecto: **15 minutos**.

> ⚠️ **Esto no es un consejo financiero.** Operar de forma automática con
> criptomonedas conlleva un riesgo real de pérdida de capital. Un buen
> resultado en backtest no garantiza resultados futuros en real. Lee
> [Antes de operar en real](#5-antes-de-operar-en-real) antes de poner
> dinero de verdad.

## 🆕 Novedades de esta versión

Sobre la primera entrega, esta versión añade (sin tocar la lógica de
señales, ya verificada bar a bar contra el Pine Script):

- **`backtest.py`**: valida la estrategia contra datos históricos REALES de
  BingX (sin necesidad de API keys) antes de arriesgar nada.
- **Reintentos con backoff** ante fallos de red transitorios al hablar con BingX.
- **Comprobación previa de saldo y de mínimos del mercado** antes de comprar
  (mensaje claro en vez de un error críptico del exchange).
- **Guarda de idempotencia**: no vuelve a procesar/avisar dos veces la misma vela.
- **Stop-loss opcional** (`STOP_LOSS_PCT`, desactivado por defecto = mismo
  comportamiento exacto que el Pine original).
- **Heartbeat periódico** ("sigo vivo") por Telegram.
- **Comandos remotos por Telegram**: `/status` `/pause` `/resume` `/close` `/help`.
- **`Dockerfile`** como alternativa de despliegue.
- **Suite de tests** (`tests/`) con `pytest`, incluida una reimplementación
  independiente de la lógica de señales para verificarla por partida doble.
- Avisos de error con **cooldown** para no saturar el Telegram si un problema persiste.

## ¿Qué hace exactamente la estrategia? (sin cambios)

1. Calcula el **RSI** (suavizado de Wilder, igual que TradingView) de longitud 10.
2. Calcula una **media móvil del RSI** (longitud 10) como línea de señal.
3. Cuenta cuántas veces el RSI cruza al alza esa media **mientras el RSI
   está por debajo de 50**. Si el RSI sube de 50, el contador se reinicia.
4. Cuando el contador llega a **2 cruces** (patrón de doble suelo / "W"),
   se genera una **señal de compra**.
5. La posición se cierra cuando el **SuperTrend** (ATR 10, factor 2.5) gira
   de tendencia alcista a bajista → **señal de venta** (o, opcionalmente,
   si salta el stop-loss antes de que eso ocurra).

Estrategia **solo long** (no abre cortos), igual que el script original.

## Estructura del proyecto

```
bingx-rsi-bot/
├── bot.py                    # Punto de entrada: bucle principal en vivo
├── backtest.py                 # Backtest histórico contra datos reales de BingX
├── strategy.py                   # RSI, SuperTrend y señales (idéntico al Pine, ya verificado)
├── exchange_client.py              # BingX vía ccxt: reintentos, balances, órdenes, límites
├── telegram_commands.py              # /status /pause /resume /close /help (hilo en background)
├── notifier.py                         # Envío de mensajes a Telegram (con cooldown de errores)
├── config.py                             # Carga y validación de variables de entorno
├── tests/                                  # Suite de pytest
├── requirements.txt                          # Dependencias de producción
├── requirements-dev.txt                        # + pytest, pandas_ta (solo para desarrollo/tests)
├── Procfile / railway.json / Dockerfile           # Despliegue en Railway
├── .env.example
└── .gitignore
```

## 1. Requisitos previos

- Cuenta en [BingX](https://bingx.com/) con **API Key** (Menú → API Management).
  - Permisos: **lectura + trading spot**. **NO actives permiso de retiros**
    en la key que uses aquí.
  - Para probar sin dinero real: BingX tiene un modo demo ("VST" / Virtual
    USDT). Genera API keys específicas para ese modo desde tu cuenta y
    actívalo con `BINGX_DEMO=true`.
- Un bot de Telegram:
  1. Habla con **@BotFather** → `/newbot` → copia el **token**.
  2. Escríbele cualquier mensaje a tu bot (para "activar" el chat).
  3. Visita `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca
     `"chat":{"id":...}` — ese número es tu `TELEGRAM_CHAT_ID`. (O pídeselo
     a **@userinfobot**.)
- Cuenta de [GitHub](https://github.com/) y de [Railway](https://railway.app/).

## 2. Valida la estrategia ANTES de tocar tu cuenta: `backtest.py`

No necesita API keys (las velas históricas son datos públicos):

```bash
pip install -r requirements-dev.txt
python3 backtest.py --symbol BTC/USDT --timeframe 15m --days 60
```

Verás el número de operaciones, tasa de acierto, resultado neto, drawdown
máximo y el detalle de cada operación. Prueba distintos `--symbol`,
`--days` o incluso variantes de los parámetros (`--st-factor`,
`--target-cross-count`, etc.) antes de decidir la configuración final.
Opcional: `--output resultados.csv` para exportar el detalle.

## 3. Probar el bot en vivo, en local

```bash
git clone <la-url-de-tu-repo>
cd bingx-rsi-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edita .env con tus claves reales. Déjalo con DRY_RUN=true y
# BINGX_DEMO=true para la primera prueba.

python3 bot.py
```

Recibirás un mensaje de Telegram de "Bot iniciado" con un resumen de la
configuración. Si `TELEGRAM_COMMANDS_ENABLED=true`, ya puedes escribirle
`/help` a tu bot desde ese mismo chat.

## 4. Subir a GitHub y desplegar en Railway

```bash
git init && git add . && git commit -m "Bot RSI + SuperTrend"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

`.env` y `state.json` están en `.gitignore`: nunca se suben tus claves.

En [railway.app](https://railway.app/): **New Project → Deploy from GitHub
repo** → selecciona el repo. Railway detecta Python (Nixpacks) e instala
`requirements.txt` automáticamente (o usa el `Dockerfile` si prefieres
construirlo así). Añade en la pestaña **Variables** todas las de
`.env.example` con tus valores reales. El arranque ya está definido
(`Procfile` / `railway.json` → `python bot.py`); si Railway no lo detecta
solo, ponlo a mano en **Settings → Deploy → Custom Start Command**.

**No hace falta** generar un dominio público ni exponer ningún puerto: el
bot no recibe tráfico web, solo se conecta hacia fuera (BingX y Telegram).

## 5. Antes de operar en real

| DRY_RUN | BINGX_DEMO | Qué pasa |
|---|---|---|
| `true`  | `true`  | 100% seguro. Analiza con datos reales, consulta tu balance demo (VST) y avisa por Telegram. **Ninguna orden real.** |
| `true`  | `false` | Consulta tu balance **real** (para simular "en posición" con precisión) y avisa, pero **sigue sin ordenar nada**. |
| `false` | `true`  | Envía órdenes reales, pero contra el entorno demo de BingX (dinero ficticio). Ideal para probar la ejecución de punta a punta. |
| `false` | `false` | 🔴 **En real.** Dinero de verdad. |

Recomendación: `backtest.py` → fila 1 → fila 2 → fila 3 → fila 4, dejando
correr el bot varias velas en cada paso. Empieza con un `TRADE_AMOUNT_USDT`
pequeño al pasar a real.

## 6. Comandos de Telegram (si `TELEGRAM_COMMANDS_ENABLED=true`)

| Comando | Qué hace |
|---|---|
| `/status` | Última vela analizada, precio, RSI, si hay posición abierta y si está en pausa |
| `/pause` | Sigue analizando y avisando, pero no envía ninguna orden hasta `/resume` |
| `/resume` | Reanuda la operativa normal |
| `/close` | Cierra la posición abierta ahora mismo, a mercado (respeta `DRY_RUN`) |
| `/help` | Lista de comandos |

Por seguridad, el bot **ignora** cualquier mensaje que no venga exactamente
del `TELEGRAM_CHAT_ID` configurado.

## 7. Variables de configuración

| Variable | Por defecto | Descripción |
|---|---|---|
| `BINGX_API_KEY` / `BINGX_API_SECRET` | — (obligatorio) | Credenciales de BingX |
| `BINGX_DEMO` | `true` | `true` = red de pruebas VST de BingX |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — (obligatorio) | Credenciales del bot de Telegram |
| `SYMBOL` | `BTC/USDT` | Par a operar (formato ccxt: `BASE/QUOTE`) |
| `TIMEFRAME` | `15m` | Temporalidad de las velas |
| `CANDLES_LOOKBACK` | `300` | Nº de velas históricas descargadas en cada ciclo |
| `TRADE_AMOUNT_USDT` | `100` | USDT gastados en cada señal de compra |
| `MIN_POSITION_VALUE_USDT` | `5` | Umbral (USDT) para considerar que "hay posición abierta" |
| `RSI_LENGTH` / `SIGNAL_LENGTH` / `TRIGGER_LEVEL` / `TARGET_CROSS_COUNT` | `10` / `10` / `50` / `2` | Parámetros del RSI (idénticos al Pine) |
| `ATR_PERIOD` / `ST_FACTOR` | `10` / `2.5` | Parámetros del SuperTrend (idénticos al Pine) |
| `DRY_RUN` | `true` | `true` = solo avisa, no envía órdenes reales |
| `POLL_BUFFER_SECONDS` | `20` | Margen tras el cierre de vela antes de pedir datos |
| `MAX_RETRIES` / `RETRY_BACKOFF_SECONDS` | `3` / `2` | Reintentos ante errores de red transitorios |
| `ERROR_NOTIFY_COOLDOWN_MINUTES` | `15` | No repetir el mismo aviso de error antes de este tiempo |
| `STOP_LOSS_PCT` | `0` (desactivado) | Si >0, cierra si el precio cae ese % desde la entrada |
| `HEARTBEAT_EVERY_HOURS` | `24` | Aviso periódico de "sigo activo". `0` = desactivado |
| `TELEGRAM_COMMANDS_ENABLED` | `true` | Activa `/status /pause /resume /close /help` |
| `LOG_LEVEL` | `INFO` | Nivel de logging |

## 8. Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

Incluye una reimplementación independiente (bucle separado, no reutiliza
`strategy.py`) de la lógica del contador `crossCount`/`specialBuy`, para
verificarla por partida doble además de la comparación contra `pandas_ta`.

## 9. Limitaciones honestas

- **SuperTrend**: TradingView no publica el código fuente exacto de su
  función incorporada `ta.supertrend()`. `strategy.py` implementa el
  algoritmo estándar de SuperTrend (ATR suavizado igual que Pine, vía
  `ta.rma`); coincide en la práctica, pero en casos límite muy puntuales
  podría haber 1 vela de diferencia. Por eso `backtest.py` y el modo
  DRY_RUN/DEMO existen: úsalos antes de ir a real.
- **Solo spot, solo largos**: sin cortos ni apalancamiento, igual que el
  Pine original.
- **P&L en Telegram**: se apoya en `state.json` (mejor esfuerzo) y, si se
  pierde tras un redeploy, intenta reconstruir el precio de entrada desde
  tu historial real de operaciones en BingX (`fetch_my_trades`). La
  detección de "¿hay posición abierta?" siempre usa tu balance real, nunca
  depende de este archivo.
- El backtest no modela slippage, huecos de liquidez ni rechazos de
  órdenes que sí pueden pasar en real — es una simulación, no una garantía.
- El stop-loss opcional se evalúa solo al cierre de cada vela (como el
  resto de la estrategia), no vela a vela en tiempo real dentro de la vela.

## 10. Seguridad

- Nunca subas tu `.env` a GitHub (ya está en `.gitignore`).
- Restringe la API key de BingX a solo lectura + trading spot (sin retiros).
- Si BingX permite whitelist de IPs y tu IP de salida en Railway es
  estable, actívalo.
- El token de Telegram y el `TELEGRAM_CHAT_ID` son tan sensibles como una
  contraseña: quien los tenga puede leer tus avisos o, si además conociera
  tu chat_id exacto, intentar mandar comandos (aunque el bot verifica el
  chat_id en cada mensaje entrante).
