"""
QF Scanner — Escanea TODOS los pares de BingX en paralelo
Busca las mejores entradas LONG y SHORT usando el motor de señales
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional
import aiohttp
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    symbol:     str
    direction:  str    # LONG | SHORT
    tier:       str    # SUPREMA | FUEL | STD | HUNT_LONG | HUNT_SHORT
    conviction: int
    score:      float
    decay:      float
    entry:      float
    sl:         float
    tp:         float
    volume_24h: float
    details:    dict

    @property
    def rank_score(self) -> float:
        """Puntuación compuesta para ranking"""
        tier_bonus = {
            "SUPREMA":    100,
            "FUEL":        70,
            "STD":         50,
            "HUNT_LONG":   30,
            "HUNT_SHORT":  30,
        }.get(self.tier, 0)
        return tier_bonus + self.conviction * 5 + abs(self.score) * 20 + self.decay * 15


class QFScanner:
    def __init__(self, exchange, signal_engine, risk_manager, notifier, cfg: dict):
        self.ex      = exchange
        self.engine  = signal_engine
        self.risk    = risk_manager
        self.tg      = notifier
        self.cfg     = cfg

        # Control de señales ya enviadas (evitar spam)
        self._sent:   dict[str, float] = {}  # symbol → timestamp última señal
        self._cooldown = float(cfg.get('scan_cooldown_min', 15)) * 60  # segundos

    # ─────────────────────────────────────────────────────────
    #  OBTENER UNIVERSO DE SÍMBOLOS
    # ─────────────────────────────────────────────────────────
    async def _get_universe(self) -> list[str]:
        """
        Obtiene todos los pares USDT de futuros perpetuos de BingX
        Filtra por volumen mínimo para evitar illiquid markets
        """
        try:
            symbols_raw = await self.ex.get_all_symbols()
        except Exception as e:
            logger.error(f"Error obteniendo símbolos: {e}")
            # Fallback: lista base
            return [
                "BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT",
                "DOGE-USDT","ADA-USDT","AVAX-USDT","LINK-USDT","DOT-USDT",
                "MATIC-USDT","UNI-USDT","LTC-USDT","ATOM-USDT","FIL-USDT",
                "APT-USDT","ARB-USDT","OP-USDT","INJ-USDT","SUI-USDT",
            ]

        min_vol = float(self.cfg.get('min_volume_usdt', 200_000))

        # Filtrar por volumen mínimo usando tickers
        valid = []
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://open-api.bingx.com/openApi/swap/v2/quote/ticker",
                    headers={"X-BX-APIKEY": self.ex.api_key}
                ) as r:
                    data = await r.json()
                    tickers = data.get("data", [])
                    vol_map = {}
                    for t in tickers:
                        sym = t.get("symbol","")
                        vol = float(t.get("quoteVolume", t.get("volume", 0)) or 0)
                        vol_map[sym] = vol

            for sym in symbols_raw:
                if vol_map.get(sym, 0) >= min_vol:
                    valid.append(sym)
        except Exception as e:
            logger.warning(f"Error filtrando por volumen: {e} — usando lista sin filtro")
            valid = symbols_raw[:100]  # limitar a 100 si falla

        logger.info(f"Universo: {len(valid)} pares (vol≥{min_vol/1000:.0f}K USDT)")
        return valid

    # ─────────────────────────────────────────────────────────
    #  ANALIZAR UN SÍMBOLO
    # ─────────────────────────────────────────────────────────
    async def _analyze_symbol(self, symbol: str) -> Optional[ScanResult]:
        try:
            df_3m  = await self.ex.get_klines(symbol, "3m",  limit=250)
            df_htf = await self.ex.get_klines(symbol, "15m", limit=100)

            if len(df_3m) < 100 or len(df_htf) < 20:
                return None

            sig = self.engine.compute(df_3m, df_htf)

            if sig.direction == "FLAT" or sig.tier == "NONE":
                return None

            # Filtros mínimos
            min_conv = int(self.cfg.get('scan_min_conviction', 1))
            if sig.conviction < min_conv:
                return None

            # Calcular TP
            tp = self.risk.calc_tp(sig.entry_price, sig.sl_price,
                                   sig.direction, sig.tier)

            # Volumen 24h aproximado
            vol_24h = float(df_3m['volume'].tail(480).sum() * sig.entry_price)

            return ScanResult(
                symbol     = symbol,
                direction  = sig.direction,
                tier       = sig.tier,
                conviction = sig.conviction,
                score      = sig.norm_score,
                decay      = sig.details.get('decay_pct', 0) / 100,
                entry      = sig.entry_price,
                sl         = sig.sl_price,
                tp         = tp,
                volume_24h = vol_24h,
                details    = sig.details,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Error analizando {symbol}: {e}")
            return None

    # ─────────────────────────────────────────────────────────
    #  SCAN COMPLETO
    # ─────────────────────────────────────────────────────────
    async def run_scan(self) -> list[ScanResult]:
        """
        Escanea todo el universo con concurrencia controlada.
        Retorna lista ordenada por rank_score.
        """
        symbols = await self._get_universe()
        concurrency = int(self.cfg.get('scan_concurrency', 20))
        semaphore   = asyncio.Semaphore(concurrency)

        async def _bounded(sym):
            async with semaphore:
                result = await self._analyze_symbol(sym)
                await asyncio.sleep(0.05)  # throttle suave
                return result

        t0      = time.time()
        tasks   = [_bounded(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        hits = [r for r in results if isinstance(r, ScanResult)]
        hits.sort(key=lambda x: x.rank_score, reverse=True)

        elapsed = time.time() - t0
        logger.info(f"Scan: {len(symbols)} pares en {elapsed:.1f}s → {len(hits)} señales")

        return hits

    # ─────────────────────────────────────────────────────────
    #  NOTIFICAR SEÑALES NUEVAS
    # ─────────────────────────────────────────────────────────
    async def notify_top_signals(self, hits: list[ScanResult], max_notify: int = 3):
        """Notifica las mejores señales nuevas (con cooldown por símbolo)"""
        now      = time.time()
        notified = 0

        for r in hits:
            if notified >= max_notify:
                break

            # Cooldown: no repetir la misma moneda
            last = self._sent.get(r.symbol, 0)
            if now - last < self._cooldown:
                continue

            paper = self.ex.paper
            await self._send_scan_signal(r, paper)
            self._sent[r.symbol] = now
            notified += 1

    async def _send_scan_signal(self, r: ScanResult, paper: bool):
        tier_emoji = {
            "SUPREMA":    "⭐⭐⭐",
            "FUEL":       "🔥🔥",
            "STD":        "▶️",
            "HUNT_LONG":  "🎯",
            "HUNT_SHORT": "🎯",
        }.get(r.tier, "")

        dir_emoji = "🟢 LONG" if r.direction == "LONG" else "🔴 SHORT"
        mode_tag  = "📋 PAPER" if paper else "💵 REAL"
        conv_bar  = "█" * r.conviction + "░" * (10 - r.conviction)
        rr        = abs(r.tp - r.entry) / abs(r.entry - r.sl) if abs(r.entry - r.sl) > 0 else 0
        d         = r.details

        # Filtros activos
        flags = []
        if d.get('htf_bull') or d.get('htf_bear'):   flags.append("HTF✅")
        if d.get('sig_alive'):                         flags.append("DECAY✅")
        if d.get('asym_bull') or d.get('asym_bear'):  flags.append("ASIM✅")
        if d.get('tl_long') or d.get('tl_short'):     flags.append("TL🔥")
        if d.get('dp_buy') or d.get('dp_sell'):       flags.append("DP🔵")
        if d.get('cvd_bull_div') or d.get('cvd_bear_div'): flags.append("CVDdiv🔶")
        if d.get('sq_bull') or d.get('sq_bear'):      flags.append("SQ💥")
        if d.get('in_bull_fvg') or d.get('in_bear_fvg'):  flags.append("FVG📦")
        if d.get('in_bull_ob') or d.get('in_bear_ob'):    flags.append("OB🧱")

        text = (
            f"{tier_emoji} *SCANNER: {r.tier}*\n"
            f"{dir_emoji}  |  {mode_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 *{r.symbol}*\n"
            f"📊 Entrada: `{r.entry:.6f}`\n"
            f"🛑 SL:      `{r.sl:.6f}`\n"
            f"🎯 TP:      `{r.tp:.6f}`\n"
            f"⚖️  R:R:     `{rr:.1f}x`\n\n"
            f"🧠 Conv:  `{conv_bar}` {r.conviction}/10\n"
            f"📈 Score: `{r.score*100:+.1f}/100`\n"
            f"🔄 Decay: `{r.decay*100:.1f}%`\n\n"
            f"*Filtros:* {' '.join(flags) if flags else '—'}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💧 Vol≈`{r.volume_24h/1e6:.1f}M USDT`"
        )

        if self.tg:
            await self.tg._send(text)

    # ─────────────────────────────────────────────────────────
    #  RESUMEN PERIÓDICO
    # ─────────────────────────────────────────────────────────
    async def send_summary(self, hits: list[ScanResult], n_scanned: int, paper: bool):
        """Envía resumen del scan: top señales por lado"""
        longs  = [h for h in hits if h.direction == "LONG"][:5]
        shorts = [h for h in hits if h.direction == "SHORT"][:5]
        mode   = "📋 PAPER" if paper else "💵 REAL"

        def fmt(r: ScanResult) -> str:
            return (f"  `{r.symbol:<18}` {r.tier:<12} "
                    f"Score:{r.score*100:+.0f} Decay:{r.decay*100:.0f}% Conv:{r.conviction}/10")

        long_lines  = "\n".join(fmt(r) for r in longs)  or "  — Sin señales LONG —"
        short_lines = "\n".join(fmt(r) for r in shorts) or "  — Sin señales SHORT —"

        text = (
            f"🔭 *QF SCANNER — {mode}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Pares escaneados: `{n_scanned}`\n"
            f"✅ Señales totales: `{len(hits)}`\n"
            f"🟢 LONG: `{len(longs)}`  🔴 SHORT: `{len(shorts)}`\n\n"
            f"*🟢 TOP LONGS:*\n{long_lines}\n\n"
            f"*🔴 TOP SHORTS:*\n{short_lines}"
        )

        if self.tg:
            await self.tg._send(text)
