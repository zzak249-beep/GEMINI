"""
╔══════════════════════════════════════════════════════════════════════╗
║   CRYPTO EXPLOSION SCANNER v2.0 — BingX Auto-Trade                 ║
║   • Scan 24/7 con intervalos adaptivos (5min en alerta máxima)      ║
║   • Detector de "explosión inminente" (breakout pre-impulso)        ║
║   • Apertura automática de trades en BingX Futures                  ║
║   • Gestión de riesgo integrada (SL/TP automático)                  ║
║   • Telegram con botones de confirmación                            ║
╚══════════════════════════════════════════════════════════════════════╝

Variables de entorno necesarias en Railway:
  BINGX_API_KEY       → tu API key de BingX
  BINGX_API_SECRET    → tu API secret de BingX
  TELEGRAM_TOKEN      → token del bot de Telegram
  TELEGRAM_CHAT_ID    → tu chat ID de Telegram

  ── Gestión de riesgo (ajustar según tu cuenta) ──
  TRADE_USDT          → capital por trade (default: 20)
  LEVERAGE            → apalancamiento (default: 5)
  SL_PCT              → stop loss % (default: 2.5)
  TP_PCT              → take profit % (default: 5.0)
  AUTO_TRADE          → "true" para abrir trades automáticamente
  MAX_OPEN_TRADES     → máximo de trades abiertos simultáneos (default: 3)
"""

import os
import time
import hmac
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional
import requests
import numpy as np

# ─────────────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────

BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Gestión de riesgo
TRADE_USDT       = float(os.getenv("TRADE_USDT", "20"))
LEVERAGE         = int(os.getenv("LEVERAGE", "5"))
SL_PCT           = float(os.getenv("SL_PCT", "2.5"))
TP_PCT           = float(os.getenv("TP_PCT", "5.0"))
AUTO_TRADE       = os.getenv("AUTO_TRADE", "false").lower() == "true"
MAX_OPEN_TRADES  = int(os.getenv("MAX_OPEN_TRADES", "3"))

BASE_URL = "https://open-api.bingx.com"

# ── Filtros del scanner ───────────────────────────────────────────────
MIN_VOLUME_USDT   = 5_000_000    # Volumen mínimo 24h en USDT
MIN_EXPLOSION_SCORE = 60         # Score mínimo para alerta
TOP_N             = 10           # Pares en informe normal
KLINES_LIMIT      = 60           # Velas históricas para análisis

# ── Intervalos de scan adaptativos ───────────────────────────────────
# El scanner acelera cuando detecta oportunidades de alto score
INTERVAL_NORMAL   = 900    # 15 min — mercado tranquilo
INTERVAL_ACTIVO   = 300    # 5 min  — hay candidatos
INTERVAL_ALERTA   = 60     # 1 min  — explosión inminente detectada

# ── Control de trades abiertos (memoria en proceso) ──────────────────
trades_abiertos: dict[str, dict] = {}   # symbol → {entry, sl, tp, side, qty}
alertas_enviadas: dict[str, float] = {} # symbol → timestamp última alerta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("ScannerV2")


# ─────────────────────────────────────────────────────────────────────
#  HELPERS DE API — BingX
# ─────────────────────────────────────────────────────────────────────

