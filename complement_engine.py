"""
QF×JP Bot — Complement Engine v1.2
joyful-art complementa a renewed-love con 4 modos simultáneos:

MODO 1 — COPY TRADE FILTRADO
  Lee trades SUP del master, copia con 0.4x size solo si score > 80.
  FIX v1.1: además exige que el trade del master NO esté ya en pérdida
  (mark vs entry favorable o neutro) antes de copiarlo.
  NUEVO v1.2: además exige que el trade del master vaya AL MENOS
  COPY_MIN_FAVORABLE_ATR * atr_estimado a favor (no solo "neutro o
  positivo") — filtra entradas recién abiertas que aún no han probado
  nada en su dirección.

MODO 2 — SÍMBOLOS EXCLUSIVOS (sin solapamiento)
  joyful-art opera SOLO en top-50 símbolos por volumen.
  renewed-love opera en los otros 514.
  Resultado: cobertura total sin duplicar posiciones.

MODO 3 — GUARDIAN DE SALIDAS
  v1.1: Monitoriza posiciones del MASTER. Si detecta CVD divergence
  contraria → alerta Telegram para cierre manual (no interfiere con el master).
  NUEVO v1.2: Monitoriza TAMBIÉN las posiciones PROPIAS de joyful-art.
  Si una posición propia va en ganancia (PnL% > 0) y se detecta
  divergencia CVD fuerte (CVD < GUARDIAN_CVD_THR * GUARDIAN_STRONG_MULT),
  ejecuta un CIERRE PARCIAL (GUARDIAN_PARTIAL_CLOSE_PCT, default 50%)
  vía position_manager.partial_close() — asegura ganancia y mueve el
  SL del remanente a breakeven. Esto SÍ actúa automáticamente, pero
  solo sobre posiciones PROPIAS (nunca sobre las del master).

MODO 4 — HEDGE MACRO
  v1.1: Si master tiene 3+ posiciones EN PÉRDIDA (direccional) >2% cada una
  → joyful-art abre SHORT/LONG en BTC como cobertura macro, todo-o-nada.
  NUEVO v1.2: HEDGE ESCALONADO. El tamaño del hedge ya no es fijo
  (~50 USDT) sino que escala con el número de posiciones master en
  pérdida real, vía tabla HEDGE_TIERS:
    2 posiciones → ~30 USDT notional
    3 posiciones → ~50 USDT notional
    4+ posiciones → ~80 USDT notional
  Si el hedge ya está activo y el número de posiciones en pérdida
  sube/baja de tier, AJUSTA el tamaño (abre/cierra notional adicional)
  en vez de cerrar y reabrir — reduce comisiones por flapping.

═══════════════════════════════════════════════════════════════════════════════
CAMBIOS v1.2 (resumen):
  ✅ COPY_MIN_FAVORABLE_ATR (default 0.5): copy mode exige que el master
     vaya >= 0.5 ATR a favor (estimado desde sl/entry), no solo >=0%.
  ✅ HEDGE_TIERS: hedge escalonado por número de posiciones en pérdida,
     con ajuste dinámico de tamaño (sin cerrar/reabrir innecesariamente).
  ✅ GUARDIAN_AUTOCLOSE_OWN (default True), GUARDIAN_PARTIAL_CLOSE_PCT
     (default 0.5), GUARDIAN_STRONG_MULT (default 1.5): guardian sobre
     posiciones propias con cierre parcial automático en divergencia fuerte.

  (FIXES v1.1 conservados: hedge direccional, copy mode anti-pérdida,
   guardian de alertas sobre el master)
═══════════════════════════════════════════════════════════════════════════════
"""
import asyncio
import logging
import os
import time
from typing import Optional

import numpy as np

import config as C
from bingx_client import BingXClient
from copier_client import MasterClient
from indicators import analyze, score_to_tier
from risk_manager import RiskManager
from position_manager import PositionManager, OpenTrade
import telegram_client as tg

log = logging.getLogger("complement")

