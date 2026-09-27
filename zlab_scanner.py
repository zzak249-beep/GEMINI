"""
KIBITO Z-LAB SCANNER
Escanea los perpetuos USDT de BingX, ejecuta un backtest de Z-LAB v2 (5m) en cada
moneda con los ultimos dias y envia a Telegram el ranking de las mejores para operar.

Solo usa endpoints publicos (no necesita API key, no opera).
"""
import os
import time
import json
import math
import logging
import datetime as dt
from dataclasses import dataclass, field, asdict

import requests

CODE_VERSION = "zlab-scanner 1.1.0"


# ───────────────────────── configuracion ─────────────────────────
def env(name, default, cast=str):
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().strip('"').strip("'").strip()
    if raw == "":
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "si", "on")
    return cast(raw)


BASE_URL        = env("BINGX_BASE_URL", "https://open-api.bingx.com")
TG_TOKEN        = env("TELEGRAM_TOKEN", "")
TG_CHAT         = env("TELEGRAM_CHAT_ID", "")
SCAN_EVERY_MIN  = env("SCAN_EVERY_MIN", 240, int)
ONE_SHOT        = env("ONE_SHOT", False, bool)
MIN_VOL_USDT    = env("MIN_VOL_USDT", 2_000_000, float)
MAX_SYMBOLS     = env("MAX_SYMBOLS", 0, int)          # 0 = todas las que pasen el volumen
PAGES           = env("KLINE_PAGES", 2, int)          # 1 pagina = 1440 velas de 5m = 5 dias
TOP_N           = env("TOP_N", 10, int)
MIN_TRADES      = env("MIN_TRADES", 4, int)
EXCLUDE         = {s.strip().upper() for s in env("EXCLUDE", "", str).split(",") if s.strip()}
REQ_PAUSE_S     = env("REQ_PAUSE_S", 1.1, float)       # rate limit 1 req/s por IP
OUT_FILE        = env("OUT_FILE", "zlab_ranking.json")

# parametros Z-LAB v2 (valores por defecto validados en 8 monedas; no tocar sin re-validar)
P = dict(
    fadeZ=2.0, extZ=3.0, horizon=24, regLen=48, minSample=10,
    minProb=55.0, minTgtPct=0.30,
    riskPct=0.5, maxLev=2.0, maxStopPct=5.0, minRR=1.0, cooldown=6,
    fadeMinZ=2.5, htfLen=50, dayMovePct=8.0, maxDay=3, lossStreak=3, pauseBars=144,
    maxDDDay=2.5, fee=0.0005,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zlab")


# ───────────────────────── datos BingX ─────────────────────────
S = requests.Session()


def api_get(path, params=None, retries=3):
    url = BASE_URL + path
    for k in range(retries):
        try:
            r = S.get(url, params=params, timeout=15)
            r.raise_for_status()
            js = r.json()
            if js.get("code", 0) not in (0, "0"):
                raise RuntimeError(f"BingX {js.get('code')}: {js.get('msg')}")
            return js.get("data")
        except Exception as e:
            log.warning("GET %s %s intento %d: %s", path, params, k + 1, e)
            time.sleep(2 + 2 * k)
    return None


def get_tickers():
    data = api_get("/openApi/swap/v2/quote/ticker") or []
    if isinstance(data, dict):
        data = [data]
    out = []
    for t in data:
        sym = str(t.get("symbol", ""))
        if not sym.endswith("-USDT"):
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            chg = float(t.get("priceChangePercent") or 0)
        except ValueError:
            continue
        out.append((sym, qv, chg))
    return out


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float
    tb: float | None = None   # volumen comprador agresivo (taker buy), si BingX lo da


def parse_kline(k):
    # BingX documenta arrays [t,o,h,l,c,v,closeT,qv,n,takerBuyBase,...]; en la practica
    # algunas versiones devuelven dicts {open,close,high,low,volume,time}. Aceptamos ambos.
    if isinstance(k, dict):
        return Bar(int(k["time"]), float(k["open"]), float(k["high"]), float(k["low"]),
                   float(k["close"]), float(k.get("volume", 0)),
                   float(k["takerBuyBaseVolume"]) if k.get("takerBuyBaseVolume") not in (None, "") else None)
    tb = float(k[9]) if len(k) > 9 and k[9] not in (None, "") else None
    return Bar(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), tb)


