"""Notification budget (C10): a proactive agent fails by nagging; silence is a feature."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .db import Database


def _local_today(tz_name: str) -> str:
    now = datetime.now()
    if tz_name != "local":
        now = now.astimezone(ZoneInfo(tz_name))
    return now.date().isoformat()


def allow(db: Database, category: str, limit: int, tz: str = "local") -> bool:
    """True (and increments the counter) if the budget permits another push."""
    day = _local_today(tz)
    db.execute(
        "INSERT OR IGNORE INTO notification_budget (day, category, sent) VALUES (?, ?, 0)",
        (day, category),
    )
    row = db.query_one(
        "SELECT sent FROM notification_budget WHERE day=? AND category=?", (day, category)
    )
    if row is None or row["sent"] >= limit:
        return False
    db.execute(
        "UPDATE notification_budget SET sent=sent+1 WHERE day=? AND category=?",
        (day, category),
    )
    return True
