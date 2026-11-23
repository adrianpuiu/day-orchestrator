"""DecayWatcher — rolling re-grade of live strategies; two-strike retirement.

"An alpha can decay in a day" (0:20:12): every live spec is re-run over a
trailing window each night; a negative avg_r window is a strike, two
consecutive strikes retire the spec (one noise window is not decay).
Streaks persist in registry.db so daemon restarts don't forget.
"""

from __future__ import annotations

import pandas as pd

from ..core import backtest
from ..core.indicators import with_indicators
from ..core.registry import Registry
from ..strategies import pullback_resumption

DECAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS decay_state (
    spec_id TEXT PRIMARY KEY,
    neg_streak INTEGER NOT NULL DEFAULT 0,
    last_window_avg_r REAL,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def ensure_decay_table(registry: Registry) -> None:
    registry.conn.execute(DECAY_SCHEMA)
    registry.conn.commit()


def get_streak(registry: Registry, spec_id: str) -> int:
    ensure_decay_table(registry)
    row = registry.conn.execute(
        "SELECT neg_streak FROM decay_state WHERE spec_id=?",
        (spec_id,)).fetchone()
    return int(row[0]) if row else 0


def evaluate_decay(registry: Registry, df: pd.DataFrame,
                   window_days: int = 5 * 365,
                   min_trades: int = 10, strikes_to_retire: int = 2) -> dict:
    """Re-grades every live spec on the trailing window; retires on streak."""
    ensure_decay_table(registry)
    if len(df) == 0:
        return {"checked": 0}
    cutoff = df.index[-1] - pd.Timedelta(days=window_days)
    window = with_indicators(df.loc[df.index >= cutoff])
    results = {"checked": 0, "retired": [], "strikes": {}, "windows": {}}
    for spec in registry.live_specs():
        trades = backtest.run(window, pullback_resumption.signal,
                              **spec.params)
        avg_r = (sum(t["r"] for t in trades) / len(trades)) if trades else 0.0
        results["checked"] += 1
        results["windows"][spec.id] = {
            "n": len(trades), "avg_r": round(avg_r, 3)}
        too_few = len(trades) < min_trades
        negative = (not too_few) and avg_r < 0
        streak = get_streak(registry, spec.id)
        streak = streak + 1 if negative else 0
        registry.conn.execute(
            "INSERT INTO decay_state (spec_id, neg_streak, last_window_avg_r)"
            " VALUES (?,?,?) ON CONFLICT(spec_id) DO UPDATE SET"
            " neg_streak=excluded.neg_streak,"
            " last_window_avg_r=excluded.last_window_avg_r,"
            " updated_at=datetime('now')",
            (spec.id, streak, round(avg_r, 3)))
        registry.conn.commit()
        results["strikes"][spec.id] = streak
        if negative:
            results["windows"][spec.id]["verdict"] = (
                f"strike {streak}/{strikes_to_retire}")
        elif too_few:
            results["windows"][spec.id]["verdict"] = (
                f"n={len(trades)} < {min_trades}; no signal either way")
        else:
            results["windows"][spec.id]["verdict"] = "healthy"
        if streak >= strikes_to_retire:
            registry.conn.execute(
                "UPDATE strategies SET status='retired' WHERE id=?", (spec.id,))
            registry.conn.commit()
            results["retired"].append(spec.id)
            results["windows"][spec.id]["verdict"] = (
                f"RETIRED after {streak} negative windows")
    return results
