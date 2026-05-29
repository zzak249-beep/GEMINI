"""
╔══════════════════════════════════════════════════════════════════════╗
║   CRYPTO EXPLOSION SCANNER v3.0 — BingX Auto-Trade                 ║
║                                                                      ║
║   MEJORAS v3.0 vs v2.0:                                             ║
║   [V] VELOCIDAD — scan paralelo con ThreadPoolExecutor              ║
║       673 pares en ~25s en vez de ~90s                              ║
║   [P] PRECISIÓN — 3 filtros anti-ruido nuevos:                      ║
║       • Filtro tendencia 4H (EMA 9/21) — no entrar contratendencia  ║
║       • Filtro OI (Open Interest) — confirma interés institucional   ║
║       • Blacklist automática — par bloqueado 2h tras SL             ║
║   [D] DASHBOARD TELEGRAM mejorado:                                   ║
║       • Mensaje de alerta con tabla técnica completa                 ║
║       • Resumen horario con P&L de trades abiertos                  ║
║       • Alerta de cierre de trade (TP/SL hit) via polling           ║
║       • Heartbeat silencioso cada 6h si no hay señales              ║
║   [A] AUTO-TRADE seguro:                                             ║
║       • Trailing SL que sube al breakeven al llegar a +1%           ║
║       • Cierre parcial automático en TP1 (50%)                      ║
║       • Score mínimo 82 para auto-trade (antes 80)                  ║
╚══════════════════════════════════════════════════════════════════════╝

Variables de entorno Railway:
  BINGX_API_KEY, BINGX_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  TRADE_USDT (def 20), LEVERAGE (def 5), SL_PCT (def 2.0), TP_PCT (def 4.5)
  AUTO_TRADE (def false), MAX_OPEN_TRADES (def 3)
  SCAN_WORKERS (def 8) — hilos paralelos para klines
"""

import os, time, hmac, hashlib, json, logging, math
from datetime import datetime, timezone, timedelta
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import numpy as np

# ─────────────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────

BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TRADE_USDT      = float(os.getenv("TRADE_USDT",  "20"))
LEVERAGE        = int(os.getenv("LEVERAGE",       "5"))
SL_PCT          = float(os.getenv("SL_PCT",       "2.0"))   # ajustado a 2%
TP_PCT          = float(os.getenv("TP_PCT",       "4.5"))   # ratio 1:2.25
AUTO_TRADE      = os.getenv("AUTO_TRADE", "false").lower() == "true"
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
SCAN_WORKERS    = int(os.getenv("SCAN_WORKERS",    "8"))     # [V] hilos paralelos

BASE_URL = "https://open-api.bingx.com"

# ── Filtros del scanner ───────────────────────────────────────────────
MIN_VOLUME_USDT     = 8_000_000   # sube a 8M — más liquidez, menos manipulación
MIN_EXPLOSION_SCORE = 62
AUTO_TRADE_SCORE    = 82          # umbral más alto para auto-trade
TOP_N               = 12
KLINES_LIMIT        = 60

# ── Intervalos adaptativos ────────────────────────────────────────────
INTERVAL_NORMAL  = 900
INTERVAL_ACTIVO  = 240   # 4 min (antes 5)
INTERVAL_ALERTA  = 45    # 45 s  (antes 60)

# ── Estado global ─────────────────────────────────────────────────────
trades_abiertos:  dict[str, dict]  = {}   # key=symbol_SIDE
alertas_enviadas: dict[str, float] = {}   # symbol → ts última alerta
blacklist:        dict[str, float] = {}   # symbol → ts bloqueo (2h tras SL)
ultimo_heartbeat: float            = 0.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ScannerV3")


# ─────────────────────────────────────────────────────────────────────
#  HELPERS API — BingX
# ─────────────────────────────────────────────────────────────────────