# ── Config complemento ────────────────────────────────────────────────────────
COMPLEMENT_MODE   = os.getenv("COMPLEMENT_MODE", "GUARDIAN,COPY,EXCLUSIVE").upper()
COPY_MIN_SCORE    = float(os.getenv("COPY_MIN_SCORE",    "80.0"))   # solo SUP del master
COPY_SIZE_MULT    = float(os.getenv("COPY_SIZE_MULT",    "0.4"))    # 40% del size del master
# FIX v1.1: no copiar si el trade del master ya está en pérdida (PnL% con signo)
COPY_MAX_ADVERSE_PCT = float(os.getenv("COPY_MAX_ADVERSE_PCT", "0.0"))  # 0.0 = solo copiar si va igual o a favor
# NUEVO v1.2: además, exigir que vaya al menos N * ATR a favor (no solo >=0%)
COPY_MIN_FAVORABLE_ATR = float(os.getenv("COPY_MIN_FAVORABLE_ATR", "0.5"))

GUARDIAN_CVD_THR  = float(os.getenv("GUARDIAN_CVD_THR", "-0.3"))   # CVD divergencia mínima (alertas master)
# NUEVO v1.2 — guardian sobre posiciones propias
GUARDIAN_AUTOCLOSE_OWN     = os.getenv("GUARDIAN_AUTOCLOSE_OWN", "true").strip().lower() in ("1", "true", "yes")
GUARDIAN_PARTIAL_CLOSE_PCT = float(os.getenv("GUARDIAN_PARTIAL_CLOSE_PCT", "0.5"))   # 50% por defecto
GUARDIAN_STRONG_MULT       = float(os.getenv("GUARDIAN_STRONG_MULT", "1.5"))        # divergencia "fuerte" = THR * 1.5

HEDGE_LOSS_COUNT  = int(os.getenv("HEDGE_LOSS_COUNT",   "3"))       # umbral legacy / fallback
HEDGE_LOSS_PCT    = float(os.getenv("HEDGE_LOSS_PCT",   "2.0"))     # % pérdida por trade (positivo)
EXCLUSIVE_TOP_N   = int(os.getenv("EXCLUSIVE_TOP_N",    "50"))      # top N símbolos exclusivos

# NUEVO v1.2 — tabla de hedge escalonado: (min_posiciones_en_perdida, notional_usdt)
# Se evalúa de mayor a menor; el primer umbral que se cumple determina el notional objetivo.
# 0 o 1 posiciones en pérdida → sin hedge (notional objetivo 0 → hedge cerrado/no abierto)
HEDGE_TIERS: list[tuple[int, float]] = [
    (4, 80.0),
    (3, 50.0),
    (2, 30.0),
]