KCACHE = {}   # symbol -> {t: Bar}; tras el primer escaneo solo se descarga la pagina mas reciente


def get_klines(symbol, pages):
    cached = KCACHE.get(symbol)
    now_ms = int(time.time() * 1000)
    if cached and max(cached) > now_ms - 4 * 86_400_000:
        data = api_get("/openApi/swap/v3/quote/klines", {"symbol": symbol, "interval": "5m", "limit": 1440})
        time.sleep(REQ_PAUSE_S)
        for k in data or []:
            b = parse_kline(k)
            cached[b.t] = b
        keep = sorted(cached)[-pages * 1440:]
        KCACHE[symbol] = {t: cached[t] for t in keep}
        return [KCACHE[symbol][t] for t in keep if t + 300_000 <= now_ms]
    bars = {}
    end = None
    for _ in range(pages):
        params = {"symbol": symbol, "interval": "5m", "limit": 1440}
        if end:
            params["endTime"] = end
        data = api_get("/openApi/swap/v3/quote/klines", params)
        time.sleep(REQ_PAUSE_S)
        if not data:
            break
        chunk = [parse_kline(k) for k in data]
        for b in chunk:
            bars[b.t] = b
        oldest = min(b.t for b in chunk)
        end = oldest - 1
        if len(chunk) < 1440:
            break
    out = sorted(bars.values(), key=lambda b: b.t)
    if out:
        KCACHE[symbol] = {b.t: b for b in out}
    # descartar la vela en curso (no cerrada)
    return [b for b in out if b.t + 300_000 <= now_ms]


# ───────────────────────── backtest Z-LAB v2 ─────────────────────────
def ema_series(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for x in vals:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def atr_series(bars, n=14):
    out, a = [], None
    for i, b in enumerate(bars):
        tr = b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c))
        a = tr if a is None else (a * (n - 1) + tr) / n      # RMA como ta.atr
        out.append(a)
    return out


@dataclass
class Result:
    symbol: str
    trades: int = 0
    wins: int = 0
    gp: float = 0.0
    gl: float = 0.0
    pnl_pct: float = 0.0
    max_dd_pct: float = 0.0
    atr_pct: float = 0.0
    eff_ratio: float = 0.0
    rev_prob: float | None = None
    rev_sample: int = 0
    vol_usdt: float = 0.0
    chg24: float = 0.0
    days: float = 0.0
    last_regime: str = ""
    score: float = 0.0
    notes: list = field(default_factory=list)

    @property
    def pf(self):
        return self.gp / self.gl if self.gl > 0 else (float("inf") if self.gp > 0 else 0.0)

    @property
    def winrate(self):
        return self.wins / self.trades * 100 if self.trades else 0.0


