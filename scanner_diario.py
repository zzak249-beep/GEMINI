"""
╔══════════════════════════════════════════════════════════════╗
║   DAILY MOMENTUM SCANNER — BingX Perpetual Futures          ║
║   Compatible con QF×JP Crypto V3                            ║
║   Stack: Python · BingX API · Telegram                      ║
╚══════════════════════════════════════════════════════════════╝

Detecta los pares con mayor probabilidad de subida diaria
usando 6 filtros combinados:
  [1] Momentum 24h (cambio de precio)
  [2] Volumen real (no manipulado)
  [3] Tendencia en 1H (EMA20)
  [4] Estructura de mercado (HH/HL)
  [5] Correlación BTC
  [6] Funding rate (proxy de sesgo del mercado)

Ejecutar manualmente o programar con cron/Railway.
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

# ─── CONFIG ──────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BASE_URL = "https://open-api.bingx.com"

# Filtros ajustables
MIN_VOLUME_USDT   = 8_000_000    # Volumen mínimo 24h en USDT
MIN_CHANGE_PCT    = 2.0          # Cambio mínimo 24h positivo (%)
MAX_CHANGE_PCT    = 25.0         # Evita los ya explotados (>25%)
MIN_SCORE         = 55           # Score mínimo para aparecer en lista
TOP_N             = 12           # Máximo de pares en el informe
KLINES_1H         = 48           # Velas 1H para análisis de estructura
EMA_PERIOD        = 20           # EMA para tendencia en 1H
SCAN_INTERVAL_SEC = 3600         # Cada hora (para modo continuo)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("Scanner")


# ─── API HELPERS ─────────────────────────────────────────────

def _sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(
        BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()


def _get_public(path: str, params: dict = None) -> Optional[dict]:
    """Llamada pública sin firma."""
    try:
        r = requests.get(BASE_URL + path, params=params or {}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"API error {path}: {e}")
        return None


def _get_private(path: str, params: dict = None) -> Optional[dict]:
    """Llamada privada con firma HMAC."""
    p = params or {}
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    try:
        r = requests.get(BASE_URL + path, params=p, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"API error {path}: {e}")
        return None


# ─── DATOS DE MERCADO ─────────────────────────────────────────

def get_all_tickers() -> list[dict]:
    """Obtiene todos los tickers de futuros perpetuos."""
    data = _get_public("/openApi/swap/v2/quote/ticker")
    if not data or "data" not in data:
        return []
    return data["data"]


def get_klines(symbol: str, interval: str = "1h", limit: int = 50) -> list:
    """Obtiene velas históricas."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = _get_public("/openApi/swap/v3/quote/klines", params)
    if not data or "data" not in data:
        return []
    return data["data"]


def get_btc_change() -> float:
    """Retorna el cambio % 24h de BTC como referencia de mercado."""
    data = _get_public("/openApi/swap/v2/quote/ticker")
    if not data:
        return 0.0
    for t in data.get("data", []):
        if t.get("symbol") == "BTC-USDT":
            try:
                return float(t.get("priceChangePercent", 0))
            except Exception:
                return 0.0
    return 0.0


def get_funding_rates() -> dict[str, float]:
    """Obtiene funding rates actuales (sesgo del mercado)."""
    data = _get_public("/openApi/swap/v2/quote/premiumIndex")
    rates = {}
    if not data or "data" not in data:
        return rates
    for item in data["data"]:
        try:
            symbol = item.get("symbol", "")
            rate   = float(item.get("lastFundingRate", 0))
            rates[symbol] = rate
        except Exception:
            pass
    return rates


# ─── INDICADORES ─────────────────────────────────────────────