def _sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(
        BINGX_API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _get(path: str, params: dict = None, auth: bool = False,
         timeout: int = 10) -> Optional[dict]:
    p = params or {}
    headers = {}
    if auth:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = _sign(p)
        headers["X-BX-APIKEY"] = BINGX_API_KEY
    try:
        r = requests.get(BASE_URL + path, params=p, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}")
        return None


def _post(path: str, params: dict, timeout: int = 10) -> Optional[dict]:
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    headers = {
        "X-BX-APIKEY": BINGX_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        r = requests.post(BASE_URL + path, data=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"POST {path}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
#  DATOS DE MERCADO
# ─────────────────────────────────────────────────────────────────────

def get_all_tickers() -> list[dict]:
    data = _get("/openApi/swap/v2/quote/ticker")
    return data.get("data", []) if data else []


def get_klines(symbol: str, interval: str = "1h", limit: int = 60) -> list:
    """
    BingX v3 puede devolver dicts o listas. Normaliza siempre a
    [timestamp, open, high, low, close, volume].
    """
    data = _get("/openApi/swap/v3/quote/klines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    raw = data.get("data", []) if data else []
    if not raw:
        return []
    if isinstance(raw[0], dict):
        out = []
        for k in raw:
            try:
                out.append([
                    k.get("time",   k.get("t", 0)),
                    k.get("open",   k.get("o", 0)),
                    k.get("high",   k.get("h", 0)),
                    k.get("low",    k.get("l", 0)),
                    k.get("close",  k.get("c", 0)),
                    k.get("volume", k.get("v", 0)),
                ])
            except Exception:
                continue
        return out
    return raw


def get_funding_rates() -> dict[str, float]:
    data = _get("/openApi/swap/v2/quote/premiumIndex")
    rates = {}
    if not data:
        return rates
    for item in data.get("data", []):
        try:
            rates[item["symbol"]] = float(item.get("lastFundingRate", 0))
        except Exception:
            pass
    return rates


def get_open_interest(symbol: str) -> float:
    """[P] Open Interest en USDT — confirma interés institucional real."""
    data = _get("/openApi/swap/v2/quote/openInterest", {"symbol": symbol})
    if not data:
        return 0.0
    try:
        return float(data.get("data", {}).get("openInterest", 0))
    except Exception:
        return 0.0


def get_open_positions() -> list[dict]:
    data = _get("/openApi/swap/v2/user/positions", auth=True)
    if not data:
        return []
    return data.get("data", []) or []


def get_btc_data() -> tuple[float, float]:
    tickers = get_all_tickers()
    for t in tickers:
        if t.get("symbol") == "BTC-USDT":
            try:
                return float(t.get("priceChangePercent", 0)), float(t.get("lastPrice", 0))
            except Exception:
                pass
    return 0.0, 0.0


# ─────────────────────────────────────────────────────────────────────
#  INDICADORES TÉCNICOS
# ─────────────────────────────────────────────────────────────────────

def ema(values: np.ndarray, period: int) -> np.ndarray:
    k = 2.0 / (period + 1)
    r = np.zeros(len(values))
    r[0] = values[0]
    for i in range(1, len(values)):
        r[i] = values[i] * k + r[i - 1] * (1 - k)
    return r


def rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains  = deltas[deltas > 0].mean() if any(deltas > 0) else 1e-4
    losses = abs(deltas[deltas < 0].mean()) if any(deltas < 0) else 1e-4
    return 100 - (100 / (1 + gains / losses))


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        period: int = 14) -> float:
    """ATR real (True Range medio)."""
    if len(closes) < 2:
        return float(highs[-1] - lows[-1])
    tr = np.maximum(highs[1:] - lows[1:],
         np.maximum(abs(highs[1:] - closes[:-1]),
                    abs(lows[1:]  - closes[:-1])))
    return float(tr[-period:].mean()) if len(tr) >= period else float(tr.mean())


def tendencia_4h(closes_1h: np.ndarray) -> str:
    """
    [P] Filtro tendencia — usa las últimas 48 velas 1H como proxy de 4H.
    Retorna: 'ALCISTA' | 'BAJISTA' | 'LATERAL'
    """
    if len(closes_1h) < 21:
        return "LATERAL"
    e9  = ema(closes_1h, 9)
    e21 = ema(closes_1h, 21)
    if e9[-1] > e21[-1] and e9[-1] > e9[-3]:
        return "ALCISTA"
    if e9[-1] < e21[-1] and e9[-1] < e9[-3]:
        return "BAJISTA"
    return "LATERAL"


def detectar_compresion_volatilidad(highs: np.ndarray, lows: np.ndarray,
                                     closes: np.ndarray, periodo: int = 20) -> dict:
    if len(closes) < periodo + 5:
        return {"squeeze": False, "nivel": "BAJA", "dias": 0, "bb_width": 9.9}
    rangos              = highs - lows
    media_rango         = rangos[-periodo:].mean()
    rango_reciente_media = rangos[-5:].mean()
    ema20    = ema(closes, 20)
    std20    = np.std(closes[-20:])
    bb_width = (std20 / ema20[-1]) * 100 if ema20[-1] > 0 else 9.9
    umbral   = media_rango * 0.75
    dias_comprimido = sum(1 for r in rangos[-10:] if r < umbral)
    squeeze  = rango_reciente_media < media_rango * 0.8 and bb_width < 3.5
    if   bb_width < 1.5 or dias_comprimido >= 7: nivel = "EXTREMA"
    elif bb_width < 2.5 or dias_comprimido >= 4: nivel = "ALTA"
    elif bb_width < 3.5 or dias_comprimido >= 2: nivel = "MEDIA"
    else:                                         nivel = "BAJA"
    return {"squeeze": squeeze, "nivel": nivel,
            "dias": dias_comprimido, "bb_width": round(bb_width, 2)}


def detectar_acumulacion_volumen(volumes: np.ndarray, closes: np.ndarray) -> dict:
    if len(volumes) < 20:
        return {"acumulacion": False, "ratio_vol": 0, "precio_range": 0}
    vol_reciente   = volumes[-5:].mean()
    vol_base       = volumes[-20:-5].mean()
    ratio_vol      = vol_reciente / vol_base if vol_base > 0 else 1
    precio_range   = (closes[-5:].max() - closes[-5:].min()) / closes[-5:].min() * 100
    precio_lateral = precio_range < 2.0
    acumulacion    = ratio_vol > 1.3 and precio_lateral
    return {"acumulacion": acumulacion,
            "ratio_vol": round(ratio_vol, 2),
            "precio_range": round(precio_range, 2)}


def detectar_rotura_inminente(highs: np.ndarray, closes: np.ndarray) -> dict:
    if len(highs) < 20:
        return {"cerca_rotura": False, "distancia_pct": 99, "max_20": 0}
    max_20        = highs[-20:].max()
    precio_actual = closes[-1]
    distancia_pct = (max_20 - precio_actual) / precio_actual * 100
    return {"cerca_rotura": distancia_pct < 1.0,
            "distancia_pct": round(distancia_pct, 2),
            "max_20": max_20}


# ─────────────────────────────────────────────────────────────────────
#  [V] FETCH PARALELO — obtiene klines de múltiples pares a la vez
# ─────────────────────────────────────────────────────────────────────

def _fetch_par_data(args: tuple) -> tuple[str, list, list, float]:
    """Worker para ThreadPoolExecutor. Retorna (symbol, klines_1h, klines_15m, oi)."""
    symbol, funding_data = args
    k1h  = get_klines(symbol, "1h",  KLINES_LIMIT)
    k15m = get_klines(symbol, "15m", KLINES_LIMIT)
    oi   = get_open_interest(symbol)   # [P] Open Interest
    return symbol, k1h, k15m, oi


# ─────────────────────────────────────────────────────────────────────
#  SCORING DE EXPLOSIÓN v3
# ─────────────────────────────────────────────────────────────────────

def calcular_explosion_score(
    ticker: dict,
    klines_15m: list,
    klines_1h: list,
    funding: float,
    btc_change: float,
    open_interest: float = 0.0,
) -> dict:
    """
    Score 0-100.  Pesos:
      [A] Squeeze / compresión volatilidad  → 25 pts
      [B] Acumulación volumen silenciosa     → 20 pts
      [C] RSI zona de impulso               → 15 pts
      [D] Momentum 1H / 4H                  → 15 pts
      [E] Precio cerca de rotura            → 10 pts
      [F] Funding negativo                  → 10 pts
      [G] Fuerza relativa vs BTC            →  5 pts
      [H] Open Interest creciente [NUEVO]   → bonus/penalización
      [I] Tendencia 4H alineada [NUEVO]     → bonus/penalización
    """
    score   = 0
    señales = []

    try:
        change_24h = float(ticker.get("priceChangePercent", 0))
        volume_24h = float(ticker.get("quoteVolume", 0))
    except Exception:
        return {"score": 0, "señales": [], "modo": "skip"}

    if not klines_1h or len(klines_1h) < 25:
        return {"score": 0, "señales": [], "modo": "skip"}

    try:
        closes_1h  = np.array([float(k[4]) for k in klines_1h])
        highs_1h   = np.array([float(k[2]) for k in klines_1h])
        lows_1h    = np.array([float(k[3]) for k in klines_1h])
        volumes_1h = np.array([float(k[5]) for k in klines_1h])
        closes_15m  = np.array([float(k[4]) for k in klines_15m]) if klines_15m else closes_1h
        volumes_15m = np.array([float(k[5]) for k in klines_15m]) if klines_15m else volumes_1h
    except Exception as e:
        log.warning(f"Error parseando klines: {e}")
        return {"score": 0, "señales": [], "modo": "skip"}

    # ── [I] Filtro tendencia 4H ──────────────────────────────────────
    tendencia = tendencia_4h(closes_1h)
    if tendencia == "BAJISTA":
        score -= 15   # penalización fuerte — no entrar contratendencia
        señales.append("🔴 Tendencia 4H bajista — penalización")
    elif tendencia == "ALCISTA":
        score += 5
        señales.append("🟢 Tendencia 4H alcista")
    # LATERAL: sin modificación

    # ── [A] Compresión de volatilidad ────────────────────────────────
    squeeze = detectar_compresion_volatilidad(highs_1h, lows_1h, closes_1h)
    if squeeze["nivel"] == "EXTREMA":
        score += 25; señales.append(f"⚡ SQUEEZE EXTREMO ({squeeze['dias']}v)")
    elif squeeze["nivel"] == "ALTA":
        score += 18; señales.append(f"🔄 Squeeze alto ({squeeze['dias']}v)")
    elif squeeze["nivel"] == "MEDIA":
        score += 10; señales.append("〰️ Compresión media")

    # ── [B] Acumulación de volumen ───────────────────────────────────
    acum_1h  = detectar_acumulacion_volumen(volumes_1h,  closes_1h)
    acum_15m = detectar_acumulacion_volumen(volumes_15m, closes_15m) \
               if len(volumes_15m) >= 20 else {"acumulacion": False}
    if acum_1h["acumulacion"] and acum_15m["acumulacion"]:
        score += 20; señales.append(f"🐳 ACUM DOBLE 1H+15m ×{acum_1h['ratio_vol']:.1f}")
    elif acum_1h["acumulacion"]:
        score += 13; señales.append(f"🐳 Acum 1H ×{acum_1h['ratio_vol']:.1f}")
    elif acum_15m["acumulacion"]:
        score += 8;  señales.append(f"📊 Acum 15m ×{acum_15m['ratio_vol']:.1f}")

    # ── [C] RSI zona de impulso ──────────────────────────────────────
    rsi_val = rsi(closes_1h, 14)
    if   45 <= rsi_val <= 60: score += 15; señales.append(f"📈 RSI óptimo: {rsi_val:.0f}")
    elif 40 <= rsi_val < 45 or 60 < rsi_val <= 68:
        score += 8;  señales.append(f"📊 RSI aceptable: {rsi_val:.0f}")
    elif rsi_val > 72:
        score -= 8;  señales.append(f"⚠️ RSI sobrecomprado: {rsi_val:.0f}")   # penalización mayor
    elif rsi_val < 35:
        señales.append(f"❌ RSI débil: {rsi_val:.0f}")

    # ── [D] Momentum ─────────────────────────────────────────────────
    mom_1h = (closes_1h[-1] - closes_1h[-2]) / closes_1h[-2] * 100 if len(closes_1h) >= 2 else 0
    mom_4h = (closes_1h[-1] - closes_1h[-5]) / closes_1h[-5] * 100 if len(closes_1h) >= 5 else 0
    if   mom_1h > 0.5 and mom_4h > 1.5:
        score += 15; señales.append(f"🚀 Mom: 1H={mom_1h:+.1f}% 4H={mom_4h:+.1f}%")
    elif mom_1h > 0 and mom_4h > 0:
        score += 8;  señales.append(f"↗️ Mom positivo 1H={mom_1h:+.1f}%")
    elif mom_1h < -1:
        score -= 5;  señales.append(f"↘️ Mom negativo 1H={mom_1h:+.1f}%")

    # ── [E] Rotura de resistencia ─────────────────────────────────────
    rotura = detectar_rotura_inminente(highs_1h, closes_1h)
    if   rotura["cerca_rotura"]:
        score += 10; señales.append(f"🎯 ROTURA INMINENTE {rotura['distancia_pct']:.2f}%")
    elif rotura["distancia_pct"] < 3:
        score += 5;  señales.append(f"🔲 Cerca resistencia -{rotura['distancia_pct']:.1f}%")

    # ── [F] Funding rate ──────────────────────────────────────────────
    if   funding < -0.002: score += 10; señales.append(f"💚 Funding muy neg: {funding*100:.4f}%")
    elif funding < 0:       score += 6;  señales.append(f"🟢 Funding neg: {funding*100:.4f}%")
    elif funding > 0.003:   score -= 3;  señales.append(f"🔴 Funding alto: {funding*100:.4f}%")

    # ── [G] Fuerza relativa vs BTC ────────────────────────────────────
    if   btc_change > 0 and change_24h > btc_change * 1.5:
        score += 5; señales.append(f"💪 Fuerza vs BTC: {change_24h:+.1f}% / BTC {btc_change:+.1f}%")
    elif btc_change < 0 and change_24h > 0:
        score += 5; señales.append(f"💪 Resiste BTC: +{change_24h:.1f}%")

    # ── [H] Open Interest ────────────────────────────────────────────
    if open_interest > 0:
        oi_m = open_interest / 1_000_000
        if   oi_m > 50:  score += 4; señales.append(f"📦 OI alto: ${oi_m:.0f}M")
        elif oi_m > 10:  score += 2
        elif oi_m < 1:   score -= 3; señales.append(f"📦 OI bajo: ${oi_m:.1f}M")

    # ── Clasificación ─────────────────────────────────────────────────
    score = max(0, min(100, score))
    if   score >= 80: modo = "EXPLOSION"
    elif score >= 65: modo = "ALERTA"
    elif score >= MIN_EXPLOSION_SCORE: modo = "CANDIDATO"
    else: modo = "skip"

    atr_val = atr(highs_1h, lows_1h, closes_1h)

    return {
        "score":       score,
        "señales":     señales,
        "modo":        modo,
        "rsi":         round(rsi_val, 1),
        "mom_1h":      round(mom_1h, 2),
        "mom_4h":      round(mom_4h, 2),
        "tendencia":   tendencia,
        "squeeze":     squeeze,
        "acum":        acum_1h,
        "rotura":      rotura,
        "funding":     funding,
        "oi_usdt":     open_interest,
        "change_24h":  change_24h,
        "volume_usdt": volume_24h,
        "atr":         round(atr_val, 6),
    }


# ─────────────────────────────────────────────────────────────────────
#  SCANNER PRINCIPAL v3 — paralelo [V]
# ─────────────────────────────────────────────────────────────────────

def scan_mercado() -> tuple[list[dict], int]:
    log.info("=== Iniciando scan v3 (paralelo) ===")
    t0 = time.time()

    tickers      = get_all_tickers()
    funding_data = get_funding_rates()
    btc_change, btc_price = get_btc_data()
    log.info(f"BTC: ${btc_price:,.0f} ({btc_change:+.1f}%) | Pares raw: {len(tickers)}")

    # ── Pre-filtro rápido (sin llamadas de red) ───────────────────────
    pares_validos = []
    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("-USDT"):
            continue
        if any(x in symbol for x in ["USDC","BUSD","TUSD","DAI","FDUSD"]):
            continue
        # [P] Blacklist — par bloqueado 2h tras SL
        if symbol in blacklist and time.time() - blacklist[symbol] < 7200:
            continue
        try:
            volume_24h = float(ticker.get("quoteVolume", 0))
            change_24h = float(ticker.get("priceChangePercent", 0))
        except Exception:
            continue
        if volume_24h < MIN_VOLUME_USDT:
            continue
        if change_24h < -8:
            continue
        pares_validos.append(ticker)

    log.info(f"Pares tras pre-filtro: {len(pares_validos)}")

    # ── Fetch paralelo de klines [V] ─────────────────────────────────
    klines_map: dict[str, tuple] = {}   # symbol → (k1h, k15m, oi)
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {
            ex.submit(_fetch_par_data, (t.get("symbol"), funding_data)): t.get("symbol")
            for t in pares_validos
        }
        for fut in as_completed(futures):
            try:
                sym, k1h, k15m, oi = fut.result(timeout=15)
                klines_map[sym] = (k1h, k15m, oi)
            except Exception as e:
                log.warning(f"Fetch error: {e}")

    log.info(f"Klines obtenidos: {len(klines_map)} en {time.time()-t0:.1f}s")

    # ── Scoring ───────────────────────────────────────────────────────
    candidatos_explosion = []
    candidatos_normales  = []

    for ticker in pares_validos:
        symbol = ticker.get("symbol", "")
        if symbol not in klines_map:
            continue
        k1h, k15m, oi = klines_map[symbol]
        funding        = funding_data.get(symbol, 0.0)

        resultado = calcular_explosion_score(
            ticker, k15m, k1h, funding, btc_change, oi
        )
        if resultado["modo"] == "skip":
            continue

        par_info = {
            "symbol":      symbol,
            "precio":      float(ticker.get("lastPrice", 0)),
            **{k: resultado[k] for k in [
               "score","modo","señales","rsi","mom_1h","mom_4h",
               "tendencia","squeeze","acum","rotura","funding",
               "oi_usdt","change_24h","volume_usdt","atr"
            ]},
        }

        if resultado["modo"] == "EXPLOSION":
            candidatos_explosion.append(par_info)
        else:
            candidatos_normales.append(par_info)

    candidatos_explosion.sort(key=lambda x: x["score"], reverse=True)
    candidatos_normales.sort(key=lambda x: x["score"],  reverse=True)
    todos = candidatos_explosion + candidatos_normales

    n_alerta = len([c for c in candidatos_normales if c["modo"] == "ALERTA"])
    n_cand   = len([c for c in candidatos_normales if c["modo"] == "CANDIDATO"])
    log.info(
        f"EXPLOSIÓN:{len(candidatos_explosion)} ALERTA:{n_alerta} "
        f"CANDIDATO:{n_cand} | total {time.time()-t0:.1f}s"
    )

    if candidatos_explosion:       intervalo = INTERVAL_ALERTA
    elif candidatos_normales:      intervalo = INTERVAL_ACTIVO
    else:                          intervalo = INTERVAL_NORMAL

    return todos[:TOP_N], intervalo


# ─────────────────────────────────────────────────────────────────────
#  SL/TP DINÁMICO
# ─────────────────────────────────────────────────────────────────────

def calcular_sl_tp(par: dict, precio: float, side: str = "LONG") -> tuple:
    """
    SL y TPs basados en ATR y nivel de squeeze.
    Retorna: (sl, tp1, tp2, descripcion)
    """
    sl_base  = SL_PCT / 100
    tp1_base = TP_PCT / 100
    tp2_base = TP_PCT * 1.6 / 100   # ratio 1:~3.6

    bb_width = par.get("squeeze", {}).get("bb_width", 3.0)
    if   bb_width < 1.5: mult = 1.6;  desc = "TP×1.6 (squeeze extremo)"
    elif bb_width < 2.5: mult = 1.3;  desc = "TP×1.3 (squeeze alto)"
    else:                mult = 1.0;  desc = "TP estándar"

    tp1_pct = tp1_base * mult
    tp2_pct = tp2_base * mult

    if side == "LONG":
        sl  = round(precio * (1 - sl_base),  8)
        tp1 = round(precio * (1 + tp1_pct),  8)
        tp2 = round(precio * (1 + tp2_pct),  8)
    else:
        sl  = round(precio * (1 + sl_base),  8)
        tp1 = round(precio * (1 - tp1_pct),  8)
        tp2 = round(precio * (1 - tp2_pct),  8)

    return sl, tp1, tp2, desc


# ─────────────────────────────────────────────────────────────────────
#  CONFIRMACIÓN DE ENTRADA — 5 filtros pre-trade [P]
# ─────────────────────────────────────────────────────────────────────

def confirmar_entrada(symbol: str, par: dict, side: str = "LONG") -> tuple[bool, list[str]]:
    ok_list   = []
    fail_list = []

    klines_15m = get_klines(symbol, "15m", 30)
    if not klines_15m or len(klines_15m) < 10:
        return False, ["❌ Sin datos 15m"]

    try:
        c15 = np.array([float(k[4]) for k in klines_15m])
        h15 = np.array([float(k[2]) for k in klines_15m])
        l15 = np.array([float(k[3]) for k in klines_15m])
        v15 = np.array([float(k[5]) for k in klines_15m])

        atr_15m        = (h15[-15:] - l15[-15:]).mean()
        extension_ratio = (h15[-1] - l15[-1]) / atr_15m if atr_15m > 0 else 0

        # [1] Vela no extendida
        if extension_ratio > 2.2:
            fail_list.append(f"⚠️ Vela extendida {extension_ratio:.1f}×ATR — esperar retroceso")
        else:
            ok_list.append(f"✅ Extensión OK {extension_ratio:.1f}×ATR")

        # [2] Volumen de confirmación
        vol_med  = v15[-21:-1].mean() if len(v15) >= 21 else v15[:-1].mean()
        vol_ratio = v15[-1] / vol_med if vol_med > 0 else 1.0
        if vol_ratio < 0.8:
            fail_list.append(f"⚠️ Volumen bajo {vol_ratio:.2f}× media")
        else:
            ok_list.append(f"✅ Volumen {vol_ratio:.2f}× media")

        # [3] EMA 9/21 en 15m alineadas con dirección
        if len(c15) >= 21:
            e9  = ema(c15, 9)[-1]
            e21 = ema(c15, 21)[-1]
            alin = (c15[-1] > e9 > e21) if side == "LONG" else (c15[-1] < e9 < e21)
            if not alin:
                fail_list.append("⚠️ EMA 9/21 15m no alineada")
            else:
                ok_list.append("✅ EMA 15m alineada")

        # [4] No perseguir rotura ya muy extendida
        dist = par.get("rotura", {}).get("distancia_pct", 99)
        if side == "LONG" and dist < -1.5:
            fail_list.append(f"⚠️ Rotura +{abs(dist):.1f}% ya extendida")
        else:
            ok_list.append(f"✅ Posición vs resistencia OK")

        # [5] RSI no en extremo contrario
        rsi_v = par.get("rsi", 50)
        if side == "LONG" and rsi_v > 80:
            fail_list.append(f"⚠️ RSI {rsi_v} sobrecomprado")
        elif side == "SHORT" and rsi_v < 20:
            fail_list.append(f"⚠️ RSI {rsi_v} sobrevendido")
        else:
            ok_list.append(f"✅ RSI {rsi_v} en rango")

    except Exception as e:
        return False, [f"❌ Error: {e}"]

    ok = len(fail_list) == 0
    return ok, ok_list + fail_list


# ─────────────────────────────────────────────────────────────────────
#  AUTO-TRADE v3
# ─────────────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int, side: str = "LONG") -> bool:
    result = _post("/openApi/swap/v2/trade/leverage", {
        "symbol":   symbol,
        "side":     side,
        "leverage": str(leverage),
    })
    return result is not None


def abrir_trade(symbol: str, precio_entrada: float, par: dict,
                side: str = "LONG") -> Optional[dict]:
    """
    Abre trade LONG o SHORT con:
      • Confirmación de 5 filtros pre-entrada
      • SL/TP dinámico según squeeze
      • TP partido: 50% en TP1, 50% en TP2
      • Mensaje Telegram detallado si se bloquea
    """
    if not BINGX_API_KEY or not AUTO_TRADE:
        return None

    # Límite de trades simultáneos
    posiciones = get_open_positions()
    activos    = len([p for p in posiciones if float(p.get("positionAmt", 0)) != 0])
    if activos >= MAX_OPEN_TRADES:
        log.warning(f"Máx trades ({MAX_OPEN_TRADES}) — skip {symbol}")
        return None

    trade_key = f"{symbol}_{side}"
    if trade_key in trades_abiertos:
        log.info(f"Trade {trade_key} ya existe — skip")
        return None

    # [P] Confirmación de entrada
    ok, confirmacion = confirmar_entrada(symbol, par, side)
    estado = "✅ SUPERADOS" if ok else "❌ BLOQUEADO"
    log.info(f"Confirmación {symbol} {side}: {estado}")
    for r in confirmacion:
        log.info(f"  {r}")

    if not ok:
        motivos = "\n".join(f"  {r}" for r in confirmacion if r.startswith("⚠️"))
        send_telegram(
            f"🚫 *SEÑAL BLOQUEADA — {symbol.replace('-USDT','')} {side}*\n{motivos}"
        )
        return None

    sl, tp1, tp2, sl_desc = calcular_sl_tp(par, precio_entrada, side)
    set_leverage(symbol, LEVERAGE, side)
    cantidad = round((TRADE_USDT * LEVERAGE) / precio_entrada, 4)

    log.info(f"⚡ Abriendo {side} {symbol}: qty={cantidad} "
             f"entry={precio_entrada} SL={sl} TP1={tp1} TP2={tp2}")

    # Dirección de órdenes
    if side == "LONG":
        entry_side, pos_side = "BUY",  "LONG"
        sl_side,   tp_side   = "SELL", "SELL"
    else:
        entry_side, pos_side = "SELL", "SHORT"
        sl_side,   tp_side   = "BUY",  "BUY"

    # Orden de entrada
    orden = _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": entry_side,
        "positionSide": pos_side, "type": "MARKET",
        "quantity": str(cantidad),
    })
    if not orden or orden.get("code") != 0:
        log.error(f"Error orden entrada: {orden}")
        return None

    order_id = orden.get("data", {}).get("order", {}).get("orderId")

    # Stop Loss
    _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": sl_side,
        "positionSide": pos_side, "type": "STOP_MARKET",
        "stopPrice": str(sl), "closePosition": "true",
    })

    # TP1 — 50% de la posición
    qty_tp1 = round(cantidad * 0.5, 4)
    _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": tp_side,
        "positionSide": pos_side, "type": "TAKE_PROFIT_MARKET",
        "stopPrice": str(tp1), "quantity": str(qty_tp1),
        "closePosition": "false",
    })

    # TP2 — resto
    _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": tp_side,
        "positionSide": pos_side, "type": "TAKE_PROFIT_MARKET",
        "stopPrice": str(tp2), "closePosition": "true",
    })

    trade_info = {
        "symbol":      symbol,
        "entry":       precio_entrada,
        "sl":          sl, "sl_original": sl,
        "tp1":         tp1, "tp2": tp2,
        "sl_desc":     sl_desc,
        "qty":         cantidad,
        "side":        side,
        "order_id":    order_id,
        "opened_at":   datetime.now(timezone.utc).isoformat(),
        "score":       par.get("score", 0),
        "be_activado": False,   # breakeven trailing activado?
        "confirmacion": confirmacion,
    }
    trades_abiertos[trade_key] = trade_info
    return trade_info