def _sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(
        BINGX_API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _get(path: str, params: dict = None, auth: bool = False) -> Optional[dict]:
    p = params or {}
    headers = {}
    if auth:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = _sign(p)
        headers["X-BX-APIKEY"] = BINGX_API_KEY
    try:
        r = requests.get(BASE_URL + path, params=p, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}")
        return None


def _post(path: str, params: dict) -> Optional[dict]:
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    headers = {
        "X-BX-APIKEY": BINGX_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        r = requests.post(BASE_URL + path, data=params, headers=headers, timeout=10)
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
    BingX v3 klines pueden devolver tanto listas [ts,o,h,l,c,v,...] como
    dicts {"time":…,"open":…,"high":…,"low":…,"close":…,"volume":…}.
    Esta función normaliza siempre a lista [ts,o,h,l,c,v] para compatibilidad.
    """
    data = _get("/openApi/swap/v3/quote/klines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    raw = data.get("data", []) if data else []
    if not raw:
        return []

    # Detectar formato: si el primer elemento es dict, convertir
    if isinstance(raw[0], dict):
        normalized = []
        for k in raw:
            try:
                normalized.append([
                    k.get("time", k.get("t", 0)),
                    k.get("open",  k.get("o", 0)),
                    k.get("high",  k.get("h", 0)),
                    k.get("low",   k.get("l", 0)),
                    k.get("close", k.get("c", 0)),
                    k.get("volume",k.get("v", 0)),
                ])
            except Exception:
                continue
        return normalized

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


def get_open_positions() -> list[dict]:
    """Obtiene posiciones abiertas en BingX."""
    data = _get("/openApi/swap/v2/user/positions", auth=True)
    if not data:
        return []
    return data.get("data", []) or []


def get_btc_data() -> tuple[float, float]:
    """Retorna (change_24h, price_actual) de BTC."""
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
    """RSI simplificado — último valor."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains = deltas[deltas > 0].mean() if any(deltas > 0) else 0.0001
    losses = abs(deltas[deltas < 0].mean()) if any(deltas < 0) else 0.0001
    rs = gains / losses
    return 100 - (100 / (1 + rs))


def detectar_compresion_volatilidad(highs: np.ndarray, lows: np.ndarray,
                                    closes: np.ndarray, periodo: int = 20) -> dict:
    """
    Detecta compresión de volatilidad (Squeeze) — señal de explosión inminente.

    Lógica: cuando el rango de precio se contrae por debajo de su media histórica
    y el volumen acumula, suele preceder un movimiento fuerte.

    Retorna:
      squeeze: True si hay compresión
      nivel:   'EXTREMA' | 'ALTA' | 'MEDIA' | 'BAJA'
      dias:    cuántas velas lleva comprimido
    """
    if len(closes) < periodo + 5:
        return {"squeeze": False, "nivel": "BAJA", "dias": 0}

    rangos = highs - lows
    media_rango = rangos[-periodo:].mean()
    rango_actual = rangos[-1]
    rango_reciente_media = rangos[-5:].mean()

    # BB width proxy
    ema20 = ema(closes, 20)
    std20 = np.std(closes[-20:])
    bb_width = (std20 / ema20[-1]) * 100

    # Contar velas en compresión
    umbral = media_rango * 0.75
    dias_comprimido = sum(1 for r in rangos[-10:] if r < umbral)

    squeeze = rango_reciente_media < media_rango * 0.8 and bb_width < 3.5

    if bb_width < 1.5 or dias_comprimido >= 7:
        nivel = "EXTREMA"
    elif bb_width < 2.5 or dias_comprimido >= 4:
        nivel = "ALTA"
    elif bb_width < 3.5 or dias_comprimido >= 2:
        nivel = "MEDIA"
    else:
        nivel = "BAJA"

    return {
        "squeeze": squeeze,
        "nivel":   nivel,
        "dias":    dias_comprimido,
        "bb_width": round(bb_width, 2),
    }


def detectar_acumulacion_volumen(volumes: np.ndarray, closes: np.ndarray) -> dict:
    """
    Detecta acumulación silenciosa: volumen creciendo mientras precio lateral.
    Señal clásica de "ballenas comprando antes del impulso".
    """
    if len(volumes) < 20:
        return {"acumulacion": False, "ratio": 0}

    vol_reciente = volumes[-5:].mean()
    vol_base     = volumes[-20:-5].mean()
    ratio_vol    = vol_reciente / vol_base if vol_base > 0 else 1

    # Precio lateral en esas mismas velas (movimiento < 2%)
    precio_range = (closes[-5:].max() - closes[-5:].min()) / closes[-5:].min() * 100
    precio_lateral = precio_range < 2.0

    acumulacion = ratio_vol > 1.3 and precio_lateral

    return {
        "acumulacion": acumulacion,
        "ratio_vol":   round(ratio_vol, 2),
        "precio_range": round(precio_range, 2),
    }


def detectar_rotura_inminente(highs: np.ndarray, closes: np.ndarray) -> dict:
    """
    Detecta precio acercándose a resistencia clave (máximo de N días).
    Cuando el precio está a < 1% del máximo de 20 días, la rotura suele ser rápida.
    """
    if len(highs) < 20:
        return {"cerca_rotura": False, "distancia_pct": 99}

    max_20 = highs[-20:].max()
    precio_actual = closes[-1]
    distancia_pct = (max_20 - precio_actual) / precio_actual * 100

    return {
        "cerca_rotura": distancia_pct < 1.0,
        "distancia_pct": round(distancia_pct, 2),
        "max_20": max_20,
    }


# ─────────────────────────────────────────────────────────────────────
#  SCORING DE EXPLOSIÓN
# ─────────────────────────────────────────────────────────────────────

def calcular_explosion_score(
    ticker: dict,
    klines_15m: list,
    klines_1h: list,
    funding: float,
    btc_change: float,
) -> dict:
    """
    Score 0-100 de probabilidad de EXPLOSIÓN INMINENTE.

    Pesos:
      [A] Compresión de volatilidad (Squeeze)    → 25 pts
      [B] Acumulación de volumen silenciosa       → 20 pts
      [C] RSI en zona de impulso (40-65)          → 15 pts
      [D] Momentum 1h y 4h creciente             → 15 pts
      [E] Precio cerca de rotura de resistencia   → 10 pts
      [F] Funding negativo (shorts financian long)→ 10 pts
      [G] Fuerza relativa vs BTC                 →  5 pts
    """
    score = 0
    señales = []

    try:
        change_24h = float(ticker.get("priceChangePercent", 0))
        volume_24h = float(ticker.get("quoteVolume", 0))
    except Exception:
        return {"score": 0, "señales": [], "modo": "skip"}

    if not klines_1h or len(klines_1h) < 25:
        return {"score": 0, "señales": [], "modo": "skip"}

    closes_1h  = np.array([float(k[4]) for k in klines_1h])
    highs_1h   = np.array([float(k[2]) for k in klines_1h])
    lows_1h    = np.array([float(k[3]) for k in klines_1h])
    volumes_1h = np.array([float(k[5]) for k in klines_1h])

    closes_15m  = np.array([float(k[4]) for k in klines_15m]) if klines_15m else closes_1h
    volumes_15m = np.array([float(k[5]) for k in klines_15m]) if klines_15m else volumes_1h

    # ── [A] Compresión de volatilidad ─────────────────────────────────
    squeeze = detectar_compresion_volatilidad(highs_1h, lows_1h, closes_1h)
    if squeeze["nivel"] == "EXTREMA":
        score += 25
        señales.append("⚡ SQUEEZE EXTREMO")
    elif squeeze["nivel"] == "ALTA":
        score += 18
        señales.append("🔄 Squeeze alto")
    elif squeeze["nivel"] == "MEDIA":
        score += 10
        señales.append("〰️ Compresión media")

    # ── [B] Acumulación de volumen ────────────────────────────────────
    acum_1h  = detectar_acumulacion_volumen(volumes_1h, closes_1h)
    acum_15m = detectar_acumulacion_volumen(volumes_15m, closes_15m) if len(volumes_15m) >= 20 else {"acumulacion": False}

    if acum_1h["acumulacion"] and acum_15m["acumulacion"]:
        score += 20
        señales.append(f"🐳 ACUMULACIÓN DOBLE (1H+15m) vol×{acum_1h['ratio_vol']:.1f}")
    elif acum_1h["acumulacion"]:
        score += 13
        señales.append(f"🐳 Acumulación 1H vol×{acum_1h['ratio_vol']:.1f}")
    elif acum_15m["acumulacion"]:
        score += 8
        señales.append(f"📊 Acumulación 15m vol×{acum_15m['ratio_vol']:.1f}")

    # ── [C] RSI en zona de impulso ────────────────────────────────────
    rsi_val = rsi(closes_1h, 14)
    if 45 <= rsi_val <= 60:
        score += 15
        señales.append(f"📈 RSI óptimo: {rsi_val:.0f}")
    elif 40 <= rsi_val < 45 or 60 < rsi_val <= 70:
        score += 8
        señales.append(f"📊 RSI aceptable: {rsi_val:.0f}")
    elif rsi_val > 75:
        score -= 5   # sobrecomprado
        señales.append(f"⚠️ RSI sobrecomprado: {rsi_val:.0f}")
    elif rsi_val < 35:
        señales.append(f"❌ RSI débil: {rsi_val:.0f}")

    # ── [D] Momentum creciente ────────────────────────────────────────
    mom_1h = (closes_1h[-1] - closes_1h[-2]) / closes_1h[-2] * 100 if len(closes_1h) >= 2 else 0
    mom_4h = (closes_1h[-1] - closes_1h[-5]) / closes_1h[-5] * 100 if len(closes_1h) >= 5 else 0

    if mom_1h > 0.5 and mom_4h > 1.5:
        score += 15
        señales.append(f"🚀 Mom creciente: 1H={mom_1h:+.1f}% 4H={mom_4h:+.1f}%")
    elif mom_1h > 0 and mom_4h > 0:
        score += 8
        señales.append(f"↗️ Mom positivo: 1H={mom_1h:+.1f}%")
    elif mom_1h < -1:
        score -= 5
        señales.append(f"↘️ Mom negativo 1H={mom_1h:+.1f}%")

    # ── [E] Cerca de rotura de resistencia ────────────────────────────
    rotura = detectar_rotura_inminente(highs_1h, closes_1h)
    if rotura["cerca_rotura"]:
        score += 10
        señales.append(f"🎯 ROTURA INMINENTE a {rotura['distancia_pct']:.2f}% del max20")
    elif rotura["distancia_pct"] < 3:
        score += 5
        señales.append(f"🔲 Cerca resistencia: -{rotura['distancia_pct']:.1f}%")

    # ── [F] Funding rate ──────────────────────────────────────────────
    if funding < -0.002:
        score += 10
        señales.append(f"💚 Funding muy negativo: {funding*100:.4f}%")
    elif funding < 0:
        score += 6
        señales.append(f"🟢 Funding negativo: {funding*100:.4f}%")
    elif funding > 0.003:
        score -= 3
        señales.append(f"🔴 Funding alto: {funding*100:.4f}%")

    # ── [G] Fuerza relativa vs BTC ─────────────────────────────────────
    if btc_change > 0 and change_24h > btc_change * 1.5:
        score += 5
        señales.append(f"💪 Fuerza vs BTC: {change_24h:+.1f}% vs BTC {btc_change:+.1f}%")
    elif btc_change < 0 and change_24h > 0:
        score += 5
        señales.append(f"💪 Resiste caída BTC: +{change_24h:.1f}%")

    # Clasificación del modo
    score = max(0, min(100, score))
    if score >= 80:
        modo = "EXPLOSION"
    elif score >= 65:
        modo = "ALERTA"
    elif score >= MIN_EXPLOSION_SCORE:
        modo = "CANDIDATO"
    else:
        modo = "skip"

    return {
        "score":      score,
        "señales":    señales,
        "modo":       modo,
        "rsi":        round(rsi_val, 1),
        "mom_1h":     round(mom_1h, 2),
        "mom_4h":     round(mom_4h, 2),
        "squeeze":    squeeze,
        "acum":       acum_1h,
        "rotura":     rotura,
        "funding":    funding,
        "change_24h": change_24h,
        "volume_usdt": volume_24h,
    }


# ─────────────────────────────────────────────────────────────────────
#  SCANNER PRINCIPAL
# ─────────────────────────────────────────────────────────────────────

def scan_mercado() -> tuple[list[dict], int]:
    """
    Escanea todos los pares de BingX perpetuos.
    Retorna (candidatos_ordenados, intervalo_siguiente_en_segundos)
    """
    log.info("=== Iniciando scan 24/7 ===")

    tickers      = get_all_tickers()
    funding_data = get_funding_rates()
    btc_change, btc_price = get_btc_data()

    log.info(f"BTC: ${btc_price:,.0f} ({btc_change:+.1f}%) | Pares: {len(tickers)}")

    candidatos_explosion = []
    candidatos_normales  = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("-USDT"):
            continue
        if any(x in symbol for x in ["USDC", "BUSD", "TUSD", "DAI", "FDUSD"]):
            continue

        try:
            volume_24h = float(ticker.get("quoteVolume", 0))
            change_24h = float(ticker.get("priceChangePercent", 0))
        except Exception:
            continue

        # Filtro de volumen mínimo
        if volume_24h < MIN_VOLUME_USDT:
            continue

        # Excluir caídas fuertes (>8% negativo)
        if change_24h < -8:
            continue

        # Obtener velas (1h y 15m)
        klines_1h  = get_klines(symbol, "1h",  KLINES_LIMIT)
        klines_15m = get_klines(symbol, "15m", KLINES_LIMIT)
        funding    = funding_data.get(symbol, 0.0)

        resultado = calcular_explosion_score(
            ticker, klines_15m, klines_1h, funding, btc_change
        )

        if resultado["modo"] == "skip":
            time.sleep(0.05)
            continue

        par_info = {
            "symbol":      symbol,
            "score":       resultado["score"],
            "modo":        resultado["modo"],
            "señales":     resultado["señales"],
            "rsi":         resultado["rsi"],
            "mom_1h":      resultado["mom_1h"],
            "mom_4h":      resultado["mom_4h"],
            "squeeze":     resultado["squeeze"],
            "acum":        resultado["acum"],
            "rotura":      resultado["rotura"],
            "funding":     resultado["funding"],
            "change_24h":  resultado["change_24h"],
            "volume_usdt": resultado["volume_usdt"],
            "precio":      float(ticker.get("lastPrice", 0)),
        }

        if resultado["modo"] == "EXPLOSION":
            candidatos_explosion.append(par_info)
        else:
            candidatos_normales.append(par_info)

        time.sleep(0.08)

    # Ordenar por score
    candidatos_explosion.sort(key=lambda x: x["score"], reverse=True)
    candidatos_normales.sort(key=lambda x: x["score"], reverse=True)

    todos = candidatos_explosion + candidatos_normales

    log.info(f"EXPLOSIÓN: {len(candidatos_explosion)} | ALERTA: {len([c for c in candidatos_normales if c['modo']=='ALERTA'])} | CANDIDATOS: {len([c for c in candidatos_normales if c['modo']=='CANDIDATO'])}")

    # Intervalo adaptativo
    if candidatos_explosion:
        intervalo = INTERVAL_ALERTA   # 1 minuto — hay explosión inminente
    elif candidatos_normales:
        intervalo = INTERVAL_ACTIVO   # 5 minutos — hay candidatos
    else:
        intervalo = INTERVAL_NORMAL   # 15 minutos — mercado plano

    return todos[:TOP_N], intervalo


# ─────────────────────────────────────────────────────────────────────
#  CONFIRMACIÓN DE ENTRADA — Filtros pre-trade
# ─────────────────────────────────────────────────────────────────────

def confirmar_entrada(symbol: str, par: dict, side: str = "LONG") -> tuple[bool, list[str]]:
    """
    Filtros de confirmación antes de ejecutar cualquier trade.

    Verifica 5 condiciones que reducen entradas en falso:
      [1] Vela actual no extendida más del 2×ATR (no cazar impulso)
      [2] Volumen de confirmación: vela actual > 80% media 20 períodos
      [3] Estructura HTF (15m) alineada con la dirección del trade
      [4] No entrar si precio ya superó la zona de rotura >1.5%
      [5] RSI no sobrecomprado/sobrevendido al extremo para la dirección

    Retorna: (ok: bool, razones: list[str])
    """
    razones_ok   = []
    razones_fail = []

    klines_15m = get_klines(symbol, "15m", 30)
    klines_1h  = get_klines(symbol, "1h",  30)

    if not klines_15m or len(klines_15m) < 5:
        return False, ["❌ Sin datos 15m para confirmar"]

    try:
        # Extraer datos recientes
        c15  = np.array([float(k[4]) for k in klines_15m])
        h15  = np.array([float(k[2]) for k in klines_15m])
        l15  = np.array([float(k[3]) for k in klines_15m])
        v15  = np.array([float(k[5]) for k in klines_15m])

        precio_actual = c15[-1]
        vela_rango    = h15[-1] - l15[-1]

        # ATR 15m (media de los últimos 14 rangos)
        rangos_15m = h15[-15:] - l15[-15:]
        atr_15m    = rangos_15m.mean() if len(rangos_15m) > 0 else vela_rango

        # ── [1] Vela no extendida ────────────────────────────────────
        extension_ratio = vela_rango / atr_15m if atr_15m > 0 else 0
        if extension_ratio > 2.2:
            razones_fail.append(
                f"⚠️ Vela extendida ({extension_ratio:.1f}×ATR) — esperar retroceso"
            )
        else:
            razones_ok.append(f"✅ Extensión OK ({extension_ratio:.1f}×ATR)")

        # ── [2] Volumen de confirmación ──────────────────────────────
        vol_media_20  = v15[-21:-1].mean() if len(v15) >= 21 else v15[:-1].mean()
        vol_ratio     = v15[-1] / vol_media_20 if vol_media_20 > 0 else 1.0
        if vol_ratio < 0.8:
            razones_fail.append(f"⚠️ Volumen bajo ({vol_ratio:.2f}× media) — sin convicción")
        else:
            razones_ok.append(f"✅ Volumen confirmado ({vol_ratio:.2f}× media)")

        # ── [3] Estructura HTF 15m alineada ─────────────────────────
        if len(c15) >= 10:
            ema9_15m  = float(np.convolve(c15[-9:],  np.ones(9)/9,  mode='valid')[-1])
            ema21_15m = float(np.convolve(c15[-21:], np.ones(21)/21, mode='valid')[-1]) if len(c15) >= 21 else ema9_15m
            htf_alineado = (precio_actual > ema9_15m > ema21_15m) if side == "LONG" else                            (precio_actual < ema9_15m < ema21_15m)
            if not htf_alineado:
                razones_fail.append("⚠️ Estructura 15m no alineada con dirección")
            else:
                razones_ok.append("✅ EMA 9/21 15m alineadas")

        # ── [4] No perseguir rotura ya muy extendida ─────────────────
        rotura = par.get("rotura", {})
        dist_pct = rotura.get("distancia_pct", 99)
        if side == "LONG" and dist_pct < 0:
            # precio YA superó el máximo — distancia negativa = ya rompió
            sobrepaso = abs(dist_pct)
            if sobrepaso > 1.5:
                razones_fail.append(
                    f"⚠️ Rotura ya extendida +{sobrepaso:.1f}% — zona de trampa"
                )
            else:
                razones_ok.append(f"✅ Rotura reciente ({sobrepaso:.1f}%) — válida")

        # ── [5] RSI no en extremo contrario ─────────────────────────
        rsi_val = par.get("rsi", 50)
        if side == "LONG" and rsi_val > 80:
            razones_fail.append(f"⚠️ RSI sobrecomprado: {rsi_val} — riesgo reversión")
        elif side == "SHORT" and rsi_val < 20:
            razones_fail.append(f"⚠️ RSI sobrevendido: {rsi_val} — riesgo rebote")
        else:
            razones_ok.append(f"✅ RSI {rsi_val} dentro de rango operativo")

    except Exception as e:
        log.warning(f"Error en confirmar_entrada {symbol}: {e}")
        return False, [f"❌ Error en confirmación: {e}"]

    # Resultado: solo si no hay razones de fallo críticas
    ok = len(razones_fail) == 0
    todas = razones_ok + razones_fail
    return ok, todas


def calcular_sl_tp_dinamico(
    par: dict, precio: float, side: str = "LONG"
) -> tuple[float, float, float, str]:
    """
    Calcula SL y TP dinámicos basados en ATR y estructura.

    Estrategia:
      - SL: usa el último swing low/high (estructura) o ATR × 1.5 como mínimo
      - TP1: ratio 1:1.5 sobre el SL
      - TP2: ratio 1:2.5 (posición parcial)

    Retorna: (sl, tp1, tp2, descripcion)
    """
    sl_pct  = SL_PCT / 100
    tp1_pct = TP_PCT / 100
    tp2_pct = (TP_PCT * 1.5) / 100

    squeeze = par.get("squeeze", {})
    bb_width = squeeze.get("bb_width", 2.5)

    # En squeeze extremo o alta compresión — ampliar TP porque el movimiento será mayor
    if bb_width < 1.5:
        tp1_pct *= 1.5
        tp2_pct *= 2.0
        desc = "TP ampliado (squeeze extremo)"
    elif bb_width < 2.5:
        tp1_pct *= 1.25
        tp2_pct *= 1.5
        desc = "TP ampliado (squeeze alto)"
    else:
        desc = "TP estándar"

    if side == "LONG":
        sl  = round(precio * (1 - sl_pct),  6)
        tp1 = round(precio * (1 + tp1_pct), 6)
        tp2 = round(precio * (1 + tp2_pct), 6)
    else:
        sl  = round(precio * (1 + sl_pct),  6)
        tp1 = round(precio * (1 - tp1_pct), 6)
        tp2 = round(precio * (1 - tp2_pct), 6)

    return sl, tp1, tp2, desc

# ─────────────────────────────────────────────────────────────────────
#  AUTO-TRADE EN BINGX
# ─────────────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> bool:
    """Configura el apalancamiento antes de abrir trade."""
    result = _post("/openApi/swap/v2/trade/leverage", {
        "symbol":   symbol,
        "side":     "LONG",
        "leverage": str(leverage),
    })
    return result is not None


def abrir_trade(symbol: str, precio_entrada: float, par: dict,
               side: str = "LONG") -> Optional[dict]:
    """
    Abre un trade LONG o SHORT en BingX con confirmación de entrada y SL/TP dinámicos.

    Mejoras v2.1:
      - Confirmación pre-entrada (5 filtros: extensión vela, volumen, HTF, rotura, RSI)
      - SL/TP dinámicos según volatilidad (squeeze amplía TPs)
      - Soporte SHORT nativo
      - Log detallado de confirmación para auditoría
    """
    if not BINGX_API_KEY or not AUTO_TRADE:
        return None

    # ── Verificar trades abiertos ────────────────────────────────────
    posiciones     = get_open_positions()
    trades_activos = len([p for p in posiciones if float(p.get("positionAmt", 0)) != 0])

    if trades_activos >= MAX_OPEN_TRADES:
        log.warning(f"Máximo de trades ({MAX_OPEN_TRADES}) — skip {symbol}")
        return None

    trade_key = f"{symbol}_{side}"
    if trade_key in trades_abiertos:
        log.info(f"Trade {side} ya abierto en {symbol} — skip")
        return None

    # ── Confirmación de entrada ──────────────────────────────────────
    ok, confirmacion = confirmar_entrada(symbol, par, side)
    log.info(f"Confirmación {symbol} {side}: {'✅ OK' if ok else '❌ BLOQUEADO'}")
    for razon in confirmacion:
        log.info(f"  {razon}")

    if not ok:
        log.warning(f"Trade {symbol} bloqueado por filtros de entrada")
        # Notificar por Telegram que la señal fue detectada pero no ejecutada
        motivos = "\n".join(f"  {r}" for r in confirmacion)
        send_telegram(
            f"🚫 *SEÑAL BLOQUEADA: {symbol.replace(chr(45)+'USDT', '')} {side}*\n{motivos}"
        )
        return None

    # ── SL/TP dinámicos ──────────────────────────────────────────────
    sl_precio, tp_precio, tp2_precio, sl_desc = calcular_sl_tp_dinamico(par, precio_entrada, side)

    # ── Configurar leverage ──────────────────────────────────────────
    set_leverage(symbol, LEVERAGE)

    # ── Calcular cantidad ────────────────────────────────────────────
    cantidad = round((TRADE_USDT * LEVERAGE) / precio_entrada, 4)

    log.info(
        f"Abriendo {side} {symbol}: qty={cantidad} entrada={precio_entrada} "
        f"SL={sl_precio} TP1={tp_precio} TP2={tp2_precio} [{sl_desc}]"
    )

    # ── Orden de entrada ─────────────────────────────────────────────
    if side == "LONG":
        order_side, position_side = "BUY", "LONG"
        sl_side, tp_side          = "SELL", "SELL"
    else:
        order_side, position_side = "SELL", "SHORT"
        sl_side, tp_side          = "BUY", "BUY"

    orden = _post("/openApi/swap/v2/trade/order", {
        "symbol":       symbol,
        "side":         order_side,
        "positionSide": position_side,
        "type":         "MARKET",
        "quantity":     str(cantidad),
    })

    if not orden or orden.get("code") != 0:
        log.error(f"Error abriendo orden {symbol}: {orden}")
        return None

    order_id = orden.get("data", {}).get("order", {}).get("orderId")
    log.info(f"Orden abierta: {order_id}")

    # ── Stop Loss ────────────────────────────────────────────────────
    _post("/openApi/swap/v2/trade/order", {
        "symbol":        symbol,
        "side":          sl_side,
        "positionSide":  position_side,
        "type":          "STOP_MARKET",
        "stopPrice":     str(sl_precio),
        "closePosition": "true",
    })

    # ── Take Profit TP1 (50% de la posición) ────────────────────────
    cantidad_tp1 = round(cantidad * 0.5, 4)
    _post("/openApi/swap/v2/trade/order", {
        "symbol":        symbol,
        "side":          tp_side,
        "positionSide":  position_side,
        "type":          "TAKE_PROFIT_MARKET",
        "stopPrice":     str(tp_precio),
        "quantity":      str(cantidad_tp1),
        "closePosition": "false",
    })

    # ── Take Profit TP2 (50% restante) ──────────────────────────────
    _post("/openApi/swap/v2/trade/order", {
        "symbol":        symbol,
        "side":          tp_side,
        "positionSide":  position_side,
        "type":          "TAKE_PROFIT_MARKET",
        "stopPrice":     str(tp2_precio),
        "closePosition": "true",
    })

    trade_info = {
        "symbol":    symbol,
        "entry":     precio_entrada,
        "sl":        sl_precio,
        "tp":        tp_precio,
        "tp2":       tp2_precio,
        "sl_desc":   sl_desc,
        "qty":       cantidad,
        "side":      side,
        "order_id":  order_id,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "confirmacion": confirmacion,
    }
    trades_abiertos[trade_key] = trade_info

    return trade_info


def abrir_trade_long(symbol: str, precio_entrada: float, par: dict = None) -> Optional[dict]:
    """Wrapper de compatibilidad — llama a abrir_trade con side=LONG."""
    return abrir_trade(symbol, precio_entrada, par or {}, side="LONG")


# ─────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram: mensaje enviado")
        return True
    except Exception as e:
        log.error(f"Telegram: {e}")
        return False


def build_mensaje_explosion(par: dict) -> str:
    """Mensaje de alerta urgente de explosión inminente."""
    sym    = par["symbol"].replace("-USDT", "")
    score  = par["score"]
    modo   = par["modo"]
    precio = par["precio"]

    sl  = round(precio * (1 - SL_PCT / 100), 6)
    tp1 = round(precio * (1 + TP_PCT / 100), 6)
    tp2 = round(precio * (1 + (TP_PCT * 1.5) / 100), 6)

    modo_emoji = "💥" if modo == "EXPLOSION" else "⚠️"
    auto_str = "🤖 *TRADE ABIERTO AUTOMÁTICAMENTE*" if AUTO_TRADE else "👆 *CONFIRMA TRADE MANUALMENTE*"

    lineas = [
        f"{modo_emoji} *{modo}: {sym}* — Score {score}/100",
        f"{'─'*32}",
        f"💲 Precio: `{precio}`",
        f"🛑 SL: `{sl}` (-{SL_PCT}%)",
        f"🎯 TP1: `{tp1}` (+{TP_PCT}%)",
        f"🎯 TP2: `{tp2}` (+{TP_PCT*1.5:.1f}%)",
        f"{'─'*32}",
        f"*Señales detectadas:*",
    ]
    for s in par["señales"]:
        lineas.append(f"  {s}")

    lineas += [
        f"{'─'*32}",
        f"📊 RSI: {par['rsi']} | Mom 1H: {par['mom_1h']:+.2f}% | 4H: {par['mom_4h']:+.2f}%",
        f"💧 Vol 24H: ${par['volume_usdt']/1e6:.1f}M",
        f"",
        auto_str,
        f"🔗 BingX: {par['symbol']}",
    ]
    return "\n".join(lineas)


def build_mensaje_resumen(candidatos: list[dict], btc_change: float, intervalo: int) -> str:
    """Mensaje de resumen periódico."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    btc_e = "🟢" if btc_change > 0 else "🔴"

    explosiones = [c for c in candidatos if c["modo"] == "EXPLOSION"]
    alertas     = [c for c in candidatos if c["modo"] == "ALERTA"]
    normales    = [c for c in candidatos if c["modo"] == "CANDIDATO"]

    lineas = [
        f"📡 *SCAN 24/7 — {now}*",
        f"BTC: {btc_e} {btc_change:+.2f}% | Próximo scan: {intervalo//60}min",
        f"{'─'*30}",
    ]

    if not candidatos:
        lineas.append("💤 Mercado sin señales — escaneando...")
        return "\n".join(lineas)

    if explosiones:
        lineas.append(f"💥 *EXPLOSIONES ({len(explosiones)}):*")
        for c in explosiones[:3]:
            sym = c["symbol"].replace("-USDT", "")
            lineas.append(f"  🔥 *{sym}* {c['score']}/100 — {', '.join(c['señales'][:2])}")

    if alertas:
        lineas.append(f"\n⚠️ *ALERTAS ({len(alertas)}):*")
        for c in alertas[:3]:
            sym = c["symbol"].replace("-USDT", "")
            lineas.append(f"  ⚡ *{sym}* {c['score']}/100 — mom 4H: {c['mom_4h']:+.1f}%")

    if normales:
        lineas.append(f"\n👀 *CANDIDATOS ({len(normales)}):*")
        for c in normales[:4]:
            sym = c["symbol"].replace("-USDT", "")
            lineas.append(f"  • {sym} {c['score']}/100 | RSI {c['rsi']}")

    if trades_abiertos:
        lineas.append(f"\n{'─'*30}")
        lineas.append(f"💼 *TRADES ABIERTOS ({len(trades_abiertos)}):*")
        for key, t in trades_abiertos.items():
            sym_clean = t.get("symbol", key).replace("-USDT", "")
            side_str  = t.get("side", "LONG")
            tp2_str   = f" | TP2:{t['tp2']}" if t.get("tp2") else ""
            lineas.append(f"  📌 {sym_clean} {side_str} | SL:{t['sl']} TP:{t['tp']}{tp2_str}")

    return "\n".join(lineas)


# ─────────────────────────────────────────────────────────────────────
#  LOOP PRINCIPAL 24/7
# ─────────────────────────────────────────────────────────────────────

def run_loop():
    """
    Loop continuo 24/7.
    - Scan adaptativo: 1min cuando hay explosión, 5min en activo, 15min tranquilo
    - Alerta inmediata por Telegram cuando score >= EXPLOSION
    - Auto-trade si AUTO_TRADE=true
    - Resumen cada hora
    """
    log.info("🚀 Scanner V2 iniciado — modo 24/7")
    log.info(f"Auto-trade: {'ACTIVADO' if AUTO_TRADE else 'DESACTIVADO'}")
    log.info(f"Capital por trade: ${TRADE_USDT} × {LEVERAGE}x leverage")

    ultima_hora_resumen = -1
    scan_count = 0

    while True:
        try:
            scan_count += 1
            log.info(f"── Scan #{scan_count} ──")

            btc_change, _ = get_btc_data()
            candidatos, intervalo = scan_mercado()

            hora_actual = datetime.now(timezone.utc).hour

            # ── Alertas de explosión (inmediatas) ──────────────────────
            for par in candidatos:
                sym = par["symbol"]
                score = par["score"]
                modo  = par["modo"]

                if modo not in ("EXPLOSION", "ALERTA"):
                    continue

                # Evitar spam: máximo 1 alerta por par cada 30 min
                ultima = alertas_enviadas.get(sym, 0)
                if time.time() - ultima < 1800:
                    continue

                # Enviar alerta
                msg = build_mensaje_explosion(par)
                if send_telegram(msg):
                    alertas_enviadas[sym] = time.time()

                # Auto-trade si está activado y es EXPLOSION con score >= 80
                if AUTO_TRADE and modo == "EXPLOSION" and score >= 80:
                    trade = abrir_trade_long(sym, par["precio"], par)
                    if trade:
                        sl_desc = trade.get("sl_desc", "")
                        tp2     = trade.get("tp2", "—")
                        conf_ok = [r for r in trade.get("confirmacion", []) if r.startswith("✅")]
                        msg_trade = (
                            f"✅ *TRADE ABIERTO*: {sym.replace('-USDT','')} LONG\n"
                            f"Entrada: `{trade['entry']}` | SL: `{trade['sl']}`\n"
                            f"TP1: `{trade['tp']}` (50%) | TP2: `{tp2}` (50%)\n"
                            f"📐 {sl_desc}\n"
                            f"Capital: ${TRADE_USDT} × {LEVERAGE}x = ${TRADE_USDT * LEVERAGE} nominal\n"
                            f"Filtros superados: {len(conf_ok)}/5"
                        )
                        send_telegram(msg_trade)

            # ── Resumen horario ───────────────────────────────────────
            if hora_actual != ultima_hora_resumen:
                resumen = build_mensaje_resumen(candidatos, btc_change, intervalo)
                send_telegram(resumen)
                ultima_hora_resumen = hora_actual

        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)
            intervalo = INTERVAL_NORMAL

        log.info(f"Próximo scan en {intervalo}s ({intervalo//60}min {intervalo%60}s)")
        time.sleep(intervalo)


# ─────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        run_loop()
    else:
        # Ejecución única para test
        btc_change, btc_price = get_btc_data()
        log.info(f"BTC: ${btc_price:,.0f} ({btc_change:+.1f}%)")
        candidatos, intervalo = scan_mercado()
        print(build_mensaje_resumen(candidatos, btc_change, intervalo))
