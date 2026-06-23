"""
QF×JP Bot v7.9 — Trade Journal
═══════════════════════════════════════════════════════════════════════════════
FIX v7.9 — adaptive offset proporcional + techo + recovery gradual

  Problema en v7.8: _recalculate_adaptive() era BINARIA: WR<40% → +8,
  WR>65% → -5, resto → 0. Con MIN_SCORE=44 y max_score disponible=55,
  un offset de +8 sube el umbral efectivo a 52 — casi nada pasa. Peor:
  WR=39% recibe el mismo castigo que WR=10%, y una sola pérdida que baje
  el WR de 40.1% a 39.9% activa el máximo castigo de golpe.

  Consecuencia real observada en joyful-art: streak_breaker bloqueaba
  23+ señales por iteración aunque el mercado hubiera cambiado, porque
  el offset de +8 se mantenía indefinidamente mientras WR<40%.

  Fix:
  1. Offset PROPORCIONAL al WR real — escalonado, no binario
  2. Techo configurable (MAX_ADAPTIVE_OFFSET) — nunca bloquear todo
  3. Recovery gradual — el offset baja en steps de 1pt cada ciclo cuando
     WR mejora, en vez de saltar a -5 de golpe
  4. Streak_breaker intraday reset — una racha mala de ayer no debería
     penalizar el bot hoy si el mercado cambió
  5. Stats por dirección (LONG vs SHORT) — detecta si el bot pierde
     sistemáticamente en una dirección
  6. Ventana adaptativa — pocos trades cerrados → ventana más pequeña
     para no esperar 20 trades en bots de baja frecuencia

FIX v7.8 (sin cambios):
  ✅ filter_tags: mide si cada filtro aporta o no (win rate confirmado vs
     no confirmado), responde "¿este filtro mejora los resultados?".
  ✅ Persistencia a disco (requiere Volume en Railway).

DE v7.7 (sin cambios):
  ✅ Auto-blacklist por símbolo (3 pérdidas consecutivas → 24h bloqueo)
  ✅ Streak breaker global (5 pérdidas → pausa 1h)
  ✅ Umbral adaptativo básico
═══════════════════════════════════════════════════════════════════════════════
"""
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

import telegram_client as tg

log = logging.getLogger("journal")

_PERSIST_PATH = os.environ.get("JOURNAL_PERSIST_PATH", "/data/trade_journal.json")
_MAX_CLOSED_PERSISTED = 1000
_MAX_CLOSED_IN_MEMORY = 5000


@dataclass
class TradeRecord:
    symbol:      str
    direction:   str
    tier:        str
    score:       float
    fr:          float
    obi:         float
    oi_delta:    float
    htf_score:   float
    adx:         float
    hour_utc:    int
    opened_at:   float
    filter_tags: dict  = field(default_factory=dict)
    closed_at:   Optional[float] = None
    pnl:         Optional[float] = None
    won:         Optional[bool]  = None
    reason:      str             = ""