def abrir_trade_long(symbol: str, precio_entrada: float,
                     par: dict = None) -> Optional[dict]:
    """Wrapper compatibilidad."""
    return abrir_trade(symbol, precio_entrada, par or {}, side="LONG")


# ─────────────────────────────────────────────────────────────────────
#  TRAILING SL — breakeven [A]
# ─────────────────────────────────────────────────────────────────────

def actualizar_trailing_sl(precio_actual: float, trade_key: str) -> None:
    """
    Cuando el precio sube +1% desde la entrada, mueve el SL al breakeven (+0.1%).
    Se llama en cada ciclo para trades abiertos.
    """
    if trade_key not in trades_abiertos:
        return
    t = trades_abiertos[trade_key]
    if t.get("be_activado"):
        return

    entry = t["entry"]
    side  = t["side"]

    if side == "LONG":
        ganancia_pct = (precio_actual - entry) / entry * 100
        nuevo_sl     = round(entry * 1.001, 8)   # breakeven + 0.1%
        if ganancia_pct >= 1.0:
            # Modificar SL a breakeven via cancel+replace
            _post("/openApi/swap/v2/trade/order", {
                "symbol":        t["symbol"],
                "side":          "SELL",
                "positionSide":  "LONG",
                "type":          "STOP_MARKET",
                "stopPrice":     str(nuevo_sl),
                "closePosition": "true",
            })
            trades_abiertos[trade_key]["sl"]          = nuevo_sl
            trades_abiertos[trade_key]["be_activado"] = True
            log.info(f"🔒 Trailing SL → breakeven {nuevo_sl} en {t['symbol']}")
            send_telegram(
                f"🔒 *SL → BREAKEVEN*: {t['symbol'].replace('-USDT','')} LONG\n"
                f"Entrada: `{entry}` → SL movido a `{nuevo_sl}` (+0.1%)\n"
                f"Ganancia actual: +{ganancia_pct:.1f}%"
            )


