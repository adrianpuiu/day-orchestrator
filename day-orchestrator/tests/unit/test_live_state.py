"""The coordinator answers from a snapshot of the system read at the start of
every chat turn (2026-11-26: "HI" was answered with the calendar list of four
days before, labelled "confirmed" — the model had called no tool and recited
its own earlier message). Offline: scripted LLM, fake channels."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from google.genai import types as gtypes

from app.core import approval, memory, tasks
from app.core.agenda import CALENDAR_DAYS, live_state
from app.core.config import Config
from app.core.db import Database
from app.core.events import TELEGRAM_COMMAND, Event
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import (
    FakeCalendar,
    FakeGmail,
    FakeTelegram,
    RecordingLlm,
    conversation_text,
    live_state_block,
)
from app.workflows.agents import pin_live_state


def _at(day: date, hour: int, minute: int) -> str:
    """Local-wall-clock ISO value on `day` (what the calendar helper and the
    coordinator tools hand over)."""
    local = datetime.now().astimezone()
    return local.replace(year=day.year, month=day.month, day=day.day,
                         hour=hour, minute=minute).isoformat(timespec="minutes")


def _ok() -> gtypes.Content:
    return gtypes.Content(parts=[gtypes.Part.from_text(text="ok")])


def test_live_state_lists_calendar_requests_and_tasks(tmp_path) -> None:
    db = Database(tmp_path / "agenda.db")
    db.init_schema()
    today = date.today()
    fmt = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731
    in_days = lambda n: fmt(today + timedelta(days=n))  # noqa: E731
    events = [
        {"id": "ek-1", "start": _at(today, 15, 0), "end": _at(today, 16, 0),
         "summary": "Team sync", "location": "Room 4"},
        {"id": "ek-2", "start": _at(today + timedelta(days=2), 9, 0),
         "end": _at(today + timedelta(days=2), 9, 30),
         "summary": "Dentist", "location": None},
    ]
    gym = approval.create(db, "create_calendar_event", {
        "title": "Gym", "start": _at(today, 18, 0), "end": _at(today, 19, 0),
    })
    move = approval.create(db, "edit_calendar_event", {
        "event_id": "ek-2", "old_title": "Dentist",
        "new_start": _at(today + timedelta(days=3), 11, 24),
        "new_end": None, "new_title": None,
    })
    tasks.add(db, "Pay invoice #4471", due=_at(today, 10, 30))

    lines = live_state(db, events, datetime.now().astimezone()).split("\n")

    assert lines[:3] == [
        f"Calendar, {fmt(today)} → {in_days(CALENDAR_DAYS)}:",
        f"- {fmt(today)} - 15:00 → 16:00 Team sync · Room 4",
        f"- {in_days(2)} - 09:00 → 09:30 Dentist",
    ]
    assert lines[3] == "Requests awaiting approval:"
    assert sorted(lines[4:6]) == sorted([
        f'- [{gym.id}] create event "Gym" — {fmt(today)} - 18:00 → 19:00',
        f'- [{move.id}] edit "Dentist": start {in_days(3)} - 11:24',
    ])
    assert lines[6:] == [
        "Open tasks:",
        f"- [today] Pay invoice #4471 — due {fmt(today)} - 10:30",
    ]
    db.close()


def test_live_state_with_nothing_and_without_the_calendar(tmp_path) -> None:
    db = Database(tmp_path / "agenda.db")
    db.init_schema()
    now = datetime.now().astimezone()

    empty = live_state(db, [], now).split("\n")
    assert empty == [
        f"Calendar, {now:%d/%m/%Y} → {(now + timedelta(days=CALENDAR_DAYS)):%d/%m/%Y}:",
        "- no events.",
        "Requests awaiting approval: none.",
        "Open tasks:",
        "No open tasks.",
    ]

    down = live_state(db, None, now).split("\n")
    assert down[1] == (
        "- unavailable right now. Say so; never list events from memory."
    )
    assert down[2:] == empty[2:], "the rest of the state is still reported"
    db.close()


def _runtime(tmp_path, calendar: FakeCalendar) -> tuple[Runtime, RecordingLlm]:
    llm = RecordingLlm(responses=[_ok(), _ok()])
    rt = Runtime(
        cfg=Config(data_dir=tmp_path),
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=calendar,  # type: ignore[arg-type]
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        coordinator_model=llm,
    )
    rt.subscribe()
    memory.set_value(rt.db, "paired_chat", 42)
    return rt, llm


async def _say(rt: Runtime, text: str, message_id: int) -> None:
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": text, "chat_id": 42,
                                 "message_id": message_id})
    )


async def test_every_chat_turn_starts_from_a_fresh_snapshot(tmp_path) -> None:
    calendar = FakeCalendar()
    rt, llm = _runtime(tmp_path, calendar)
    today = date.today()
    fmt = today.strftime("%d/%m/%Y")
    calendar.events.append({"id": "ek-1", "title": "Team sync",
                            "start": _at(today, 15, 0), "end": _at(today, 16, 0)})

    await _say(rt, "salut", 1)
    first = live_state_block(llm.requests[0])
    assert "LIVE STATE" in first and "{live_state}" not in first
    assert llm.requests[0].contents[-1].parts[0].text == "salut", (
        "the block follows the owner's message"
    )
    assert f"- {fmt} - 15:00 → 16:00 Team sync" in first
    assert "Requests awaiting approval: none." in first
    assert "No open tasks." in first
    assert calendar.fetched == [CALENDAR_DAYS]

    # The next turn reads again: what changed in between is what the model sees.
    calendar.events.clear()
    tasks.add(rt.db, "Pay invoice #4471", due=_at(today, 10, 30))
    await _say(rt, "salut", 2)
    second = live_state_block(llm.requests[1])
    assert "Team sync" not in second and "- no events." in second
    assert f"- [today] Pay invoice #4471 — due {fmt} - 10:30" in second
    assert calendar.fetched == [CALENDAR_DAYS, CALENDAR_DAYS]
    await rt.shutdown()


async def test_snapshot_says_so_when_the_calendar_cannot_be_read(tmp_path) -> None:
    class DownCalendar(FakeCalendar):
        def get_events(self, days: int) -> list[dict]:
            del days  # the calendar is down: the window is irrelevant
            raise RuntimeError("calendar access denied in System Settings")

    rt, llm = _runtime(tmp_path, DownCalendar())

    await _say(rt, "ce am în calendar?", 1)

    block = live_state_block(llm.requests[0])
    assert "unavailable right now. Say so; never list events from memory." in block
    assert "Requests awaiting approval: none." in block
    await rt.shutdown()


def test_live_state_block_follows_a_tool_result_without_touching_it() -> None:
    """Mid-turn the request ends with a tool result; the block goes after it,
    leaving the call/result pair intact."""
    call = gtypes.Content(
        role="model",
        parts=[gtypes.Part(function_call=gtypes.FunctionCall(name="add_task"))],
    )
    result = gtypes.Content(
        role="user",
        parts=[gtypes.Part(
            function_response=gtypes.FunctionResponse(name="add_task", response={"ok": True})
        )],
    )

    contents = pin_live_state("Calendar: no events.", [call, result])

    assert contents[:2] == [call, result]
    assert len(contents) == 3 and contents[2].role == "user"
    assert contents[2].parts[0].text.endswith("Calendar: no events.")


async def test_a_conversation_is_context_only_until_it_goes_quiet(
    tmp_path, monkeypatch
) -> None:
    """One endless session is how "HI" on 2026-11-26 got the calendar of
    2026-11-22 back. An active conversation keeps its history; after a silence
    the next message starts without it."""
    import time

    llm = RecordingLlm(responses=[_ok(), _ok(), _ok()])
    rt = Runtime(
        cfg=Config(data_dir=tmp_path),
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        coordinator_model=llm,
    )
    rt.subscribe()
    memory.set_value(rt.db, "paired_chat", 42)

    await _say(rt, "primul mesaj", 1)
    await _say(rt, "al doilea mesaj", 2)
    assert "al doilea mesaj" not in conversation_text(llm.requests[0])
    assert "primul mesaj" in conversation_text(llm.requests[1]), (
        "an active conversation keeps its history"
    )

    later = time.time() + Runtime.CHAT_MEMORY_IDLE_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    await _say(rt, "HI", 3)
    cold = conversation_text(llm.requests[2])
    assert "HI" in cold
    assert "primul mesaj" not in cold and "al doilea mesaj" not in cold, (
        "a conversation that went quiet is not context for the next one"
    )
    await rt.shutdown()
