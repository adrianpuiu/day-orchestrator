"""Triage persistence and the table-driven sweep/brief queries."""

from __future__ import annotations

from app.core.db import Database, needs_reply_open, persist_triage


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    return db


def test_persist_first_seen_wins(tmp_path) -> None:
    db = _db(tmp_path)
    item = {"uid": "<a@x>", "summary": "Invoice overdue",
            "category": "needs_reply", "importance": "high"}

    assert persist_triage(db, [item]) == 1
    assert persist_triage(db, [dict(item, importance="low")]) == 0, (
        "re-triage must not overwrite first-seen classification"
    )
    rows = needs_reply_open(db)
    assert rows and rows[0]["importance"] == "high"


def test_needs_reply_window_and_category(tmp_path) -> None:
    db = _db(tmp_path)
    persist_triage(db, [
        {"uid": "<r@x>", "summary": "need reply", "category": "needs_reply",
         "importance": "normal"},
        {"uid": "<f@x>", "summary": "fyi only", "category": "fyi",
         "importance": "low"},
    ])
    db.execute(
        "UPDATE email_triage SET created_at=? WHERE uid='<r@x>'",
        ("2020-01-01T00:00:00+00:00",),
    )

    assert needs_reply_open(db, days=7) == [], (
        "items older than the window must not resurface"
    )


def test_uidless_items_are_skipped(tmp_path) -> None:
    db = _db(tmp_path)
    assert persist_triage(db, [{"summary": "no uid", "category": "fyi",
                                "importance": "low"}]) == 0
    assert needs_reply_open(db) == []
