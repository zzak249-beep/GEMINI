# ⚡ ZigZag Institutional Elite V6 — Trading Bot

Bot de trading algorítmico que implementa la estrategia **ZigZag Institutional Elite** en Python, con ejecución automática en **BingX Futures**, despliegue en **Railway** e informes detallados en **Telegram**.

---

## 🧠 Lógica de la estrategia

Traducción fiel del Pine Script original:

| Componente | Implementación |
|------------|----------------|
| **ZigZag** | Pivot High/Low con ventana configurable (`PIVOT_LEN`) |
| **Volumen institucional** | `volume > SMA(volume,20) × VOL_MULT` |
| **Señal LONG** | Cruce alcista sobre último peak + vol institucional + vela alcista |
| **Señal SHORT** | Cruce bajista bajo último valley + vol institucional + vela bajista |
| **Stop Loss** | Último valley (long) / último peak (short) |
| **Take Profit** | `entry + (entry - SL) × TP_MULT` |
| **Tamaño posición** | `(balance × riesgo% × leverage) / precio` |

---

## 🚀 Despliegue en Railway (5 pasos)

### 1. Prepara el repositorio

```bash
git init
git add .
git commit -m "ZigZag Bot V6 inicial"
gh repo create zigzag-bot --private --push  # o usa la web de GitHub
```

### 2. Crea el proyecto en Railway

1. Ve a [railway.app](https://railway.app) → **New Project**
2. Selecciona **Deploy from GitHub repo**
3. Elige tu repositorio `zigzag-bot`
4. Railway detecta el `Dockerfile` automáticamente ✅

### 3. Configura las variables de entorno

En Railway → tu proyecto → **Variables**, agrega las del archivo `.env.example`:

```
BINGX_API_KEY          = (tu clave de BingX)
BINGX_SECRET_KEY       = (tu secret de BingX)
TELEGRAM_TOKEN         = (token de @BotFather)
TELEGRAM_CHAT_ID       = (tu chat ID)
SYMBOL                 = BTC-USDT
TIMEFRAME              = 15m
RISK_PERCENT           = 1.0
LEVERAGE               = 5
...
```

### 4. Obtén tus credenciales

**BingX API:**
1. [bingx.com](https://bingx.com) → Perfil → **API Management**
2. Crear API → activa **Futures Trading**
3. Whitelist IP: Railway te da una IP fija (Settings → Networking)

**Telegram:**
1. Habla con [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copia el token
3. Habla con [@userinfobot](https://t.me/userinfobot) para obtener tu `chat_id`

### 5. Deploy

Railway hace deploy automático al hacer push. Para redeploy manual:
```bash
git commit --allow-empty -m "redeploy"
git push
```

---

## 📊 Informes en Telegram

El bot envía mensajes automáticos en estos eventos:

### 🚀 Arranque del bot
```
🚀 ZigZag Institutional Elite V6
━━━━━━━━━━━━━━━━━━━━
Estado: ✅ Bot activo
Par:       BTC-USDT
Temporalidad: 15m
Apalancamiento: 5x
Riesgo/trade:  1.0%
Balance:    1000.00 USDT
```

### ⚡ Entrada en trade
```
⚡ ZigZag Elite — ENTRADA
━━━━━━━━━━━━━━━━━━━━
Par: BTC-USDT | 15m
Dirección: 🟢 LONG
Precio entrada: 65420.0000
Stop Loss:      64800.0000
Take Profit:    66660.0000
RR Ratio: 1:2.0
━━━━━━━━━━━━━━━━━━━━
Cantidad:    0.0015 contratos
Balance:     1000.00 USDT
Riesgo:      1.0% = 10.00 USDT
Volumen:     2.3x MA (⚡ institucional)
ATR:         420.0000
```

### ✅ Cierre de posición
```
✅ ZigZag Elite — CIERRE
━━━━━━━━━━━━━━━━━━━━
Par: BTC-USDT
Dirección: 🟢 LONG
Entrada:   65420.0000
PnL:       +24.80 USDT
Razón:     Take Profit alcanzado
```

---

## ⚠️ Advertencias de riesgo

- **Nunca arriesgues más del 1-2% por trade** (`RISK_PERCENT`)
- **Apalancamiento conservador**: empieza con 3x-5x
- **Backtest primero**: usa los parámetros en TradingView antes de ir en vivo
- El bot cierra posiciones con SL/TP automáticos via BingX. Monitorea igualmente.
- Este software es **experimental**. El trading conlleva riesgo de pérdida de capital.

---

## 🛠️ Estructura del proyecto

```
zigzag-bot/
├── src/
│   └── bot.py          # Bot principal (estrategia + API + Telegram)
├── logs/               # Logs locales (auto-generado)
├── Dockerfile          # Para Railway
├── requirements.txt
├── .env.example        # Plantilla de variables (NO subir .env real)
├── .gitignore
└── README.md
```

---

## 🔧 Parámetros ajustables

| Variable | Default | Descripción |
|----------|---------|-------------|
| `SYMBOL` | `BTC-USDT` | Par de futuros BingX |
| `TIMEFRAME` | `15m` | Temporalidad |
| `PIVOT_LEN` | `5` | Sensibilidad ZigZag |
| `VOL_MULT` | `1.5` | Umbral volumen institucional |
| `ATR_LEN` | `14` | Periodo ATR |
| `TP_MULT` | `2.0` | Ratio Riesgo/Beneficio |
| `RISK_PERCENT` | `1.0` | % balance por trade |
| `LEVERAGE` | `5` | Apalancamiento |
| `LOOP_SECONDS` | `60` | Frecuencia de análisis |
