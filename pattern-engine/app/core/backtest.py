"""Backtest engine — verbatim port of the session-verified engine.

Reference results it must reproduce on the SPY fixture (regression lock):
  B long-only, 1-ATR stop, 3-ATR target, 10-bar time stop:
    72 trades, PF 1.46, avgR +0.306
  C long-only, 1-ATR stop, 2.5-ATR trail:
    59 trades, PF 1.54, avgR +0.356
Conservative fills: a bar touching both stop and target counts as stop;
entry bar can only stop out; gaps through stops fill at the open.
"""

from __future__ import annotations

import pandas as pd

from .types import Signal  # noqa: F401  (re-exported for type consumers)


def run(df: pd.DataFrame, signal_fn, **exit_kwargs) -> list[dict]:
    """Sweeps history bar-by-bar through signal_fn + inline exit logic.

    signal_fn(df, i, **exit_kwargs) -> Signal | None: strategy-agnostic driver.
    """
    n = len(df)
    O, H, L, C = (df[k].values for k in ("Open", "High", "Low", "Close"))
    trades: list[dict] = []
    i = 0
    while i < n - 1:
        sig = signal_fn(df, i, **exit_kwargs)
        if sig is None:
            i += 1
            continue
        entry, stop = sig.entry, sig.stop
        stop_dist = sig.stop_dist
        target = sig.target
        j = i + 1
        exit_px: float | None = None
        exit_ts = None
        if sig.trail_mult is not None:
            extreme = entry
            while j < n:
                extreme = max(extreme, H[j])
                stop = max(stop, extreme
                           - sig.trail_mult * df.at[df.index[i], "atr"])
                if L[j] <= stop:
                    exit_px, exit_ts = min(O[j], stop), df.index[j]
                    break
                j += 1
            if exit_px is None:
                j = min(j, n - 1)
                exit_px, exit_ts = C[j], df.index[j]
        else:
            while j < n and j <= i + sig.time_stop_bars:
                if L[j] <= stop:
                    exit_px, exit_ts = min(O[j], stop), df.index[j]
                    break
                if target is not None and H[j] >= target:
                    exit_px, exit_ts = max(O[j], target), df.index[j]
                    break
                j += 1
            if exit_px is None:
                j = min(j, i + sig.time_stop_bars, n - 1)
                exit_px, exit_ts = C[j], df.index[j]
        assert exit_px is not None and exit_ts is not None
        r = (exit_px - entry) / stop_dist
        trades.append({
            "entry_ts": df.index[i], "exit_ts": exit_ts, "side": 1,
            "entry": float(entry), "exit": float(exit_px),
            "stop_dist": float(stop_dist), "r": float(r),
        })
        i = j + 1
    return trades


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "pf": 0.0, "avg_r": 0.0, "win_pct": 0.0}
    rs = pd.Series([t["r"] for t in trades])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    return {"n": len(trades), "pf": round(pf, 2), "avg_r": round(float(rs.mean()), 3),
            "win_pct": round(float(100 * (rs > 0).mean()), 1)}