class ComplementEngine:
    def __init__(self, client: BingXClient, risk: RiskManager,
                 pos_mgr: PositionManager, master: MasterClient):
        self.client  = client
        self.risk    = risk
        self.pos_mgr = pos_mgr
        self.master  = master

        self._copied_trades:  set[str]   = set()   # símbolos ya copiados
        self._exclusive_syms: list[str]  = []      # top-50 por volumen
        self._last_guardian:  float      = 0.0
        self._last_copy:      float      = 0.0
        self._last_hedge:     float      = 0.0

        # ── v1.2: hedge escalonado — tamaño objetivo actual (0 = sin hedge) ───
        self._hedge_notional: float       = 0.0

        # ── v1.2: anti-spam de cierres parciales por guardian propio ─────────
        self._guardian_partial_done: set[str] = set()

    # ══════════════════════════════════════════════════════════════════════════
    # MODO 2 — SÍMBOLOS EXCLUSIVOS
    # ══════════════════════════════════════════════════════════════════════════

    async def refresh_exclusive_symbols(self):
        """
        joyful-art opera SOLO en los top-N símbolos por volumen 24h.
        renewed-love opera en el resto.
        → Sin solapamiento, cobertura total del mercado.
        """
        try:
            all_syms = await self.client.get_all_symbols()
            # get_all_symbols ya devuelve ordenados por volumen (ver bingx_client)
            self._exclusive_syms = all_syms[:EXCLUSIVE_TOP_N]
            log.info("Símbolos exclusivos joyful-art: %d (top-%d por volumen)",
                     len(self._exclusive_syms), EXCLUSIVE_TOP_N)
        except Exception as e:
            log.warning("refresh_exclusive_symbols: %s", e)

    def get_exclusive_symbols(self) -> list[str]:
        return self._exclusive_syms

    # ══════════════════════════════════════════════════════════════════════════
    # MODO 1 — COPY TRADE FILTRADO
    # ══════════════════════════════════════════════════════════════════════════

    def _master_trade_pnl_pct(self, trade_data: dict, mark: float) -> Optional[float]:
        """
        Calcula el PnL% direccional del trade del master, dado el mark actual.
        Retorna None si faltan datos.
        Positivo = a favor, Negativo = en contra.
        """
        entry     = float(trade_data.get("entry", 0))
        direction = trade_data.get("direction", "")
        if entry <= 0 or mark <= 0 or direction not in ("LONG", "SHORT"):
            return None
        raw_pct = (mark - entry) / entry * 100.0
        if direction == "SHORT":
            raw_pct = -raw_pct
        return raw_pct

    def _master_trade_favorable_atr(self, trade_data: dict, mark: float) -> Optional[float]:
        """
        NUEVO v1.2: cuántos "ATR" lleva el trade del master a favor,
        estimando el ATR del trade a partir de |entry - sl| / SL_ATR_MULT
        (config.SL_ATR_MULT es el múltiplo usado para fijar el SL original,
        así que atr_estimado = |entry - sl| / SL_ATR_MULT).

        Retorna None si faltan datos o atr_estimado <= 0.
        Positivo = a favor en unidades de ATR.
        """
        entry     = float(trade_data.get("entry", 0))
        sl        = float(trade_data.get("sl", 0))
        direction = trade_data.get("direction", "")
        if entry <= 0 or sl <= 0 or mark <= 0 or direction not in ("LONG", "SHORT"):
            return None

        sl_mult = getattr(C, "SL_ATR_MULT", 2.0) or 2.0
        atr_est = abs(entry - sl) / sl_mult
        if atr_est <= 0:
            return None

        if direction == "LONG":
            favorable_dist = mark - entry
        else:  # SHORT
            favorable_dist = entry - mark

        return favorable_dist / atr_est

    async def run_copy_mode(self):
        """
        Copia trades SUP del master con 40% del size.
        Solo copia si:
          - Tier SUP (score > 80) en el master
          - joyful-art no está ya en ese símbolo
          - joyful-art tiene slots disponibles
          - El símbolo NO está en los exclusivos de joyful-art
            (para no duplicar análisis propios)
          - FIX v1.1: el trade del master NO está ya en pérdida
            (pnl_pct del master >= COPY_MAX_ADVERSE_PCT, default 0%)
          - NUEVO v1.2: el trade del master va >= COPY_MIN_FAVORABLE_ATR
            ATR a favor (default 0.5 ATR) — no solo "neutro o positivo".
            Esto filtra señales SUP recién abiertas que aún no han
            demostrado nada en su dirección.
        """
        now = time.time()
        if now - self._last_copy < 30:   # revisar cada 30s
            return
        self._last_copy = now

        master_trades = await self.master.get_master_trades()
        if not master_trades:
            return

        can, reason = await self.risk.can_trade()
        if not can:
            return

        for symbol, trade_data in master_trades.items():
            # Solo copiar tier SUP
            if trade_data.get("tier", "") != "SUP":
                continue
            if symbol in self._copied_trades:
                continue
            if self.pos_mgr.is_trading(symbol):
                continue
            # No copiar si es símbolo exclusivo propio (joyful-art lo analizará solo)
            if symbol in self._exclusive_syms:
                continue

            direction = trade_data.get("direction", "")
            entry     = float(trade_data.get("entry", 0))
            sl        = float(trade_data.get("sl",    0))
            tp1       = float(trade_data.get("tp1",   0))
            tp2       = float(trade_data.get("tp2",   0))

            if not direction or entry <= 0 or sl <= 0:
                continue

            # ── FIX v1.1: verificar que el trade del master no esté ya perdiendo ──
            try:
                ticker = await self.client.get_ticker(symbol)
                mark   = float(ticker.get("lastPrice", 0) or 0)
            except Exception as e:
                log.debug("[COPY] %s no se pudo obtener mark: %s", symbol, e)
                continue

            pnl_pct = self._master_trade_pnl_pct(trade_data, mark)
            if pnl_pct is None:
                continue
            if pnl_pct < COPY_MAX_ADVERSE_PCT:
                log.info("[COPY] %s SUP pero master ya en pérdida (%.2f%% < %.2f%%) — skip",
                         symbol, pnl_pct, COPY_MAX_ADVERSE_PCT)
                continue

            # ── NUEVO v1.2: exigir mínimo de ATR a favor ─────────────────────
            fav_atr = self._master_trade_favorable_atr(trade_data, mark)
            if fav_atr is None:
                log.debug("[COPY] %s no se pudo estimar ATR a favor — skip", symbol)
                continue
            if fav_atr < COPY_MIN_FAVORABLE_ATR:
                log.info("[COPY] %s SUP pero solo %.2f ATR a favor (< %.2f requerido) — skip "
                         "(señal aún sin validar)",
                         symbol, fav_atr, COPY_MIN_FAVORABLE_ATR)
                continue

            # Tamaño: 40% del master pero respetando nuestro propio cap
            master_qty = float(trade_data.get("qty", 0))
            qty = master_qty * COPY_SIZE_MULT

            # Verificar cap notional propio
            notional = qty * entry
            if notional > C.MAX_NOTIONAL_USDT:
                qty = C.MAX_NOTIONAL_USDT / entry

            if qty <= 0:
                continue

            log.info("[COPY] %s %s qty=%.4f (40%% del master, master_pnl=%.2f%%, %.2f ATR a favor) notional=%.1f",
                     symbol, direction, qty, pnl_pct, fav_atr, qty * entry)

            try:
                results = await self.client.open_trade(
                    symbol=symbol, direction=direction, quantity=qty,
                    sl_price=sl, tp1_price=tp1, tp2_price=tp2,
                )
                entry_resp = results.get("entry", {})
                if entry_resp.get("code", -1) == 0:
                    sl_resp = results.get("sl", {})
                    if isinstance(sl_resp, dict) and sl_resp.get("code", -1) != 0:
                        log.error("[COPY] %s SL fallido — cerrando", symbol)
                        await self.client.close_position_market(symbol, qty, direction)
                        continue

                    self._copied_trades.add(symbol)
                    trade = OpenTrade(
                        symbol=symbol, direction=direction,
                        entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                        qty=qty, atr=abs(entry - sl) / 2,
                        order_id="copy_" + symbol,
                    )
                    await self.pos_mgr.register_trade(trade)
                    await tg.send(
                        f"📋 *COPY TRADE* — `{symbol}` {direction}\n"
                        f"Master SUP, {fav_atr:.2f} ATR a favor → joyful-art 40% "
                        f"(master PnL: {pnl_pct:+.2f}%)\n"
                        f"Entry: `{entry:.6f}` | SL: `{sl:.6f}`\n"
                        f"Qty: `{qty:.4f}` notional: `{qty*entry:.1f}` USDT"
                    )
                    await self.risk.on_trade_opened(symbol=symbol)
                else:
                    log.warning("[COPY] %s entrada rechazada: %s", symbol, entry_resp)
            except Exception as e:
                log.error("[COPY] %s error: %s", symbol, e)

            await asyncio.sleep(0.5)

    # ══════════════════════════════════════════════════════════════════════════
    # MODO 3 — GUARDIAN DE SALIDAS
    # ══════════════════════════════════════════════════════════════════════════

    def _cvd_divergence(self, klines: np.ndarray, direction: str, period: int = 10):
        """
        Calcula divergencia CVD vs precio sobre las últimas `period` velas.
        Retorna (danger: bool, reason: str, cvd_chg: float, price_chg: float)
        danger=True si hay divergencia EN CONTRA de `direction`.
        """
        o_ = klines[:, 1]
        c_ = klines[:, 4]
        v_ = klines[:, 5]

        delta = np.where(c_ > o_, v_, np.where(c_ < o_, -v_, 0))
        cvd   = np.cumsum(delta)

        price_chg = c_[-1] - c_[-period]
        cvd_chg   = cvd[-1] - cvd[-period]

        danger = False
        reason = ""
        if direction == "LONG" and price_chg > 0 and cvd_chg < 0:
            danger = True
            reason = f"CVD divergencia bajista (precio +{price_chg:.4f}, CVD {cvd_chg:.0f})"
        elif direction == "SHORT" and price_chg < 0 and cvd_chg > 0:
            danger = True
            reason = f"CVD divergencia alcista (precio {price_chg:.4f}, CVD +{cvd_chg:.0f})"

        return danger, reason, cvd_chg, price_chg

    async def run_guardian_mode(self):
        """
        v1.1: Monitoriza posiciones del MASTER. Si detecta CVD divergence
        contraria a la posición → alerta Telegram. El trader decide si
        cerrar o no (no interferimos con el master).

        NUEVO v1.2: Además, monitoriza las posiciones PROPIAS de joyful-art.
        Si una posición propia va en ganancia (PnL% > 0) y se detecta
        divergencia CVD "fuerte" (cvd_chg cruza GUARDIAN_CVD_THR * GUARDIAN_STRONG_MULT
        en magnitud) → ejecuta cierre parcial automático via
        position_manager.partial_close() (toma ganancia + SL del resto a breakeven).
        Esto SOLO actúa sobre posiciones propias, nunca sobre el master.
        """
        now = time.time()
        if now - self._last_guardian < 60:   # revisar cada 60s
            return
        self._last_guardian = now

        alerts = []

        # ── Parte A: posiciones del MASTER (alertas, igual que v1.1) ─────────
        master_trades = await self.master.get_master_trades()
        for symbol, trade_data in (master_trades or {}).items():
            direction = trade_data.get("direction", "")
            entry     = float(trade_data.get("entry", 0))
            if not direction or entry <= 0:
                continue

            try:
                klines = await self.client.get_klines(symbol, "3m", 50)
                if len(klines) < 20:
                    continue
                arr = np.array(klines, dtype=float)

                danger, reason, cvd_chg, price_chg = self._cvd_divergence(arr, direction)
                if danger:
                    current_price = float(arr[-1, 4])
                    pnl_pct = self._master_trade_pnl_pct(trade_data, current_price)
                    pnl_pct = pnl_pct if pnl_pct is not None else 0.0
                    alerts.append(
                        f"⚠️ *GUARDIAN (master)* — `{symbol}` {direction}\n"
                        f"Precio actual: `{current_price:.6f}`\n"
                        f"PnL est: `{pnl_pct:+.2f}%`\n"
                        f"⚡ {reason}\n"
                        f"_Considera cerrar en renewed-love_"
                    )
            except Exception as e:
                log.debug("[GUARDIAN/master] %s: %s", symbol, e)

            await asyncio.sleep(0.1)

        if alerts:
            for alert in alerts[:3]:
                await tg.send(alert)
                await asyncio.sleep(1)

        # ── Parte B (v1.2): posiciones PROPIAS de joyful-art ─────────────────
        if not GUARDIAN_AUTOCLOSE_OWN:
            return

        own_trades = self.pos_mgr.get_tracked()
        strong_thr = GUARDIAN_CVD_THR * GUARDIAN_STRONG_MULT  # ej. -0.3 * 1.5 = -0.45

        for symbol, trade in own_trades.items():
            if symbol in self._guardian_partial_done:
                continue
            if trade.partial_closed:
                self._guardian_partial_done.add(symbol)
                continue

            try:
                ticker = await self.client.get_ticker(symbol)
                mark   = float(ticker.get("lastPrice", 0) or 0)
            except Exception:
                continue
            if mark <= 0:
                continue

            pnl_pct = self.pos_mgr.get_pnl_pct(symbol, mark)
            if pnl_pct is None or pnl_pct <= 0:
                continue   # solo actuamos si la posición propia va en ganancia

            try:
                klines = await self.client.get_klines(symbol, "3m", 50)
                if len(klines) < 20:
                    continue
                arr = np.array(klines, dtype=float)
                danger, reason, cvd_chg, price_chg = self._cvd_divergence(arr, trade.direction)
            except Exception as e:
                log.debug("[GUARDIAN/own] %s: %s", symbol, e)
                continue

            if not danger:
                continue

            # "Fuerte" = magnitud de cvd_chg supera GUARDIAN_CVD_THR * GUARDIAN_STRONG_MULT
            # Normalizamos cvd_chg a una escala comparable usando el propio volumen
            # de la ventana: cvd_norm = cvd_chg / sum(v_) de las últimas `period` velas.
            v_period = arr[-10:, 5]
            vol_sum  = float(np.sum(v_period)) or 1.0
            cvd_norm = cvd_chg / vol_sum

            if abs(cvd_norm) < abs(strong_thr):
                # Divergencia presente pero no "fuerte" — solo loguear, no actuar
                log.debug("[GUARDIAN/own] %s divergencia leve (cvd_norm=%.4f, umbral=%.4f) — sin acción",
                          symbol, cvd_norm, strong_thr)
                continue

            log.info("[GUARDIAN/own] %s PnL=%.2f%% + divergencia FUERTE (%s) → cierre parcial %.0f%%",
                     symbol, pnl_pct, reason, GUARDIAN_PARTIAL_CLOSE_PCT * 100)

            ok = await self.pos_mgr.partial_close(
                symbol, GUARDIAN_PARTIAL_CLOSE_PCT,
                reason=f"guardian_cvd_divergence({reason})"
            )
            if ok:
                self._guardian_partial_done.add(symbol)

            await asyncio.sleep(0.2)

    # ══════════════════════════════════════════════════════════════════════════
    # MODO 4 — HEDGE MACRO (v1.2: escalonado)
    # ══════════════════════════════════════════════════════════════════════════

    def _target_hedge_notional(self, total_losing: int) -> float:
        """
        NUEVO v1.2: devuelve el notional objetivo del hedge según el
        número de posiciones master en pérdida real (>= HEDGE_LOSS_PCT).
        0.0 = sin hedge (0 o 1 posiciones en pérdida).
        """
        for min_count, notional in HEDGE_TIERS:
            if total_losing >= min_count:
                return min(notional, C.MAX_NOTIONAL_USDT)  # nunca exceder cap propio
        return 0.0

    async def run_hedge_mode(self):
        """
        v1.1: Si master tiene posiciones EN PÉRDIDA (direccional) >2%
        simultáneas, joyful-art abre/ajusta una posición de cobertura
        macro en BTCUSDT.

        NUEVO v1.2 — HEDGE ESCALONADO:
          - El tamaño objetivo del hedge se calcula con _target_hedge_notional()
            según HEDGE_TIERS (2→30, 3→50, 4+→80 USDT, capado por MAX_NOTIONAL_USDT).
          - Si no hay hedge activo y target > 0 → abrir hedge con ese notional.
          - Si hedge activo y target == 0 → cerrar hedge (recuperación total).
          - Si hedge activo y target != notional actual → AJUSTAR tamaño:
              * target > actual → abrir notional adicional en la misma dirección
                (ampliar cobertura)
              * target < actual → cerrar parcialmente para reducir notional
            En vez de cerrar/reabrir todo, lo que reduce comisiones por flapping.
          - Si el sesgo de mercado cambia de dirección (más longs perdiendo
            ↔ más shorts perdiendo) entre ajustes, se cierra el hedge viejo
            y se abre uno nuevo en la dirección correcta (no tiene sentido
            "ajustar" un hedge en la dirección equivocada).
        """
        now = time.time()
        if now - self._last_hedge < 120:
            return
        self._last_hedge = now

        master_trades = await self.master.get_master_trades()
        if not master_trades:
            return

        try:
            positions = await self.client.get_open_positions()
        except Exception:
            return

        pos_map = {p["symbol"]: p for p in positions if float(p.get("positionAmt", 0)) != 0}

        losing_longs  = []
        losing_shorts = []

        for sym, td in master_trades.items():
            pos = pos_map.get(sym)
            if not pos:
                continue
            mark = float(pos.get("markPrice", 0) or 0)
            if mark <= 0:
                continue

            pnl_pct = self._master_trade_pnl_pct(td, mark)
            if pnl_pct is None:
                continue

            direction = td.get("direction", "")

            # Solo cuenta como "en pérdida" si pnl_pct <= -HEDGE_LOSS_PCT
            if pnl_pct <= -HEDGE_LOSS_PCT:
                if direction == "LONG":
                    losing_longs.append((sym, pnl_pct))
                elif direction == "SHORT":
                    losing_shorts.append((sym, pnl_pct))

        total_losing = len(losing_longs) + len(losing_shorts)
        target_notional = self._target_hedge_notional(total_losing)

        # Dirección deseada del hedge según el sesgo actual del drawdown
        # Más LONGs perdiendo (mercado cae) → hedge SHORT
        # Más SHORTs perdiendo (mercado sube) → hedge LONG
        desired_dir = "SHORT" if len(losing_longs) >= len(losing_shorts) else "LONG"

        hedge_open = self.pos_mgr.is_trading("BTCUSDT")
        current_notional = self._hedge_notional if hedge_open else 0.0

        # ── Caso 1: sin hedge y sin necesidad → nada que hacer ───────────────
        if not hedge_open and target_notional <= 0:
            return

        # ── Caso 2: hedge activo pero ya no se necesita → cerrar ─────────────
        if hedge_open and target_notional <= 0:
            log.info("[HEDGE] Drawdown recuperado (total_losing=%d) — cerrando hedge BTCUSDT",
                     total_losing)
            await self.pos_mgr.close_position_emergency("BTCUSDT", "hedge_exit")
            self._hedge_notional = 0.0
            return

        can, reason = await self.risk.can_trade()

        # ── Caso 3: sin hedge y se necesita → abrir con target_notional ──────
        if not hedge_open and target_notional > 0:
            if not can:
                return
            await self._open_hedge(desired_dir, target_notional, total_losing,
                                    losing_longs, losing_shorts)
            return

        # ── Caso 4: hedge activo, target distinto al actual ──────────────────
        if hedge_open and target_notional > 0:
            existing = self.pos_mgr.get_tracked().get("BTCUSDT")
            existing_dir = existing.direction if existing else desired_dir

            # 4a. Sesgo cambió de dirección → cerrar y reabrir en dirección correcta
            if existing_dir != desired_dir:
                log.info("[HEDGE] Sesgo cambió (%s→%s, total_losing=%d) — "
                         "cerrando hedge y reabriendo en nueva dirección",
                         existing_dir, desired_dir, total_losing)
                await self.pos_mgr.close_position_emergency("BTCUSDT", "hedge_direction_flip")
                self._hedge_notional = 0.0
                if not can:
                    return
                await self._open_hedge(desired_dir, target_notional, total_losing,
                                        losing_longs, losing_shorts)
                return

            # 4b. Misma dirección, tamaño objetivo distinto → ajustar
            diff = target_notional - current_notional
            if abs(diff) < 5.0:   # cambios <5 USDT no justifican ajuste (ruido)
                return

            if diff > 0:
                # Ampliar cobertura: abrir notional adicional en la misma dirección
                if not can:
                    return
                log.info("[HEDGE] Escalando cobertura %.0f→%.0f USDT (total_losing=%d) — "
                         "ampliando %s BTCUSDT en +%.0f USDT",
                         current_notional, target_notional, total_losing, existing_dir, diff)
                await self._adjust_hedge_open_more(existing_dir, diff)
            else:
                # Reducir cobertura: cierre parcial proporcional
                pct = min(0.95, abs(diff) / current_notional) if current_notional > 0 else 0
                if pct <= 0:
                    return
                log.info("[HEDGE] Reduciendo cobertura %.0f→%.0f USDT (total_losing=%d) — "
                         "cierre parcial %.0f%% de BTCUSDT",
                         current_notional, target_notional, total_losing, pct * 100)
                ok = await self.pos_mgr.partial_close(
                    "BTCUSDT", pct, reason=f"hedge_downscale(total_losing={total_losing})"
                )
                if ok:
                    self._hedge_notional = max(0.0, current_notional - abs(diff))
                    # partial_close marca trade.partial_closed=True; resetear para
                    # permitir futuros ajustes del hedge
                    existing2 = self.pos_mgr.get_tracked().get("BTCUSDT")
                    if existing2:
                        existing2.partial_closed = False

    async def _open_hedge(self, hedge_dir: str, notional: float, total_losing: int,
                           losing_longs: list, losing_shorts: list):
        """Abre el hedge inicial en BTCUSDT con el notional objetivo."""
        try:
            ticker = await self.client.get_ticker("BTCUSDT")
            btc_price = float(ticker.get("lastPrice", 0))
            if btc_price <= 0:
                return

            hedge_qty = notional / btc_price

            sl_pct  = 0.015  # 1.5% SL en BTC
            tp1_pct = 0.02   # 2% TP

            if hedge_dir == "SHORT":
                sl  = btc_price * (1 + sl_pct)
                tp1 = btc_price * (1 - tp1_pct)
                tp2 = btc_price * (1 - tp1_pct * 2)
            else:
                sl  = btc_price * (1 - sl_pct)
                tp1 = btc_price * (1 + tp1_pct)
                tp2 = btc_price * (1 + tp1_pct * 2)

            results = await self.client.open_trade(
                symbol="BTCUSDT", direction=hedge_dir, quantity=hedge_qty,
                sl_price=sl, tp1_price=tp1, tp2_price=tp2,
            )
            if results.get("entry", {}).get("code", -1) == 0:
                self._hedge_notional = notional
                trade = OpenTrade(
                    symbol="BTCUSDT", direction=hedge_dir,
                    entry=btc_price, sl=sl, tp1=tp1, tp2=tp2,
                    qty=hedge_qty, atr=btc_price * 0.01,
                    order_id="hedge_btc",
                )
                await self.pos_mgr.register_trade(trade)
                await tg.send(
                    f"🛡️ *HEDGE MACRO* activado (escalonado)\n"
                    f"BTCUSDT {hedge_dir} — {total_losing} posiciones master en pérdida real "
                    f"(longs={len(losing_longs)}, shorts={len(losing_shorts)})\n"
                    f"Notional: `{notional:.0f}` USDT | SL: `{sl:.0f}`"
                )
        except Exception as e:
            log.error("[HEDGE] _open_hedge error: %s", e)

    async def _adjust_hedge_open_more(self, hedge_dir: str, extra_notional: float):
        """
        Abre `extra_notional` USDT adicionales en BTCUSDT, misma dirección
        que el hedge existente, y suma la qty al OpenTrade trackeado
        (promediando entry) para mantener una sola posición lógica.
        """
        try:
            ticker = await self.client.get_ticker("BTCUSDT")
            btc_price = float(ticker.get("lastPrice", 0))
            if btc_price <= 0:
                return

            extra_qty = extra_notional / btc_price

            results = await self.client.open_trade(
                symbol="BTCUSDT", direction=hedge_dir, quantity=extra_qty,
                sl_price=0, tp1_price=0, tp2_price=0,  # ampliación: sin nuevos SL/TP propios
            )
            if results.get("entry", {}).get("code", -1) == 0:
                existing = self.pos_mgr.get_tracked().get("BTCUSDT")
                if existing:
                    total_qty = existing.qty + extra_qty
                    # Promediar entry ponderado
                    existing.entry = (existing.entry * existing.qty + btc_price * extra_qty) / total_qty
                    existing.qty   = total_qty
                self._hedge_notional += extra_notional
                await tg.send(
                    f"🛡️ *HEDGE MACRO* ampliado — `+{extra_notional:.0f}` USDT "
                    f"{hedge_dir} BTCUSDT (total ≈ `{self._hedge_notional:.0f}` USDT)"
                )
            else:
                log.warning("[HEDGE] _adjust_hedge_open_more: entrada rechazada: %s", results)
        except Exception as e:
            log.error("[HEDGE] _adjust_hedge_open_more error: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # LOOP PRINCIPAL
    # ══════════════════════════════════════════════════════════════════════════

    async def run_loop(self):
        log.info("Complement Engine v1.2 iniciado — modos: %s | "
                 "COPY_MIN_FAVORABLE_ATR=%.2f | HEDGE_TIERS=%s | GUARDIAN_AUTOCLOSE_OWN=%s",
                 COMPLEMENT_MODE, COPY_MIN_FAVORABLE_ATR, HEDGE_TIERS, GUARDIAN_AUTOCLOSE_OWN)

        # Refresh inicial de símbolos exclusivos
        await self.refresh_exclusive_symbols()

        iteration = 0
        while True:
            iteration += 1

            # Refresh símbolos cada 30 iteraciones
            if iteration % 30 == 0:
                await self.refresh_exclusive_symbols()

            try:
                if "COPY" in COMPLEMENT_MODE and os.getenv("MASTER_URL"):
                    await self.run_copy_mode()

                if "GUARDIAN" in COMPLEMENT_MODE:
                    # Parte A (alertas master) requiere MASTER_URL.
                    # Parte B (cierre parcial propio) NO requiere MASTER_URL,
                    # pero run_guardian_mode comprueba MASTER_URL solo para
                    # la parte A internamente vía master.get_master_trades()
                    # (devuelve {} si no hay master configurado).
                    await self.run_guardian_mode()

                if "HEDGE" in COMPLEMENT_MODE and os.getenv("MASTER_URL"):
                    await self.run_hedge_mode()

            except Exception as e:
                log.error("complement_loop error: %s", e)

            await asyncio.sleep(30)