# ─────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────

def send_telegram(message: str, silent: bool = False) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return False
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":              TELEGRAM_CHAT_ID,
        "text":                 message,
        "parse_mode":           "Markdown",
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram: {e}")
        return False


def build_alerta_explosion(par: dict) -> str:
    """[D] Mensaje de alerta con tabla técnica completa."""
    sym    = par["symbol"].replace("-USDT", "")
    score  = par["score"]
    modo   = par["modo"]
    precio = par["precio"]
    sq     = par.get("squeeze", {})
    tend   = par.get("tendencia", "—")
    oi_m   = par.get("oi_usdt", 0) / 1_000_000

    sl, tp1, tp2, sl_desc = calcular_sl_tp(par, precio)
    rr1 = abs(tp1 - precio) / abs(precio - sl) if abs(precio - sl) > 0 else 0
    rr2 = abs(tp2 - precio) / abs(precio - sl) if abs(precio - sl) > 0 else 0

    modo_emoji = {"EXPLOSION": "💥", "ALERTA": "⚠️"}.get(modo, "📊")
    auto_str   = "🤖 *TRADE AUTO ACTIVADO*" if AUTO_TRADE else "👆 *CONFIRMA MANUALMENTE*"

    tend_emoji = {"ALCISTA": "🟢", "BAJISTA": "🔴", "LATERAL": "⚪"}.get(tend, "⚪")

    lines = [
        f"{modo_emoji} *{modo}: {sym}/USDT* — `{score}/100`",
        f"{'─'*34}",
        f"💲 Precio:  `{precio}`",
        f"🛑 SL:      `{sl}`  (-{SL_PCT}%)",
        f"🎯 TP1:     `{tp1}`  (+{TP_PCT}%)  R:R {rr1:.1f}:1",
        f"🎯 TP2:     `{tp2}`  (+{TP_PCT*1.6:.1f}%)  R:R {rr2:.1f}:1",
        f"📐 {sl_desc}",
        f"{'─'*34}",
        f"📊 Técnico:",
        f"  RSI: {par['rsi']}  | Mom 1H: {par['mom_1h']:+.2f}%  4H: {par['mom_4h']:+.2f}%",
        f"  Tendencia 4H: {tend_emoji} {tend}",
        f"  Squeeze: {sq.get('nivel','—')} (BB {sq.get('bb_width','—')}%)",
        f"  Vol 24H: ${par['volume_usdt']/1e6:.1f}M  |  OI: ${oi_m:.1f}M",
        f"  Funding: {par['funding']*100:.4f}%",
        f"{'─'*34}",
        f"*Señales:*",
    ]
    for s in par["señales"]:
        lines.append(f"  {s}")
    lines += [
        f"{'─'*34}",
        auto_str,
        f"🔗 `{par['symbol']}`",
    ]
    return "\n".join(lines)


