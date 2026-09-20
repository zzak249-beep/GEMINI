"""
crowding_bot.py — bot de SEÑALES del posicionamiento amontonado.

NO OPERA. NO PIDE CLAVES DE API. Usa solo endpoints públicos de BingX, así
que no puede tocar tu cuenta ni por error. Su único trabajo es generar
señales y MEDIRSE A SÍ MISMO.

═══════════════════════════════════════════════════════════════════════
LO QUE MIDE Y POR QUÉ ASÍ
═══════════════════════════════════════════════════════════════════════
Cada señal abre una operación VIRTUAL con stop y objetivo, y el bot la
sigue hasta que toca uno o se agota el tiempo. Apunta el resultado en R,
con el coste descontado. Sin eso, a los 15 días tendrías una lista de
señales y cero respuestas.

Convención: si en la misma vela el precio toca stop y objetivo, se cuenta
STOP. No sabemos el orden dentro de la vela y equivocarse al revés es
justo lo que infla los backtests.

═══════════════════════════════════════════════════════════════════════
CUÁNTA MUESTRA HACE FALTA (para que no te engañes al leerlo)
═══════════════════════════════════════════════════════════════════════
Con desviación típica de 1R por operación, para distinguir del ruido:

    ventaja 0.50 R/op  ->    31 operaciones
    ventaja 0.30 R/op  ->    87 operaciones
    ventaja 0.20 R/op  ->   196 operaciones
    ventaja 0.10 R/op  ->   784 operaciones

En 15 días, escaneando todos los perpetuos, deberías rondar las 300-500.
Eso solo alcanza para detectar una ventaja GRANDE. Si sale +0.08 R/op,
la respuesta correcta no es "funciona poco": es "no lo sé todavía".

Y ojo con la correlación: en un desplome las alts se mueven juntas, así
que 500 señales del mismo día valen como muchas menos. Por eso el informe
separa el resultado por día y avisa si un solo día domina el total.

═══════════════════════════════════════════════════════════════════════
CALENTAMIENTO
═══════════════════════════════════════════════════════════════════════
Los z-score de basis y open interest se calculan contra la historia que
el propio bot va acumulando; BingX no sirve histórico de OI. Necesita
unos 3 días antes de emitir nada. Durante ese tiempo dirá "calentando" y
es correcto que no haga nada.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import requests

import confirm as cf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("crowding")

_ultimo_ciclo = 0.0

BASE = "https://open-api.bingx.com"
UA = {"User-Agent": "crowding-signal-bot/1.0"}


def env(k, d):
    v = os.getenv(k)
    if v is None:
        return d
    try:
        if isinstance(d, bool):
            return str(v).strip().lower() in ("1", "true", "yes", "si", "sí", "on")
        if isinstance(d, int):
            return int(float(v))
        if isinstance(d, float):
            return float(v)
    except ValueError:
        log.warning("%s='%s' ilegible, se usa %r", k, v, d)
        return d
    return v


CFG = {
    "TIMEFRAME": env("TIMEFRAME", "15m"),
    "SCAN_SEC": env("SCAN_SEC", 300),
    "MIN_VOL_24H": env("MIN_VOL_24H", 2_000_000.0),
    "MAX_SYMBOLS": env("MAX_SYMBOLS", 300),
    # EN HORAS, NO EN MUESTRAS. El bot toma una muestra por ciclo, y la
    # duración del ciclo depende de cuántos símbolos escanee: con 300 salen
    # 9,4 min, con 100 saldrían ~3. Si estos valores fueran contadores, cada
    # cambio de MAX_SYMBOLS los reinterpretaría en silencio. En horas
    # significan siempre lo mismo y el bot hace la conversión él.
    "HIST_HORAS": env("HIST_HORAS", 168.0),   # retención (7 días)
    "MIN_HORAS": env("MIN_HORAS", 30.0),      # antes de emitir
    "MIN_MUESTRAS": env("MIN_MUESTRAS", 200), # y además esta cuenta mínima
    "OI_LOOK_H": env("OI_LOOK_H", 6.0),       # ventana de variación de OI
    "Z_BASIS": env("Z_BASIS", 2.0),
    "Z_OI": env("Z_OI", 1.0),
    "EXT_PCT": env("EXT_PCT", 80.0),
    "ATR_LEN": env("ATR_LEN", 14),
    "SL_ATR": env("SL_ATR", 1.5),
    "TP_R": env("TP_R", 2.0),
    "MAX_BARS": env("MAX_BARS", 16),
    "MIN_ATR_PCT": env("MIN_ATR_PCT", 1.0),
    "COST_PCT": env("COST_PCT", 0.25),
    "MAX_COST_R": env("MAX_COST_R", 0.20),
    "STATE": env("STATE", "/data/crowding_state.json"),
    "CSV": env("CSV", "/data/crowding_ops.csv"),
    "TG_TOKEN": env("TG_TOKEN", ""),
    "TG_CHAT": env("TG_CHAT", ""),
    "REPORT_HOUR": env("REPORT_HOUR", 7),
    # Aviso por señal y por cierre APAGADOS por defecto. Con ~300 símbolos
    # salen unos 70 mensajes al día, y un chat con 70 mensajes diarios se
    # deja de leer en una semana. Todo queda igualmente en el CSV, que es
    # de donde sale la respuesta a los 15 días. El informe diario sí llega.
    "TG_SIGNALS": env("TG_SIGNALS", False),
    "TG_CLOSES": env("TG_CLOSES", False),
    "PACING": env("PACING", 0.15),
    # Régimen: se APUNTA, no decide. El crowding opera CONTRA la multitud,
    # o sea que es reversión — el veto de confirm.py está pensado para
    # ruptura y le quitaría sus mejores entradas. Aquí solo se registra
    # para poder responder en 15 días si rinde mejor en régimen reversivo.
    "CONFIRM_ENABLED": env("CONFIRM_ENABLED", True),
    "CONFIRM_BLOQUEAR": False,
    "CONFIRM_Q": env("CONFIRM_Q", 8),
    "CONFIRM_WIN": env("CONFIRM_WIN", 240),
    "CONFIRM_LAMBDA": env("CONFIRM_LAMBDA", 0.985),
    "CONFIRM_Z": env("CONFIRM_Z", 1.5),
    "CONFIRM_MIN_VELAS": env("CONFIRM_MIN_VELAS", 120),
}

TF_MS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
BAR_SEC = TF_MS.get(str(CFG["TIMEFRAME"]), 900)


# ─────────────────────────────────────────────────────── API pública
def _get(path: str, params: dict | None = None, intentos: int = 3):
    for i in range(intentos):
        try:
            r = requests.get(BASE + path, params=params or {}, headers=UA, timeout=15)
            r.raise_for_status()
            j = r.json()
            if str(j.get("code", 0)) not in ("0", "None"):
                log.debug("%s devolvió code=%s", path, j.get("code"))
                return None
            return j.get("data")
        except Exception as e:
            if i == intentos - 1:
                log.debug("%s falló: %s", path, e)
            time.sleep(0.5 * (i + 1))
    return None


def contratos() -> list[str]:
    d = _get("/openApi/swap/v2/quote/contracts")
    if not d:
        return []
    out = []
    for c in d:
        s = c.get("symbol", "")
        if s.endswith("-USDT") and str(c.get("status", 1)) in ("1", "True", "true"):
            out.append(s)
    return out


def volumenes() -> dict:
    d = _get("/openApi/swap/v2/quote/ticker")
    if not d:
        return {}
    out = {}
    for t in d:
        try:
            out[t.get("symbol")] = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            pass
    return out


def prima(symbol: str):
    """markPrice e indexPrice: el basis sale de restarlos, sin feed de spot."""
    d = _get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
    if isinstance(d, list):
        d = d[0] if d else None
    if not isinstance(d, dict):
        return None
    try:
        mark = float(d.get("markPrice") or 0)
        index = float(d.get("indexPrice") or 0)
        fr = float(d.get("lastFundingRate") or 0)
        if mark <= 0 or index <= 0:
            return None
        return {"mark": mark, "index": index, "basis": (mark - index) / index * 100.0,
                "funding": fr * 100.0}
    except (TypeError, ValueError):
        return None


def open_interest(symbol: str):
    d = _get("/openApi/swap/v2/quote/openInterest", {"symbol": symbol})
    if isinstance(d, list):
        d = d[0] if d else None
    if not isinstance(d, dict):
        return None
    try:
        v = float(d.get("openInterest") or 0)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def klines(symbol: str, limit: int = 200):
    d = _get("/openApi/swap/v3/quote/klines",
             {"symbol": symbol, "interval": CFG["TIMEFRAME"], "limit": limit})
    if not d:
        d = _get("/openApi/swap/v2/quote/klines",
                 {"symbol": symbol, "interval": CFG["TIMEFRAME"], "limit": limit})
    if not d:
        return []
    filas = []
    for k in d:
        try:
            if isinstance(k, dict):
                filas.append({"t": int(k["time"]), "o": float(k["open"]), "h": float(k["high"]),
                              "l": float(k["low"]), "c": float(k["close"])})
            else:
                filas.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                              "l": float(k[3]), "c": float(k[4])})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    filas.sort(key=lambda x: x["t"])
    return filas


# ─────────────────────────────────────────────────────── indicadores
def atr(velas, n):
    if len(velas) < n + 1:
        return 0.0
    trs = []
    for i in range(len(velas) - n, len(velas)):
        h, l, cp = velas[i]["h"], velas[i]["l"], velas[i - 1]["c"]
        trs.append(max(h - l, abs(h - cp), abs(l - cp)))
    return sum(trs) / len(trs) if trs else 0.0


def percentil(valores, x):
    if not valores:
        return 50.0
    return sum(1 for v in valores if v <= x) / len(valores) * 100.0


def zscore(hist, x):
    if len(hist) < 30:
        return None
    mu = statistics.fmean(hist)
    sd = statistics.pstdev(hist)
    return (x - mu) / sd if sd > 1e-12 else 0.0


# ─────────────────────────────────────────────────────── estado
@dataclass
class Virtual:
    symbol: str
    lado: str
    abierta_ts: int
    entrada: float
    sl: float
    tp: float
    riesgo: float
    coste_r: float
    basis_z: float
    oi_z: float
    funding: float
    atr_pct: float
    conf_z: float = 0.0
    conf_regimen: str = "sin datos"
    barras: int = 0


def _podar(h: deque, ahora: float, horas: float):
    """Tira lo más viejo que la ventana de retención."""
    limite = ahora - horas * 3600.0
    while h and h[0][0] < limite:
        h.popleft()


def _valor_hace(h: deque, ahora: float, horas: float):
    """Valor de hace ~N horas: la muestra más cercana a ese instante."""
    if not h:
        return None
    objetivo = ahora - horas * 3600.0
    if h[0][0] > objetivo:
        return None                      # la historia no llega tan atrás
    mejor = min(h, key=lambda p: abs(p[0] - objetivo))
    return mejor[1]


def _span_horas(h: deque) -> float:
    return (h[-1][0] - h[0][0]) / 3600.0 if len(h) > 1 else 0.0


class Estado:
    def __init__(self, path):
        self.path = path
        self.basis: dict[str, deque] = {}
        self.oi: dict[str, deque] = {}
        self.abiertas: dict[str, Virtual] = {}
        self.ultimo_informe = ""
        self._cargar()

    def _cargar(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            # Migración del formato antiguo (lista de floats sin marca de
            # tiempo): se les asignan tiempos hacia atrás a la cadencia
            # nominal, para no tirar horas de calentamiento ya ganadas.
            ahora = time.time()
            paso = float(CFG["SCAN_SEC"]) + 260.0
            migradas = 0

            def _cargar(bloque):
                nonlocal migradas
                out = {}
                for k, v in bloque.items():
                    dq = deque()
                    if v and not isinstance(v[0], (list, tuple)):
                        migradas += 1
                        base = ahora - len(v) * paso
                        for i, x in enumerate(v):
                            dq.append((base + i * paso, float(x)))
                    else:
                        for p in v:
                            dq.append((float(p[0]), float(p[1])))
                    out[k] = dq
                return out

            self.basis = _cargar(d.get("basis", {}))
            self.oi = _cargar(d.get("oi", {}))
            if migradas:
                log.warning("Migradas %d series del formato antiguo (sin marca de "
                            "tiempo): los tiempos son estimados", migradas)
            self.abiertas = {k: Virtual(**v) for k, v in d.get("abiertas", {}).items()}
            self.ultimo_informe = d.get("ultimo_informe", "")
            log.info("Estado cargado: %d símbolos con historia, %d virtuales abiertas",
                     len(self.basis), len(self.abiertas))
        except FileNotFoundError:
            log.info("Sin estado previo, empezando de cero")
        except Exception:
            log.exception("Estado ilegible, se empieza de cero")

    def guardar(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "basis": {k: [list(p) for p in v] for k, v in self.basis.items()},
                    "oi": {k: [list(p) for p in v] for k, v in self.oi.items()},
                    "abiertas": {k: asdict(v) for k, v in self.abiertas.items()},
                    "ultimo_informe": self.ultimo_informe,
                }, f)
            os.replace(tmp, self.path)   # atómico: un corte no deja el JSON a medias
        except Exception:
            log.exception("No se pudo guardar el estado")


class _Cfg:
    """Puente para que confirm.py lea CFG con getattr, sin config.py."""

    def __getattr__(self, k):
        if k in CFG:
            return CFG[k]
        raise AttributeError(k)


_CFG_OBJ = _Cfg()

COLS = ["cerrada_utc", "symbol", "lado", "abierta_utc", "entrada", "salida",
        "motivo", "r_bruto", "coste_r", "r_neto", "barras", "basis_z",
        "oi_z", "funding", "atr_pct", "conf_z", "conf_regimen"]


def anotar(fila: dict):
    try:
        ruta = CFG["CSV"]
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        nuevo = not os.path.exists(ruta) or os.path.getsize(ruta) == 0
        with open(ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
            if nuevo:
                w.writeheader()
            w.writerow(fila)
    except Exception:
        log.exception("No se pudo anotar en el CSV")


def tg(texto: str, tipo: str = "informe"):
    """tipo: 'senal' | 'cierre' | 'informe'. Los dos primeros son opcionales."""
    if tipo == "senal" and not CFG["TG_SIGNALS"]:
        return
    if tipo == "cierre" and not CFG["TG_CLOSES"]:
        return
    if not CFG["TG_TOKEN"] or not CFG["TG_CHAT"]:
        log.info("[telegram] %s", texto.replace("\n", " | ")[:300])
        return
    try:
        requests.post(f"https://api.telegram.org/bot{CFG['TG_TOKEN']}/sendMessage",
                      json={"chat_id": CFG["TG_CHAT"], "text": texto,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15)
    except Exception:
        log.exception("Telegram falló")


# ─────────────────────────────────────────────────────── lógica
def evaluar(symbol, velas, st: Estado):
    """Devuelve (senal|None, motivo). senal = ('LONG'|'SHORT', datos)."""
    if len(velas) < 60:
        return None, "pocas velas"

    p = prima(symbol)
    oi = open_interest(symbol)
    if p is None:
        return None, "sin premiumIndex"
    if oi is None:
        return None, "sin open interest"

    ahora = time.time()
    hb = st.basis.setdefault(symbol, deque())
    ho = st.oi.setdefault(symbol, deque())
    hb.append((ahora, p["basis"]))
    ho.append((ahora, oi))
    horas_ret = float(CFG["HIST_HORAS"])
    _podar(hb, ahora, horas_ret)
    _podar(ho, ahora, horas_ret)

    span = _span_horas(hb)
    min_h = float(CFG["MIN_HORAS"])
    min_n = int(CFG["MIN_MUESTRAS"])
    if span < min_h or len(hb) < min_n:
        return None, f"calentando ({span:.1f}/{min_h:.0f}h, {len(hb)}/{min_n})"

    look_h = float(CFG["OI_LOOK_H"])
    oi_prev = _valor_hace(ho, ahora, look_h)
    if oi_prev is None or oi_prev <= 0:
        return None, "sin ventana de OI"
    oi_chg = (oi - oi_prev) / oi_prev * 100.0

    # El z del cambio de OI se calcula contra los cambios pasados, no contra
    # el nivel: el OI crece con el interés del mercado y el nivel no es
    # comparable consigo mismo de hace un mes. Cada cambio se mide contra el
    # valor de hace look_h HORAS, no de hace N muestras.
    lo = list(ho)
    cambios = []
    for i in range(len(lo)):
        ref = _valor_hace(deque(lo[:i + 1]), lo[i][0], look_h)
        if ref and ref > 0:
            cambios.append((lo[i][1] - ref) / ref * 100.0)
    zb = zscore([x[1] for x in hb], p["basis"])
    zo = zscore(cambios, oi_chg)
    if zb is None or zo is None:
        return None, "z no calculable"

    cierres = [v["c"] for v in velas[-100:]]
    px = velas[-1]["c"]
    pr = percentil(cierres, px)

    a = atr(velas, int(CFG["ATR_LEN"]))
    atr_pct = a / px * 100.0 if px > 0 else 0.0
    riesgo = float(CFG["SL_ATR"]) * a
    coste_r = (float(CFG["COST_PCT"]) / 100.0 * px) / riesgo if riesgo > 0 else 99.0

    largos_amont = zb >= float(CFG["Z_BASIS"]) and zo >= float(CFG["Z_OI"]) and pr >= float(CFG["EXT_PCT"])
    cortos_amont = zb <= -float(CFG["Z_BASIS"]) and zo >= float(CFG["Z_OI"]) and pr <= (100 - float(CFG["EXT_PCT"]))

    if not (largos_amont or cortos_amont):
        return None, f"sin amontonamiento (zb {zb:.2f})"
    if atr_pct < float(CFG["MIN_ATR_PCT"]):
        return None, f"sin amplitud ({atr_pct:.2f}%)"
    if coste_r > float(CFG["MAX_COST_R"]):
        return None, f"coste {coste_r:.2f}R"

    # La cerilla: primera vela EN CONTRA de la multitud.
    ult, ant = velas[-1], velas[-2]
    lado = None
    if largos_amont and ult["c"] < ant["l"] and ult["c"] < ult["o"]:
        lado = "SHORT"
    elif cortos_amont and ult["c"] > ant["h"] and ult["c"] > ult["o"]:
        lado = "LONG"
    if lado is None:
        return None, "esperando vela en contra"

    reg = cf.calcular(_CFG_OBJ, cierres)
    return (lado, {"px": px, "riesgo": riesgo, "coste_r": coste_r, "zb": zb,
                   "zo": zo, "funding": p["funding"], "atr_pct": atr_pct,
                   "conf_z": reg.z if reg.ok else 0.0,
                   "conf_regimen": reg.etiqueta if reg.ok else reg.motivo}), "señal"


def seguir_virtuales(st: Estado, symbol: str, velas):
    """Cierra las virtuales que tocaron stop, objetivo o se quedaron sin tiempo."""
    v = st.abiertas.get(symbol)
    if v is None:
        return
    nuevas = [k for k in velas if k["t"] > v.abierta_ts]
    if not nuevas:
        return
    v.barras = len(nuevas)
    largo = v.lado == "LONG"
    salida = motivo = None
    for k in nuevas:
        toca_sl = (k["l"] <= v.sl) if largo else (k["h"] >= v.sl)
        toca_tp = (k["h"] >= v.tp) if largo else (k["l"] <= v.tp)
        if toca_sl:                      # si tocan los dos, manda el STOP
            salida, motivo = v.sl, "stop"
            break
        if toca_tp:
            salida, motivo = v.tp, "objetivo"
            break
    if salida is None and v.barras >= int(CFG["MAX_BARS"]):
        salida, motivo = nuevas[-1]["c"], "tiempo"
    if salida is None:
        return

    bruto = ((salida - v.entrada) if largo else (v.entrada - salida)) / v.riesgo
    neto = bruto - v.coste_r
    anotar({
        "cerrada_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": v.symbol, "lado": v.lado,
        "abierta_utc": datetime.fromtimestamp(v.abierta_ts / 1000, timezone.utc).isoformat(timespec="seconds"),
        "entrada": v.entrada, "salida": salida, "motivo": motivo,
        "r_bruto": round(bruto, 4), "coste_r": round(v.coste_r, 4),
        "r_neto": round(neto, 4), "barras": v.barras,
        "basis_z": round(v.basis_z, 3), "oi_z": round(v.oi_z, 3),
        "funding": round(v.funding, 5), "atr_pct": round(v.atr_pct, 3),
        "conf_z": round(v.conf_z, 3), "conf_regimen": v.conf_regimen,
    })
    icono = "✅" if neto > 0 else "🔴"
    tg(f"{icono} <b>{v.symbol.split('-')[0]}</b> {v.lado} virtual cerrada por {motivo}: "
       f"<b>{neto:+.2f} R</b> en {v.barras} velas", "cierre")
    st.abiertas.pop(symbol, None)


def informe(st: Estado):
    try:
        with open(CFG["CSV"], encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
    except Exception:
        filas = []
    if not filas:
        return (f"📊 <b>Crowding · informe</b>\nSin operaciones cerradas todavía.\n"
                f"Historia acumulada en {len(st.basis)} símbolos.\n"
                f"<i>Necesita ~200 muestras por símbolo antes de emitir.</i>")
    rs = [float(x["r_neto"]) for x in filas]
    n = len(rs)
    media = statistics.fmean(rs)
    sd = statistics.pstdev(rs) if n > 1 else 1.0
    t = media * math.sqrt(n) / sd if sd > 1e-9 else 0.0
    gan = sum(1 for r in rs if r > 0)
    por_dia: dict[str, float] = {}
    for x in filas:
        d = x["cerrada_utc"][:10]
        por_dia[d] = por_dia.get(d, 0.0) + float(x["r_neto"])
    peor = max(por_dia.items(), key=lambda kv: abs(kv[1])) if por_dia else ("-", 0.0)
    dom = abs(peor[1]) / max(abs(sum(rs)), 1e-9) * 100 if rs else 0

    if n < 31:
        lectura = f"muestra {n}: no alcanza ni para detectar 0.50 R/op"
    elif n < 87:
        lectura = f"muestra {n}: solo detectaría una ventaja ≥0.50 R/op"
    elif n < 196:
        lectura = f"muestra {n}: solo detectaría una ventaja ≥0.30 R/op"
    else:
        lectura = f"muestra {n}: detecta ventajas ≥0.20 R/op"

    L = [f"📊 <b>Crowding · informe</b>",
         f"<b>{n}</b> operaciones · {gan / n * 100:.0f}% ganadoras",
         f"<b>{media:+.3f} R/op</b> · total {sum(rs):+.1f} R · t={t:.2f}",
         f"<i>{lectura}</i>",
         f"Días con operaciones: {len(por_dia)}"]
    if dom > 40:
        L.append(f"⚠️ El día {peor[0]} pesa el {dom:.0f}% del total: son señales "
                 f"correlacionadas, no {n} independientes")
    if abs(t) < 2:
        L.append("Sin significación: <b>esto todavía no dice nada</b>")
    # ¿El crowding rinde mejor en régimen reversivo? Esta es la pregunta
    # que el módulo de régimen está aquí para contestar, no para decidir.
    por_reg: dict[str, list] = {}
    for x in filas:
        por_reg.setdefault(x.get("conf_regimen") or "?", []).append(float(x["r_neto"]))
    if len(por_reg) > 1:
        L.append("Por régimen:")
        for k, v in sorted(por_reg.items(), key=lambda kv: -len(kv[1])):
            aviso = " ⚠" if len(v) < 31 else ""
            L.append(f"  {k}: {statistics.fmean(v):+.3f} R/op (n={len(v)}){aviso}")
    L.append(f"Virtuales abiertas: {len(st.abiertas)}")
    if not CFG["TG_SIGNALS"]:
        L.append("<i>Avisos por señal apagados (TG_SIGNALS). Todo está en el CSV.</i>")
    return "\n".join(L)


def ciclo(st: Estado, simbolos: list[str]):
    señales = motivos = 0
    razones: dict[str, int] = {}
    for sym in simbolos:
        try:
            velas = klines(sym, 200)
            if not velas:
                continue
            seguir_virtuales(st, sym, velas)
            if sym in st.abiertas:
                continue
            sig, motivo = evaluar(sym, velas, st)
            clave = motivo.split("(")[0].strip()
            razones[clave] = razones.get(clave, 0) + 1
            motivos += 1
            if sig is None:
                continue
            lado, d = sig
            entrada = d["px"]
            sl = entrada - d["riesgo"] if lado == "LONG" else entrada + d["riesgo"]
            tp = entrada + float(CFG["TP_R"]) * d["riesgo"] if lado == "LONG" else entrada - float(CFG["TP_R"]) * d["riesgo"]
            st.abiertas[sym] = Virtual(
                symbol=sym, lado=lado, abierta_ts=velas[-1]["t"], entrada=entrada,
                sl=sl, tp=tp, riesgo=d["riesgo"], coste_r=d["coste_r"],
                basis_z=d["zb"], oi_z=d["zo"], funding=d["funding"], atr_pct=d["atr_pct"],
                conf_z=d["conf_z"], conf_regimen=d["conf_regimen"])
            señales += 1
            flecha = "🟢" if lado == "LONG" else "🔴"
            tg(f"{flecha} <b>{sym.split('-')[0]}</b> {lado} (virtual)\n"
               f"Entrada <code>{entrada:.8g}</code> · SL <code>{sl:.8g}</code> · TP <code>{tp:.8g}</code>\n"
               f"basis z {d['zb']:+.2f} · OI z {d['zo']:+.2f} · funding {d['funding']:+.4f}%\n"
               f"ATR {d['atr_pct']:.2f}% · coste {d['coste_r']:.2f} R\n"
               f"<i>Solo señal. El bot no opera.</i>", "senal")
        except Exception:
            log.exception("Fallo evaluando %s", sym)
        finally:
            time.sleep(float(CFG["PACING"]))
    top = sorted(razones.items(), key=lambda kv: -kv[1])[:4]
    global _ultimo_ciclo
    ahora = time.time()
    cad = (ahora - _ultimo_ciclo) / 60.0 if _ultimo_ciclo else 0.0
    _ultimo_ciclo = ahora
    log.info("Ciclo: %d símbolos · %d señales · cadencia %.1f min | %s",
             motivos, señales, cad, " · ".join(f"{k}: {v}" for k, v in top))
    st.guardar()


def main():
    log.info("Crowding bot — SOLO SEÑALES, sin claves de API, %s", CFG["TIMEFRAME"])
    st = Estado(CFG["STATE"])
    syms = contratos()
    vols = volumenes()
    if vols:
        syms = [s for s in syms if vols.get(s, 0) >= float(CFG["MIN_VOL_24H"])]
        syms.sort(key=lambda s: vols.get(s, 0), reverse=True)
    syms = syms[: int(CFG["MAX_SYMBOLS"])]
    log.info("Universo: %d símbolos", len(syms))
    tg(f"🤖 <b>Crowding bot arrancado</b>\n{len(syms)} símbolos · {CFG['TIMEFRAME']}\n"
       f"Avisos: señal {'ON' if CFG['TG_SIGNALS'] else 'off'} · "
       f"cierre {'ON' if CFG['TG_CLOSES'] else 'off'} · informe diario a las "
       f"{CFG['REPORT_HOUR']}:00 UTC\n"
       f"<i>Sin claves de API: solo lee endpoints públicos. No puede operar.</i>\n"
       f"<i>Necesita ~3 días de calentamiento antes de emitir.</i>")

    ultimo_universo = time.time()
    while True:
        try:
            if time.time() - ultimo_universo > 6 * 3600:
                v2 = volumenes()
                if v2:
                    s2 = [s for s in contratos() if v2.get(s, 0) >= float(CFG["MIN_VOL_24H"])]
                    s2.sort(key=lambda s: v2.get(s, 0), reverse=True)
                    if s2:
                        syms = s2[: int(CFG["MAX_SYMBOLS"])]
                ultimo_universo = time.time()

            ciclo(st, syms)

            hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if (datetime.now(timezone.utc).hour == int(CFG["REPORT_HOUR"])
                    and st.ultimo_informe != hoy):
                st.ultimo_informe = hoy
                st.guardar()
                tg(informe(st))
        except Exception:
            log.exception("Fallo en el ciclo")
        time.sleep(int(CFG["SCAN_SEC"]))


if __name__ == "__main__":
    main()
