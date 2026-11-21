"""Task store: emails become tasks with deadlines (capability: task extraction)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from .db import Database


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def add(
    db: Database,
    title: str,
    source: str = "user",
    source_ref: str | None = None,
    due: str | None = None,
) -> dict[str, Any]:
    task_id = uuid.uuid4().hex[:12]
    now = _now()
    db.execute(
        "INSERT INTO tasks (id, title, source, source_ref, due, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
        (task_id, title, source, source_ref, due, now, now),
    )
    return {"id": task_id, "title": title, "due": due, "status": "open"}


def complete(db: Database, task_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if row is None:
        raise LookupError(f"unknown task {task_id!r}")
    db.execute(
        "UPDATE tasks SET status='done', updated_at=? WHERE id=?", (_now(), task_id)
    )
    return {"id": task_id, "status": "done"}


def list_open(db: Database) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM tasks WHERE status='open' ORDER BY "
        "due IS NULL, due, created_at"
    )
    return [dict(r) for r in rows]
