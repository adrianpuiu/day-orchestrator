"""SQLite persistence: one file, WAL, single connection guarded by a lock (C2).

ADK's DatabaseSessionService manages its own tables in the same file via
SQLAlchemy; this module owns the app tables only. Names never clash.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS email_triage (
        uid TEXT PRIMARY KEY,           -- email Message-ID
        summary TEXT NOT NULL,
        category TEXT NOT NULL,         -- triage taxonomy (needs_reply, ...)
        importance TEXT NOT NULL,       -- low | normal | high
        created_at TEXT NOT NULL        -- first-seen ISO (INSERT OR IGNORE)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,             -- 'send_email' | future send-class kinds
        payload TEXT NOT NULL,          -- JSON
        state TEXT NOT NULL CHECK (state IN
            ('pending','approved','rejected','cancelled','superseded')),
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolved_via TEXT,              -- 'telegram:approve' | 'telegram:reject' | ...
        telegram_message_id INTEGER,
        session_ref TEXT,               -- app session holding a paused run (ADR-004)
        dedup_key TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS action_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        effect_class TEXT NOT NULL CHECK (effect_class IN
            ('read','draft','send','notify','system')),
        approval_id TEXT,
        dedup_key TEXT UNIQUE,          -- idempotency claims (ADR-005)
        detail TEXT                     -- JSON
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        source TEXT,                    -- 'email' | 'user' | 'triage'
        source_ref TEXT,                -- e.g. email Message-ID
        due TEXT,                       -- ISO 8601, nullable
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dropped')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # One task per source email: re-running extraction on the same mail folds
    # onto the existing row (open, done or dropped) instead of duplicating it.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source_ref ON tasks (source_ref) "
    "WHERE source_ref IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS reminders (
        id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        fire_at TEXT NOT NULL,          -- ISO 8601 local wall time
        status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','fired','dropped')),
        created_at TEXT NOT NULL,
        fired_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,            -- JSON
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_budget (
        day TEXT NOT NULL,              -- YYYY-MM-DD local
        category TEXT NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, category)
    )
    """,
]


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def init_schema(self) -> None:
        with self._lock:
            for stmt in SCHEMA:
                self._conn.execute(stmt)
            # Migrations (CREATE IF NOT EXISTS won't alter existing tables).
            try:
                self._conn.execute(
                    "ALTER TABLE reminders ADD COLUMN retries INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema v{row['value']} newer than supported v{SCHEMA_VERSION}"
                )
            self._conn.commit()

    @contextmanager
    def tx(self) -> Generator[sqlite3.Connection]:
        """Serialized transaction; commits on success, rolls back on error."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.tx() as conn:
            return conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def persist_triage(db: Database, items: list[dict[str, Any]]) -> int:
    """First-seen INSERT OR IGNORE of triage items; returns how many were new."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    new = 0
    for item in items:
        uid = str(item.get("uid") or "")
        if not uid:
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO email_triage "
            "(uid, summary, category, importance, created_at) VALUES (?,?,?,?,?)",
            (uid, str(item.get("summary", "")), str(item.get("category", "other")),
             str(item.get("importance", "normal")), now),
        )
        new += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return new


def needs_reply_open(db: Database, days: int = 7, limit: int = 10) -> list[dict]:
    """needs_reply emails first seen within `days`, newest first (BFS: the
    sweep and the brief both render from this one query)."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.query(
        "SELECT uid, summary, importance, created_at FROM email_triage "
        "WHERE category='needs_reply' AND created_at>=? "
        "ORDER BY created_at DESC LIMIT ?",
        (cutoff, limit),
    )
    return [dict(r) for r in rows]
