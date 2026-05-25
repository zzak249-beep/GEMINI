import logging
import pandas as pd
import numpy as np
from bingx_client import BingXClient
import config as C

log = logging.getLogger(__name__)

def fetch(client: BingXClient, symbol: str, interval: str, limit: int = 250):
    raw = client.get_klines(symbol, interval, limit)
    if not raw:
        return None
    rows = []
    for k in raw:
        try:
            if isinstance(k, list):
                ts = pd.Timestamp(int(k[0]), unit="ms", tz="UTC")
                o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            else:
                ts = pd.Timestamp(int(k.get("time", k.get("t", 0))), unit="ms", tz="UTC")
                o  = float(k.get("open",   k.get("o", 0)))
                h  = float(k.get("high",   k.get("h", 0)))
                l  = float(k.get("low",    k.get("l", 0)))
                c  = float(k.get("close",  k.get("c", 0)))
                v  = float(k.get("volume", k.get("v", 0)))
            rows.append({"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
        except Exception as e:
            log.debug(f"Kline parse: {e}")
    if not rows:
        return None
    df = pd.DataFrame(rows).set_index("time").sort_index()
    return df[~df.index.duplicated(keep="last")]

def ok(df, min_rows=80):
    return df is not None and len(df) >= min_rows and df["close"].isna().sum() < 5