def backtest(symbol, bars):
    p = P
    n = len(bars)
    res = Result(symbol)
    if n < 600:
        res.notes.append("pocos datos")
        return res
    res.days = n * 5 / 1440

    atr = atr_series(bars)

    # VWAP diario (UTC) con desviacion estandar ponderada por volumen, como ta.vwap
    vw, sd = [0.0] * n, [0.0] * n
    sv = svp = svp2 = 0.0
    day_open = [0.0] * n
    cur_day, dopen = None, bars[0].o
    for i, b in enumerate(bars):
        d = b.t // 86_400_000
        if d != cur_day:
            cur_day, sv, svp, svp2, dopen = d, 0.0, 0.0, 0.0, b.o
        src = (b.h + b.l + b.c) / 3
        v = max(b.v, 1e-12)
        sv += v
        svp += v * src
        svp2 += v * src * src
        m = svp / sv
        vw[i] = m
        sd[i] = math.sqrt(max(svp2 / sv - m * m, 0.0))
        day_open[i] = dopen

    # tendencia 1h sin repintar: EMA50 de velas 1h cerradas
    hour_close = {}
    for b in bars:
        hour_close[b.t // 3_600_000] = b.c
    hours = sorted(hour_close)
    h_ema = dict(zip(hours, ema_series([hour_close[h] for h in hours], p["htfLen"])))
    htf_up, htf_dn = [False] * n, [False] * n
    for i, b in enumerate(bars):
        ph = b.t // 3_600_000 - 1       # ultima hora cerrada
        if ph in h_ema:
            htf_up[i] = hour_close[ph] > h_ema[ph]
            htf_dn[i] = hour_close[ph] < h_ema[ph]

    # delta: taker buy real si existe, si no aproximacion por direccion de vela
    def delta(i):
        b = bars[i]
        if b.tb is not None:
            return 2 * b.tb - b.v
        return b.v if b.c > b.o else (-b.v if b.c < b.o else 0.0)

    def zv(i, price):
        return (price - vw[i]) / sd[i] if sd[i] > 0 else 0.0

    # estado
    ev = []                              # eventos de toque: [bar, dir, reg]
    n_touch, n_rev = [0, 0], [0, 0]
    above = [1 if bars[i].c > vw[i] else 0 for i in range(n)]
    cross = [0] + [1 if (bars[i].c - vw[i]) * (bars[i - 1].c - vw[i - 1]) < 0 else 0 for i in range(1, n)]
    equity = 10_000.0
    peak = equity
    max_dd = 0.0
    pos = None                           # dict con side, qty, entry, stop, tgt, type, bar
    pending = None
    last_sig = -100
    last_exit = -1000
    trades_day, day_id = 0, None
    fail_l = fail_s = 0
    streak, pause_until = 0, -1
    day_start_eq = equity
    day_block = False
    is_rot_prev = True

    def close_trade(price, i):
        nonlocal equity, peak, max_dd, pos, last_exit, streak, pause_until, fail_l, fail_s
        side = pos["side"]
        gross = (price - pos["entry"]) * pos["qty"] * side
        fees = (pos["entry"] + price) * pos["qty"] * p["fee"]
        pnl = gross - fees
        equity += pnl
        res.trades += 1
        if pnl > 0:
            res.wins += 1
            res.gp += pnl
            streak = 0
        else:
            res.gl += -pnl
            streak += 1
            if streak >= p["lossStreak"]:
                pause_until = i + p["pauseBars"]
                streak = 0
        if pos["type"] == "fade":
            if side == 1:
                fail_l = fail_l + 1 if pnl <= 0 else 0
            else:
                fail_s = fail_s + 1 if pnl <= 0 else 0
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        last_exit = i
        pos = None

    for i in range(1, n):
        b = bars[i]
        d = b.t // 86_400_000
        if d != day_id:
            day_id, trades_day, fail_l, fail_s = d, 0, 0, 0
            day_start_eq, day_block = equity, False

        # 1) ejecutar entrada pendiente a la apertura de esta vela
        if pending and pos is None:
            entry = b.o
            dist = abs(entry - pending["stop"])
            if dist > 0 and (pending["side"] == 1 and pending["stop"] < entry or pending["side"] == -1 and pending["stop"] > entry):
                qty = min(equity * p["riskPct"] / 100 / dist, equity * p["maxLev"] / entry)
                pos = dict(side=pending["side"], qty=qty, entry=entry, stop=pending["stop"],
                           tgt=pending["tgt"], type=pending["type"], bar=i)
                trades_day += 1
            pending = None

        # 2) gestionar posicion abierta (stop primero si ambos en la misma vela)
        if pos is not None:
            tgt = vw[i] if pos["type"] == "fade" else pos["tgt"]
            if pos["side"] == 1:
                if b.l <= pos["stop"]:
                    close_trade(min(pos["stop"], b.o), i)
                elif b.h >= tgt:
                    close_trade(max(tgt, b.o), i)
            else:
                if b.h >= pos["stop"]:
                    close_trade(max(pos["stop"], b.o), i)
                elif b.l <= tgt:
                    close_trade(min(tgt, b.o), i)
            if pos is not None and i - pos["bar"] >= p["horizon"] * 2:
                close_trade(b.c, i)

        if (day_start_eq - equity) / day_start_eq * 100 >= p["maxDDDay"]:
            day_block = True

        # 3) estadistica de toques de +-2 sigma (causal)
        keep = []
        for (eb, edir, ereg) in ev:
            age = i - eb
            ext = b.h >= vw[i] + p["extZ"] * sd[i] if edir == 1 else b.l <= vw[i] - p["extZ"] * sd[i]
            rev = b.l <= vw[i] if edir == 1 else b.h >= vw[i]
            if ext or rev or age >= p["horizon"]:
                n_touch[ereg] += 1
                if rev and not ext:
                    n_rev[ereg] += 1
            else:
                keep.append((eb, edir, ereg))
        ev = keep

        zHi, zLo, z = zv(i, b.h), zv(i, b.l), zv(i, b.c)
        zHi1, zLo1 = zv(i - 1, bars[i - 1].h), zv(i - 1, bars[i - 1].l)

        reg_len = p["regLen"]
        if i >= reg_len:
            side_r = sum(above[i - reg_len + 1:i + 1]) / reg_len
            crosses = sum(cross[i - reg_len + 1:i + 1])
        else:
            side_r, crosses = 0.5, 99
        tr_up = side_r >= 0.8 and crosses <= 2
        tr_dn = side_r <= 0.2 and crosses <= 2
        is_rot = not tr_up and not tr_dn

        if sd[i] > 0:
            reg_idx = 0 if is_rot_prev else 1
            if zHi >= p["fadeZ"] and zHi1 < p["fadeZ"]:
                ev.append((i, 1, reg_idx))
            if zLo <= -p["fadeZ"] and zLo1 > -p["fadeZ"]:
                ev.append((i, -1, reg_idx))
        is_rot_prev = is_rot

        if pos is not None or pending is not None or sd[i] <= 0 or i < 3:
            continue

        # 4) senales
        p_rev = n_rev[0] / n_touch[0] * 100 if n_touch[0] else 0.0
        day_move = (b.c - day_open[i]) / day_open[i] * 100
        pump = abs(day_move) >= p["dayMovePct"]
        zhi3 = max(zv(k, bars[k].h) for k in range(i - 2, i + 1))
        zlo3 = min(zv(k, bars[k].l) for k in range(i - 2, i + 1))
        dlt = delta(i)
        u2, l2 = vw[i] + p["fadeZ"] * sd[i], vw[i] - p["fadeZ"] * sd[i]

        ok_fs = fail_s < 2 and not pump and not htf_up[i] and zhi3 >= p["fadeMinZ"]
        ok_fl = fail_l < 2 and not pump and not htf_dn[i] and zlo3 <= -p["fadeMinZ"]
        calib = n_touch[0] >= p["minSample"] and p_rev >= p["minProb"]

        fade_s = is_rot and ok_fs and zHi1 >= p["fadeZ"] and b.c < u2 and b.c < b.o and dlt < 0 and calib \
            and (b.c - vw[i]) / b.c * 100 >= p["minTgtPct"]
        fade_l = is_rot and ok_fl and zLo1 <= -p["fadeZ"] and b.c > l2 and b.c > b.o and dlt > 0 and calib \
            and (vw[i] - b.c) / b.c * 100 >= p["minTgtPct"]
        trend_l = tr_up and htf_up[i] and zLo <= 0.5 and z > 0.5 and b.c > b.o and dlt > 0 \
            and ((vw[i] + 2 * sd[i]) - b.c) / b.c * 100 >= p["minTgtPct"]
        trend_s = tr_dn and htf_dn[i] and zHi >= -0.5 and z < -0.5 and b.c < b.o and dlt < 0 \
            and (b.c - (vw[i] - 2 * sd[i])) / b.c * 100 >= p["minTgtPct"]

        if not (i - last_sig > 6):
            continue
        sig = None
        if fade_l:
            sig = (1, "fade")
        elif fade_s:
            sig = (-1, "fade")
        elif trend_l:
            sig = (1, "trend")
        elif trend_s:
            sig = (-1, "trend")
        if sig is None:
            continue
        last_sig = i

        can = (i - last_exit > p["cooldown"]) and i > pause_until and trades_day < p["maxDay"] and not day_block
        if not can:
            continue
        side, typ = sig
        stop = (min(b.l, bars[i - 1].l) - atr[i] * 0.3) if side == 1 else (max(b.h, bars[i - 1].h) + atr[i] * 0.3)
        dist = abs(b.c - stop)
        if dist <= 0 or dist / b.c * 100 > p["maxStopPct"]:
            continue
        tgt = vw[i] if typ == "fade" else (vw[i] + 2 * sd[i] if side == 1 else vw[i] - 2 * sd[i])
        rr = abs(tgt - b.c) / dist
        if rr < p["minRR"]:
            continue
        pending = dict(side=side, stop=stop, tgt=tgt, type=typ)

    if pos is not None:
        close_trade(bars[-1].c, n - 1)

    res.pnl_pct = (equity - 10_000) / 100
    res.max_dd_pct = max_dd
    res.rev_prob = n_rev[0] / n_touch[0] * 100 if n_touch[0] else None
    res.rev_sample = n_touch[0]

    # metricas de idoneidad
    last = bars[-288:]
    res.atr_pct = sum(atr[-288:]) / len(last) / (sum(b.c for b in last) / len(last)) * 100
    hc = [hour_close[h] for h in hours[-48:]]
    if len(hc) > 2:
        path = sum(abs(hc[k] - hc[k - 1]) for k in range(1, len(hc)))
        res.eff_ratio = abs(hc[-1] - hc[0]) / path if path > 0 else 0.0
    i = n - 1
    side_r = sum(above[-P["regLen"]:]) / P["regLen"]
    crosses = sum(cross[-P["regLen"]:])
    res.last_regime = "TEND↑" if side_r >= 0.8 and crosses <= 2 else "TEND↓" if side_r <= 0.2 and crosses <= 2 else "RANGO"
    return res


def score(r: Result):
    """Puntuacion: premia beneficio consistente con muestra suficiente y castiga drawdown."""
    if r.trades < MIN_TRADES:
        return -1.0
    pf = min(r.pf, 3.0)
    s = (pf - 1.0) * math.sqrt(r.trades)          # ventaja x muestra
    s -= max(r.max_dd_pct - 3.0, 0) * 0.3          # drawdown > 3 % penaliza
    if r.atr_pct < 0.25:
        s -= 0.5                                   # poca volatilidad: comisiones pesan
    return round(s, 3)


# ───────────────────────── Telegram ─────────────────────────
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        log.info("Telegram no configurado; mensaje:\n%s", text)
        return
    try:
        r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": "true"}, timeout=15)
        if r.status_code != 200:
            log.warning("Telegram %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram error: %s", e)


def fmt_report(ranked, scanned, took_s):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"<b>🔎 Z-LAB v2 · Mejores monedas (5m)</b>",
             f"<i>{now} · {scanned} escaneadas · {took_s:.0f}s</i>", ""]
    good = [r for r in ranked if r.score > 0]
    if not good:
        lines.append("Ninguna moneda con ventaja clara ahora mismo. Mejor no operar Z-LAB.")
    for k, r in enumerate(good[:TOP_N], 1):
        pf = "∞" if r.pf == float("inf") else f"{r.pf:.2f}"
        rp = "-" if r.rev_prob is None else f"{r.rev_prob:.0f}%"
        lines.append(
            f"<b>{k}. {r.symbol}</b>  score {r.score:.2f}\n"
            f"   PF {pf} · {r.trades} trades · win {r.winrate:.0f}% · PnL {r.pnl_pct:+.2f}% · DD {r.max_dd_pct:.1f}%\n"
            f"   ATR {r.atr_pct:.2f}% · efic {r.eff_ratio:.2f} · rev {rp} ({r.rev_sample}) · {r.last_regime} · vol {r.vol_usdt/1e6:.0f}M")
    bad = [r for r in ranked if r.trades >= MIN_TRADES and r.pf < 1.0][:5]
    if bad:
        lines += ["", "<b>⛔ Evitar (PF &lt; 1):</b> " + ", ".join(f"{r.symbol} ({r.pf:.2f})" for r in bad)]
    lines += ["", f"<i>Backtest {P['riskPct']}% riesgo · comisión {P['fee']*100:.2f}% · {PAGES*5} días. "
                  f"Resultados pasados, no garantía.</i>"]
    return "\n".join(lines)


