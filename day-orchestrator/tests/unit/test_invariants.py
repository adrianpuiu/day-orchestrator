"""Invariant-2 unit tests: at-most-once latch, notification budget, dedup window."""

from __future__ import annotations

from app.core import actionlog, approval, budget
from app.core.db import Database


def make_db(tmp_path) -> Database:
    db = Database(tmp_path / "inv.db")
    db.init_schema()
    return db


def test_claim_is_at_most_once(tmp_path) -> None:
    """The send:<id> latch fires exactly once, no matter who asks again."""
    db = make_db(tmp_path)
    assert actionlog.claim(db, "send:abc", "gate") is True
    assert actionlog.claim(db, "send:abc", "gate") is False
    assert actionlog.claim(db, "send:other", "gate") is True


def test_budget_allows_n_then_blocks(tmp_path) -> None:
    """budget.allow passes exactly N times per category-day, then suppresses."""
    db = make_db(tmp_path)
    for _ in range(3):
        assert budget.allow(db, "triage_alerts", 3, "local") is True
    assert budget.allow(db, "triage_alerts", 3, "local") is False
    # A fresh day-key resets the counter.
    db.execute("UPDATE notification_budget SET day = '2000-01-01'", ())
    assert budget.allow(db, "triage_alerts", 3, "local") is True


def test_find_open_same_covers_recently_approved(tmp_path) -> None:
    """An approved twin blocks an identical re-request inside the window."""
    db = make_db(tmp_path)
    payload = {"to": "a@b.c", "subject": "s", "body": "b"}
    row = approval.create(db, "send_email", payload, session_ref='{"u":1}')
    approval.resolve(db, row.id, "approved", "test")
    # Fresh approval: the twin guard must return it (would double-apply).
    assert approval.find_open_same(db, "send_email", payload) is not None
    # Aged past the window: no longer an open twin.
    db.execute("UPDATE approvals SET created_at = '2000-01-01T00:00:00+00:00'", ())