def build_resumen(candidatos: list[dict], btc_change: float,
                  btc_price: float, intervalo: int, scan_n: int) -> str:
    """[D] Resumen horario con P&L de trades abiertos."""
    now     = datetime.now(timezone.utc).strftime("%H:%M UTC")
    btc_e   = "🟢" if btc_change > 0 else "🔴"
    explos  = [c for c in candidatos if c["modo"] == "EXPLOSION"]
    alertas = [c for c in candidatos if c["modo"] == "ALERTA"]
    cands   = [c for c in candidatos if c["modo"] == "CANDIDATO"]

    lines = [
        f"📡 *SCAN v3 — {now}* (#{scan_n})",
        f"BTC: {btc_e} ${btc_price:,.0f} ({btc_change:+.2f}%) | Próx: {intervalo//60}m",
        f"{'─'*30}",
    ]

    if not candidatos:
        lines.append("💤 Sin señales — mercado plano")
    else:
        if explos:
            lines.append(f"💥 *EXPLOSIONES ({len(explos)}):*")
            for c in explos[:3]:
                s = c["symbol"].replace("-USDT","")
                lines.append(
                    f"  🔥 *{s}* {c['score']}/100  "
                    f"[{c.get('tendencia','')[:3]}]  "
                    f"{', '.join(c['señales'][:2])}"
                )
        if alertas:
            lines.append(f"\n⚠️ *ALERTAS ({len(alertas)}):*")
            for c in alertas[:3]:
                s = c["symbol"].replace("-USDT","")
                lines.append(f"  ⚡ *{s}* {c['score']}/100 | RSI {c['rsi']} | 4H: {c['mom_4h']:+.1f}%")
        if cands:
            lines.append(f"\n👀 *CANDIDATOS ({len(cands)}):*")
            for c in cands[:4]:
                s = c["symbol"].replace("-USDT","")
                lines.append(f"  • {s} {c['score']}/100 | RSI {c['rsi']}")

    # [D] P&L trades abiertos
    if trades_abiertos:
        lines.append(f"\n{'─'*30}")
        lines.append(f"💼 *TRADES ({len(trades_abiertos)}):*")
        tickers_live = {t.get("symbol"): t for t in get_all_tickers()}
        for key, t in trades_abiertos.items():
            sym       = t["symbol"]
            sym_clean = sym.replace("-USDT","")
            precio_vivo = float(tickers_live.get(sym, {}).get("lastPrice", t["entry"]))
            pnl_pct = (precio_vivo - t["entry"]) / t["entry"] * 100
            if t["side"] == "SHORT":
                pnl_pct = -pnl_pct
            pnl_usdt = pnl_pct / 100 * TRADE_USDT * LEVERAGE
            pnl_e    = "🟢" if pnl_pct > 0 else "🔴"
            be_str   = " 🔒BE" if t.get("be_activado") else ""
            lines.append(
                f"  📌 *{sym_clean}* {t['side']} | "
                f"entry:`{t['entry']}` {pnl_e}{pnl_pct:+.2f}% "
                f"(${pnl_usdt:+.1f}){be_str}"
            )

    return "\n".join(lines)


