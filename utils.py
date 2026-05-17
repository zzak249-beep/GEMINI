"""
bot/utils.py — Logging con colores y utilidades generales.
"""
import logging
import sys
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    try:
        import colorlog
        formatter = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(name)s: %(message)s",
            log_colors={
                "DEBUG":    "cyan",
                "INFO":     "green",
                "WARNING":  "yellow",
                "ERROR":    "red",
                "CRITICAL": "bold_red",
            }
        )
    except ImportError:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    Path("logs").mkdir(exist_ok=True)
    fh = logging.FileHandler("logs/nexus.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[fh, sh]
    )


def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1].lower()
    val  = int(tf[:-1])
    return val * {"m": 60, "h": 3600, "d": 86400}.get(unit, 60)
