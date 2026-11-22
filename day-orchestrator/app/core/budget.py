"""Notification budget (C10): a proactive agent fails by nagging; silence is a feature."""

from __future__ import annotations

from .db import Database
from .tasks import local_today


def allow(db: Database, category: str, limit: int, tz: str = "local") -> bool:
    """True (and increments the counter) if the budget permits another push."""
    day = local_today(tz).isoformat()
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
