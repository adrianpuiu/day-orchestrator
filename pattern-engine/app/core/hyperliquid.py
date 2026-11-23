"""Hyperliquid market-data adapter (public endpoints, no credentials).

Serves the basket wishlist: BTC, ETH, SPX (main dex) and XYZ100, GOLD,
SILVER, BRENTOIL (the `xyz` builder dex — equities + commodities perps).

Notes discovered against the live API:
  - candleSnapshot REQUIRES startTime/endTime; the daily interval key is
    rejected, so we page 1h candles and aggregate to UTC days.
  - Builder-dex coins are addressed as "xyz:GOLD" etc.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

HL_API = "https://api.hyperliquid.xyz"
WISHLIST = ["BTC", "ETH", "SPX", "xyz:XYZ100", "xyz:GOLD", "xyz:SILVER",
            "xyz:BRENTOIL"]
PAGE_MS = 4_000 * 8 * 3_600_000         # ~4000 8h candles per request


def _info(payload: dict) -> object:
    r = requests.post(f"{HL_API}/info", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def hl_universe(dex: str | None = None) -> list[str]:
    payload = {"type": "meta"}
    if dex:
        payload["dex"] = dex
    return [a["name"] for a in _info(payload)["universe"]]


def hl_bars(coin: str, days: int, interval: str = "8h") -> pd.DataFrame:
    """Pages 8h candles over the last `days` days. The API caps 5000 candles
    per request and serves the trailing window; 8h reaches ~3 years back."""
    now_ms = int(time.time() * 1000)
    start = now_ms - days * 86_400_000
    rows: list = []
    while start < now_ms:
        end = min(start + PAGE_MS, now_ms)
        batch = _info({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": start, "endTime": end}})
        if isinstance(batch, list):
            rows.extend(batch)
        if not batch:
            break
        start = end
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame([{
        "ts": pd.Timestamp(c["t"], unit="ms", tz="UTC"),
        "Open": float(c["o"]), "High": float(c["h"]), "Low": float(c["l"]),
        "Close": float(c["c"]), "Volume": float(c["v"]),
    } for c in rows]).set_index("ts").sort_index()
    return df[~df.index.duplicated(keep="last")]


def hl_daily(coin: str, days: int = 3 * 365) -> pd.DataFrame:
    """UTC-day OHLCV aggregated from 8h candles (our engine's schema)."""
    bars = hl_bars(coin, days)
    if bars.empty:
        return bars
    daily = bars.resample("1D").agg(
        Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
        Close=("Close", "last"), Volume=("Volume", "sum")).dropna()
    daily.index = daily.index.tz_localize(None)      # engine uses naive dates
    return daily


def ingest_hl(coin: str, dest_dir: Path, days: int = 3 * 365) -> pd.DataFrame:
    safe = coin.replace(":", "_").lower()
    dest = dest_dir / f"hl_{safe}_daily.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    df = hl_daily(coin, days)
    if not df.empty:
        df.to_parquet(dest)
    return df


def load_hl(coin: str, dest_dir: Path) -> pd.DataFrame:
    safe = coin.replace(":", "_").lower()
    return pd.read_parquet(dest_dir / f"hl_{safe}_daily.parquet")


def ingest_hl_8h(coin: str, dest_dir: Path, days: int = 3 * 365) -> pd.DataFrame:
    """Stores raw 8h bars — the intraday variant timeframe (HL-native)."""
    safe = coin.replace(":", "_").lower()
    dest = dest_dir / f"hl_{safe}_8h.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    df = hl_bars(coin, days)
    if not df.empty:
        df.index = df.index.tz_localize(None)
        df.to_parquet(dest)
    return df


def load_hl_8h(coin: str, dest_dir: Path) -> pd.DataFrame:
    safe = coin.replace(":", "_").lower()
    return pd.read_parquet(dest_dir / f"hl_{safe}_8h.parquet")
