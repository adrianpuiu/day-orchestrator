"""Append-only action log + idempotency claims (auditability +4, ADR-005).

`claim(dedup_key)` is the at-most-once latch for scheduled effects: unique
constraint + INSERT OR IGNORE; unclaimed keys rerun after a crash between
claim and completion (at-least-once, so effects must stay idempotent).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .db import Database


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(
    db: Database,
    actor: str,
    action: str,
    effect_class: str,
    approval_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO action_log "
        "(ts, actor, action, effect_class, approval_id, dedup_key, detail) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?)",
        (
            _now(),
            actor,
            action,
            effect_class,
            approval_id,
            json.dumps(detail) if detail else None,
        ),
    )


def claim(db: Database, dedup_key: str, actor: str) -> bool:
    """Atomically claim a scheduled effect. True = we run it; False = already done."""
    cur = db.execute(
        "INSERT OR IGNORE INTO action_log (ts, actor, action, effect_class, dedup_key) "
        "VALUES (?, ?, 'claim', 'system', ?)",
        (_now(), actor, dedup_key),
    )
    return cur.rowcount > 0


def recent(db: Database, limit: int = 50) -> list:
    return db.query(
        "SELECT * FROM action_log ORDER BY seq DESC LIMIT ?", (limit,)
    )
