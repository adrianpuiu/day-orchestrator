"""Strategy registry — the actuarial table (sqlite; sessions use ADK's own db).

Layer note: this module is storage only. Signal functions and seed specs live
in app.strategies.seeds (the layer above), so core never imports upward.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    signal_fn TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL,
    stats TEXT NOT NULL
);
"""

UPSERT = (
    "INSERT INTO strategies (id, market, timeframe, signal_fn, params,"
    " status, stats) VALUES (?,?,?,?,?,?,?)"
    " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
    " params=excluded.params"
)


@dataclass
class StrategySpec:
    id: str
    market: str = "SPY"
    timeframe: str = "1d"
    signal_fn: str = "pullback_resumption.signal"
    params: dict = field(default_factory=dict)
    status: str = "candidate"          # candidate | live | retired
    stats: dict = field(default_factory=dict)


def _row_to_spec(r) -> StrategySpec:
    return StrategySpec(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5],
                        json.loads(r[6]))


class Registry:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def upsert(self, spec: StrategySpec) -> None:
        self.conn.execute(
            UPSERT,
            (spec.id, spec.market, spec.timeframe, spec.signal_fn,
             json.dumps(spec.params), spec.status, json.dumps(spec.stats)))
        self.conn.commit()

    def live_specs(self) -> list[StrategySpec]:
        rows = self.conn.execute(
            "SELECT id, market, timeframe, signal_fn, params, status, stats"
            " FROM strategies WHERE status='live'").fetchall()
        return [_row_to_spec(r) for r in rows]

    def update_stats(self, spec_id: str, stats: dict) -> None:
        self.conn.execute("UPDATE strategies SET stats=? WHERE id=?",
                          (json.dumps(stats), spec_id))
        self.conn.commit()

    def as_dicts(self) -> list[dict]:
        return [asdict(s) for s in self.live_specs()]

    def close(self) -> None:
        self.conn.close()