def ema(values: np.ndarray, period: int) -> np.ndarray:
    k = 2.0 / (period + 1)
    result = np.zeros(len(values))
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def analizar_estructura_1h(klines: list) -> dict:
    """
    Analiza estructura de mercado en 1H:
    - Tendencia (precio vs EMA20)
    - Higher Highs / Higher Lows (estructura alcista)
    - Volumen creciente
    - Momentum de las últimas 4h vs 4h anteriores
    """
    if len(klines) < EMA_PERIOD + 5:
        return {"valido": False}

    try:
        closes  = np.array([float(k[4]) for k in klines])
        highs   = np.array([float(k[2]) for k in klines])
        lows    = np.array([float(k[3]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])

        # EMA20
        ema20 = ema(closes, EMA_PERIOD)
        sobre_ema = closes[-1] > ema20[-1]

        # Distancia % al EMA (cuánto ha subido ya)
        dist_ema_pct = (closes[-1] - ema20[-1]) / ema20[-1] * 100

        # Higher Highs en últimas 6 velas
        highs_recientes = highs[-6:]
        hh_count = sum(1 for i in range(1, len(highs_recientes))
                       if highs_recientes[i] > highs_recientes[i - 1])

        # Higher Lows en últimas 6 velas
        lows_recientes = lows[-6:]
        hl_count = sum(1 for i in range(1, len(lows_recientes))
                       if lows_recientes[i] > lows_recientes[i - 1])

        estructura_alcista = hh_count >= 3 and hl_count >= 2

        # Volumen: últimas 4h vs 4h anteriores
        vol_reciente  = volumes[-4:].mean()
        vol_anterior  = volumes[-8:-4].mean()
        vol_creciente = vol_reciente > vol_anterior * 1.15

        # Momentum 4h
        mom_4h = (closes[-1] - closes[-4]) / closes[-4] * 100

        # Retroceso desde máximo 24h (no comprar en el pico)
        max_24h  = highs[-24:].max() if len(highs) >= 24 else highs.max()
        ret_max  = (max_24h - closes[-1]) / max_24h * 100  # % de retroceso desde máximo
        no_pico  = ret_max > 1.5  # Al menos 1.5% por debajo del máximo

        return {
            "valido":           True,
            "sobre_ema":        sobre_ema,
            "dist_ema_pct":     round(dist_ema_pct, 2),
            "hh_count":         hh_count,
            "hl_count":         hl_count,
            "estructura":       estructura_alcista,
            "vol_creciente":    vol_creciente,
            "mom_4h":           round(mom_4h, 2),
            "no_en_pico":       no_pico,
            "retroceso_max":    round(ret_max, 2),
        }
    except Exception as e:
        log.debug(f"Error análisis estructura: {e}")
        return {"valido": False}


# ─── SCORING ─────────────────────────────────────────────────

def calcular_score(ticker: dict, estructura: dict, funding: float, btc_change: float) -> dict:
    """
    Sistema de puntuación 0-100 para probabilidad de subida.

    Pesos:
      [1] Momentum 24h         → hasta 25 pts
      [2] Estructura 1H        → hasta 25 pts
      [3] Volumen              → hasta 15 pts
      [4] Funding rate         → hasta 15 pts
      [5] Correlación BTC      → hasta 10 pts
      [6] Posición vs EMA      → hasta 10 pts
    """
    score   = 0
    detalle = {}

    try:
        change_24h = float(ticker.get("priceChangePercent", 0))
        volume_24h = float(ticker.get("quoteVolume", 0))
    except Exception:
        return {"score": 0, "detalle": {}}

    # ── [1] Momentum 24h (25pts) ──────────────────────────────
    if change_24h >= 15:
        pts_mom = 10   # ya subió mucho, riesgo de corrección
    elif change_24h >= 8:
        pts_mom = 20
    elif change_24h >= 5:
        pts_mom = 25
    elif change_24h >= 2:
        pts_mom = 18
    else:
        pts_mom = 5
    score += pts_mom
    detalle["momentum_24h"] = f"{change_24h:+.1f}% → {pts_mom}pts"

    # ── [2] Estructura 1H (25pts) ────────────────────────────
    pts_est = 0
    if estructura.get("valido"):
        if estructura.get("sobre_ema"):
            pts_est += 8
        if estructura.get("estructura"):
            pts_est += 10
        if estructura.get("vol_creciente"):
            pts_est += 4
        if estructura.get("no_en_pico"):
            pts_est += 3
        pts_est = min(pts_est, 25)
    score += pts_est
    detalle["estructura_1h"] = f"HL:{estructura.get('hl_count',0)} HH:{estructura.get('hh_count',0)} → {pts_est}pts"

    # ── [3] Volumen (15pts) ───────────────────────────────────
    if volume_24h >= 50_000_000:
        pts_vol = 15
    elif volume_24h >= 20_000_000:
        pts_vol = 12
    elif volume_24h >= 10_000_000:
        pts_vol = 8
    else:
        pts_vol = 4
    score += pts_vol
    detalle["volumen"] = f"${volume_24h/1e6:.1f}M → {pts_vol}pts"

    # ── [4] Funding Rate (15pts) ──────────────────────────────
    # Funding negativo = shorts pagan = mercado bajista = rebote probable
    # Funding muy positivo = longs pagan = mercado sobrecalentado
    pts_fund = 0
    if funding < -0.001:
        pts_fund = 15   # muy bajista → rebote alcista probable
    elif funding < 0:
        pts_fund = 12   # ligeramente bajista → oportunidad long
    elif funding < 0.001:
        pts_fund = 8    # neutral
    elif funding < 0.003:
        pts_fund = 4    # longs pagando, algo caliente
    else:
        pts_fund = 0    # sobrecalentado, evitar long
    score += pts_fund
    detalle["funding"] = f"{funding*100:.4f}% → {pts_fund}pts"

    # ── [5] Correlación BTC (10pts) ───────────────────────────
    # Si BTC sube y el par también sube → confluencia
    pts_btc = 0
    if btc_change > 1 and change_24h > btc_change:
        pts_btc = 10   # supera a BTC en tendencia alcista
    elif btc_change > 0 and change_24h > 0:
        pts_btc = 6    # ambos suben
    elif btc_change < 0 and change_24h > 0:
        pts_btc = 8    # par fuerte aunque BTC flojea (señal de fuerza relativa)
    elif btc_change < -2 and change_24h < 0:
        pts_btc = 2    # ambos bajan
    score += pts_btc
    detalle["vs_btc"] = f"BTC:{btc_change:+.1f}% Par:{change_24h:+.1f}% → {pts_btc}pts"

    # ── [6] Posición vs EMA (10pts) ───────────────────────────
    pts_ema = 0
    if estructura.get("valido"):
        dist = estructura.get("dist_ema_pct", 0)
        if 0.5 <= dist <= 3:
            pts_ema = 10  # cerca del EMA por encima → zona óptima de entrada
        elif 3 < dist <= 6:
            pts_ema = 6   # algo alejado pero tendencia intacta
        elif dist > 6:
            pts_ema = 2   # muy estirado, riesgo pull-back
        elif dist < 0:
            pts_ema = 0   # bajo EMA, no operar long
    score += pts_ema
    detalle["pos_ema"] = f"Dist EMA: {estructura.get('dist_ema_pct',0):+.1f}% → {pts_ema}pts"

    return {
        "score":   min(score, 100),
        "detalle": detalle,
    }


# ─── SCANNER PRINCIPAL ────────────────────────────────────────

def scan_mercado() -> list[dict]:
    """
    Escanea todos los pares de BingX y devuelve
    los candidatos alcistas ordenados por score.
    """
    log.info("Iniciando scan de mercado...")

    tickers      = get_all_tickers()
    funding_data = get_funding_rates()
    btc_change   = get_btc_change()

    log.info(f"BTC 24h: {btc_change:+.1f}% | Pares disponibles: {len(tickers)}")

    candidatos = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        # Solo pares USDT
        if not symbol.endswith("-USDT"):
            continue
        # Excluir pares estables y exóticos
        if any(x in symbol for x in ["USDC", "BUSD", "TUSD", "DAI", "FDUSD"]):
            continue

        try:
            change_24h = float(ticker.get("priceChangePercent", 0))
            volume_24h = float(ticker.get("quoteVolume", 0))
        except Exception:
            continue

        # Filtros rápidos (evita llamadas API innecesarias)
        if change_24h < MIN_CHANGE_PCT:
            continue
        if change_24h > MAX_CHANGE_PCT:
            continue
        if volume_24h < MIN_VOLUME_USDT:
            continue

        # Análisis de estructura 1H (llamada API por par)
        klines    = get_klines(symbol, "1h", KLINES_1H)
        estructura = analizar_estructura_1h(klines)
        funding   = funding_data.get(symbol, 0.0)

        # Solo pares sobre EMA en 1H (tendencia alcista confirmada)
        if not estructura.get("sobre_ema", False):
            continue

        # Score
        resultado = calcular_score(ticker, estructura, funding, btc_change)
        score = resultado["score"]

        if score < MIN_SCORE:
            continue

        candidatos.append({
            "symbol":       symbol,
            "score":        score,
            "detalle":      resultado["detalle"],
            "change_24h":   change_24h,
            "volume_usdt":  volume_24h,
            "mom_4h":       estructura.get("mom_4h", 0),
            "hl_count":     estructura.get("hl_count", 0),
            "hh_count":     estructura.get("hh_count", 0),
            "dist_ema":     estructura.get("dist_ema_pct", 0),
            "funding":      funding,
            "ret_max":      estructura.get("retroceso_max", 0),
        })

        # Rate limit suave
        time.sleep(0.08)

    # Ordenar por score
    candidatos.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Candidatos encontrados: {len(candidatos)}")

    return candidatos[:TOP_N]


# ─── TELEGRAM ────────────────────────────────────────────────

def build_message(candidatos: list[dict], btc_change: float) -> str:
    """Construye el mensaje de Telegram con formato legible."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    emoji_score = lambda s: "🔥" if s >= 80 else "✅" if s >= 65 else "👀"
    emoji_fund  = lambda f: "🟢" if f < 0 else "🟡" if f < 0.002 else "🔴"

    btc_emoji = "🟢" if btc_change > 0 else "🔴"
    lines = [
        f"📡 *SCANNER DIARIO — {now}*",
        f"{'─'*30}",
        f"BTC: {btc_emoji} {btc_change:+.2f}%",
        f"{'─'*30}",
    ]

    if not candidatos:
        lines.append("⚠️ Sin candidatos alcistas válidos ahora mismo.")
        lines.append("💤 Mercado sin momentum claro → esperar")
        return "\n".join(lines)

    for i, c in enumerate(candidatos, 1):
        s      = c["score"]
        sym    = c["symbol"].replace("-USDT", "")
        chg    = c["change_24h"]
        vol    = c["volume_usdt"] / 1e6
        mom4h  = c["mom_4h"]
        dist   = c["dist_ema"]
        fund   = c["funding"] * 100
        hh     = c["hh_count"]
        hl     = c["hl_count"]
        retmax = c["ret_max"]

        # Calidad de la señal
        if s >= 80:
            calidad = "★ ALTA"
        elif s >= 65:
            calidad = "● MEDIA"
        else:
            calidad = "○ BAJA"

        lines += [
            f"",
            f"{emoji_score(s)} *{i}. {sym}* — Score: {s}/100 [{calidad}]",
            f"   📈 24h: +{chg:.1f}%  |  4h: {mom4h:+.1f}%",
            f"   💧 Vol: ${vol:.1f}M  |  EMA dist: {dist:+.1f}%",
            f"   📊 HH:{hh}/6 HL:{hl}/6  |  Ret.máx: {retmax:.1f}%",
            f"   {emoji_fund(fund)} Funding: {fund:+.4f}%",
        ]

    lines += [
        f"",
        f"{'─'*30}",
        f"📋 *Cómo usar:*",
        f"1️⃣ Abre el par en TradingView 3min",
        f"2️⃣ Verifica QF×JP: DECAIMIENTO=VIVA",
        f"3️⃣ Espera señal SUPREMA o SUP V3",
        f"4️⃣ SL = swing low del dashboard",
    ]

    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    """Envía mensaje a Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado — imprimiendo en consola")
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
        log.error(f"Telegram error: {e}")
        return False


# ─── MODO CONTINUO (Railway) ─────────────────────────────────

def run_loop():
    """Ejecuta el scanner en loop continuo."""
    log.info("Scanner iniciado en modo continuo")

    while True:
        try:
            hora_utc = datetime.now(timezone.utc).hour

            # Solo scannear en sesiones relevantes (EU + NY + pre-apertura)
            # 06:00-22:00 UTC
            if 6 <= hora_utc < 22:
                btc_change  = get_btc_change()
                candidatos  = scan_mercado()
                mensaje     = build_message(candidatos, btc_change)
                send_telegram(mensaje)
            else:
                log.info(f"Fuera de ventana horaria ({hora_utc}h UTC) — esperando")

        except Exception as e:
            log.error(f"Error en ciclo principal: {e}")

        log.info(f"Próximo scan en {SCAN_INTERVAL_SEC//60} minutos")
        time.sleep(SCAN_INTERVAL_SEC)


# ─── ENTRY POINT ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        run_loop()
    else:
        # Ejecución única
        btc_change = get_btc_change()
        candidatos = scan_mercado()
        mensaje    = build_message(candidatos, btc_change)
        send_telegram(mensaje)
