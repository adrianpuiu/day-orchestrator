"""Integration proof of the approval gate (ADR-004): pause → approve → send,
and pause → reject → nothing sent. Fully offline (fake channels, scripted LLM)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from google.genai import types as gtypes

from app.core import approval, memory
from app.core.config import Config
from app.core.events import TELEGRAM_COMMAND, Event
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import (
    FakeCalendar,
    FakeGmail,
    FakeTelegram,
    ScriptedLlm,
    run_gate_scenario,
)
from app.tools import make_tools


async def test_approval_gate_approve_then_send(tmp_path: Path) -> None:
    result = await run_gate_scenario(tmp_path, verdict="approved")

    assert result["approval_state"] == "approved"
    assert result["sent"], "approved email must actually send via the transport"
    sent = result["sent"][0]
    assert sent["to"] == "bob@example.com"
    assert sent["subject"] == "Lunch on Tuesday?"

    actions = {row["action"] for row in result["actions"]}
    assert "email_sent" in actions, "every send must land in the action log"


async def test_approval_gate_reject_never_sends(tmp_path: Path) -> None:
    result = await run_gate_scenario(tmp_path, verdict="rejected")

    assert result["approval_state"] == "rejected"
    assert result["sent"] == [], "rejected email must never reach the transport"

    actions = {row["action"] for row in result["actions"]}
    assert "send_rejected" in actions
    assert "email_sent" not in actions


async def test_approval_gate_calendar_write(tmp_path: Path) -> None:
    result = await run_gate_scenario(
        tmp_path,
        verdict="approved",
        function_call=("add_calendar_event", {
            "title": "Plată factură curent",
            "start": "2026-11-23T10:30",
            "end": "2026-11-23T11:00",
        }),
    )

    assert result["approval_state"] == "approved"
    assert result["events"], "approved calendar write must create the event"
    assert result["events"][0]["title"] == "Plată factură curent"

    actions = {row["action"] for row in result["actions"]}
    assert "calendar_event_created" in actions
    assert "email_sent" not in actions


async def test_approval_gate_calendar_reject_never_writes(tmp_path: Path) -> None:
    result = await run_gate_scenario(
        tmp_path,
        verdict="rejected",
        function_call=("add_calendar_event", {
            "title": "Dinner", "start": "2026-11-24T19:00", "end": "2026-11-24T21:00",
        }),
    )

    assert result["approval_state"] == "rejected"
    assert result["events"] == [], "rejected calendar write must never happen"
    assert "calendar_event_created" not in {r["action"] for r in result["actions"]}


async def test_approval_gate_geocodes_location(tmp_path: Path) -> None:
    result = await run_gate_scenario(
        tmp_path,
        verdict="approved",
        function_call=("add_calendar_event", {
            "title": "Dentist",
            "start": "2026-11-24T14:00",
            "end": "2026-11-24T15:00",
            "location": "Strada Exemplu 10, București",
        }),
    )

    assert result["events"][0]["location"].endswith("(validated)"), (
        "the raw location must be canonicalized by geocoding before the card"
    )


async def test_approval_gate_edit_calendar_event(tmp_path: Path) -> None:
    result = await run_gate_scenario(
        tmp_path,
        verdict="approved",
        function_call=("edit_calendar_event", {
            "event_id": "ek-1", "new_start": "2026-11-25T10:00",
        }),
        calendar_seed=[{"id": "ek-1", "title": "Dentist",
                        "start": "2026-11-24T10:00", "end": "2026-11-24T11:00"}],
    )

    assert result["approval_state"] == "approved"
    assert result["events"][0]["start"] == "2026-11-25T10:00", (
        "approved edit must move exactly the seeded occurrence"
    )
    actions = {row["action"] for row in result["actions"]}
    assert "calendar_event_updated" in actions
    assert "email_sent" not in actions


async def test_approval_gate_delete_calendar_event(tmp_path: Path) -> None:
    result = await run_gate_scenario(
        tmp_path,
        verdict="approved",
        function_call=("delete_calendar_event", {"event_id": "ek-1"}),
        calendar_seed=[{"id": "ek-1", "title": "Dentist",
                        "start": "2026-11-24T10:00", "end": "2026-11-24T11:00"}],
    )

    assert result["approval_state"] == "approved"
    assert result["events"] == [], "approved delete must remove the event"
    actions = {row["action"] for row in result["actions"]}
    assert "calendar_event_deleted" in actions


async def test_approval_gate_delete_rejected_keeps_event(tmp_path: Path) -> None:
    result = await run_gate_scenario(
        tmp_path,
        verdict="rejected",
        function_call=("delete_calendar_event", {"event_id": "ek-1"}),
        calendar_seed=[{"id": "ek-1", "title": "Dentist",
                        "start": "2026-11-24T10:00", "end": "2026-11-24T11:00"}],
    )

    assert result["approval_state"] == "rejected"
    assert len(result["events"]) == 1, "rejected delete must keep the event"
    assert "calendar_event_deleted" not in {r["action"] for r in result["actions"]}


async def test_chat_cancellation_closes_card_and_resumes_run(tmp_path: Path) -> None:
    """The 2026-11-23 incident: "delete the pending ones" must cancel the
    requests (rows + cards + paused runs) and never reach for a calendar
    delete on an event that does not exist."""
    cfg = Config(data_dir=tmp_path)
    telegram = FakeTelegram()
    calendar = FakeCalendar()
    runtime = Runtime(
        cfg=cfg,
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=calendar,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        coordinator_model=ScriptedLlm(
            responses=[
                gtypes.Content(
                    parts=[
                        gtypes.Part(
                            function_call=gtypes.FunctionCall(
                                id="fc_add_1",
                                name="add_calendar_event",
                                args={"title": "Dentist",
                                      "start": "2026-11-24T14:00",
                                      "end": "2026-11-24T15:00"},
                            )
                        )
                    ]
                ),
            ]
        ),
    )
    runtime.subscribe()
    memory.set_value(runtime.db, "paired_chat", 42)
    await runtime.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "dentist tue 14:00", "chat_id": 42,
                                 "message_id": 1})
    )
    pending = approval.list_pending(runtime.db)
    assert len(pending) == 1 and len(telegram.cards) == 1

    tools = {t.name: t for t in make_tools(runtime.deps)}
    res = await tools["cancel_pending_approvals"].func(
        approval_ids=[p.id for p in pending]
    )
    assert res["cancelled"] == [pending[0].id]
    assert runtime._bg_tasks, "cancellation must schedule the cleanup"
    await asyncio.gather(*runtime._bg_tasks)  # runs after the chat turn

    assert approval.get(runtime.db, pending[0].id).state == "cancelled"
    assert any("cancelled — by owner in chat" in e for e in telegram.edits), (
        "the card must close so no button stays live"
    )
    assert calendar.events == [], "a cancelled request must never write"
    await runtime.shutdown()
