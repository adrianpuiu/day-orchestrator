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
    assert approval.find_open_same(db, "send_email", payload) is None


def test_effect_completed_marks_latch_phase_two(tmp_path) -> None:
    """Claim alone is incomplete; an outcome row completes the latch."""
    db = make_db(tmp_path)
    assert actionlog.claim(db, "send:abc", "gate") is True
    assert actionlog.effect_completed(db, "abc") is False
    actionlog.log(db, "gate", "email_sent", "send", approval_id="abc")
    assert actionlog.effect_completed(db, "abc") is True


def test_budget_refund_returns_the_slot(tmp_path) -> None:
    """A failed push gives its slot back; the counter never goes negative."""
    db = make_db(tmp_path)
    for _ in range(3):
        assert budget.allow(db, "briefing", 3, "local") is True
    assert budget.allow(db, "briefing", 3, "local") is False
    budget.refund(db, "briefing", "local")
    assert budget.allow(db, "briefing", 3, "local") is True
    assert budget.allow(db, "briefing", 3, "local") is False
    budget.refund(db, "briefing", "local")
    budget.refund(db, "briefing", "local")  # floor at zero
    assert budget.allow(db, "briefing", 3, "local") is True


def _bare_runtime(db, notifier=None):
    """Runtime shell with only the fields on_reminder/run_maintenance touch."""
    from app.runtime import NullNotifier, Runtime

    rt = object.__new__(Runtime)
    rt.db = db
    rt.notifier = notifier or NullNotifier()
    return rt


def test_reminder_push_failure_requeues_then_drops(tmp_path) -> None:
    """A failing reminder push goes back to scheduled (max 3 retries), then drops."""
    import asyncio
    from datetime import UTC, datetime

    from app.core.events import REMINDER_DUE, Event
    from app.runtime import NullNotifier


    class FailingNotifier(NullNotifier):
        async def send(self, text: str, category: str) -> dict:
            raise RuntimeError("telegram down")


    db = make_db(tmp_path)
    rt = _bare_runtime(db, FailingNotifier())
    db.execute(
        "INSERT INTO reminders (id, text, fire_at, status, created_at) "
        "VALUES ('r1', 'call mom', '2000-01-01T00:00:00', 'fired', ?)",
        (datetime.now(UTC).isoformat(timespec="seconds"),),
    )
    ev = Event(REMINDER_DUE, {"id": "r1", "text": "call mom"})
    for _ in range(3):
        asyncio.run(rt.on_reminder(ev))
    row = db.query_one("SELECT status, retries FROM reminders WHERE id='r1'")
    assert row["status"] == "scheduled" and row["retries"] == 3
    asyncio.run(rt.on_reminder(ev))
    row = db.query_one("SELECT status FROM reminders WHERE id='r1'")
    assert row["status"] == "dropped"


def test_maintenance_prunes_old_incoming_sessions_only(tmp_path) -> None:
    """Retention (owner decision): incoming-% sessions >30d pruned with their
    events; live sessions and the append-only audit log survive."""
    import asyncio
    from datetime import UTC, datetime


    db = make_db(tmp_path)
    rt = _bare_runtime(db)
    # sessions/events are ADK-owned tables; create their minimal shape here.
    db.execute(
        "CREATE TABLE IF NOT EXISTS sessions (app_name VARCHAR(128) NOT NULL, "
        "user_id VARCHAR(128) NOT NULL, id VARCHAR(128) NOT NULL, state TEXT NOT NULL, "
        "create_time DATETIME NOT NULL, update_time DATETIME NOT NULL, "
        "PRIMARY KEY (app_name, user_id, id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS events (id VARCHAR(128) NOT NULL, "
        "app_name VARCHAR(128) NOT NULL, user_id VARCHAR(128) NOT NULL, "
        "session_id VARCHAR(128) NOT NULL, invocation_id VARCHAR(256) NOT NULL, "
        "timestamp DATETIME NOT NULL, event_data TEXT, "
        "PRIMARY KEY (id, app_name, user_id, session_id))"
    )
    old = "2020-01-01 00:00:00"
    db.execute(
        "INSERT INTO sessions (app_name, user_id, id, state, create_time, update_time) "
        "VALUES ('app', 'system:incoming', 'incoming-old', '{}', ?, ?)",
        (old, old),
    )
    db.execute(
        "INSERT INTO events (id, app_name, user_id, session_id, invocation_id, timestamp) "
        "VALUES ('e1', 'app', 'system:incoming', 'incoming-old', 'i1', ?)",
        (old,),
    )
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO sessions (app_name, user_id, id, state, create_time, update_time) "
        "VALUES ('app', 'tg:42', 'tg-42', '{}', ?, ?)",
        (now, now),
    )
    actionlog.log(db, "gate", "email_sent", "send", approval_id="keepme")
    asyncio.run(rt.run_maintenance("maintenance:test"))
    assert db.query_one("SELECT 1 FROM sessions WHERE id='incoming-old'") is None
    assert db.query_one("SELECT 1 FROM events WHERE session_id='incoming-old'") is None
    assert db.query_one("SELECT 1 FROM sessions WHERE id='tg-42'") is not None
    assert actionlog.recent(db, 10), "audit trail is never pruned"
