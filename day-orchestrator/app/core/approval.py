"""Approval gate: the single trust boundary (C4, ADR-004).

State machine owned entirely by this module:

    pending --> approved | rejected | cancelled | superseded

`send`-class tools may act only on `require_resolved(...)` returning an
`approved` row. Chat text never resolves anything; only `resolve()` does,
and only the Telegram adapter calls it from button callbacks.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .db import Database

ALLOWED = {"pending", "approved", "rejected", "cancelled", "superseded"}
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"approved", "rejected", "cancelled", "superseded"},
}


class ApprovalError(Exception):
    pass


class ApprovalPending(ApprovalError):
    """Row exists but is not resolved yet — the paused tool must keep waiting."""


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    kind: str
    payload: dict[str, Any]
    state: str
    resolved_via: str | None = None
    session_ref: str | None = None
    telegram_message_id: int | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_approval(row) -> Approval:  # sqlite3.Row
    return Approval(
        id=row["id"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        state=row["state"],
        resolved_via=row["resolved_via"],
        session_ref=row["session_ref"],
        telegram_message_id=row["telegram_message_id"],
    )


def create(
    db: Database,
    kind: str,
    payload: dict[str, Any],
    dedup_key: str | None = None,
    session_ref: str | None = None,
) -> Approval:
    approval_id = uuid.uuid4().hex[:16]
    db.execute(
        "INSERT INTO approvals (id, kind, payload, state, created_at, dedup_key, session_ref) "
        "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
        (approval_id, kind, json.dumps(payload), _now(), dedup_key, session_ref),
    )
    return get(db, approval_id)  # type: ignore[return-value]


def resolve(db: Database, approval_id: str, verdict: str, via: str) -> Approval:
    if verdict not in {"approved", "rejected", "cancelled"}:
        raise ApprovalError(f"not a resolution verdict: {verdict!r}")
    row = db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if row is None:
        raise ApprovalError(f"unknown approval {approval_id!r}")
    if verdict not in TRANSITIONS.get(row["state"], set()):
        raise ApprovalError(f"illegal transition {row['state']!r} -> {verdict!r}")
    db.execute(
        "UPDATE approvals SET state=?, resolved_at=?, resolved_via=? WHERE id=?",
        (verdict, _now(), via, approval_id),
    )
    return get(db, approval_id)  # type: ignore[return-value]


def supersede(db: Database, approval_id: str) -> Approval:
    """Pending approval replaced by a newer draft of the same effect."""
    return resolve(db, approval_id, "cancelled", "superseded")


def update_payload(db: Database, approval_id: str, payload: dict[str, Any]) -> Approval:
    """Edit flow: owner adjusted the draft text while still pending (ADR-004)."""
    row = db.query_one("SELECT state FROM approvals WHERE id=?", (approval_id,))
    if row is None:
        raise ApprovalError(f"unknown approval {approval_id!r}")
    if row["state"] != "pending":
        raise ApprovalError(f"cannot edit approval in state {row['state']!r}")
    db.execute(
        "UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), approval_id)
    )
    return get(db, approval_id)  # type: ignore[return-value]


def get(db: Database, approval_id: str) -> Approval | None:
    row = db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    return _row_to_approval(row) if row else None


def require_resolved(db: Database, approval_id: str) -> Approval:
    """Gate for send-class tools: returns the row only once terminal."""
    approval = get(db, approval_id)
    if approval is None:
        raise ApprovalError(f"unknown approval {approval_id!r}")
    if approval.state == "pending":
        raise ApprovalPending(approval_id)
    return approval


def list_pending(db: Database) -> list[Approval]:
    rows = db.query(
        "SELECT * FROM approvals WHERE state='pending' ORDER BY created_at"
    )
    return [_row_to_approval(r) for r in rows]


def attach_telegram_message(db: Database, approval_id: str, message_id: int) -> None:
    db.execute(
        "UPDATE approvals SET telegram_message_id=? WHERE id=?", (message_id, approval_id)
    )
