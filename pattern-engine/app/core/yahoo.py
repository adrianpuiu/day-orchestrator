"""Yahoo research-history layer (long series; HL instruments stay the truth).

Research windows come from Yahoo (decades: BTC-USD since 2014, futures since
the '80s); confirmation and everything live stays Hyperliquid-native. No
proxy exists for xyz:XYZ100 — it runs HL-only windows.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# HL coin -> Yahoo proxy for the long research window.
YAHOO_MAP = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SPX": "^GSPC",
    "xyz:GOLD": "GC=F",
    "xyz:SILVER": "SI=F",
    "xyz:BRENTOIL": "CL=F",     # WTI proxy: Brent (BZ=F) has a 12-session
                                # Yahoo gap; WTI negative prints curated below
    # xyz:XYZ100: intentionally absent — HL-only.
}


def ingest_yahoo(hl_coin: str, dest_dir: Path, years: int = 15) -> pd.DataFrame:
    """Pulls the Yahoo proxy for an HL coin into the engine schema."""
    import yfinance as yf

    from .market import repair_ohlc

    symbol = YAHOO_MAP[hl_coin]
    df = yf.download(symbol, period="max", interval="1d", auto_adjust=True,
                     progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        lv = 1 if df.columns[0][0] in ("Open", "High", "Low", "Close", "Volume") else 0
        df = df.droplevel(lv, axis=1)
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = repair_ohlc(df[~df.index.duplicated(keep="last")].sort_index())
    # WTI traded negative on 2020-04-20/-21 — real prints, but ATR units and
    # R-multiples need positive prices; curate them out of the proxy.
    df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    if len(df) > years * 365:
        df = df.iloc[-years * 365:]
    dest = dest_dir / f"yh_{hl_coin.replace(':', '_').lower()}_daily.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dest)
    return df


def load_yahoo(hl_coin: str, dest_dir: Path) -> pd.DataFrame:
    return pd.read_parquet(
        dest_dir / f"yh_{hl_coin.replace(':', '_').lower()}_daily.parquet")
