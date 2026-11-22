"""Duplicate-gated-call guard: identical open approvals are detected."""

from __future__ import annotations

from app.core import approval
from app.core.db import Database


def test_find_open_same_matches_pending_twin(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    payload = {"title": "Cafea cu Diana", "start": "2026-11-23T17:30",
               "end": "2026-11-23T18:30"}
    row = approval.create(db, "create_calendar_event", payload)

    hit = approval.find_open_same(db, "create_calendar_event", payload)
    assert hit is not None and hit.id == row.id

    other = dict(payload, title="Altceva")
    assert approval.find_open_same(db, "create_calendar_event", other) is None
    assert approval.find_open_same(db, "send_email", payload) is None


def test_superseded_rows_do_not_block(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    payload = {"title": "Cafea", "start": "2026-11-23T17:30", "end": "2026-11-23T18:00"}
    row = approval.create(db, "create_calendar_event", payload)
    approval.resolve(db, row.id, "cancelled", "operator")

    assert approval.find_open_same(db, "create_calendar_event", payload) is None
