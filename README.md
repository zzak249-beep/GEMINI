# 🌌 NEXUS Bot v1.0 — BingX Perpetual Futures

Bot algorítmico de **6 capas independientes** para BingX USDT-M Perpetuals.  
Desplegable en Railway en minutos. Señales y PnL en Telegram.

---

## 🧠 Arquitectura de señal (scoring 0–100)

| Capa | Tecnología | Puntos | Ventaja |
|---|---|---|---|
| **SAMA Adaptativa** | Slope Adaptive MA (port MZ Pine Script) | 20 | Trend filter ultra-selectivo |
| **Markov + ADX** | Matriz 3×3 sliding window | 20 | Probabilidad estadística de continuación |
| **CVD Sintético ★** | Cumulative Volume Delta + Divergencia | 15+8 | Presión institucional real desde OHLCV |
| **Liquidity Sweeps ★** | Stop hunt detection + fuerza | 20+5 | Detecta caza de stops → reversión |
| **Funding Rate** | Temperatura extrema del mercado | 10 | Contrarian en sobrecarga de posiciones |
| **Kotegawa / STC** | Dip reversal + Schaff Trend Cycle | 15 | Confirmación reversión/sobrecompra |

**Umbral mínimo: 55 puntos** (≥3 capas convergiendo)

### Vetos automáticos (anulan la señal)
- `CVD bear divergence`: precio nuevo máximo sin CVD → señal alcista bloqueada
- `Rango sin volumen`: ADX bajo + RVOL < mínimo → mercado dormido ignorado

---

## 🌟 Edge exclusivo vs otros bots

### Liquidity Sweep Detection
El 80% de las reversiones significativas comienzan con una caza de stops.
El bot detecta cuando el precio rompe un extremo reciente con una mecha pero **cierra de vuelta** — señal de que los institucionales absorbieron esa liquidez y van a mover el precio en sentido contrario.

### CVD Divergence Veto
El CVD sintético aproxima si el volumen está siendo comprado o vendido.
Si el precio sube a nuevos máximos pero el CVD no confirma → los compradores se están agotando → **veto de LONG** aunque otras capas den verde.

Combinados: Sweep + CVD confirmando = entrada de muy alta probabilidad.

---

## 📂 Estructura

```
nexus-bot/
├── main.py                   # Loop principal async + health server Railway
├── config.py                 # Variables de entorno
├── requirements.txt
├── Procfile
├── railway.json
├── .env.example
└── bot/
    ├── indicators.py          # SAMA, CVD, Sweeps, ADX, RSI, BB, STC, VWAP, RVOL, POC
    ├── markov.py              # Motor Markov con ventana deslizante
    ├── strategy.py            # NexusStrategy — fusión de 6 capas
    ├── risk_manager.py        # Kelly 1/4 + Triple Barrera + DD guard
    ├── bingx_client.py        # BingX CCXT async (TP/SL automáticos)
    ├── telegram_notifier.py   # Mensajes ricos en Telegram
    └── utils.py               # Logging con colores
```

---

## ⚙️ Configuración

### 1. Credenciales BingX
1. BingX → API Management → Nueva API Key
2. Permisos: ✅ Lectura ✅ Futuros Perpetuos ❌ NO retiradas
3. Guarda `API Key` y `Secret Key`

### 2. Bot Telegram
```bash
# 1. Habla con @BotFather → /newbot → guarda TOKEN
# 2. Habla con @userinfobot → guarda CHAT_ID
```

### 3. Variables de entorno
```bash
cp .env.example .env
# Edita .env con tus credenciales
```

---

## 🚀 Deploy en Railway

### Opción A — GitHub (recomendado)
1. Sube el proyecto a GitHub
2. Railway → **New Project → Deploy from GitHub**
3. Selecciona el repo
4. **Variables** → agrega todas las del `.env.example`
5. Railway detecta el `Procfile` y despliega automáticamente

### Variables obligatorias en Railway
```
BINGX_API_KEY
BINGX_SECRET_KEY
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
```

---

## 📊 Mensajes Telegram

| Evento | Contenido |
|---|---|
| 🚀 Arranque | Config, pares, leverage, capas activas |
| 📈📉 Entrada | Precio, qty, TP, SL + desglose completo de 6 capas + score |
| ✅❌ Salida | Motivo (TP/SL/Tiempo), PnL USDT, PnL%, balance |
| 💓 Heartbeat | Cada hora: balance, PnL diario, DD, score_L/score_S por par |
| ⚠️ Error | Stack trace truncado |

---

## 🔧 Parámetros clave

### Conservador (cuentas pequeñas)
```env
LEVERAGE=3
RISK_PER_TRADE=1.0
MAX_DAILY_LOSS_PCT=2.0
MIN_SCORE=60
ATR_MULT_TP=2.2
ATR_MULT_SL=1.2
```

### Agresivo (mercado trending)
```env
LEVERAGE=7
RISK_PER_TRADE=2.0
MAX_DAILY_LOSS_PCT=4.0
MIN_SCORE=50
PROB_THRESHOLD=35.0
```

### Tabla de ajuste por régimen
| Parámetro | Base | Bull market | Lateral |
|---|---|---|---|
| `PROB_THRESHOLD` | 40% | 35% | 50% |
| `RVOL_MIN` | 1.5x | 1.3x | 2.0x |
| `ATR_MULT_TP` | 2.2 | 2.5 | 1.8 |
| `MIN_SCORE` | 55 | 50 | 65 |
| `LIQUIDITY_LOOKBACK` | 20 | 15 | 25 |

---

## ⚠️ Advertencia de riesgo

> El trading con apalancamiento puede resultar en pérdida total del capital.  
> Empieza siempre con la configuración más conservadora.  
> Valida al menos 2 semanas en paper trading antes de capital real.

**Los parámetros por defecto son conservadores. Ajusta gradualmente.**
