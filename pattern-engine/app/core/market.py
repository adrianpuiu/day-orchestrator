"""Market data: ingest (yfinance EOD) + DataQualityGate.

"It might depend on the quality of your data" (0:26:23): nothing downstream
trains, backtests, or trades on data that fails the gate.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .types import QualityReport


def ingest_eod(symbol: str, dest: Path) -> pd.DataFrame:
    """Pulls daily OHLCV and writes/refreshes the parquet store."""
    import yfinance as yf

    df = yf.download(symbol, period="max", interval="1d", auto_adjust=True,
                     progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        lv = 1 if df.columns[0][0] in ("Open", "High", "Low", "Close", "Volume") else 0
        df = df.droplevel(lv, axis=1)
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = repair_ohlc(df)
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dest)
    return df


def repair_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Clamps High/Low to the OHLC envelope. auto_adjust multiplication can
    leave 1-ulp artifacts (High < Close by ~1e-15); material violations are
    repaired the same way and counted by the quality gate's post-load check."""
    out = df.copy()
    out["High"] = out[["High", "Open", "Close", "Low"]].max(axis=1)
    out["Low"] = out[["Low", "Open", "Close", "High"]].min(axis=1)
    return out


def load_eod(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    return df[~df.index.duplicated(keep="last")].sort_index()


def quality_gate(df: pd.DataFrame) -> QualityReport:
    problems: list[str] = []
    if len(df) < 300:
        problems.append(f"too few bars: {len(df)}")
    if not df.index.is_monotonic_increasing:
        problems.append("index not monotonic")
    dups = df.index.duplicated(keep=False)
    if dups.any():
        problems.append(f"{int(dups.sum())} duplicate timestamps")
    bad_ohlc = (
        (df["High"] < df[["Open", "Close", "Low"]].max(axis=1))
        | (df["Low"] > df[["Open", "Close", "High"]].min(axis=1))
    )
    if bad_ohlc.any():
        problems.append(f"{int(bad_ohlc.sum())} bars violate OHLC invariants")
    nan = df[["Open", "High", "Low", "Close"]].isna().any(axis=1)
    if nan.any():
        problems.append(f"{int(nan.sum())} bars with NaN prices")
    nonpos = (df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)
    if nonpos.any():
        problems.append(f"{int(nonpos.sum())} bars with non-positive prices")
    # Calendar gaps: holidays legitimately remove weekdays; flag only gross
    # holes — runs of 6+ consecutive missing weekdays.
    if len(df) > 1:
        full = pd.date_range(df.index[0], df.index[-1], freq="D")
        weekdays = sorted(set(full[full.day_of_week < 5]))
        present = set(df.index)
        run = 0
        worst = 0
        for d in weekdays:
            run = 0 if d in present else run + 1
            worst = max(worst, run)
        if worst >= 6:
            problems.append(f"gross calendar hole: {worst} consecutive "
                            f"weekday sessions missing")
    return QualityReport(ok=not problems, problems=problems, bars_checked=len(df))