class TradeJournal:

    # ── Configuración del auto-blacklist ──────────────────────────────────────
    AUTO_BLACKLIST_MIN_TRADES   = 3
    AUTO_BLACKLIST_LOSS_STREAK  = 3
    AUTO_BLACKLIST_DURATION_S   = 86400

    # ── Streak breaker global ─────────────────────────────────────────────────
    STREAK_BREAKER_THRESHOLD    = 5
    STREAK_BREAKER_PAUSE_S      = 1800   # FIX v7.9: 30min en vez de 1h
                                          # (1h es demasiado largo para un scalper
                                          # que puede ver 10-20 trades al día)

    # ── Adaptive offset ───────────────────────────────────────────────────────
    # FIX v7.9: techo explícito para que nunca se paralice el bot
    MAX_ADAPTIVE_OFFSET         = 6.0    # antes implícitamente era 8 sin techo
    MIN_ADAPTIVE_OFFSET         = -4.0   # antes era -5, también demasiado agresivo

    # ── Win rate mínimo de trades por bucket (filtros) ────────────────────────
    MIN_TRADES_PER_FILTER_BUCKET = 8

    def __init__(self):
        self._open:   dict[str, TradeRecord] = {}
        self._closed: list[TradeRecord]      = []
        self._recent_wins:        list[bool]  = []
        self._adaptive_min_score: float       = 0.0
        self._symbol_pnl:         dict[str, float] = {}
        self._symbol_losses:      dict[str, int]   = {}
        self._auto_blacklist:     dict[str, float] = {}
        self._consecutive_losses: int   = 0
        self._streak_pause_until: float = 0.0
        # FIX v7.9: fecha del streak para reset intraday
        self._streak_date:        str   = ""
        log.info("TradeJournal v7.9 iniciado")
        self._load_state()

    # ── Persistencia ──────────────────────────────────────────────────────────

    def _load_state(self):
        if not os.path.exists(_PERSIST_PATH):
            log.info("[journal] sin estado previo — arrancando en limpio")
            return
        try:
            with open(_PERSIST_PATH, "r") as f:
                data = json.load(f)
            self._closed              = [TradeRecord(**r) for r in data.get("closed", [])]
            self._open                = {k: TradeRecord(**v) for k, v in data.get("open", {}).items()}
            self._recent_wins         = data.get("recent_wins", [])
            self._adaptive_min_score  = data.get("adaptive_min_score", 0.0)
            self._symbol_pnl          = data.get("symbol_pnl", {})
            self._symbol_losses       = data.get("symbol_losses", {})
            self._auto_blacklist      = data.get("auto_blacklist", {})
            self._consecutive_losses  = data.get("consecutive_losses", 0)
            self._streak_pause_until  = data.get("streak_pause_until", 0.0)
            self._streak_date         = data.get("streak_date", "")
            log.info(
                "[journal] estado cargado — %d cerrados, %d abiertos, "
                "%d auto-blacklist, adaptive_offset=%+.0f",
                len(self._closed), len(self._open),
                len(self._auto_blacklist), self._adaptive_min_score,
            )
        except Exception as e:
            log.warning("[journal] no se pudo cargar estado: %s — arrancando en limpio", e)

    def _save_state(self):
        try:
            dirpath = os.path.dirname(_PERSIST_PATH)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            data = {
                "closed":             [asdict(r) for r in self._closed[-_MAX_CLOSED_PERSISTED:]],
                "open":               {k: asdict(v) for k, v in self._open.items()},
                "recent_wins":        self._recent_wins,
                "adaptive_min_score": self._adaptive_min_score,
                "symbol_pnl":         self._symbol_pnl,
                "symbol_losses":      self._symbol_losses,
                "auto_blacklist":     self._auto_blacklist,
                "consecutive_losses": self._consecutive_losses,
                "streak_pause_until": self._streak_pause_until,
                "streak_date":        self._streak_date,
            }
            tmp = _PERSIST_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, _PERSIST_PATH)
        except Exception as e:
            log.warning("[journal] no se pudo guardar estado: %s", e)

    # ── Apertura ──────────────────────────────────────────────────────────────

    def on_open(
        self,
        symbol:      str,
        direction:   str,
        tier:        str,
        score:       float,
        fr:          float = 0.0,
        obi:         float = 0.0,
        oi_delta:    float = 0.0,
        htf_score:   float = 0.0,
        adx:         float = 0.0,
        filter_tags: Optional[dict] = None,
    ):
        rec = TradeRecord(
            symbol=symbol, direction=direction, tier=tier,
            score=score, fr=fr, obi=obi, oi_delta=oi_delta,
            htf_score=htf_score, adx=adx,
            hour_utc=time.gmtime().tm_hour,
            opened_at=time.time(),
            filter_tags=dict(filter_tags) if filter_tags else {},
        )
        self._open[symbol] = rec
        log.debug("[journal] abierto: %s %s score=%.1f filtros=%s",
                  symbol, direction, score, list(rec.filter_tags.keys()))
        self._save_state()

    # ── Cierre ────────────────────────────────────────────────────────────────

    async def on_close(self, symbol: str, pnl: float, reason: str = ""):
        rec = self._open.pop(symbol, None)
        if rec is None:
            return
        rec.closed_at = time.time()
        rec.pnl       = pnl
        rec.won       = pnl > 0
        rec.reason    = reason
        self._closed.append(rec)
        if len(self._closed) > _MAX_CLOSED_IN_MEMORY:
            self._closed = self._closed[-_MAX_CLOSED_IN_MEMORY:]

        # FIX v7.9: ventana adaptativa al número de trades disponibles
        # (mínimo 10, máximo 20). Bots de baja frecuencia no necesitan
        # esperar 20 trades para que el adaptativo reaccione.
        n_closed = len(self._closed)
        window = max(10, min(20, n_closed))
        self._recent_wins.append(rec.won)
        if len(self._recent_wins) > window:
            self._recent_wins.pop(0)

        # ── Auto-blacklist por símbolo ────────────────────────────────────────
        self._symbol_pnl[symbol] = self._symbol_pnl.get(symbol, 0.0) + pnl
        if rec.won:
            self._symbol_losses[symbol] = 0
        else:
            self._symbol_losses[symbol] = self._symbol_losses.get(symbol, 0) + 1
            n_trades_symbol = sum(1 for t in self._closed if t.symbol == symbol)
            if (n_trades_symbol >= self.AUTO_BLACKLIST_MIN_TRADES and
                    self._symbol_losses[symbol] >= self.AUTO_BLACKLIST_LOSS_STREAK and
                    symbol not in self._auto_blacklist):
                self._auto_blacklist[symbol] = time.time()
                log.warning(
                    "[journal] 🚫 AUTO-BLACKLIST: %s — %d pérdidas consecutivas "
                    "(PnL acumulado: %.4f) — bloqueado %dh",
                    symbol, self._symbol_losses[symbol],
                    self._symbol_pnl[symbol], self.AUTO_BLACKLIST_DURATION_S // 3600,
                )
                try:
                    await tg.notify_auto_blacklist(
                        symbol, self._symbol_losses[symbol],
                        self._symbol_pnl[symbol], self.AUTO_BLACKLIST_DURATION_S // 3600,
                    )
                except Exception:
                    pass

        # ── Circuit breaker por racha GLOBAL ──────────────────────────────────
        # FIX v7.9: reset de racha si es un día nuevo — una racha mala de
        # ayer en un mercado diferente no debería seguir penalizando hoy.
        today_str = str(date.today())
        if self._streak_date and self._streak_date != today_str:
            if self._consecutive_losses > 0:
                log.info("[journal] Nuevo día — reset de racha (%d pérdidas consecutivas de ayer)",
                         self._consecutive_losses)
            self._consecutive_losses = 0
            self._streak_pause_until = 0.0  # expirar cualquier pausa pendiente
        self._streak_date = today_str

        if rec.won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.STREAK_BREAKER_THRESHOLD:
                already_paused = time.time() < self._streak_pause_until
                self._streak_pause_until = time.time() + self.STREAK_BREAKER_PAUSE_S
                pause_min = self.STREAK_BREAKER_PAUSE_S // 60
                log.warning(
                    "[journal] ⏸️ STREAK BREAKER: %d pérdidas consecutivas — "
                    "pausa de %dmin",
                    self._consecutive_losses, pause_min,
                )
                if not already_paused:
                    try:
                        await tg.notify_streak_breaker(
                            self._consecutive_losses, pause_min,
                        )
                    except Exception:
                        pass

        self._recalculate_adaptive()
        log.info("[journal] cerrado: %s pnl=%.4f won=%s offset=%+.0f (total=%d)",
                 symbol, pnl, rec.won, self._adaptive_min_score, len(self._closed))
        self._save_state()

    # ── Auto-blacklist y streak queries ───────────────────────────────────────

    def is_symbol_auto_blacklisted(self, symbol: str) -> tuple[bool, str]:
        ts = self._auto_blacklist.get(symbol)
        if ts is None:
            return False, ""
        elapsed = time.time() - ts
        if elapsed > self.AUTO_BLACKLIST_DURATION_S:
            del self._auto_blacklist[symbol]
            self._symbol_losses[symbol] = 0
            return False, ""
        remaining_h = (self.AUTO_BLACKLIST_DURATION_S - elapsed) / 3600
        return True, f"auto_blacklist({symbol}, {remaining_h:.1f}h)"

    def is_streak_paused(self) -> tuple[bool, str]:
        if time.time() < self._streak_pause_until:
            remaining_min = (self._streak_pause_until - time.time()) / 60
            return True, f"streak_breaker({self._consecutive_losses} pérdidas, {remaining_min:.0f}min)"
        return False, ""

    # ── Adaptive offset — FIX v7.9 ────────────────────────────────────────────

    def _recalculate_adaptive(self):
        """
        FIX v7.9: offset PROPORCIONAL al WR real, con techo y recovery gradual.

        Antes (v7.8): binario — WR<40% → +8, WR>65% → -5, else → 0.
        Problema: WR=39.9% recibe el mismo castigo que WR=10%. Un solo
        trade malo que baje el WR de 40.1% a 39.9% activaba el máximo
        castigo de golpe. Y el offset de +8 con MIN_SCORE=44 sube el
        umbral efectivo a 52, que con un score máximo de ~55 prácticamente
        bloquea todo.

        Ahora:
        - Proporcional: el castigo escala con cuánto de malo es el WR
        - Techo: MAX_ADAPTIVE_OFFSET = 6 (nunca sube más de 6 puntos)
        - Recovery gradual: el offset baja de a 1pt por ciclo cuando WR
          mejora, no salta de +8 a -5 de golpe
        - Datos insuficientes: con <10 trades no actúa
        """
        n = len(self._recent_wins)
        if n < 10:
            self._adaptive_min_score = 0.0
            return

        wr = sum(1 for w in self._recent_wins if w) / n

        # Offset objetivo según WR
        if wr < 0.30:
            target = 6.0    # muy malo — castigo máximo (techo)
        elif wr < 0.38:
            target = 4.0    # malo
        elif wr < 0.45:
            target = 2.0    # por debajo del promedio
        elif wr < 0.55:
            target = 0.0    # normal — sin ajuste
        elif wr < 0.65:
            target = -2.0   # bueno — bajar umbral ligeramente
        else:
            target = -4.0   # muy bueno — mercado favorable

        # FIX: recovery y castigo GRADUAL — el offset se mueve 1pt por ciclo
        # hacia el target, nunca salta de golpe. Esto evita que una racha
        # de 1-2 pérdidas active el castigo máximo inmediatamente, y que
        # una racha buena baje el umbral demasiado rápido.
        current = self._adaptive_min_score
        if target > current:
            # subiendo (castigando) — moverse 1pt hacia el target
            new_offset = min(current + 1.0, target)
        elif target < current:
            # bajando (recuperando) — moverse 1pt hacia el target
            new_offset = max(current - 1.0, target)
        else:
            new_offset = target

        # Aplicar techo y suelo absolutos
        new_offset = max(self.MIN_ADAPTIVE_OFFSET, min(self.MAX_ADAPTIVE_OFFSET, new_offset))

        if new_offset != current:
            direction_str = "↑" if new_offset > current else "↓"
            log.info(
                "[journal] adaptive_offset %+.0f → %+.0f %s (wr=%.0f%%, target=%+.0f)",
                current, new_offset, direction_str, wr * 100, target,
            )
        self._adaptive_min_score = new_offset

    def get_adaptive_offset(self) -> float:
        return self._adaptive_min_score

    # ── Win rate por filtro ───────────────────────────────────────────────────

    def _filter_breakdown(self, closed: list[TradeRecord]) -> dict:
        all_filter_names: set[str] = set()
        for t in closed:
            all_filter_names.update(t.filter_tags.keys())

        out: dict[str, dict] = {}
        for fname in all_filter_names:
            confirmados    = [t for t in closed if fname in t.filter_tags]
            no_confirmados = [t for t in closed if fname not in t.filter_tags]

            def _bucket(group: list[TradeRecord]) -> dict:
                n = len(group)
                if n == 0:
                    return {"n": 0, "wr": None, "pnl": 0.0, "suficiente": False}
                w = sum(1 for t in group if t.won)
                return {
                    "n":         n,
                    "wr":        round(w / n * 100, 1),
                    "pnl":       round(sum(t.pnl or 0 for t in group), 4),
                    "suficiente": n >= self.MIN_TRADES_PER_FILTER_BUCKET,
                }

            b_c = _bucket(confirmados)
            b_n = _bucket(no_confirmados)
            veredicto = "datos_insuficientes"
            if b_c["suficiente"] and b_n["suficiente"]:
                diff = (b_c["wr"] or 0) - (b_n["wr"] or 0)
                veredicto = "aporta" if diff >= 10 else ("perjudica" if diff <= -10 else "sin_diferencia_clara")

            out[fname] = {"confirmado": b_c, "no_confirmado": b_n, "veredicto": veredicto}
        return out

    # ── Stats ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        closed = self._closed
        n = len(closed)
        if n == 0:
            return {"total": 0}

        wins   = sum(1 for t in closed if t.won)
        losses = n - wins
        wr     = wins / n
        total_pnl = sum(t.pnl for t in closed if t.pnl is not None)

        # Por tier
        by_tier: dict[str, dict] = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
        for t in closed:
            k = t.tier
            if t.won: by_tier[k]["w"] += 1
            else:     by_tier[k]["l"] += 1
            by_tier[k]["pnl"] += t.pnl or 0

        # FIX v7.9: stats por dirección (LONG vs SHORT)
        # Si el bot pierde sistemáticamente en una dirección, es señal
        # de que el scoring no está calibrado para ese lado del mercado.
        by_direction: dict[str, dict] = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
        for t in closed:
            d = t.direction
            if t.won: by_direction[d]["w"] += 1
            else:     by_direction[d]["l"] += 1
            by_direction[d]["pnl"] += t.pnl or 0

        # Por hora UTC
        by_hour: dict[int, dict] = defaultdict(lambda: {"w": 0, "l": 0})
        for t in closed:
            h = t.hour_utc
            if t.won: by_hour[h]["w"] += 1
            else:     by_hour[h]["l"] += 1

        def _wr_h(h):
            d = by_hour[h]
            tot = d["w"] + d["l"]
            return d["w"] / tot if tot >= 2 else -1

        best_hours = sorted(
            [h for h in by_hour if by_hour[h]["w"] + by_hour[h]["l"] >= 2],
            key=_wr_h, reverse=True
        )[:3]

        # Por símbolo
        by_sym: dict[str, float] = defaultdict(float)
        for t in closed:
            by_sym[t.symbol] += t.pnl or 0
        sym_sorted = sorted(by_sym.items(), key=lambda x: x[1], reverse=True)

        # Score medio de ganadores
        winning_scores = [t.score for t in closed if t.won]
        opt_score = sum(winning_scores) / len(winning_scores) if winning_scores else 0

        recent_wr = (
            sum(1 for w in self._recent_wins if w) / len(self._recent_wins)
            if self._recent_wins else 0
        )

        # FIX v7.9: alerta si el WR por dirección difiere mucho
        dir_alert = ""
        for d, data in by_direction.items():
            tot = data["w"] + data["l"]
            if tot >= 8:
                dwr = data["w"] / tot
                if dwr < 0.30:
                    dir_alert += f" ⚠️ {d} WR={dwr:.0%} — revisar scoring para esta dirección."

        return {
            "total":             n,
            "wins":              wins,
            "losses":            losses,
            "win_rate":          round(wr * 100, 1),
            "recent_wr":         round(recent_wr * 100, 1),
            "total_pnl":         round(total_pnl, 4),
            "opt_score":         round(opt_score, 1),
            "adaptive_offset":   self._adaptive_min_score,
            "consecutive_losses": self._consecutive_losses,
            "streak_paused":     time.time() < self._streak_pause_until,
            "by_tier": {
                k: {
                    "wr":  round(d["w"] / (d["w"] + d["l"]) * 100, 1) if (d["w"] + d["l"]) > 0 else 0,
                    "pnl": round(d["pnl"], 4),
                    "n":   d["w"] + d["l"],
                }
                for k, d in by_tier.items()
            },
            "by_direction": {    # FIX v7.9
                d: {
                    "wr":  round(data["w"] / (data["w"] + data["l"]) * 100, 1) if (data["w"] + data["l"]) > 0 else 0,
                    "pnl": round(data["pnl"], 4),
                    "n":   data["w"] + data["l"],
                }
                for d, data in by_direction.items()
            },
            "direction_alert":   dir_alert,
            "best_hours_utc":    best_hours,
            "top5_symbols":      sym_sorted[:5],
            "bot5_symbols":      sym_sorted[-5:][::-1],
            "by_filter":         self._filter_breakdown(closed),
        }

    def recent_win_rate(self) -> float:
        if not self._recent_wins:
            return 0.5
        return sum(1 for w in self._recent_wins if w) / len(self._recent_wins)

    def open_count(self) -> int:
        return len(self._open)

    def total_closed(self) -> int:
        return len(self._closed)
