"""
╔══════════════════════════════════════════════════════════════════════╗
║         CRYPTO SCANNER v5.0 — MOTOR QF×JP APEX EDGE               ║
║                                                                      ║
║  NUEVAS FEATURES v5.0:                                               ║
║  ✅ SL dinámico por ATR (SL_ATR_MULT × ATR)                        ║
║  ✅ TP por Risk/Reward (TP_RR × SL_dist)                           ║
║  ✅ TP1 parcial a TP1_RR con cierre TP1_QTY_PCT%                   ║
║  ✅ RR mínimo exigido (RR_MIN) antes de abrir                       ║
║  ✅ Filtro horario UTC (HORA_INICIO_UTC – HORA_FIN_UTC)             ║
║  ✅ Filtro BTC tendencia (BTC_LONG_FILTER / BTC_SHORT_FILTER)       ║
║  ✅ Filtro RSI (RSI_MIN_LONG / RSI_MAX_SHORT)                       ║
║  ✅ Filtro volumen por barra (VOL_BAR_MIN_X × media)                ║
║  ✅ Score mínimos más exigentes: STD≥65 FUEL≥75 SUP≥82             ║
║  ✅ Safety net: cierre forzado si precio cruza SL                   ║
║  ✅ Cache info instrumento + limpiezas automáticas                  ║
║  ✅ Resumen horario ampliado con equity y RR medio                  ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, sys, time, hmac, hashlib, logging, math, threading, urllib.parse, csv, json
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
import requests
import numpy as np

# ══════════════════════════════════════════════════════════════════════
# SERVIDOR DE SALUD
# ══════════════════════════════════════════════════════════════════════
_PUERTO = int(os.environ.get("PORT", "8080"))

_estado = {
    "escaneos": 0, "señales": 0, "trades": 0,
    "wins": 0, "losses": 0, "ultimo": "iniciando",
    "balance": 0.0, "pnl_dia": 0.0, "version": "5.0",
    "circuit_breaker": False, "modo": "iniciando",
    "rr_medio": 0.0, "rr_acum": 0.0, "rr_count": 0,
}

class _ServidorSalud(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json":
            body = json.dumps(_estado, ensure_ascii=False).encode()
            ct = "application/json"
        else:
            total = _estado["wins"] + _estado["losses"]
            wr = f"{round(_estado['wins']/total*100)}%" if total > 0 else "-"
            cuerpo = (
                f"OK v{_estado['version']} modo={_estado['modo']} "
                f"escaneos={_estado['escaneos']} señales={_estado['señales']} "
                f"trades={_estado['trades']} W/L={_estado['wins']}/{_estado['losses']} "
                f"WR={wr} RR_medio={_estado['rr_medio']:.2f} "
                f"balance=${_estado['balance']:.2f} "
                f"pnl_dia=${_estado['pnl_dia']:.2f} "
                f"cb={'SI' if _estado['circuit_breaker'] else 'no'} "
                f"ultimo={_estado['ultimo']}"
            )
            body = cuerpo.encode()
            ct = "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

_http_listo = threading.Event()

def _iniciar_http():
    try:
        srv = HTTPServer(("0.0.0.0", _PUERTO), _ServidorSalud)
        _http_listo.set()
        srv.serve_forever()
    except Exception as e:
        print(f"[salud] ERROR: {e}", flush=True)
        _http_listo.set()

threading.Thread(target=_iniciar_http, daemon=True, name="http").start()
_http_listo.wait(timeout=5)
print(f"[salud] HTTP listo en 0.0.0.0:{_PUERTO}", flush=True)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════
def _env_int(key, default):
    return int(os.getenv(key, str(default)).strip().strip('"').split()[0])

def _env_float(key, default):
    return float(os.getenv(key, str(default)).strip().strip('"').split()[0])

def _env_bool(key, default):
    v = os.getenv(key, str(default)).strip().strip('"').lower()
    return v in ("true", "1", "yes")

BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = (os.getenv("BINGX_API_SECRET", "") or os.getenv("BINGX_SECRET", ""))
TELEGRAM_TOKEN   = (os.getenv("TELEGRAM_TOKEN", "") or os.getenv("TG_TOKEN", ""))
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TG_CHAT_ID", ""))

# ── Capital y apalancamiento ───────────────────────────────────────
TRADE_USDT      = _env_float("TRADE_USDT",      5)
RIESGO_PCT_BAL  = _env_float("RIESGO_PCT_BAL",  0)
LEVERAGE        = _env_int("LEVERAGE",           5)

# ── SL/TP dinámicos por ATR + RR ──────────────────────────────────
SL_PCT          = _env_float("SL_PCT",          2.0)    # fallback fijo
SL_ATR_MULT     = _env_float("SL_ATR_MULT",     1.5)    # SL = entrada ± ATR × mult
TP_RR           = _env_float("TP_RR",           2.5)    # TP = entrada ± SL_dist × RR
TP1_RR          = _env_float("TP1_RR",          1.2)    # TP1 parcial a 1.2× SL_dist
TP1_QTY_PCT     = _env_float("TP1_QTY_PCT",     50)     # % qty a cerrar en TP1
RR_MIN          = _env_float("RR_MIN",          2.0)    # mínimo RR para abrir trade
TRAILING_PCT    = _env_float("TRAILING_PCT",     0)
MAX_TRADES      = _env_int("MAX_OPEN_TRADES",    3)

# ── Modos ─────────────────────────────────────────────────────────
_auto_env  = os.getenv("AUTO_TRADE", "").lower()
AUTO_TRADE = (_auto_env == "true") or (
    _auto_env == "" and bool(BINGX_API_KEY) and bool(BINGX_API_SECRET))
DRY_RUN    = _env_bool("DRY_RUN", False)

# ── Filtros de calidad de señal ───────────────────────────────────
SC_MIN_STD  = _env_int("SC_MIN_STD",  65)
SC_MIN_FUEL = _env_int("SC_MIN_FUEL", 75)
SC_MIN_SUP  = _env_int("SC_MIN_SUP",  82)
CONV_MIN    = _env_int("CONV_MIN",    6)
ADX_MIN     = _env_float("ADX_MIN",  22)

# ── Filtros de mercado ────────────────────────────────────────────
BTC_LONG_FILTER  = _env_float("BTC_LONG_FILTER",  -2.0)  # % BTC 24h mín para LONG
BTC_SHORT_FILTER = _env_float("BTC_SHORT_FILTER",  2.0)  # % BTC 24h máx para SHORT
HORA_INICIO_UTC  = _env_int("HORA_INICIO_UTC",     7)
HORA_FIN_UTC     = _env_int("HORA_FIN_UTC",       21)
RSI_MIN_LONG     = _env_int("RSI_MIN_LONG",       52)
RSI_MAX_SHORT    = _env_int("RSI_MAX_SHORT",       48)
VOL_BAR_MIN_X    = _env_float("VOL_BAR_MIN_X",    1.5)   # volumen barra ≥ X × media

# ── Circuit breaker y cooldown ────────────────────────────────────
CB_MAX_LOSSES   = _env_int("CB_MAX_LOSSES",   3)
CB_PAUSA_MIN    = _env_int("CB_PAUSE_MIN",    30)
COOLDOWN_LOSS_M = _env_int("COOLDOWN_LOSS_MIN", 60)

# ── Universo de pares ─────────────────────────────────────────────
BLACKLIST_RAW = os.getenv(
    "BLACKLIST",
    "ANIME-USDT,WCT-USDT,TAO-USDT,AAPLX-USDT,NCSKGOOGL2USD-USDT,VINE-USDT,"
    "NCSKRCL2USD-USDT,NCSKTSLA2USD-USDT,NCSKAMZN2USD-USDT,NCSKNVDA2USD-USDT,"
    "NCSKMSFT2USD-USDT,NCSKAAPL2USD-USDT,NCSKSPY2USD-USDT"
)
BLACKLIST = set(s.strip().upper() for s in BLACKLIST_RAW.split(",") if s.strip())

_PATRONES_EXCLUIR = (
    "USDC", "BUSD", "TUSD", "DAI", "FDUSD",
    "NCSK", "2USD", "2GBP", "2EUR", "2JPY", "2AUD", "2CAD",
    "NCFX", "AAPLX", "TESLAX", "GOOGLX", "AMZNX",
    "PAXG", "XAUT", "BVOL", "DVOL",
)
VOL_MIN_USDT    = _env_float("MIN_VOLUME_USDT", 5_000_000)
TOP_N           = _env_int("TOP_N", 10)
INT_NORMAL      = _env_int("INTERVAL_NORMAL", 900)
INT_ACTIVO      = _env_int("INTERVAL_ACTIVO", 300)
INT_ALERTA      = _env_int("INTERVAL_ALERTA",  60)
ALERTA_COOLDOWN = _env_int("ALERTA_COOLDOWN_SEG", 1800)

URL_BASE          = "https://open-api.bingx.com"
TRADES_STATE_FILE = os.getenv("TRADES_STATE_FILE", "trades_state.json")

# ══════════════════════════════════════════════════════════════════════
# PARÁMETROS MOTOR QF×JP
# ══════════════════════════════════════════════════════════════════════
I_MOM=20;I_REV=8;I_VOL_L=14;I_ATR_L=10;I_SMO=3
I_W1=0.40;I_W2=0.30;I_W3=0.30
I_ADX_LEN=14;I_ADX_TH=25
I_DLEN=40;I_DTHR=0.35;I_DECAY_PCT=30
I_DPM=2.5;I_DPB=20;I_BPT=0.18;I_ASL=10;I_ARR=1.20;I_ABR=1.20
I_TLB=30;I_TLL=5;I_TLR=3;I_TLM=0.15
I_PLL=5;I_PLR=3;I_PHL=5;I_PHR=3;I_HLC=2;I_HHC=2;I_HLW=40
I_FVG_MIN=0.3;I_FVG_BARS=40;I_OB_IMP=1.5;I_OB_BARS=50
I_CVD_LEN=20;I_CVD_DIV=5;I_CVD_ROLL=100
I_SQ_LEN=20;I_SQ_BBM=2.0;I_SQ_KCM=1.5
SC_W_SCORE=0.30;SC_W_CVD=0.25;SC_W_MOM=0.20;SC_W_DECAY=0.15;SC_W_HTF=0.10
VOL_ATR_THR=0.60
RSI_LEN=14

# ══════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════
trades_abiertos: dict  = {}
alertas_enviadas: dict = {}
cooldown_sym: dict     = {}
racha_perdidas: int    = 0
circuit_breaker_hasta: float = 0.0
pnl_acumulado_dia: float = 0.0
trades_historico: list = []
archivo_csv = "trades_log.csv"
_info_cache: dict = {}
_simbolos_configurados: set = set()

# ══════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ScannerV50")

# ══════════════════════════════════════════════════════════════════════
# PERSISTENCIA
# ══════════════════════════════════════════════════════════════════════
def guardar_estado_trades():
    try:
        with open(TRADES_STATE_FILE, "w") as f:
            json.dump(trades_abiertos, f, indent=2)
    except Exception as e:
        log.warning(f"No se pudo guardar estado: {e}")

def cargar_estado_trades():
    global trades_abiertos
    if not os.path.exists(TRADES_STATE_FILE):
        return
    try:
        with open(TRADES_STATE_FILE) as f:
            datos = json.load(f)
        if isinstance(datos, dict) and datos:
            trades_abiertos = datos
            log.info(f"♻️  Estado recuperado: {len(trades_abiertos)} trades")
            for sym, t in trades_abiertos.items():
                log.info(f"   → {sym} {t.get('direccion','?')} @ {t.get('entrada','?')}")
    except Exception as e:
        log.warning(f"No se pudo cargar estado: {e}")

# ══════════════════════════════════════════════════════════════════════
# FIRMA HMAC
# ══════════════════════════════════════════════════════════════════════
def _firmar(params: dict) -> str:
    query = urllib.parse.urlencode(params)
    return hmac.new(
        BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()

# ══════════════════════════════════════════════════════════════════════
# LLAMADAS API
# ══════════════════════════════════════════════════════════════════════
def _get(ruta: str, params: dict = None, auth: bool = False) -> Optional[dict]:
    p = dict(params or {})
    headers = {}
    if auth:
        p["timestamp"]  = int(time.time() * 1000)
        p["recvWindow"] = 5000
        p["signature"]  = _firmar(p)
        headers["X-BX-APIKEY"] = BINGX_API_KEY
        url = URL_BASE + ruta + "?" + urllib.parse.urlencode(p)
        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            log.warning(f"GET {ruta}: timeout")
        except requests.exceptions.HTTPError as e:
            log.warning(f"GET {ruta}: HTTP {e.response.status_code}")
        except Exception as e:
            log.warning(f"GET {ruta}: {e}")
        return None
    try:
        r = requests.get(URL_BASE + ruta, params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {ruta}: {e}")
        return None

def _esperar_rate_limit(msg: str, ruta: str) -> None:
    import re
    match = re.search(r'after\s+(\d{13})', msg)
    espera_s = max(1.0, (int(match.group(1)) - int(time.time()*1000) + 500) / 1000.0) if match else 30.0
    log.warning(f"⏱ Rate limit en {ruta} — esperando {espera_s:.1f}s")
    time.sleep(espera_s)

def _post(ruta: str, params: dict, reintentos: int = 3) -> Optional[dict]:
    for intento in range(reintentos):
        p = dict(params)
        p["timestamp"]  = int(time.time() * 1000)
        p["recvWindow"] = 5000
        p["signature"]  = _firmar(p)
        url = URL_BASE + ruta + "?" + urllib.parse.urlencode(p)
        headers = {"X-BX-APIKEY": BINGX_API_KEY}
        try:
            r = requests.post(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0:
                return data
            codigo = data.get("code"); msg = data.get("msg", "?")
            log.error(f"POST {ruta} ({intento+1}/{reintentos}) code={codigo} msg={msg}")
            if codigo in (100001, 100004, 100419):
                log.error("❌ Autenticación fallida"); return None
            if codigo == 100410:
                _esperar_rate_limit(msg, ruta); continue
        except requests.exceptions.Timeout:
            log.error(f"POST {ruta} ({intento+1}/{reintentos}): timeout")
        except Exception as e:
            log.error(f"POST {ruta} ({intento+1}/{reintentos}): {e}")
        if intento < reintentos - 1:
            time.sleep(1.0 * (2 ** intento))
    return None

def _delete(ruta: str, params: dict) -> Optional[dict]:
    p = dict(params)
    p["timestamp"]  = int(time.time() * 1000)
    p["recvWindow"] = 5000
    p["signature"]  = _firmar(p)
    url = URL_BASE + ruta + "?" + urllib.parse.urlencode(p)
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    try:
        r = requests.delete(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if data.get("code") == 0 else None
    except Exception as e:
        log.error(f"DELETE {ruta}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
# FUNCIONES DE MERCADO
# ══════════════════════════════════════════════════════════════════════
def get_tickers() -> list:
    d = _get("/openApi/swap/v2/quote/ticker")
    return d.get("data", []) if d else []

def get_klines(simbolo: str, intervalo: str = "3m", limite: int = 100) -> list:
    d = _get("/openApi/swap/v3/quote/klines",
             {"symbol": simbolo, "interval": intervalo, "limit": limite})
    raw = d.get("data", []) if d else []
    normalizado = []
    for k in raw:
        try:
            if isinstance(k, dict):
                normalizado.append([
                    k.get("time", 0),
                    float(k.get("open",   k.get("o", 0))),
                    float(k.get("high",   k.get("h", 0))),
                    float(k.get("low",    k.get("l", 0))),
                    float(k.get("close",  k.get("c", 0))),
                    float(k.get("volume", k.get("v", 0))),
                ])
            elif isinstance(k, (list, tuple)) and len(k) >= 6:
                normalizado.append([float(x) for x in k[:6]])
        except Exception:
            continue
    return normalizado

def get_precio_actual(simbolo: str) -> float:
    k = get_klines(simbolo, "3m", 3)
    return float(k[-1][4]) if k else 0.0

def get_posiciones_abiertas() -> list:
    posiciones = []
    d = _get("/openApi/swap/v2/trade/openPositions", auth=True)
    if d:
        data = d.get("data")
        if isinstance(data, list):
            posiciones = data
        elif isinstance(data, dict):
            posiciones = data.get("positions", [])
    result = []
    for p in posiciones:
        try:
            amt = float(p.get("positionAmt", p.get("availableAmt", 0)))
            if abs(amt) > 1e-9:
                result.append(p)
        except Exception:
            continue
    if not result:
        d2 = _get("/openApi/swap/v2/user/positions", auth=True)
        if d2:
            for p in d2.get("data", []) or []:
                try:
                    amt = float(p.get("positionAmt", p.get("availableAmt", 0)))
                    if abs(amt) > 1e-9:
                        result.append(p)
                except Exception:
                    continue
    return result

def get_ordenes_abiertas(simbolo: str) -> list:
    d = _get("/openApi/swap/v2/trade/openOrders", {"symbol": simbolo}, auth=True)
    if not d:
        return []
    data = d.get("data", {})
    if isinstance(data, dict):
        return data.get("orders", [])
    return data if isinstance(data, list) else []

def cancelar_ordenes(simbolo: str) -> bool:
    result = _delete("/openApi/swap/v2/trade/allOpenOrders", {"symbol": simbolo})
    if result:
        log.info(f"🗑 Órdenes canceladas: {simbolo}")
        return True
    log.warning(f"⚠️  No se pudo cancelar órdenes de {simbolo}")
    return False

def get_balance() -> float:
    d = _get("/openApi/swap/v2/user/balance", auth=True)
    if not d or (d.get("code") and d.get("code") != 0):
        return 0.0
    try:
        data = d.get("data", {})
        if isinstance(data, dict):
            bal = data.get("balance", {})
            if isinstance(bal, dict):
                for k in ("availableMargin", "available", "equity"):
                    if (v := bal.get(k)) is not None:
                        return float(v)
            for k in ("availableMargin", "available", "equity"):
                if (v := data.get(k)) is not None:
                    return float(v)
        if isinstance(data, list):
            for activo in data:
                if activo.get("asset", "").upper() in ("USDT", ""):
                    for k in ("availableMargin", "available", "equity"):
                        if (v := activo.get(k)) is not None:
                            return float(v)
    except Exception as e:
        log.error(f"get_balance(): {e}")
    return 0.0

def get_info_instrumento(simbolo: str) -> dict:
    if simbolo in _info_cache:
        return _info_cache[simbolo]
    try:
        d = _get("/openApi/swap/v2/quote/contracts")
        if d and d.get("data"):
            for c in d["data"]:
                if c.get("symbol") == simbolo:
                    info = {
                        "step_size":       float(c.get("tradeMinQuantity",   0.001)),
                        "min_qty":         float(c.get("tradeMinQuantity",   0.001)),
                        "price_precision": int(c.get("pricePrecision",       6)),
                    }
                    _info_cache[simbolo] = info
                    return info
    except Exception:
        pass
    default = {"step_size": 0.001, "min_qty": 0.001, "price_precision": 6}
    _info_cache[simbolo] = default
    return default

def redondear_qty(qty: float, step: float) -> float:
    if step <= 0:
        return round(qty, 4)
    decimales = max(0, -int(math.floor(math.log10(step))))
    return round(math.floor(qty / step) * step, decimales)

def calcular_usdt_trade(balance: float) -> float:
    if RIESGO_PCT_BAL > 0 and balance > 0:
        return max(1.0, round(balance * RIESGO_PCT_BAL / 100.0, 2))
    return TRADE_USDT

# ══════════════════════════════════════════════════════════════════════
# FILTROS DE MERCADO
# ══════════════════════════════════════════════════════════════════════
def en_horario_trading() -> bool:
    """Filtra operaciones fuera del horario UTC configurado."""
    hora = datetime.now(timezone.utc).hour
    return HORA_INICIO_UTC <= hora < HORA_FIN_UTC

def btc_permite_long(btc_cambio: float) -> bool:
    """BTC no debe estar cayendo demasiado para abrir LONG."""
    return btc_cambio >= BTC_LONG_FILTER

def btc_permite_short(btc_cambio: float) -> bool:
    """BTC no debe estar subiendo demasiado para abrir SHORT."""
    return btc_cambio <= BTC_SHORT_FILTER

# ══════════════════════════════════════════════════════════════════════
# SYNC INICIAL CON BINGX
# ══════════════════════════════════════════════════════════════════════
def sincronizar_posiciones_bingx():
    global trades_abiertos
    if not BINGX_API_KEY:
        return
    log.info("🔄 Sincronizando posiciones con BingX...")
    posiciones = get_posiciones_abiertas()
    syms_bingx = {p.get("symbol") for p in posiciones if p.get("symbol")}

    for sym in [s for s in list(trades_abiertos.keys()) if s not in syms_bingx]:
        t = trades_abiertos.pop(sym)
        log.warning(f"⚠️  {sym}: cerrado externamente")
        enviar_telegram(
            f"⚠️ *Posición cerrada externamente*: {sym.replace('-USDT','')}\n"
            f"Dir: {t.get('direccion','?')} | Entrada: `{t.get('entrada','?')}`"
        )

    for pos in posiciones:
        sym = pos.get("symbol", "")
        if not sym or sym in trades_abiertos:
            continue
        try:
            amt      = float(pos.get("positionAmt", 0))
            entry    = float(pos.get("avgPrice", pos.get("entryPrice", 0)))
            ps       = pos.get("positionSide", "BOTH")
            dir_     = ("LONG" if amt > 0 else "SHORT") if ps in ("BOTH", "") else \
                       ("LONG" if ps == "LONG" else "SHORT")
            sl_dist  = entry * SL_PCT / 100
            trades_abiertos[sym] = {
                "simbolo": sym, "direccion": dir_, "entrada": entry,
                "sl":  round(entry - sl_dist if dir_=="LONG" else entry + sl_dist, 6),
                "tp":  round(entry + sl_dist*TP_RR if dir_=="LONG" else entry - sl_dist*TP_RR, 6),
                "tp1": round(entry + sl_dist*TP1_RR if dir_=="LONG" else entry - sl_dist*TP1_RR, 6),
                "tp_desc": "adoptado", "qty": abs(amt), "usdt": TRADE_USDT,
                "apalancamiento": LEVERAGE,
                "abierto_en": datetime.now(timezone.utc).isoformat(),
                "dry_run": False, "huerfano": True, "sl_aplicado": False,
                "tp1_activado": False, "rr": TP_RR,
            }
            log.warning(f"🔗 Adoptado: {sym} {dir_} @ {entry} qty={abs(amt)}")
        except Exception as e:
            log.warning(f"No se pudo adoptar {sym}: {e}")

    guardar_estado_trades()
    log.info(f"✅ Sync completo — {len(trades_abiertos)} trades activos")

# ══════════════════════════════════════════════════════════════════════
# VERIFICAR Y REAPLICAR SL/TP
# ══════════════════════════════════════════════════════════════════════
def verificar_sl_tp(simbolo: str, trade: dict):
    if trade.get("dry_run"):
        return

    qty = float(trade.get("qty", 0))
    if qty <= 0:
        for pos in get_posiciones_abiertas():
            if pos.get("symbol") == simbolo:
                qty = abs(float(pos.get("positionAmt", pos.get("availableAmt", 0))))
                trade["qty"] = qty
                break
    if qty <= 0:
        log.error(f"{simbolo}: qty=0 — skip verificar_sl_tp")
        return

    ordenes = get_ordenes_abiertas(simbolo)
    tipos_activos = {o.get("type", "").upper() for o in ordenes}

    es_long = trade["direccion"] == "LONG"
    lado_c  = "SELL" if es_long else "BUY"
    info    = get_info_instrumento(simbolo)
    qty_str = str(redondear_qty(qty, info["step_size"]))

    tiene_sl = any(t in tipos_activos for t in ("STOP_MARKET", "STOP"))
    tiene_tp = any(t in tipos_activos for t in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT",
                                                 "TRAILING_STOP_MARKET"))

    if not tiene_sl:
        sl_p = trade.get("sl")
        if sl_p:
            log.warning(f"⚠️  {simbolo}: SL faltante — reaplicando @ {sl_p}")
            _post("/openApi/swap/v2/trade/order",
                  {"symbol": simbolo, "side": lado_c, "type": "STOP_MARKET",
                   "stopPrice": str(sl_p), "quantity": qty_str})
            time.sleep(0.5)

    if not tiene_tp and not trade.get("tp1_activado"):
        tp_p = trade.get("tp")
        if tp_p and TRAILING_PCT <= 0:
            log.warning(f"⚠️  {simbolo}: TP faltante — reaplicando @ {tp_p}")
            _post("/openApi/swap/v2/trade/order",
                  {"symbol": simbolo, "side": lado_c, "type": "TAKE_PROFIT_MARKET",
                   "stopPrice": str(tp_p), "quantity": qty_str})
            time.sleep(0.5)
        elif TRAILING_PCT > 0:
            log.warning(f"⚠️  {simbolo}: Trailing TP faltante — reaplicando")
            _post("/openApi/swap/v2/trade/order",
                  {"symbol": simbolo, "side": lado_c, "type": "TRAILING_STOP_MARKET",
                   "callbackRate": str(round(TRAILING_PCT, 2)), "quantity": qty_str})
            time.sleep(0.5)

# ══════════════════════════════════════════════════════════════════════
# SAFETY NET — cierre forzado si precio cruza SL
# ══════════════════════════════════════════════════════════════════════
def safety_net_sl(simbolo: str, trade: dict, precio_actual: float):
    """Cierra a mercado si el precio cruza el SL (red de seguridad)."""
    if trade.get("dry_run"):
        return
    sl_p    = trade.get("sl", 0)
    es_long = trade["direccion"] == "LONG"
    cruza   = (es_long and precio_actual < sl_p) or (not es_long and precio_actual > sl_p)
    if not cruza:
        return
    qty  = float(trade.get("qty", 0))
    if qty <= 0:
        return
    info     = get_info_instrumento(simbolo)
    lado_c   = "SELL" if es_long else "BUY"
    qty_str  = str(redondear_qty(qty, info["step_size"]))
    log.warning(f"🛡 SAFETY NET {simbolo}: precio {precio_actual} cruzó SL {sl_p} — cerrando")
    cancelar_ordenes(simbolo)
    time.sleep(0.3)
    _post("/openApi/swap/v2/trade/order",
          {"symbol": simbolo, "side": lado_c, "type": "MARKET", "quantity": qty_str})
    enviar_telegram(
        f"🛡 *Safety Net activado*: {simbolo.replace('-USDT','')}\n"
        f"Precio `{precio_actual}` cruzó SL `{sl_p}` — cerrado a mercado"
    )

# ══════════════════════════════════════════════════════════════════════
# TP1 PARCIAL + BREAKEVEN — v5.0
# ══════════════════════════════════════════════════════════════════════
def ejecutar_tp1_parcial(simbolo: str, trade: dict, precio_actual: float):
    global pnl_acumulado_dia

    qty_total = float(trade.get("qty", 0))
    if qty_total <= 0:
        return

    info         = get_info_instrumento(simbolo)
    qty_cerrar   = redondear_qty(qty_total * TP1_QTY_PCT / 100, info["step_size"])
    qty_restante = redondear_qty(qty_total - qty_cerrar, info["step_size"])

    if qty_cerrar < info["min_qty"]:
        log.warning(f"{simbolo}: qty_cerrar={qty_cerrar} < minQty — skip TP1")
        return
    if qty_restante < info["min_qty"]:
        qty_cerrar   = qty_total
        qty_restante = 0.0

    es_long  = trade["direccion"] == "LONG"
    lado_c   = "SELL" if es_long else "BUY"
    entrada  = trade["entrada"]

    r = _post("/openApi/swap/v2/trade/order",
              {"symbol": simbolo, "side": lado_c,
               "type": "MARKET", "quantity": str(qty_cerrar)})
    if not r:
        log.error(f"{simbolo}: fallo al cerrar TP1 parcial")
        return

    pnl_pct  = (precio_actual - entrada) / entrada * 100 * (1 if es_long else -1)
    pnl_usdt = trade["usdt"] * LEVERAGE * pnl_pct / 100 * TP1_QTY_PCT / 100
    pnl_acumulado_dia += pnl_usdt
    _estado["pnl_dia"] = round(pnl_acumulado_dia, 2)

    log.info(f"🎯 TP1 PARCIAL {simbolo}: {TP1_QTY_PCT}% @ {precio_actual} | "
             f"PnL {pnl_pct:+.2f}% ~${pnl_usdt:+.2f}")

    time.sleep(0.8)
    cancelar_ordenes(simbolo)
    time.sleep(0.5)

    trade["tp1_activado"] = True

    if qty_restante >= info["min_qty"]:
        # SL → breakeven (entrada)
        _post("/openApi/swap/v2/trade/order",
              {"symbol": simbolo, "side": lado_c,
               "type": "STOP_MARKET",
               "stopPrice": str(entrada),
               "quantity": str(qty_restante)})
        # Si no hay trailing, colocar TP2 (TP_RR completo)
        if TRAILING_PCT <= 0:
            tp2_p = trade.get("tp")
            if tp2_p:
                _post("/openApi/swap/v2/trade/order",
                      {"symbol": simbolo, "side": lado_c,
                       "type": "TAKE_PROFIT_MARKET",
                       "stopPrice": str(tp2_p),
                       "quantity": str(qty_restante)})
        else:
            _post("/openApi/swap/v2/trade/order",
                  {"symbol": simbolo, "side": lado_c,
                   "type": "TRAILING_STOP_MARKET",
                   "callbackRate": str(round(TRAILING_PCT, 2)),
                   "quantity": str(qty_restante)})
        trade["qty"] = qty_restante
        trade["sl"]  = entrada
        log.info(f"🔒 {simbolo}: SL→breakeven @ {entrada}, qty restante={qty_restante}")
    else:
        trade["qty"] = 0.0

    guardar_estado_trades()
    enviar_telegram(
        f"🎯 *TP1 PARCIAL*: {simbolo.replace('-USDT','')}\n"
        f"Cerrado `{TP1_QTY_PCT}%` @ `{precio_actual}`\n"
        f"PnL parcial: `{pnl_pct:+.2f}%` (~`${pnl_usdt:+.2f}`)\n"
        f"{'SL → Breakeven @ `' + str(entrada) + '` | TP2 libre' if qty_restante >= info['min_qty'] else 'Posición cerrada'}"
    )

# ══════════════════════════════════════════════════════════════════════
# INDICADORES TÉCNICOS
# ══════════════════════════════════════════════════════════════════════
def f_tanh(x):
    x2 = max(min(2.0 * x, 20.0), -20.0); e = math.exp(x2)
    return (e - 1.0) / (e + 1.0)

def ema(arr, p):
    k = 2.0 / (p + 1); r = np.empty(len(arr)); r[0] = arr[0]
    for i in range(1, len(arr)):
        r[i] = arr[i] * k + r[i-1] * (1-k)
    return r

def sma(arr, p):
    out = np.full(len(arr), np.nan)
    for i in range(p-1, len(arr)):
        out[i] = arr[i-p+1:i+1].mean()
    return out

def stdev(arr, p):
    out = np.full(len(arr), np.nan)
    for i in range(p-1, len(arr)):
        out[i] = arr[i-p+1:i+1].std(ddof=0)
    return out

def atr_series(h, l, c, p):
    tr = np.empty(len(c)); tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return ema(tr, p)

def adx_series(h, l, c, p):
    n = len(c); pdm = np.zeros(n); mdm = np.zeros(n); tr = np.zeros(n)
    for i in range(1, n):
        hd = h[i]-h[i-1]; ld = l[i-1]-l[i]
        pdm[i] = hd if hd > ld and hd > 0 else 0
        mdm[i] = ld if ld > hd and ld > 0 else 0
        tr[i]  = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    ae  = ema(tr, p)
    pdi = 100 * ema(pdm, p) / np.maximum(ae, 1e-10)
    mdi = 100 * ema(mdm, p) / np.maximum(ae, 1e-10)
    dx  = 100 * np.abs(pdi-mdi) / np.maximum(pdi+mdi, 1e-10)
    return pdi, mdi, ema(dx, p)

def rsi_series(c, p=14):
    """RSI clásico."""
    n = len(c)
    gains = np.zeros(n); losses = np.zeros(n)
    for i in range(1, n):
        d = c[i] - c[i-1]
        if d > 0: gains[i] = d
        else:     losses[i] = -d
    avg_g = ema(gains, p); avg_l = ema(losses, p)
    rs = avg_g / np.maximum(avg_l, 1e-10)
    return 100 - 100 / (1 + rs)

def obv_series(c, v):
    obv = np.zeros(len(c))
    for i in range(1, len(c)):
        if   c[i] > c[i-1]: obv[i] = obv[i-1] + v[i]
        elif c[i] < c[i-1]: obv[i] = obv[i-1] - v[i]
        else:                obv[i] = obv[i-1]
    return obv

def pivot_high(h, left, right):
    n = len(h); ph = np.full(n, np.nan)
    for i in range(left, n-right):
        w = h[i-left:i+right+1]
        if h[i] == w.max() and (w < h[i]).any():
            ph[i] = h[i]
    return ph

def pivot_low(l, left, right):
    n = len(l); pl = np.full(n, np.nan)
    for i in range(left, n-right):
        w = l[i-left:i+right+1]
        if l[i] == w.min() and (w > l[i]).any():
            pl[i] = l[i]
    return pl

def linreg(arr, longitud):
    if len(arr) < longitud:
        return float(arr[-1])
    y = arr[-longitud:]; x = np.arange(longitud)
    m, b = np.polyfit(x, y, 1)
    return m * (longitud-1) + b

# ══════════════════════════════════════════════════════════════════════
# MOTOR QF×JP v5.0
# ══════════════════════════════════════════════════════════════════════
def analizar_par(klines_3m: list, klines_15m: list) -> Optional[dict]:
    if len(klines_3m) < 60:
        return None

    def _col(kl, idx):
        out = []
        for k in kl:
            try:    out.append(float(k[idx]))
            except: out.append(out[-1] if out else 0.0)
        return np.array(out)

    o=_col(klines_3m,1); h=_col(klines_3m,2); l=_col(klines_3m,3)
    c=_col(klines_3m,4); v=_col(klines_3m,5); n=len(c)

    # ── ATR ──────────────────────────────────────────────────────────
    atr_arr   = atr_series(h, l, c, I_ATR_L)
    atr_ahora = float(atr_arr[-1])
    atr_avg20 = float(sma(atr_arr, 20)[-1] or atr_ahora)
    vol_ok    = atr_ahora > atr_avg20 * VOL_ATR_THR
    vol_pct   = round(atr_ahora / atr_avg20 * 100) if atr_avg20 > 0 else 100

    # ── Filtro volumen por barra (v5.0) ──────────────────────────────
    vol_media   = float(sma(v, 20)[-1]) if n >= 20 else float(v.mean())
    vol_bar_ok  = float(v[-1]) >= vol_media * VOL_BAR_MIN_X

    # ── Exec drain ───────────────────────────────────────────────────
    hi_lo      = np.log(np.maximum(h/l, 1e-10))
    bp_drain   = (sma(hi_lo, 5) * c / np.maximum(c, 1e-10)) * 100
    exec_ok    = bool(bp_drain[-1] < I_BPT)

    # ── ADX / tendencia ───────────────────────────────────────────────
    pdi, mdi, adx_v = adx_series(h, l, c, I_ADX_LEN)
    adx_ahora    = float(adx_v[-1])
    trend_fuerte = adx_ahora >= I_ADX_TH
    trend_up     = bool(pdi[-1] > mdi[-1] and trend_fuerte)
    trend_dn     = bool(mdi[-1] > pdi[-1] and trend_fuerte)
    adx_ok       = adx_ahora >= ADX_MIN

    # ── RSI (v5.0) ───────────────────────────────────────────────────
    rsi_arr   = rsi_series(c, RSI_LEN)
    rsi_ahora = float(rsi_arr[-1])
    rsi_long_ok  = rsi_ahora >= RSI_MIN_LONG
    rsi_short_ok = rsi_ahora <= RSI_MAX_SHORT

    # ── Score compuesto ───────────────────────────────────────────────
    sma_mom = float(sma(c, I_MOM)[-1]); std_mom = float(stdev(c, I_MOM)[-1])
    voln    = std_mom / sma_mom if sma_mom else 1e-10
    f_mom_v = ((c[-1]-c[-I_MOM])/c[-I_MOM])/voln if (voln and c[-I_MOM]) else 0.0

    bsma=sma(c,I_REV); bstd=stdev(c,I_REV)
    f_rev_v = -(c[-1]-bsma[-1])/bstd[-1] if bstd[-1] else 0.0

    obv_a=obv_series(c,v); oe=ema(obv_a,I_VOL_L); os_=stdev(obv_a,I_VOL_L)
    f_vol_v = (obv_a[-1]-oe[-1])/os_[-1] if os_[-1] else 0.0

    adx_f  = min(1.0, adx_ahora/(I_ADX_TH*2.0))
    w_mom  = I_W1 + adx_f*I_W1*0.40
    w_rev  = max(I_W2*0.30, I_W2 - adx_f*I_W2*0.50)
    w_tot  = w_mom + w_rev + I_W3
    raw_v  = (w_mom*f_mom_v + w_rev*f_rev_v + I_W3*f_vol_v) / max(w_tot, 1e-10)

    sc_std     = float(stdev(np.array([raw_v]*n), I_DLEN)[-1]) or 1e-10
    norm_score = f_tanh(raw_v / sc_std)

    ic_num = 0.3
    window = min(I_DLEN, n-5)
    if window >= 8:
        try:
            roc_s = np.array([(c[i]-c[max(0,i-I_MOM)])/max(c[max(0,i-I_MOM)],1e-10) for i in range(n)])
            fwd   = np.diff(c) / np.maximum(c[:-1], 1e-10)
            seg_s = roc_s[max(0,n-window-1):n-1]
            seg_f = fwd[max(0,n-window-1):n-1]
            if len(seg_s) > 4 and seg_s.std() > 1e-10 and seg_f.std() > 1e-10:
                ic_raw = float(np.corrcoef(seg_s, seg_f)[0,1])
                ic_num = 0.0 if np.isnan(ic_raw) else abs(ic_raw)
        except Exception:
            ic_num = 0.3

    decay_r   = min(1.0, ic_num / max(ic_num, 0.01))
    sig_alive = decay_r >= I_DTHR or ic_num >= 0.15

    vb=float(sma(v,I_DPB)[-1]); vs=bool(v[-1]>vb*I_DPM)
    rn=bool((h[-1]-l[-1]) < atr_ahora*0.6)
    dp_buy=bool(vs and rn and c[-1]>o[-1]); dp_sell=bool(vs and rn and c[-1]<o[-1])

    if klines_15m and len(klines_15m) >= 22:
        c15=_col(klines_15m,4)
        htf_bull=float(ema(c15,9)[-1])>float(ema(c15,21)[-1])
        htf_bear=float(ema(c15,9)[-1])<float(ema(c15,21)[-1])
    else:
        htf_bull=norm_score>0; htf_bear=norm_score<0

    ur=np.where(c>o,h-l,0.0); dr=np.where(c<o,h-l,0.0)
    aur=float(sma(ur,I_ASL)[-1]); adr=float(sma(dr,I_ASL)[-1])
    asim_bull=(aur/adr if adr>0 else 1.0)>=I_ARR
    asim_bear=(adr/aur if aur>0 else 1.0)>=I_ABR

    ph_arr=pivot_high(h,I_TLL,I_TLR); pl_arr=pivot_low(l,I_PLL,I_PLR)
    phv=[(i,v2) for i,v2 in enumerate(ph_arr) if not np.isnan(v2)]
    plv=[(i,v2) for i,v2 in enumerate(pl_arr) if not np.isnan(v2)]

    tl_break_long=tl_break_short=False
    if len(phv)>=2:
        (pb2,ph2),(pb1,ph1)=phv[-2],phv[-1]
        if ph2>ph1 and (n-1-pb2)<=I_TLB:
            sl2=(ph1-ph2)/max(pb1-pb2,1)
            if c[-1]>ph1+sl2*(n-1-pb1)+atr_ahora*I_TLM: tl_break_long=True
    if len(plv)>=2:
        (lb2,pl2),(lb1,pl1)=plv[-2],plv[-1]
        if pl2<pl1 and (n-1-lb2)<=I_TLB:
            sl2=(pl1-pl2)/max(lb1-lb2,1)
            if c[-1]<pl1+sl2*(n-1-lb1)-atr_ahora*I_TLM: tl_break_short=True

    win=min(I_HLW,n)
    plr=[(i,v2) for i,v2 in enumerate(pl_arr[-win:]) if not np.isnan(v2)]
    phr=[(i,v2) for i,v2 in enumerate(ph_arr[-win:]) if not np.isnan(v2)]
    hl_c=sum(1 for j in range(1,len(plr)) if plr[j][1]>plr[j-1][1])
    lh_c=sum(1 for j in range(1,len(phr)) if phr[j][1]<phr[j-1][1])
    venta_agotada=hl_c>=I_HLC; compra_agotada=lh_c>=I_HHC

    last_sl=float(plr[-1][1]) if plr else float(l[-10:].min())
    last_sh=float(phr[-1][1]) if phr else float(h[-10:].max())

    en_bull_fvg=en_bear_fvg=False
    for i in range(max(0,n-I_FVG_BARS),n-2):
        if l[i+2]>h[i] and (l[i+2]-h[i])>atr_ahora*I_FVG_MIN:
            if h[i]<=c[-1]<=l[i+2]: en_bull_fvg=True
        if h[i+2]<l[i] and (l[i]-h[i+2])>atr_ahora*I_FVG_MIN:
            if h[i+2]<=c[-1]<=l[i]: en_bear_fvg=True

    en_bull_ob=en_bear_ob=False
    for i in range(max(0,n-I_OB_BARS),n-1):
        if i>=1:
            if (c[i]-o[i])>atr_ahora*I_OB_IMP and c[i]>c[i-1] and c[i-1]<o[i-1]:
                if o[i-1]>=c[-1]>=c[i-1]: en_bull_ob=True
            if (o[i]-c[i])>atr_ahora*I_OB_IMP and c[i]<c[i-1] and c[i-1]>o[i-1]:
                if c[i-1]>=c[-1]>=o[i-1]: en_bear_ob=True

    hlr=h-l; hlr_safe=np.where(hlr>0,hlr,1.0)
    bv=np.where(hlr>0,(c-l)/hlr_safe*v,v*0.5)
    sv=np.where(hlr>0,(h-c)/hlr_safe*v,v*0.5)
    db=bv-sv; roll=min(I_CVD_ROLL,n)
    cvd=float(sma(db,roll)[-1])*roll; cvde=float(ema(db,I_CVD_LEN)[-1])
    cvd_rising=cvd>cvde
    cvds=float(stdev(db,min(I_CVD_LEN*2,n))[-1])
    cvdz=(cvd-cvde)/cvds if cvds else 0.0
    cvd_score_v=max(0.0,min(1.0,(f_tanh(cvdz)+1)/2))
    dw=min(I_CVD_DIV,n-1)
    cvd_prev=float(sma(db[:-dw],roll)[-1])*roll if n>dw+roll else cvd
    cvd_bull_div=bool(c[-1]<c[-dw-1] and cvd>cvd_prev)
    cvd_bear_div=bool(c[-1]>c[-dw-1] and cvd<cvd_prev)

    sb=float(sma(c,I_SQ_LEN)[-1]); sd=float(stdev(c,I_SQ_LEN)[-1])
    sk=float(atr_series(h,l,c,I_SQ_LEN)[-1]); se=float(ema(c,I_SQ_LEN)[-1])
    sq_on=(sb+I_SQ_BBM*sd)<(se+I_SQ_KCM*sk) and (sb-I_SQ_BBM*sd)>(se-I_SQ_KCM*sk)
    sq_fire=sq_bull=sq_bear=False
    if n>=I_SQ_LEN+2:
        sb_p=float(sma(c[:-1],I_SQ_LEN)[-1]); sd_p=float(stdev(c[:-1],I_SQ_LEN)[-1])
        sk_p=float(atr_series(h[:-1],l[:-1],c[:-1],I_SQ_LEN)[-1])
        se_p=float(ema(c[:-1],I_SQ_LEN)[-1])
        sq_on_p=(sb_p+I_SQ_BBM*sd_p)<(se_p+I_SQ_KCM*sk_p) and \
                (sb_p-I_SQ_BBM*sd_p)>(se_p-I_SQ_KCM*sk_p)
        sq_fire=not sq_on and sq_on_p
    if sq_fire:
        slr=linreg(c-(max(h[-I_SQ_LEN:])+min(l[-I_SQ_LEN:])+float(sma(c,I_SQ_LEN)[-1]))/3,I_SQ_LEN)
        sq_bull=slr>0; sq_bear=slr<0

    nsl=(f_tanh(norm_score)+1)/2; mml=(f_tanh(f_mom_v*2)+1)/2; dn=min(1.0,decay_r)
    hal=(0.5 if htf_bull else 0.0)+(0.5 if asim_bull else 0.0)
    has=(0.5 if htf_bear else 0.0)+(0.5 if asim_bear else 0.0)

    cl=round(min(100,(SC_W_SCORE*nsl+SC_W_CVD*cvd_score_v+SC_W_MOM*mml+SC_W_DECAY*dn+SC_W_HTF*hal)*100))
    nss=(f_tanh(-norm_score)+1)/2; mms=(f_tanh(-f_mom_v*2)+1)/2
    cs=round(min(100,(SC_W_SCORE*nss+SC_W_CVD*(1-cvd_score_v)+SC_W_MOM*mms+SC_W_DECAY*dn+SC_W_HTF*has)*100))

    lconv=sum([norm_score>0.10,sig_alive,exec_ok,htf_bull,asim_bull,
               venta_agotada,tl_break_long,dp_buy,cvd_rising,
               sq_bull or en_bull_fvg or en_bull_ob])
    sconv=sum([norm_score<-0.10,sig_alive,exec_ok,htf_bear,asim_bear,
               compra_agotada,tl_break_short,dp_sell,not cvd_rising,
               sq_bear or en_bear_fvg or en_bear_ob])

    comp_long  = min(100, cl + round(lconv*0.5))
    comp_short = min(100, cs + round(sconv*0.5))

    # v5.0: filtros RSI integrados en clasificación
    long_base  = (comp_long >=SC_MIN_STD and exec_ok and sig_alive and vol_ok
                  and adx_ok and lconv>=CONV_MIN and vol_bar_ok and rsi_long_ok)
    short_base = (comp_short>=SC_MIN_STD and exec_ok and sig_alive and vol_ok
                  and adx_ok and sconv>=CONV_MIN and vol_bar_ok and rsi_short_ok)

    long_std   = long_base  and htf_bull
    short_std  = short_base and htf_bear
    long_fuel  = long_std   and comp_long >=SC_MIN_FUEL and (tl_break_long  or sq_bull or cvd_rising    or en_bull_fvg or en_bull_ob)
    short_fuel = short_std  and comp_short>=SC_MIN_FUEL and (tl_break_short or sq_bear or not cvd_rising or en_bear_fvg or en_bear_ob)
    long_sup   = long_fuel  and comp_long >=SC_MIN_SUP  and (dp_buy  or cvd_bull_div or venta_agotada)
    short_sup  = short_fuel and comp_short>=SC_MIN_SUP  and (dp_sell or cvd_bear_div or compra_agotada)

    if   long_sup:   señal,ss="★ LONG SUP",  comp_long
    elif long_fuel:  señal,ss="▲ LONG FUEL", comp_long
    elif long_std:   señal,ss="▲ LONG STD",  comp_long
    elif short_sup:  señal,ss="★ SHORT SUP", comp_short
    elif short_fuel: señal,ss="▼ SHORT FUEL",comp_short
    elif short_std:  señal,ss="▼ SHORT STD", comp_short
    else:            señal,ss="ESPERAR",      max(comp_long,comp_short)

    return {
        "señal":señal,"score_señal":ss,
        "long_sup":long_sup,"long_fuel":long_fuel,"long_std":long_std,
        "short_sup":short_sup,"short_fuel":short_fuel,"short_std":short_std,
        "comp_long":comp_long,"comp_short":comp_short,
        "norm_score":round(norm_score*100),
        "long_conv":lconv,"short_conv":sconv,
        "sig_alive":sig_alive,"exec_ok":exec_ok,"vol_ok":vol_ok,"vol_pct":vol_pct,
        "vol_bar_ok":vol_bar_ok,"adx_ok":adx_ok,
        "rsi":round(rsi_ahora,1),"rsi_long_ok":rsi_long_ok,"rsi_short_ok":rsi_short_ok,
        "htf_bull":htf_bull,"htf_bear":htf_bear,
        "asim_bull":asim_bull,"asim_bear":asim_bear,
        "dp_buy":dp_buy,"dp_sell":dp_sell,
        "tl_break_long":tl_break_long,"tl_break_short":tl_break_short,
        "venta_agotada":venta_agotada,"compra_agotada":compra_agotada,
        "en_bull_fvg":en_bull_fvg,"en_bear_fvg":en_bear_fvg,
        "en_bull_ob":en_bull_ob,"en_bear_ob":en_bear_ob,
        "cvd_rising":cvd_rising,"cvd_bull_div":cvd_bull_div,"cvd_bear_div":cvd_bear_div,
        "sq_bull":sq_bull,"sq_bear":sq_bear,"sq_on":sq_on,
        "trend_up":trend_up,"trend_dn":trend_dn,"adx":round(adx_ahora,1),
        "last_sl":round(last_sl,6),"last_sh":round(last_sh,6),
        "decay_r":round(decay_r*100),"atr":atr_ahora,
    }

# ══════════════════════════════════════════════════════════════════════
# SCANNER PRINCIPAL
# ══════════════════════════════════════════════════════════════════════
def escanear_mercado():
    log.info("=== Escaneo QF×JP v5.0 ===")
    _estado["escaneos"] += 1

    tickers = get_tickers()
    btc_cambio = btc_precio = 0.0
    for t in tickers:
        if t.get("symbol") == "BTC-USDT":
            try: btc_cambio=float(t.get("priceChangePercent",0)); btc_precio=float(t.get("lastPrice",0))
            except: pass
            break
    log.info(f"BTC: ${btc_precio:,.0f} ({btc_cambio:+.1f}%) | Pares: {len(tickers)}")

    if not en_horario_trading():
        log.info(f"⏰ Fuera de horario trading ({HORA_INICIO_UTC}-{HORA_FIN_UTC} UTC)")
        return [], INT_NORMAL, btc_cambio

    resultados = []
    for ticker in tickers:
        sym = ticker.get("symbol","")
        if not sym.endswith("-USDT") or sym in BLACKLIST: continue
        if any(x in sym for x in _PATRONES_EXCLUIR): continue
        if time.time() < cooldown_sym.get(sym,0): continue
        try:
            vol24=float(ticker.get("quoteVolume",0))
            precio=float(ticker.get("lastPrice",0))
            chg24=float(ticker.get("priceChangePercent",0))
        except: continue
        if vol24 < VOL_MIN_USDT: continue

        k3m  = get_klines(sym,"3m",100)
        k15m = get_klines(sym,"15m",30)
        if not k3m or len(k3m) < 60: time.sleep(0.05); continue

        an = analizar_par(k3m, k15m)
        if not an: time.sleep(0.05); continue
        if an["señal"]=="ESPERAR" and an["comp_long"]<45 and an["comp_short"]<45:
            time.sleep(0.05); continue

        resultados.append({"simbolo":sym,"precio":precio,"cambio_24h":chg24,
                           "volumen_usdt":vol24,**an})
        time.sleep(0.08)

    orden={"★ LONG SUP":0,"★ SHORT SUP":1,"▲ LONG FUEL":2,"▼ SHORT FUEL":3,
           "▲ LONG STD":4,"▼ SHORT STD":5,"ESPERAR":6}
    resultados.sort(key=lambda x:(orden.get(x["señal"],9),-x["score_señal"]))

    señales=[r for r in resultados if r["señal"]!="ESPERAR"][:TOP_N]
    _estado["señales"]=len(señales)
    _estado["ultimo"]=datetime.now(timezone.utc).strftime("%H:%M")
    log.info(f"Con señal: {len(señales)} | Total analizados: {len(resultados)}")

    tiene_sup  = any(r["long_sup"]  or r["short_sup"]  for r in señales)
    tiene_fuel = any(r["long_fuel"] or r["short_fuel"] for r in señales)
    intervalo  = INT_ALERTA if tiene_sup else INT_ACTIVO if (tiene_fuel or señales) else INT_NORMAL
    return señales, intervalo, btc_cambio

# ══════════════════════════════════════════════════════════════════════
# AUTO-TRADE v5.0
# ══════════════════════════════════════════════════════════════════════
def configurar_apalancamiento(simbolo: str) -> bool:
    if simbolo in _simbolos_configurados: return True
    exito = False
    r = _post("/openApi/swap/v2/trade/leverage", {"symbol":simbolo,"leverage":str(LEVERAGE)})
    time.sleep(0.8)
    if r:
        exito = True
    else:
        rl=_post("/openApi/swap/v2/trade/leverage",{"symbol":simbolo,"side":"LONG","leverage":str(LEVERAGE)}); time.sleep(0.8)
        rs=_post("/openApi/swap/v2/trade/leverage",{"symbol":simbolo,"side":"SHORT","leverage":str(LEVERAGE)}); time.sleep(0.8)
        exito=bool(rl or rs)
    _post("/openApi/swap/v2/trade/marginType",{"symbol":simbolo,"marginType":"ISOLATED"}); time.sleep(0.5)
    _simbolos_configurados.add(simbolo)
    log.info(f"⚙️  Leverage {LEVERAGE}x ISOLATED: {simbolo} {'OK' if exito else 'no confirmado'}")
    return exito

def circuit_breaker_activo() -> bool:
    if time.time() < circuit_breaker_hasta:
        log.warning(f"⚡ Circuit breaker — faltan {int(circuit_breaker_hasta-time.time())}s")
        return True
    return False

def calcular_sl_tp_atr(precio: float, atr: float, es_long: bool, price_precision: int):
    """
    v5.0: SL dinámico por ATR, TP por RR múltiplo del SL.
    Devuelve (sl, tp1, tp, rr_real) redondeados.
    """
    sl_dist = atr * SL_ATR_MULT
    tp_dist = sl_dist * TP_RR
    tp1_dist= sl_dist * TP1_RR

    if es_long:
        sl  = round(precio - sl_dist, price_precision)
        tp  = round(precio + tp_dist, price_precision)
        tp1 = round(precio + tp1_dist, price_precision)
    else:
        sl  = round(precio + sl_dist, price_precision)
        tp  = round(precio - tp_dist, price_precision)
        tp1 = round(precio - tp1_dist, price_precision)

    rr_real = tp_dist / max(sl_dist, 1e-10)
    return sl, tp1, tp, round(rr_real, 2)

def abrir_trade(simbolo: str, precio: float, direccion: str,
                atr: float, balance_cache: float = -1.0) -> Optional[dict]:
    global racha_perdidas, circuit_breaker_hasta
    if not BINGX_API_KEY and not DRY_RUN: return None
    if simbolo in trades_abiertos or circuit_breaker_activo() or simbolo in BLACKLIST: return None
    if balance_cache == -2.0: return None

    n_pos = len(get_posiciones_abiertas()) if not DRY_RUN else len(trades_abiertos)
    if n_pos >= MAX_TRADES:
        log.warning(f"Max trades ({MAX_TRADES}) — skip {simbolo}"); return None

    balance = get_balance() if balance_cache < 0 else balance_cache
    if balance_cache < 0: _estado["balance"] = balance
    usdt_trade = calcular_usdt_trade(balance)
    if balance < usdt_trade:
        log.warning(f"Balance insuf. ${balance:.2f} < ${usdt_trade:.2f}"); return None

    es_long    = (direccion=="LONG")
    info       = get_info_instrumento(simbolo)
    sl_p, tp1_p, tp_p, rr_real = calcular_sl_tp_atr(precio, atr, es_long, info["price_precision"])

    # Verificar RR mínimo
    if rr_real < RR_MIN:
        log.info(f"RR {rr_real:.2f} < RR_MIN {RR_MIN} — skip {simbolo}")
        return None

    if DRY_RUN:
        trade = {"simbolo":simbolo,"direccion":direccion,"entrada":precio,
                 "usdt":usdt_trade,"sl":sl_p,"tp1":tp1_p,"tp":tp_p,
                 "tp_desc":f"RR{rr_real:.2f}","qty":0,
                 "apalancamiento":LEVERAGE,"rr":rr_real,
                 "abierto_en":datetime.now(timezone.utc).isoformat(),
                 "dry_run":True,"sl_aplicado":True,"tp1_activado":False}
        trades_abiertos[simbolo]=trade; guardar_estado_trades()
        _estado["trades"]=len(trades_abiertos)
        log.info(f"[DRY RUN] {direccion} {simbolo} @ {precio} SL={sl_p} TP={tp_p} RR={rr_real}")
        return trade

    configurar_apalancamiento(simbolo)
    qty     = redondear_qty((usdt_trade*LEVERAGE)/precio, info["step_size"])
    if qty < info["min_qty"]:
        log.warning(f"Qty {qty} < minQty {info['min_qty']} — skip {simbolo}"); return None

    lado_a  = "BUY" if es_long else "SELL"
    lado_c  = "SELL" if es_long else "BUY"
    qty_str = str(qty)

    # Entrada a mercado
    orden = _post("/openApi/swap/v2/trade/order",
                  {"symbol":simbolo,"side":lado_a,"type":"MARKET","quantity":qty_str})
    if not orden: log.error(f"Orden {direccion} {simbolo} fallida"); return None
    time.sleep(1.0)

    # SL
    r_sl = _post("/openApi/swap/v2/trade/order",
                 {"symbol":simbolo,"side":lado_c,"type":"STOP_MARKET",
                  "stopPrice":str(sl_p),"quantity":qty_str})
    time.sleep(0.8)

    # TP1 parcial
    r_tp1 = None
    if TP1_QTY_PCT > 0 and TP1_RR > 0:
        qty_tp1 = str(redondear_qty(qty * TP1_QTY_PCT / 100, info["step_size"]))
        r_tp1 = _post("/openApi/swap/v2/trade/order",
                      {"symbol":simbolo,"side":lado_c,"type":"TAKE_PROFIT_MARKET",
                       "stopPrice":str(tp1_p),"quantity":qty_tp1})
        time.sleep(0.8)

    # TP2 (el RR completo)
    if TRAILING_PCT > 0:
        r_tp = _post("/openApi/swap/v2/trade/order",
                     {"symbol":simbolo,"side":lado_c,"type":"TRAILING_STOP_MARKET",
                      "callbackRate":str(round(TRAILING_PCT,2)),"quantity":qty_str})
        tp_desc = f"Trail{TRAILING_PCT}% RR{rr_real:.2f}"
    else:
        r_tp = _post("/openApi/swap/v2/trade/order",
                     {"symbol":simbolo,"side":lado_c,"type":"TAKE_PROFIT_MARKET",
                      "stopPrice":str(tp_p),"quantity":qty_str})
        tp_desc = f"{tp_p} (RR{rr_real:.2f})"

    sl_ok=bool(r_sl); tp_ok=bool(r_tp)
    if not sl_ok: log.warning(f"⚠️  {simbolo}: SL no confirmado")
    if not tp_ok: log.warning(f"⚠️  {simbolo}: TP no confirmado")

    trade = {"simbolo":simbolo,"direccion":direccion,"entrada":precio,
             "sl":sl_p,"tp1":tp1_p,"tp":tp_p,"tp_desc":tp_desc,"qty":qty,
             "usdt":usdt_trade,"apalancamiento":LEVERAGE,"rr":rr_real,
             "abierto_en":datetime.now(timezone.utc).isoformat(),
             "dry_run":False,"sl_aplicado":sl_ok,"tp_aplicado":tp_ok,
             "tp1_activado":False}
    trades_abiertos[simbolo]=trade; guardar_estado_trades()
    _estado["trades"]=len(trades_abiertos)
    log.info(f"✅ TRADE {direccion} {simbolo} @ {precio} | SL={sl_p}({'OK' if sl_ok else 'FAIL'}) "
             f"TP1={tp1_p} TP={tp_p} RR={rr_real} Qty={qty}")
    return trade

# ══════════════════════════════════════════════════════════════════════
# ACTUALIZAR TRADES
# ══════════════════════════════════════════════════════════════════════
def actualizar_trades():
    global racha_perdidas, circuit_breaker_hasta, pnl_acumulado_dia

    if not trades_abiertos:
        return

    ahora_ts = time.time()
    expirados = [s for s, t in cooldown_sym.items() if ahora_ts >= t]
    for s in expirados:
        del cooldown_sym[s]

    try:
        posiciones_activas = set()
        if not DRY_RUN:
            posiciones = get_posiciones_abiertas()
            posiciones_activas = {p.get("symbol") for p in posiciones}

            for sym, trade in list(trades_abiertos.items()):
                if sym not in posiciones_activas:
                    continue

                precio_actual = get_precio_actual(sym)

                # Safety net
                if precio_actual:
                    safety_net_sl(sym, trade, precio_actual)

                # Verificar SL/TP
                verificar_sl_tp(sym, trade)

                # TP1 parcial
                if (TP1_QTY_PCT > 0 and not trade.get("tp1_activado")
                        and not trade.get("dry_run") and precio_actual):
                    tp1_p = trade.get("tp1")
                    if tp1_p:
                        es_long   = trade["direccion"] == "LONG"
                        tp1_hit   = (es_long and precio_actual >= tp1_p) or \
                                    (not es_long and precio_actual <= tp1_p)
                        if tp1_hit:
                            ejecutar_tp1_parcial(sym, trade, precio_actual)
        else:
            posiciones_activas = set(trades_abiertos.keys())

        cerrados = [s for s in list(trades_abiertos.keys())
                    if s not in posiciones_activas or trades_abiertos[s].get("qty", 1) <= 0]

        for sym in cerrados:
            trade = trades_abiertos.pop(sym)
            guardar_estado_trades()
            k = get_klines(sym,"3m",3)
            if k:
                pa      = float(k[-1][4])
                en      = trade["entrada"]
                es_long = trade["direccion"]=="LONG"
                pnl     = (pa-en)/en*100*(1 if es_long else -1)
                ganado  = pnl > 0

                if ganado:
                    _estado["wins"]+=1; racha_perdidas=0
                    resultado=f"✅ WIN +{pnl:.2f}%"
                else:
                    _estado["losses"]+=1; racha_perdidas+=1
                    resultado=f"❌ LOSS {pnl:.2f}%"
                    cooldown_sym[sym]=time.time()+COOLDOWN_LOSS_M*60
                    if racha_perdidas>=CB_MAX_LOSSES:
                        circuit_breaker_hasta=time.time()+CB_PAUSA_MIN*60
                        _estado["circuit_breaker"]=True
                        log.warning(f"⚡ Circuit breaker: {racha_perdidas} pérdidas → {CB_PAUSA_MIN}min")
                        enviar_telegram(f"⚡ *Circuit breaker*\n{racha_perdidas} pérdidas → pausa {CB_PAUSA_MIN}min")

                pnl_usdt=trade["usdt"]*LEVERAGE*pnl/100
                pnl_acumulado_dia+=pnl_usdt; _estado["pnl_dia"]=round(pnl_acumulado_dia,2)

                # Actualizar RR medio
                rr_trade = trade.get("rr", TP_RR)
                _estado["rr_count"]+=1
                _estado["rr_acum"]+=rr_trade
                _estado["rr_medio"]=round(_estado["rr_acum"]/_estado["rr_count"],2)

                _guardar_trade_csv(trade,pa,pnl,pnl_usdt,ganado)
                log.info(f"Trade cerrado: {sym} {trade['direccion']} | {resultado} | ${pnl_usdt:+.2f} RR={rr_trade}")
                enviar_telegram(
                    f"📊 *Trade cerrado*: {sym.replace('-USDT','')}\n"
                    f"Dir: {trade['direccion']} | Entrada: `{en}`\n"
                    f"Cierre: `{pa:.6f}`\n{resultado}\n"
                    f"PnL: `${pnl_usdt:+.2f}` | Día: `${pnl_acumulado_dia:+.2f}`\n"
                    f"RR real: `{rr_trade:.2f}` | TP1: `{'✓' if trade.get('tp1_activado') else '✗'}`"
                )

        if circuit_breaker_hasta and time.time()>=circuit_breaker_hasta:
            _estado["circuit_breaker"]=False

    except Exception as e:
        log.error(f"actualizar_trades(): {e}", exc_info=True)

    _estado["trades"]=len(trades_abiertos)

def _guardar_trade_csv(trade, precio_cierre, pnl_pct, pnl_usdt, ganado):
    existe=os.path.exists(archivo_csv)
    try:
        with open(archivo_csv,"a",newline="") as f:
            w=csv.writer(f)
            if not existe:
                w.writerow(["fecha_cierre","simbolo","direccion","entrada","cierre",
                            "sl","tp1","tp","qty","usdt","apalancamiento",
                            "pnl_pct","pnl_usdt","resultado","rr","dry_run",
                            "abierto_en","tp1_activado"])
            w.writerow([
                datetime.now(timezone.utc).isoformat(),
                trade["simbolo"],trade["direccion"],trade["entrada"],precio_cierre,
                trade["sl"],trade.get("tp1",""),trade["tp"],trade["qty"],
                trade["usdt"],trade["apalancamiento"],
                round(pnl_pct,4),round(pnl_usdt,4),"WIN" if ganado else "LOSS",
                trade.get("rr",TP_RR),trade.get("dry_run",False),
                trade["abierto_en"],trade.get("tp1_activado",False),
            ])
    except Exception as e:
        log.warning(f"CSV log error: {e}")

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════
def enviar_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg); return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"},
                        timeout=10)
        r.raise_for_status(); return True
    except Exception as e:
        log.error(f"Telegram error: {e}"); return False

def construir_alerta(par: dict) -> str:
    sym=par["simbolo"].replace("-USDT",""); sig=par["señal"]
    scl=par["comp_long"]; scs=par["comp_short"]; p=par["precio"]
    atr=par.get("atr", p*0.02)
    es_long="LONG" in sig; es_sup="SUP" in sig; es_fuel="FUEL" in sig
    es_long_dir=es_long

    info_cache = get_info_instrumento(par["simbolo"])
    sl_p, tp1_p, tp_p, rr_r = calcular_sl_tp_atr(p, atr, es_long_dir, info_cache["price_precision"])

    emoji="🔵" if es_sup else "🟡" if es_fuel else "🟢"
    dir_e="🟢" if es_long else "🔴"
    modo_tag=" [DRY]" if DRY_RUN else ""
    db=max(0,min(8,round(par["decay_r"]/100*8)))
    barra="█"*db+"░"*(8-db)
    trailing=f"  Trailing SL: `{TRAILING_PCT}%`\n" if TRAILING_PCT>0 else ""
    rsi_tag=f" RSI:`{par['rsi']}`"

    lineas=[
        f"{emoji} *{sig}: {sym}*{modo_tag}",
        f"{'─'*30}",
        f"{dir_e} SC LONG: `{scl}/100` | SC SHORT: `{scs}/100`",
        f"📊 SCORE: `{par['norm_score']}` | CONV: `{par['long_conv']}▲/{par['short_conv']}▼`",
        f"{'─'*30}",
        f"💲 `{p}` | 24h: {par['cambio_24h']:+.1f}% | Vol: ${par['volumen_usdt']/1e6:.1f}M{rsi_tag}",
        f"📐 ATR: `{round(atr,6)}` | RR: `{rr_r:.2f}:1`",
        f"🛑 SL: `{sl_p}` (ATR×{SL_ATR_MULT})",
        f"🎯 TP1: `{tp1_p}` (RR{TP1_RR}) · TP2: `{tp_p}` (RR{TP_RR})",
        f"{trailing}{'─'*30}",
        f"*Dashboard QF×JP v5.0:*",
        f"  DECAY `{barra} {par['decay_r']}%` {'ok' if par['sig_alive'] else 'x'}",
        f"  HTF   `{'BULL' if par['htf_bull'] else 'BEAR' if par['htf_bear'] else '-'}` | "
        f"ADX `{par['adx']} {'↑' if par['trend_up'] else '↓' if par['trend_dn'] else '~'}` "
        f"{'ok' if par['adx_ok'] else 'bajo'}",
        f"  ASIM  `{'↑' if par['asim_bull'] else '↓' if par['asim_bear'] else '-'}` | "
        f"VOL ATR `{par['vol_pct']}%` {'ok' if par['vol_ok'] else 'x'} | "
        f"VBAR `{'ok' if par['vol_bar_ok'] else 'x'}`",
        f"  TL    `{'LONG' if par['tl_break_long'] else 'SHORT' if par['tl_break_short'] else '-'}`",
        f"  SWING `{'HL ↑' if par['venta_agotada'] else 'LH ↓' if par['compra_agotada'] else '-'}`",
        f"  DP    `{'↑' if par['dp_buy'] else '↓' if par['dp_sell'] else '-'}`",
        f"  FVG   `{'↑' if par['en_bull_fvg'] else '↓' if par['en_bear_fvg'] else '-'}` | "
        f"OB `{'↑' if par['en_bull_ob'] else '↓' if par['en_bear_ob'] else '-'}`",
        f"  CVD   `{'DIV ↑' if par['cvd_bull_div'] else 'DIV ↓' if par['cvd_bear_div'] else '↑' if par['cvd_rising'] else '↓'}`",
        f"  SQ    `{'fire ↑' if par['sq_bull'] else 'fire ↓' if par['sq_bear'] else 'compreso' if par['sq_on'] else '-'}`",
        f"  EXEC  `{'OK' if par['exec_ok'] else 'BLOQUEADO'}`",
        f"{'─'*30}",
        f"SL ref: `{par['last_sl'] if es_long else par['last_sh']}`",
    ]
    if AUTO_TRADE and (es_sup or es_fuel):
        estado_t="abierto" if par["simbolo"] in trades_abiertos else "pendiente"
        lineas.append(f"Auto-trade: {estado_t}{' (DRY)' if DRY_RUN else ''}")
    else:
        lineas.append("Verifica en TradingView 3m + QF×JP")
    return "\n".join(lineas)

def construir_resumen(resultados: list, btc_cambio: float, intervalo: int) -> str:
    ahora=datetime.now(timezone.utc).strftime("%H:%M UTC")
    signo="+" if btc_cambio>0 else ""
    wins=_estado["wins"]; losses=_estado["losses"]; total=wins+losses
    wr_str=f"{wins}/{total} ({round(wins/total*100)}%)" if total>0 else "-"
    rr_str=f"{_estado['rr_medio']:.2f}" if _estado["rr_count"]>0 else "-"
    cb_str=""
    if time.time()<circuit_breaker_hasta:
        cb_str=f"\n⚡ CB: {int((circuit_breaker_hasta-time.time())/60)}min restantes"
    modo_str=" [DRY]" if DRY_RUN else ""
    horario_str=f"[{HORA_INICIO_UTC}-{HORA_FIN_UTC}h UTC]"
    sup_l=[r for r in resultados if r["long_sup"]]
    sup_s=[r for r in resultados if r["short_sup"]]
    fuel_l=[r for r in resultados if r["long_fuel"] and not r["long_sup"]]
    fuel_s=[r for r in resultados if r["short_fuel"] and not r["short_sup"]]
    std_l=[r for r in resultados if r["long_std"] and not r["long_fuel"]]
    std_s=[r for r in resultados if r["short_std"] and not r["short_fuel"]]
    lineas=[
        f"QF×JP v5.0{modo_str} — {ahora} {horario_str}",
        f"BTC {signo}{btc_cambio:.2f}% | próximo scan {intervalo//60}min",
        f"W/L: {wr_str} | RR medio: {rr_str} | PnL día: ${pnl_acumulado_dia:+.2f}",
        f"Racha: {racha_perdidas}{cb_str}",
        f"{'─'*24}",
    ]
    if not resultados:
        lineas.append("Sin señales"); return "\n".join(lineas)
    for lst,etiqueta in [(sup_l,"LONG SUP"),(sup_s,"SHORT SUP"),
                         (fuel_l,"LONG FUEL"),(fuel_s,"SHORT FUEL")]:
        if lst:
            lineas.append(f"{etiqueta} ({len(lst)}):")
            for r in lst[:3]:
                sc=r["comp_long"] if "LONG" in etiqueta else r["comp_short"]
                lineas.append(f"  {r['simbolo'].replace('-USDT','')} {sc}/100 RSI:{r['rsi']}")
    if std_l or std_s:
        lineas.append(f"STD ({len(std_l)}L/{len(std_s)}S):")
        for r,d in [(r,"L") for r in std_l[:2]]+[(r,"S") for r in std_s[:2]]:
            sc=r["comp_long"] if d=="L" else r["comp_short"]
            lineas.append(f"  {d} {r['simbolo'].replace('-USDT','')} {sc}/100")
    if trades_abiertos:
        lineas+=[f"{'─'*24}",f"Trades ({len(trades_abiertos)}):"]
        for sym,t in trades_abiertos.items():
            tp1_tag=" [TP1✓]" if t.get("tp1_activado") else ""
            rr_t=f" RR{t.get('rr',TP_RR):.1f}" if t.get('rr') else ""
            lineas.append(f"  {sym.replace('-USDT','')} {t['direccion']} "
                          f"SL:{t['sl']} TP:{t.get('tp_desc',t['tp'])}{rr_t}{tp1_tag}")
    return "\n".join(lineas)

# ══════════════════════════════════════════════════════════════════════
# LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════════════════
def ejecutar():
    global pnl_acumulado_dia

    modo="DRY RUN" if DRY_RUN else ("AUTO TRADE ON" if AUTO_TRADE else "SOLO ALERTAS")
    _estado["modo"]=modo

    log.info(f"╔══════════════════════════════════════╗")
    log.info(f"║  QF×JP Scanner v5.0 — {modo:<13} ║")
    log.info(f"╚══════════════════════════════════════╝")
    log.info(f"  ${TRADE_USDT}×{LEVERAGE} | SL ATR×{SL_ATR_MULT} | TP RR{TP_RR} | Max {MAX_TRADES} trades")
    log.info(f"  TP1: RR{TP1_RR} ({TP1_QTY_PCT}%) | RR_MIN: {RR_MIN} | Trailing: {'ON '+str(TRAILING_PCT)+'%' if TRAILING_PCT>0 else 'OFF'}")
    log.info(f"  Horario: {HORA_INICIO_UTC}-{HORA_FIN_UTC}h UTC | BTC filtro: L>{BTC_LONG_FILTER}% S<{BTC_SHORT_FILTER}%")
    log.info(f"  RSI: LONG≥{RSI_MIN_LONG} SHORT≤{RSI_MAX_SHORT} | VolBar: ×{VOL_BAR_MIN_X}")
    log.info(f"  Scores: STD≥{SC_MIN_STD} FUEL≥{SC_MIN_FUEL} SUP≥{SC_MIN_SUP} CONV≥{CONV_MIN} ADX≥{ADX_MIN}")
    log.info(f"  CB: {CB_MAX_LOSSES} pérd→{CB_PAUSA_MIN}min | Cooldown: {COOLDOWN_LOSS_M}min")

    cargar_estado_trades()
    if BINGX_API_KEY:
        sincronizar_posiciones_bingx()
        bal=get_balance(); _estado["balance"]=bal
        log.info(f"  Balance: ${bal:.2f} USDT")

    enviar_telegram(
        f"🤖 *QF×JP Scanner v5.0 iniciado*\n"
        f"Modo: *{modo}*\n"
        f"SL: ATR×`{SL_ATR_MULT}` | TP: RR`{TP_RR}` | TP1: RR`{TP1_RR}` (`{TP1_QTY_PCT}%`)\n"
        f"RR mín: `{RR_MIN}` | Max trades: `{MAX_TRADES}`\n"
        f"Horario: `{HORA_INICIO_UTC}-{HORA_FIN_UTC}h UTC`\n"
        f"BTC filtro: `L>{BTC_LONG_FILTER}%` / `S<{BTC_SHORT_FILTER}%`\n"
        f"RSI: `LONG≥{RSI_MIN_LONG}` / `SHORT≤{RSI_MAX_SHORT}` | VolBar: `×{VOL_BAR_MIN_X}`\n"
        f"Scores: STD≥`{SC_MIN_STD}` FUEL≥`{SC_MIN_FUEL}` SUP≥`{SC_MIN_SUP}`\n"
        f"CB: `{CB_MAX_LOSSES}`pérd→`{CB_PAUSA_MIN}`min | Cooldown: `{COOLDOWN_LOSS_M}`min\n"
        f"Trades recuperados: `{len(trades_abiertos)}`"
    )

    ultima_hora=-1; ultimo_dia=-1; btc_cambio=0.0; intervalo=INT_NORMAL

    while True:
        ahora=datetime.now(timezone.utc)

        if ahora.day != ultimo_dia:
            if ultimo_dia != -1:
                wins=_estado["wins"]; losses=_estado["losses"]; total=wins+losses
                wr=f"{round(wins/total*100)}%" if total>0 else "-"
                enviar_telegram(
                    f"📅 *Resumen diario*\n"
                    f"PnL: `${pnl_acumulado_dia:+.2f}` USDT\n"
                    f"W/L: `{wins}/{losses}` WR: `{wr}`\n"
                    f"RR medio: `{_estado['rr_medio']:.2f}`"
                )
                pnl_acumulado_dia=0.0
                _estado["wins"]=_estado["losses"]=0
                _estado["rr_acum"]=0.0; _estado["rr_count"]=0
            ultimo_dia=ahora.day

        try:
            actualizar_trades()
            resultados, intervalo, btc_cambio = escanear_mercado()

            balance_ciclo=-1.0
            if (AUTO_TRADE or DRY_RUN) and BINGX_API_KEY:
                hay_accionables=any(
                    r["long_sup"] or r["short_sup"] or r["long_fuel"] or r["short_fuel"]
                    for r in resultados)
                if hay_accionables:
                    balance_ciclo=get_balance(); _estado["balance"]=balance_ciclo
                    usdt_need=calcular_usdt_trade(balance_ciclo)
                    if balance_ciclo<usdt_need:
                        log.warning(f"Balance ${balance_ciclo:.2f} < ${usdt_need:.2f}")
                        balance_ciclo=-2.0
                    else:
                        log.info(f"Balance: ${balance_ciclo:.2f} USDT")

            for par in resultados:
                sym=par["simbolo"]
                accionable=(par["long_sup"] or par["short_sup"] or
                            par["long_fuel"] or par["short_fuel"])
                if not accionable: continue
                if time.time()-alertas_enviadas.get(sym,0) < ALERTA_COOLDOWN: continue

                # v5.0: filtro BTC antes de alerta/trade
                es_long_dir="LONG" in par["señal"]
                if es_long_dir and not btc_permite_long(btc_cambio):
                    log.info(f"{sym}: LONG bloqueado por BTC {btc_cambio:+.1f}% < {BTC_LONG_FILTER}%")
                    continue
                if not es_long_dir and not btc_permite_short(btc_cambio):
                    log.info(f"{sym}: SHORT bloqueado por BTC {btc_cambio:+.1f}% > {BTC_SHORT_FILTER}%")
                    continue

                msg=construir_alerta(par)
                if enviar_telegram(msg):
                    alertas_enviadas[sym]=time.time()

                if AUTO_TRADE or DRY_RUN:
                    atr = par.get("atr", par["precio"]*0.02)
                    if (par["long_sup"] or par["long_fuel"]) and sym not in trades_abiertos:
                        trade=abrir_trade(sym,par["precio"],"LONG",atr,balance_ciclo)
                        if trade:
                            enviar_telegram(
                                f"{'[DRY] ' if DRY_RUN else ''}✅ *LONG ABIERTO*: {sym.replace('-USDT','')}\n"
                                f"Entrada: `{trade['entrada']}` | SL: `{trade['sl']}`\n"
                                f"TP1: `{trade.get('tp1','-')}` (RR{TP1_RR}) | TP2: `{trade['tp']}` (RR{trade.get('rr',TP_RR):.2f})\n"
                                f"Qty: `{trade['qty']}` | `${trade['usdt']}×{LEVERAGE}x`")
                    elif (par["short_sup"] or par["short_fuel"]) and sym not in trades_abiertos:
                        trade=abrir_trade(sym,par["precio"],"SHORT",atr,balance_ciclo)
                        if trade:
                            enviar_telegram(
                                f"{'[DRY] ' if DRY_RUN else ''}✅ *SHORT ABIERTO*: {sym.replace('-USDT','')}\n"
                                f"Entrada: `{trade['entrada']}` | SL: `{trade['sl']}`\n"
                                f"TP1: `{trade.get('tp1','-')}` (RR{TP1_RR}) | TP2: `{trade['tp']}` (RR{trade.get('rr',TP_RR):.2f})\n"
                                f"Qty: `{trade['qty']}` | `${trade['usdt']}×{LEVERAGE}x`")

            if ahora.hour != ultima_hora:
                enviar_telegram(construir_resumen(resultados,btc_cambio,intervalo))
                ultima_hora=ahora.hour

        except Exception as e:
            log.error(f"Error en ciclo principal: {e}", exc_info=True)
            enviar_telegram(f"⚠️ *Error en scanner*\n`{str(e)[:200]}`")
            intervalo=INT_NORMAL

        log.info(f"Próximo escaneo en {intervalo}s ({intervalo//60}min)")
        time.sleep(intervalo)


if __name__ == "__main__":
    ejecutar()
