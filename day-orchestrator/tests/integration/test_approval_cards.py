"""Approval cards after the pause (ADR-004): several cards raised in one turn
each resume on their own, the owner learns what actually happened, and a
request that replaces a pending one never leaves the old card live.

Reproduces the 2026-11-22/23 incidents from the live session + stderr log:
"approval <id>: no pending confirmation" for every card but the last, cards
stuck at "approved — sending", three pending moves of one dentist event.
Offline: fake channels + a scripted LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from google.genai import types as gtypes

from app.core import approval, memory
from app.core.config import Config
from app.core.events import TELEGRAM_APPROVAL, TELEGRAM_COMMAND, Event
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import FakeCalendar, FakeGmail, FakeTelegram, ScriptedLlm


def _call(name: str, call_id: str, args: dict) -> gtypes.Part:
    return gtypes.Part(
        function_call=gtypes.FunctionCall(id=call_id, name=name, args=args)
    )


def _runtime(
    tmp_path: Path, responses: list[gtypes.Content], calendar: FakeCalendar | None = None
) -> tuple[Runtime, FakeTelegram, FakeCalendar]:
    telegram = FakeTelegram()
    calendar = calendar or FakeCalendar()
    runtime = Runtime(
        cfg=Config(data_dir=tmp_path),
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=calendar,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        coordinator_model=ScriptedLlm(responses=responses),
    )
    runtime.subscribe()
    memory.set_value(runtime.db, "paired_chat", 42)
    return runtime, telegram, calendar


async def _say(runtime: Runtime, text: str, message_id: int) -> None:
    await runtime.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": text, "chat_id": 42,
                                 "message_id": message_id})
    )


async def _approve(runtime: Runtime, approval_id: str, message_id: int,
                   verdict: str = "approved") -> None:
    await runtime.bus.publish(
        Event(TELEGRAM_APPROVAL, {"approval_id": approval_id, "verdict": verdict,
                                  "chat_id": 42, "message_id": message_id})
    )


async def test_every_card_of_a_multi_card_turn_resumes(tmp_path: Path) -> None:
    """Four cards raised in one turn, approved out of order: four events."""
    blocks = [
        {"title": f"Block {i}", "start": f"2026-11-24T1{i}:00",
         "end": f"2026-11-24T1{i}:30"}
        for i in range(4)
    ]
    runtime, telegram, calendar = _runtime(
        tmp_path,
        [gtypes.Content(parts=[
            _call("add_calendar_event", f"fc_block_{i}", block)
            for i, block in enumerate(blocks)
        ])],
    )
    await _say(runtime, "block tomorrow 10:00 to 13:30", 1)
    pending = approval.list_pending(runtime.db)
    assert len(pending) == 4 and len(telegram.cards) == 4

    for n, row in enumerate(reversed(pending)):  # the owner taps bottom-up
        await _approve(runtime, row.id, 10 + n)

    assert sorted(e["title"] for e in calendar.events) == sorted(
        b["title"] for b in blocks
    ), "each approved card must apply its own effect"
    edits = "\n".join(telegram.edits)
    assert "FAILED to resume" not in edits
    assert edits.count("[✅ created]") == 4
    await runtime.shutdown()


async def test_approved_card_reports_the_outcome_and_delivers_the_reply(
    tmp_path: Path,
) -> None:
    """After approval the card says what happened, and the coordinator's reply
    to the tool result reaches the owner instead of being dropped."""
    runtime, telegram, calendar = _runtime(
        tmp_path,
        [
            gtypes.Content(parts=[_call("add_calendar_event", "fc_ci", {
                "title": "Dentist", "start": "2026-11-27T11:24",
                "end": "2026-11-27T12:24", "location": "Str. Exemplu 10, București",
            })]),
            gtypes.Content(parts=[gtypes.Part.from_text(
                text="Added: Dentist, 27/11/2026 - 11:24, at Str. Exemplu 10, București."
            )]),
        ],
    )
    await _say(runtime, "dentist thursday 11:24", 1)
    (row,) = approval.list_pending(runtime.db)
    await _approve(runtime, row.id, 2)

    assert len(calendar.events) == 1
    assert telegram.edits[-1].endswith("[✅ created]")
    assert any("Added: Dentist" in m for m in telegram.messages), (
        "the reply that follows the tool result is the owner's confirmation"
    )
    await runtime.shutdown()


async def test_failed_effect_is_on_the_card(tmp_path: Path) -> None:
    class RefusingCalendar(FakeCalendar):
        def create_event(self, *args, **kwargs) -> str:
            raise RuntimeError("calendar access denied")

    runtime, telegram, calendar = _runtime(
        tmp_path,
        [gtypes.Content(parts=[_call("add_calendar_event", "fc_ci", {
            "title": "Dentist", "start": "2026-11-27T11:24",
            "end": "2026-11-27T12:24",
        })])],
        calendar=RefusingCalendar(),
    )
    await _say(runtime, "dentist thursday 11:24", 1)
    (row,) = approval.list_pending(runtime.db)
    await _approve(runtime, row.id, 2)

    assert calendar.events == []
    assert telegram.edits[-1].endswith(
        "[❌ failed — it will NOT be retried automatically]"
    )
    await runtime.shutdown()


async def test_rejected_card_stays_rejected_and_writes_nothing(tmp_path: Path) -> None:
    runtime, telegram, calendar = _runtime(
        tmp_path,
        [gtypes.Content(parts=[_call("add_calendar_event", "fc_ci", {
            "title": "Dentist", "start": "2026-11-27T11:24",
            "end": "2026-11-27T12:24",
        })])],
    )
    await _say(runtime, "dentist thursday 11:24", 1)
    (row,) = approval.list_pending(runtime.db)
    await _approve(runtime, row.id, 2, verdict="rejected")

    assert calendar.events == []
    assert telegram.edits[-1].endswith("[rejected]")
    await runtime.shutdown()


async def test_new_move_replaces_the_pending_move_of_the_same_event(
    tmp_path: Path,
) -> None:
    """Three dentist moves were pending at once on 2026-11-22; the owner had
    to reject the stale cards by hand. A newer card for the same event now
    retires the older ones; only the approved move may apply."""
    runtime, telegram, calendar = _runtime(
        tmp_path,
        [
            gtypes.Content(parts=[_call("edit_calendar_event", "fc_move_1", {
                "event_id": "ek-1", "new_start": "2026-11-24T14:00",
                "new_end": "2026-11-24T15:00",
            })]),
            gtypes.Content(parts=[_call("edit_calendar_event", "fc_move_2", {
                "event_id": "ek-1", "new_start": "2026-11-27T11:24",
                "new_end": "2026-11-27T12:24",
            })]),
        ],
    )
    calendar.events.append({"id": "ek-1", "title": "Dentist",
                            "start": "2026-11-26T14:00",
                            "end": "2026-11-26T15:00"})
    await _say(runtime, "move the dentist to tuesday 14:00", 1)
    (first,) = approval.list_pending(runtime.db)
    await _say(runtime, "no, thursday 11:24", 2)
    await asyncio.gather(*runtime._bg_tasks)  # card close runs after the turn

    pending = approval.list_pending(runtime.db)
    assert [p.id for p in pending] and pending[0].id != first.id, (
        "only the newer move may still await approval"
    )
    retired = approval.get(runtime.db, first.id)
    assert retired is not None and retired.state == "superseded"
    assert any("superseded" in e for e in telegram.edits), (
        "the replaced card must close so no stale button stays live"
    )

    await _approve(runtime, pending[0].id, 3)
    assert calendar.events[0]["start"] == "2026-11-27T11:24"
    assert calendar.update_calls == ["ek-1"], "the retired move must never apply"
    await runtime.shutdown()


async def test_unrelated_pending_cards_are_kept(tmp_path: Path) -> None:
    """Superseding is per event: a card for another event stays live."""
    runtime, telegram, calendar = _runtime(
        tmp_path,
        [
            gtypes.Content(parts=[_call("edit_calendar_event", "fc_move_1", {
                "event_id": "ek-1", "new_start": "2026-11-24T14:00",
                "new_end": "2026-11-24T15:00",
            })]),
            gtypes.Content(parts=[_call("edit_calendar_event", "fc_move_2", {
                "event_id": "ek-2", "new_start": "2026-11-24T14:00",
                "new_end": "2026-11-24T15:00",
            })]),
        ],
    )
    for event_id in ("ek-1", "ek-2"):
        calendar.events.append({"id": event_id, "title": event_id,
                                "start": "2026-11-26T14:00",
                                "end": "2026-11-26T15:00"})
    await _say(runtime, "move ek-1 to tuesday", 1)
    (first,) = approval.list_pending(runtime.db)
    await _say(runtime, "move ek-2 to tuesday", 2)
    await asyncio.gather(*runtime._bg_tasks)

    pending_ids = {p.id for p in approval.list_pending(runtime.db)}
    assert first.id in pending_ids and len(pending_ids) == 2
    assert not any("superseded" in e for e in telegram.edits)
    await runtime.shutdown()
