"""JSON memory store: small facts that outlive sessions (preferences, last triage)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .db import Database


def set_value(db: Database, key: str, value: Any) -> None:
    db.execute(
        "INSERT INTO memory (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), datetime.now(UTC).isoformat(timespec="seconds")),
    )


def get_value(db: Database, key: str) -> Any | None:
    row = db.query_one("SELECT value FROM memory WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None
