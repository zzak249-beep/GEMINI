"""
utils.py — helpers used across the scanner.
Key fix: is_blacklisted() normalizes symbol names before matching.
Previously GOLD(XAU) and EURUSD bypassed the blacklist because BingX
symbol strings didn't match the raw blacklist entries.

FIX: _normalize() ahora también quita espacios. "Oil WTI" / "Oil
Brent" (con espacio, tal como aparecen en BingX) normalizaban a
"OIL WTI" / "OIL BRENT" — con espacio de por medio — y nunca
coincidían con las entradas de blacklist OILWTI / OILBRENT (sin
espacio). Confirmado con capturas reales: ambos operando hoy pese a
estar en la lista.

PENDIENTE: EURUSD y GOLD(XAU), rastreados a mano contra este código,
deberían quedar bloqueados ya tal como estaba escrito — sin el fix de
arriba. Si siguen operando después de desplegar esto, lo más probable
es que esta versión con normalización no fuera la que corría de
verdad en producción. Confirmar en GitHub/Railway que es este archivo
exacto el desplegado, no una copia anterior sin normalizar.
"""
from datetime import datetime, timezone
import config


# ── Blacklist ─────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Strip USDT suffix, parentheses, dashes, spaces → uppercase base."""
    return (
        s.upper()
        .replace("(", "").replace(")", "")
        .replace("-", "").replace("_", "")
        .replace(" ", "")          # FIX: "Oil WTI" → "OILWTI"
        .removesuffix("USDT")
    )


def is_blacklisted(symbol: str) -> bool:
    """
    FIX: normalized matching so GOLD(XAU)→GOLDXAU and EURUSD→EURUSD
    both match their respective blacklist entries regardless of BingX
    symbol format quirks.
    Examples:
      GOLD(XAU)USDT → base=GOLDXAU  matches entry GOLDXAU ✓
      GOLD(XAU)     → base=GOLDXAU  matches entry GOLD    ✓
      EURUSD        → base=EURUSD   matches entry EURUSD  ✓
      SILVERXAGUSDT → base=SILVERXAG matches SILVERXAG   ✓
      Oil WTI       → base=OILWTI   matches entry OILWTI ✓ (nuevo)
    """
    sym_base = _normalize(symbol)
    for bl in config.BLACKLIST:
        bl_base = _normalize(bl)
        if bl_base and (sym_base == bl_base or sym_base.startswith(bl_base)):
            return True
    return False


# ── Trading session ───────────────────────────────────────────

def in_trading_session() -> bool:
    """True if current UTC hour is within [TRADE_START_UTC, TRADE_END_UTC)."""
    now_utc = datetime.now(tz=timezone.utc)
    h = now_utc.hour + now_utc.minute / 60.0
    return config.TRADE_START_UTC <= h < config.TRADE_END_UTC


def utc_hour() -> float:
    now = datetime.now(tz=timezone.utc)
    return now.hour + now.minute / 60.0


# ── Symbol helpers ────────────────────────────────────────────

def base_symbol(symbol: str) -> str:
    """BTC-USDT → BTC,  BTCUSDT → BTC,  GOLD(XAU)USDT → GOLDXAU."""
    return _normalize(symbol)


def count_direction(positions: list, side: str) -> int:
    return sum(1 for p in positions if p.get("positionSide") == side)
