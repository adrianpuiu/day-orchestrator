"""Chat-side cancellation of pending approvals (ADR-004 amendment):
revocation resolves rows, finalizes exactly the pending ones, never touches
the calendar, and missing events fail fast instead of carding a doomed
effect."""

from __future__ import annotations

from app.core import actionlog, approval
from app.core.db import Database
from app.tools import ToolDeps, make_tools


def _tool_map(deps: ToolDeps) -> dict:
    return {t.name: t for t in make_tools(deps)}


async def test_cancel_resolves_only_pending_rows(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    keep = approval.create(
        db, "create_calendar_event",
        {"title": "Spălat pe dinți", "start": "2026-11-23T12:00",
         "end": "2026-11-23T12:30"},
    )
    untouched = approval.create(
        db, "send_email", {"to": "a@b.c", "subject": "s", "body": "b"},
    )
    done = approval.create(
        db, "send_email", {"to": "c@d.e", "subject": "s", "body": "b"},
    )
    approval.resolve(db, done.id, "approved", "telegram:approved")

    finalized: list[list[str]] = []

    async def finalizer(rows: list[approval.Approval]) -> None:
        finalized.append([r.id for r in rows])

    tools = _tool_map(ToolDeps(
        db=db, gmail=None, calendar=None, notifier=None, budgets={},
        approval_finalizer=finalizer,
    ))
    assert "cancel_pending_approvals" in tools

    res = await tools["cancel_pending_approvals"].func(
        approval_ids=[keep.id, done.id, "deadbeef"]
    )

    assert res["status"] == "success"
    assert res["cancelled"] == [keep.id]
    assert {s["id"] for s in res["skipped"]} == {done.id, "deadbeef"}
    assert approval.get(db, keep.id).state == "cancelled"
    # Rows the owner did not name stay pending; resolved rows stay as they were.
    assert approval.get(db, untouched.id).state == "pending"
    assert approval.get(db, done.id).state == "approved"
    # Only the newly cancelled rows reach the finalizer (card close + resume).
    assert finalized == [[keep.id]]
    actions = [r["action"] for r in actionlog.recent(db, limit=10)]
    assert "approval_cancelled" in actions


async def test_cancel_without_any_pending_row_is_a_noop(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    tools = _tool_map(ToolDeps(
        db=db, gmail=None, calendar=None, notifier=None, budgets={},
    ))

    res = await tools["cancel_pending_approvals"].func(approval_ids=["nope"])

    assert res == {"status": "noop", "cancelled": [],
                   "skipped": [{"id": "nope", "reason": "unknown"}]}


async def test_delete_missing_event_fails_fast_before_the_gate(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()

    class MissingCalendar:
        def get_event_meta(self, event_id: str) -> None:
            return None

    stub = MissingCalendar()
    tools = _tool_map(ToolDeps(
        db=db, gmail=None, calendar=stub, notifier=None, budgets={},
        calendar_writer=stub,
    ))

    res = await tools["delete_calendar_event"].func(None, event_id="ek-404")

    assert res["status"] == "error"
    assert "not on the calendar" in res["error"]
    # No approval row, no card-worthy pause: the doomed effect never gated.
    assert approval.list_pending(db) == []


async def test_edit_missing_event_fails_fast_before_the_gate(tmp_path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()

    class MissingCalendar:
        def get_event_meta(self, event_id: str) -> None:
            return None

    stub = MissingCalendar()
    tools = _tool_map(ToolDeps(
        db=db, gmail=None, calendar=stub, notifier=None, budgets={},
        calendar_writer=stub,
    ))

    res = await tools["edit_calendar_event"].func(
        None, event_id="ek-404", new_start="2026-11-24T10:00"
    )

    assert res["status"] == "error"
    assert "not on the calendar" in res["error"]
    assert approval.list_pending(db) == []
