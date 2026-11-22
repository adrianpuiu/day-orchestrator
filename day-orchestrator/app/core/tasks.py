"""Task store: emails become tasks with deadlines (capability: task extraction)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database

BUCKET_ORDER = ("overdue", "today", "tomorrow", "later", "someday")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_today(tz: str = "local") -> date:
    now = datetime.now()
    if tz != "local":
        now = now.astimezone(ZoneInfo(tz))
    return now.date()


def _due_date(due: Any, tz: str) -> date | None:
    """Parses a due value to a local calendar date; unparseable → None (someday)."""
    if not due:
        return None
    try:
        dt = datetime.fromisoformat(str(due))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        tzinfo = datetime.now().astimezone().tzinfo if tz == "local" else ZoneInfo(tz)
        dt = dt.astimezone(tzinfo)
    return dt.date()


def bucket(task: dict[str, Any], today: date, tz: str = "local") -> str:
    d = _due_date(task.get("due"), tz)
    if d is None:
        return "someday"
    if d < today:
        return "overdue"
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return "later"


def list_open_bucketed(db: Database, tz: str = "local") -> dict[str, list[dict[str, Any]]]:
    today = local_today(tz)
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in BUCKET_ORDER}
    for task in list_open(db):
        buckets[bucket(task, today)].append(task)
    return buckets


def as_brief_lines(buckets: dict[str, list[dict[str, Any]]]) -> str:
    """Pre-rendered task lines for the brief; the planner copies them verbatim."""
    lines: list[str] = []
    for name in BUCKET_ORDER:
        for t in buckets.get(name, ()):
            due = f" — due {t['due']}" if t.get("due") else ""
            lines.append(f"- [{name}] {t['title']}{due}")
    return "\n".join(lines) if lines else "No open tasks."



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