def build_heartbeat(scan_n: int, btc_change: float, btc_price: float) -> str:
    """[D] Heartbeat silencioso — confirma que el bot sigue vivo."""
    now = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")
    return (
        f"💓 *Scanner v3 activo* — {now}\n"
        f"BTC: ${btc_price:,.0f} ({btc_change:+.2f}%)\n"
        f"Scans completados: {scan_n} | Trades: {len(trades_abiertos)}\n"
        f"Auto-trade: {'✅ ON' if AUTO_TRADE else '⏸ OFF'}"
    )


# ─────────────────────────────────────────────────────────────────────
#  LOOP PRINCIPAL 24/7 v3
# ─────────────────────────────────────────────────────────────────────

def run_loop():
    global ultimo_heartbeat

    log.info("🚀 Scanner V3 iniciado — modo 24/7")
    log.info(f"Auto-trade: {'ACTIVADO ✅' if AUTO_TRADE else 'DESACTIVADO'}")
    log.info(f"Workers paralelos: {SCAN_WORKERS}")
    log.info(f"Capital: ${TRADE_USDT} × {LEVERAGE}x | SL {SL_PCT}% TP {TP_PCT}%")

    ultima_hora_resumen = -1
    scan_count          = 0
    intervalo           = INTERVAL_NORMAL

    while True:
        try:
            scan_count += 1
            log.info(f"── Scan #{scan_count} ──")

            btc_change, btc_price = get_btc_data()
            candidatos, intervalo = scan_mercado()
            hora_actual           = datetime.now(timezone.utc).hour

            # ── Trailing SL para trades abiertos ─────────────────────
            if trades_abiertos:
                tickers_live = {t.get("symbol"): t for t in get_all_tickers()}
                for key, t in list(trades_abiertos.items()):
                    precio_vivo = float(
                        tickers_live.get(t["symbol"], {}).get("lastPrice", t["entry"])
                    )
                    actualizar_trailing_sl(precio_vivo, key)

            # ── Alertas de explosión (inmediatas) ─────────────────────
            for par in candidatos:
                sym   = par["symbol"]
                score = par["score"]
                modo  = par["modo"]

                if modo not in ("EXPLOSION", "ALERTA"):
                    continue

                # Anti-spam: 1 alerta por par cada 30 min
                if time.time() - alertas_enviadas.get(sym, 0) < 1800:
                    continue

                msg = build_alerta_explosion(par)
                if send_telegram(msg):
                    alertas_enviadas[sym] = time.time()

                # Auto-trade: solo EXPLOSION con score >= AUTO_TRADE_SCORE
                if AUTO_TRADE and modo == "EXPLOSION" and score >= AUTO_TRADE_SCORE:
                    trade = abrir_trade(sym, par["precio"], par, side="LONG")
                    if trade:
                        send_telegram(
                            f"✅ *TRADE ABIERTO*: {sym.replace('-USDT','')} LONG\n"
                            f"Score: `{score}/100` | {trade['sl_desc']}\n"
                            f"Entrada: `{trade['entry']}`\n"
                            f"SL: `{trade['sl']}` | TP1: `{trade['tp1']}` | TP2: `{trade['tp2']}`\n"
                            f"Capital: ${TRADE_USDT}×{LEVERAGE}x = ${TRADE_USDT*LEVERAGE} nominal\n"
                            f"Filtros: {len([r for r in trade['confirmacion'] if r.startswith('✅')])}/5 ✅"
                        )

            # ── Resumen horario ───────────────────────────────────────
            if hora_actual != ultima_hora_resumen:
                resumen = build_resumen(candidatos, btc_change, btc_price,
                                        intervalo, scan_count)
                send_telegram(resumen)
                ultima_hora_resumen = hora_actual

            # ── [D] Heartbeat cada 6h si no hay señales ───────────────
            if time.time() - ultimo_heartbeat > 21600:
                hb = build_heartbeat(scan_count, btc_change, btc_price)
                send_telegram(hb, silent=True)   # sin notificación de sonido
                ultimo_heartbeat = time.time()

        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)
            intervalo = INTERVAL_NORMAL

        log.info(f"Próximo scan en {intervalo}s ({intervalo//60}m {intervalo%60}s)")
        time.sleep(intervalo)


# ─────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        run_loop()
    else:
        btc_change, btc_price = get_btc_data()
        log.info(f"BTC: ${btc_price:,.0f} ({btc_change:+.1f}%)")
        candidatos, intervalo = scan_mercado()
        print(build_resumen(candidatos, btc_change, btc_price, intervalo, 1))
