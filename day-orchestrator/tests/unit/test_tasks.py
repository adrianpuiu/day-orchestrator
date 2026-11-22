"""Task bucketing: today / tomorrow / later / overdue / someday by local calendar date."""

from __future__ import annotations

from datetime import date

from app.core import tasks
from app.core.db import Database


def make_db(tmp_path) -> Database:
    db = Database(tmp_path / "tasks.db")
    db.init_schema()
    return db


def test_buckets_by_local_calendar_date(tmp_path) -> None:
    db = make_db(tmp_path)
    today = date(2026, 11, 22)
    cases = {
        "t1": ("2026-11-21T14:00", "overdue"),
        "t2": ("2026-11-22", "today"),  # date-only ISO
        "t3": ("2026-11-22T14:00+02:00", "today"),  # tz-aware same local day
        "t4": ("2026-11-23T09:00", "tomorrow"),
        "t5": ("2026-11-30", "later"),
        "t6": (None, "someday"),
        "t7": ("not a date", "someday"),  # unparseable → someday, never crash
    }
    for tid, (due, _expected) in cases.items():
        tasks.add(db, f"task {tid}", due=due)
    rows = {r["title"]: r for r in tasks.list_open(db)}
    for tid, (_, expected) in cases.items():
        assert tasks.bucket(rows[f"task {tid}"], today) == expected, tid


def test_list_open_bucketed_groups_in_order(tmp_path) -> None:
    db = make_db(tmp_path)
    tasks.add(db, "overdue one", due="2026-11-01")
    tasks.add(db, "today one", due="2026-11-22T08:00")
    tasks.add(db, "tomorrow one", due="2026-11-23")
    tasks.add(db, "someday one")
    buckets = tasks.list_open_bucketed(db)
    assert [t["title"] for t in buckets["overdue"]] == ["overdue one"]
    assert [t["title"] for t in buckets["today"]] == ["today one"]
    assert [t["title"] for t in buckets["tomorrow"]] == ["tomorrow one"]
    assert buckets["later"] == []


def test_brief_lines_render_buckets_verbatim(tmp_path) -> None:
    db = make_db(tmp_path)
    tasks.add(db, "pay invoice", due="2026-11-21")
    tasks.add(db, "call dentist", due="2026-11-22T14:00")
    tasks.add(db, "renew hosting", due="2026-11-25")
    tasks.add(db, "call dentist")
    lines = tasks.as_brief_lines(tasks.list_open_bucketed(db))
    assert lines == (
        "- [today · carried from 21/11/2026] pay invoice — due 21/11/2026\n"
        "- [today] call dentist — due 22/11/2026 - 14:00\n"
        "- [later] renew hosting — due 25/11/2026\n"
        "- [someday] call dentist"
    )
    empty = {"overdue": [], "today": [], "tomorrow": [], "later": [], "someday": []}
    assert tasks.as_brief_lines(empty) == "No open tasks."