# ───────────────────────── ciclo principal ─────────────────────────
def scan_once():
    t0 = time.time()
    tickers = get_tickers()
    time.sleep(REQ_PAUSE_S)
    def is_crypto(sym):
        base = sym.split("-")[0]
        # BingX lista acciones/materias primas tokenizadas como NCxxxx2USD-USDT: fuera
        return not (base.startswith("NC") and base.endswith("USD"))
    cands = [t for t in tickers if t[1] >= MIN_VOL_USDT and is_crypto(t[0])
             and t[0].split("-")[0] not in EXCLUDE and t[0] not in EXCLUDE]
    cands.sort(key=lambda t: t[1], reverse=True)
    if MAX_SYMBOLS > 0:
        cands = cands[:MAX_SYMBOLS]
    for gone in set(KCACHE) - {c[0] for c in cands}:
        KCACHE.pop(gone, None)
    log.info("%d candidatos (vol >= %.0f USDT)", len(cands), MIN_VOL_USDT)

    results = []
    for sym, qv, chg in cands:
        try:
            bars = get_klines(sym, PAGES)
            r = backtest(sym, bars)
            r.vol_usdt, r.chg24 = qv, chg
            r.score = score(r)
            results.append(r)
            log.info("%-14s trades=%2d PF=%5.2f pnl=%+6.2f%% dd=%4.1f%% score=%.2f",
                     sym, r.trades, min(r.pf, 99), r.pnl_pct, r.max_dd_pct, r.score)
        except Exception as e:
            log.exception("error en %s: %s", sym, e)

    ranked = sorted(results, key=lambda r: r.score, reverse=True)
    took = time.time() - t0
    try:
        with open(OUT_FILE, "w") as f:
            json.dump({"time": int(time.time()), "version": CODE_VERSION, "params": P,
                       "ranking": [dict(asdict(r), pf=(None if r.pf == float("inf") else r.pf), winrate=r.winrate)
                                   for r in ranked]}, f, indent=1, default=str)
    except Exception as e:
        log.warning("no se pudo guardar %s: %s", OUT_FILE, e)
    tg_send(fmt_report(ranked, len(results), took))
    return ranked


def main():
    log.info("arrancando %s · cada %d min · top %d · vol min %.0f", CODE_VERSION, SCAN_EVERY_MIN, TOP_N, MIN_VOL_USDT)
    tg_send(f"🤖 {CODE_VERSION} iniciado · escaneo cada {SCAN_EVERY_MIN} min")
    while True:
        try:
            scan_once()
        except Exception as e:
            log.exception("fallo en escaneo: %s", e)
            tg_send(f"⚠️ Z-LAB scanner: error en escaneo: {e}")
        if ONE_SHOT:
            break
        time.sleep(SCAN_EVERY_MIN * 60)


if __name__ == "__main__":
    main()
