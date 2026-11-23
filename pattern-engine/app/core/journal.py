"""JournalStore — persistent trade journal (sqlite).

The daemon restarts; the book must not. Every closed trade lands here, and
reports/decay read from it rather than in-memory gateway state.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    exit_ts TEXT NOT NULL,
    side INTEGER NOT NULL,
    entry REAL NOT NULL,
    exit REAL NOT NULL,
    r REAL NOT NULL,
    pnl_usd REAL,
    exit_kind TEXT,
    recorded_at TEXT DEFAULT (datetime('now'))
);
"""


class JournalStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def append(self, closed: list[dict]) -> None:
        """Inserts closed trades idempotently on (strategy_id, entry_ts)."""
        for t in closed:
            dup = self.conn.execute(
                "SELECT 1 FROM journal WHERE strategy_id=? AND entry_ts=?",
                (t["strategy_id"], str(t["entry_ts"]))).fetchone()
            if dup:
                continue
            self.conn.execute(
                "INSERT INTO journal (strategy_id, entry_ts, exit_ts, side,"
                " entry, exit, r, pnl_usd, exit_kind) VALUES (?,?,?,?,?,?,?,?,?)",
                (t["strategy_id"], str(t["entry_ts"]), str(t["exit_ts"]),
                 t["side"], t["entry"], t["exit"], t["r"],
                 t.get("pnl_usd"), t.get("exit_kind")))
        self.conn.commit()

    def recent(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT strategy_id, entry_ts, exit_ts, side, entry, exit, r,"
            " pnl_usd, exit_kind FROM journal ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(zip(("strategy_id", "entry_ts", "exit_ts", "side",
                          "entry", "exit", "r", "pnl_usd", "exit_kind"), r))
                for r in rows]

    def summary(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(r), 0) FROM journal").fetchone()
        return {"trades": row[0], "total_r": round(row[1], 2)}

    def close(self) -> None:
        self.conn.close()


