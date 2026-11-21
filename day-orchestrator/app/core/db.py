"""SQLite persistence: one file, WAL, single connection guarded by a lock (C2).

ADK's DatabaseSessionService manages its own tables in the same file via
SQLAlchemy; this module owns the app tables only. Names never clash.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
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
