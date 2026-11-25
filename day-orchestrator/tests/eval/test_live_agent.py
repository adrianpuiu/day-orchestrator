"""Live agent evals (rule-graded, no LLM judge) — run with:

    RUN_LIVE=1 uv run pytest tests/eval -q

Each case runs the real pipeline against the configured model (config.toml)
with fake channels; assertions check the behavior the architecture promises:
correct triage importance, honest section structure, correct tool routing.
Skipped by default so `uv run pytest` stays offline and fast.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import approval, memory, tasks
from app.core.config import Config
from app.core.events import TELEGRAM_APPROVAL, TELEGRAM_COMMAND, Event
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import FakeCalendar, FakeGmail, FakeTelegram

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE"),
    reason="live model evals: set RUN_LIVE=1",
)


def make_email(uid: str, subject: str, snippet: str, from_: str = "a@b.com") -> dict:
    # Received an hour ago: a brief covers mail since the previous brief.
    received = datetime.now(UTC) - timedelta(hours=1)
    return {
        "uid": uid, "message_id": f"<{uid}@x>", "subject": subject,
        "from_": from_, "snippet": snippet,
        "date": received.isoformat(),
    }


@pytest.fixture()
async def runtime() -> AsyncIterator[Runtime]:
    cfg = dataclasses.replace(Config.load(), data_dir=Path(tempfile.mkdtemp()))
    rt = Runtime(
        cfg=cfg,
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        telegram=FakeTelegram(),  # type: ignore[arg-type]
    )
    rt.subscribe()  # bus handlers otherwise register only in start()
    yield rt
    await rt.shutdown()


# --- triage judgment: the classes that matter, graded by rule -----------------

TRIAGE_CASES = [
    # (subject, snippet, rule(importance, category))
    ("Your verification code is 482910",
     "Use this code to log in. It expires in 5 minutes.",
     lambda imp, cat: imp == "low"),  # OTP must never be urgent
    ("Weekly digest: 10 links you missed",
     "This week in technology news and articles.",
     lambda imp, cat: imp == "low"),
    ("Invoice #4471 is 7 days overdue",
     "Pay invoice #4471 today to avoid service suspension.",
     lambda imp, cat: imp in {"high", "normal"}),  # action + money
    ("Can we move our call to Thursday?",
     "Hey, does Thursday 15:00 work for our weekly sync?",
     lambda imp, cat: cat in {"needs_reply", "meeting"}),
]


@pytest.mark.parametrize("subject,snippet,rule", TRIAGE_CASES)
async def test_triage_classification(
    runtime: Runtime, subject: str, snippet: str, rule: Callable
) -> None:
    rt = runtime
    rt.gmail.msgs = [make_email("1", subject, snippet)]
    await rt.on_new_mail(Event("gmail.new_messages", {"messages": rt.gmail.msgs}))

    rows = rt.db.query(
        "SELECT summary, category, importance FROM email_triage"
    )
    items = [dict(r) for r in rows]
    assert items, f"triage produced no items for {subject!r}"
    item = items[0]
    imp, cat = item.get("importance"), item.get("category")
    assert rule(imp, cat), (
        f"{subject!r}: got importance={imp!r} category={cat!r}"
    )


# --- planner structure: sections exist, carry-over format, no invented times --

async def test_brief_structure_and_no_invented_times(runtime: Runtime) -> None:
    rt = runtime
    rt.gmail.msgs = [make_email("1", "Invoice overdue", "Pay invoice #4471 today.")]
    tasks.add(rt.db, "Pay invoice #4471", due="2026-11-22T14:00")

    await rt.run_briefing("eval-brief")
    brief = "\n".join(rt.notifier.messages)
    for section in ("Day plan", "Schedule", "Tasks", "Needs attention"):
        assert section in brief, f"brief missing section {section!r}"
    assert "[today" in brief and "[overdue]" not in brief  # carry-over format
    times = set(re.findall(r"\b\d{2}:\d{2}\b", brief))
    assert times <= {"14:00"}, f"brief invented times: {times}"


# --- coordinator tool routing -------------------------------------------------

async def test_coordinator_adds_task_via_tool(runtime: Runtime) -> None:
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "add task buy milk tomorrow at 14:00",
                                 "chat_id": 42, "message_id": 1})
    )
    rows = tasks.list_open(rt.db)
    assert rows, "coordinator did not create the task"
    due = rows[0].get("due") or ""
    assert "14:00" in due, f"task due lost the time: {due!r}"
    bucket = tasks.bucket(rows[0], tasks.local_today(rt.cfg.timezone), rt.cfg.timezone)
    assert bucket == "tomorrow", (
        f"'tomorrow' resolved to the wrong date: due={due!r} bucket={bucket!r}"
    )


async def test_coordinator_email_goes_through_gate(runtime: Runtime) -> None:
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "send an email to bob@example.com "
                                         "saying lunch on tuesday works",
                                 "chat_id": 42, "message_id": 2})
    )
    approvals = approval.list_pending(rt.db)
    assert approvals, "send_email must pause behind an approval row"
    assert rt.gmail.sent == [], "nothing may send without approval"


async def test_coordinator_calendar_write_goes_through_gate(runtime: Runtime) -> None:
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "put dinner with Ana in my calendar "
                                         "tomorrow at 19:00",
                                 "chat_id": 42, "message_id": 3})
    )
    approvals = approval.list_pending(rt.db)
    assert approvals, "add_calendar_event must pause behind an approval row"
    kind = approvals[0].kind
    assert kind == "create_calendar_event", f"wrong approval kind: {kind!r}"
    assert rt.deps.calendar_writer.events == [], (
        "nothing may be written to the calendar without approval"
    )


async def test_w2_pipeline_persists_triage(runtime: Runtime) -> None:
    """W2's PersistTriage stage must land classifications in email_triage —
    the sweep and the brief read the table, never session memory."""
    rt = runtime
    rows = rt.db.query("SELECT uid FROM email_triage")
    assert not rows, "eval starts with a clean triage table"

    from app.workflows.pipelines import make_incoming_pipeline

    pipeline = make_incoming_pipeline(rt.deps, rt.cfg, [
        {"uid": "<quote@x>", "message_id": "<quote@x>",
         "subject": "Question about the quote",
         "snippet": "Hi, can you send me the updated quote for the website?",
         "date": "2026-11-22T10:00:00+00:00"},
    ])
    await rt._run_agent(
        pipeline, "test:w2", "w2-persist",
        _content("run"),
    )
    rows = rt.db.query("SELECT uid, category FROM email_triage")
    assert rows, "W2 must persist triage results deterministically"


def _content(text: str):
    from google.genai import types as gtypes

    return gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=text)])


async def test_coordinator_edit_goes_through_gate(runtime: Runtime) -> None:
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    from datetime import datetime, timedelta

    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    rt.deps.calendar_writer.events.append({
        "id": "ek-dentist", "title": "Dentist",
        "start": tomorrow.isoformat(timespec="minutes"),
        "end": (tomorrow + timedelta(hours=1)).isoformat(timespec="minutes"),
        "notes": "", "invitees": [], "alarm_minutes": 0, "location": "",
    })
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "move the dentist to the day after "
                                         "tomorrow at 15:00",
                                 "chat_id": 42, "message_id": 5})
    )
    pending = approval.list_pending(rt.db)
    assert pending and pending[0].kind == "edit_calendar_event", (
        f"move must raise an edit card; got {[a.kind for a in pending]!r}"
    )
    assert rt.deps.calendar_writer.update_calls == [], (
        "nothing may move before approval"
    )
    await rt.bus.publish(
        Event(TELEGRAM_APPROVAL, {"approval_id": pending[0].id,
                                  "verdict": "approved", "chat_id": 42,
                                  "message_id": 6})
    )
    assert rt.deps.calendar_writer.update_calls == ["ek-dentist"], (
        "approved edit must apply the move"
    )


async def test_coordinator_delete_goes_through_gate(runtime: Runtime) -> None:
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    rt.deps.calendar_writer.events.append({
        "id": "ek-coffee", "title": "Cafea cu Diana",
        "start": "2026-11-23T17:30", "end": "2026-11-23T18:30",
        "notes": "", "invitees": [], "alarm_minutes": 0, "location": "",
    })
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "delete the coffee with Diana event",
                                 "chat_id": 42, "message_id": 7})
    )
    pending = approval.list_pending(rt.db)
    assert pending and pending[0].kind == "delete_calendar_event", (
        f"delete must raise a delete card; got {[a.kind for a in pending]!r}"
    )
    await rt.bus.publish(
        Event(TELEGRAM_APPROVAL, {"approval_id": pending[0].id,
                                  "verdict": "approved", "chat_id": 42,
                                  "message_id": 8})
    )
    assert rt.deps.calendar_writer.get_event_meta("ek-coffee") is None, (
        "approved delete must remove the event"
    )


async def test_coordinator_answers_from_live_state(
    runtime: Runtime, monkeypatch
) -> None:
    """2026-11-26: three days after the last chat turn, "HI" was answered with
    the calendar list of 2026-11-22, labelled "confirmed" and "azi". Calendar,
    pending and task state comes from the snapshot read at the start of every
    turn — never from the conversation."""
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    events = rt.deps.calendar_writer.events
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    at = lambda h, m: tomorrow.replace(  # noqa: E731
        hour=h, minute=m, second=0, microsecond=0
    ).isoformat(timespec="minutes")
    stale = {"id": "ek-old", "title": "Old planning call", "start": at(10, 0),
             "end": at(11, 0), "notes": "", "invitees": [], "alarm_minutes": 0,
             "location": ""}
    current = {"id": "ek-1", "title": "Vizionare apartament",
               "start": at(15, 0), "end": at(16, 0), "notes": "", "invitees": [],
               "alarm_minutes": 0, "location": ""}
    events.append(stale)
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "ce am în calendar?", "chat_id": 42,
                                 "message_id": 1})
    )
    assert "Old planning call" in rt.telegram.messages[-1], (
        "the first answer must list the event that exists"
    )

    events.remove(stale)  # removed in Calendar.app since the last turn
    events.append(current)
    for message_id, text in enumerate(("HI", "salut, ce mai e pe azi?"), start=2):
        await rt.bus.publish(
            Event(TELEGRAM_COMMAND, {"text": text, "chat_id": 42,
                                     "message_id": message_id})
        )
        reply = rt.telegram.messages[-1]
        assert "Old planning call" not in reply, (
            f"{text!r}: a removed event came back from conversation memory: {reply!r}"
        )

    # …and a conversation that went quiet for the night is over: its history
    # is not context for the next day.
    events.remove(current)
    later = time.time() + 9 * 3600
    monkeypatch.setattr(time, "time", lambda: later)
    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "HI", "chat_id": 42, "message_id": 4})
    )
    reply = rt.telegram.messages[-1]
    assert "Vizionare apartament" not in reply, (
        f"a cold conversation came back as current state: {reply!r}"
    )


# --- brief readability: same local model judges against a fixed rubric --------

async def test_brief_readability_judge(runtime: Runtime) -> None:
    """ds41 grades its own brief on a fixed rubric (self-judge bias is known —
    the value is drift detection: if brief quality collapses, this fails)."""

    from openai import AsyncOpenAI

    rt = runtime
    rt.gmail.msgs = [
        make_email("1", "Invoice overdue", "Pay invoice #4471 today."),
        make_email("2", "Team offsite notes", "Notes from yesterday's offsite."),
    ]
    tasks.add(rt.db, "Pay invoice #4471", due="2026-11-22T14:00")
    await rt.run_briefing("eval-judge")
    brief = "\n".join(rt.notifier.messages)
    assert brief.strip(), "no brief was produced to judge"

    client = AsyncOpenAI(base_url=rt.cfg.model_api_base, api_key=rt.cfg.model_api_key)
    judge = await client.chat.completions.create(
        model=rt.cfg.coordinator_model,
        messages=[
            {"role": "system", "content":
                "You are a strict judge of daily briefs written for a busy owner. "
                "Grade the brief below on three axes, 0-5 each: grounding (every "
                "claim traceable to the inputs), conciseness (<=250 words, no "
                "fluff), actionability (owner knows exactly what to do today). "
                "Respond ONLY with JSON: {\"grounding\": n, \"conciseness\": n, "
                "\"actionability\": n, \"reason\": \"one sentence\"}"},
            {"role": "user", "content": f"Inputs the brief was built from:\n"
             "emails: invoice #4471 overdue; team offsite notes.\n"
             "tasks: 'Pay invoice #4471' due 14:00 today.\n"
             f"\nBrief to judge:\n{brief}"},
        ],
        temperature=0,
        max_tokens=300,
    )
    raw = judge.choices[0].message.content or "{}"
    from app.workflows.pipelines import _extract_json

    verdict = _extract_json(raw)
    avg = (verdict.get("grounding", 0) + verdict.get("conciseness", 0)
           + verdict.get("actionability", 0)) / 3
    assert avg >= 3.5, (
        f"brief quality below bar (avg {avg:.1f}/5): {verdict.get('reason')!r}\n{brief[:400]}"
    )


# --- multi-turn: session continuity + tool use across turns --------------------

async def test_coordinator_multi_turn(runtime: Runtime) -> None:
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    replies: list[str] = []
    orig = rt.notifier.send

    async def tee(text: str, category: str) -> dict:
        result = await orig(text, category)
        replies.append(text)
        return result

    rt.notifier.send = tee
    rt.deps.notifier.send = tee
    orig_sm = rt.telegram.send_message

    async def tee_sm(text: str, category: str, reply_markup=None) -> dict:
        result = await orig_sm(text, category, reply_markup)
        replies.append(text)  # coordinator replies go through send_message
        return result

    rt.telegram.send_message = tee_sm

    async def say(text: str, mid: int) -> None:
        await rt.bus.publish(
            Event(TELEGRAM_COMMAND, {"text": text, "chat_id": 42, "message_id": mid})
        )

    await say("add task buy milk tomorrow at 14:00", 1)
    assert tasks.list_open(rt.db), "turn 1: task not created"
    await say("what are my open tasks?", 2)
    assert replies and "milk" in replies[-1].lower(), (
        f"turn 2: answer lost session context: {replies[-1:]!r}"
    )
    await say("mark the milk task as done", 3)
    assert tasks.list_open(rt.db) == [], "turn 3: task not completed"


async def test_coordinator_proposes_target_for_underspecified_move(runtime: Runtime) -> None:
    """'move the dentist' without a target: the coordinator must PROPOSE a
    concrete conflict-free slot — a card with a chosen target, explained in
    chat — and not touch the calendar before approval."""
    rt = runtime
    memory.set_value(rt.db, "paired_chat", 42)
    replies: list[str] = []
    orig_sm = rt.telegram.send_message

    async def tee_sm(text: str, category: str, reply_markup=None) -> dict:
        result = await orig_sm(text, category, reply_markup)
        replies.append(text)
        return result

    rt.telegram.send_message = tee_sm

    # Seed a real upcoming appointment so there is something to move.
    from datetime import datetime, timedelta

    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    rt.deps.calendar_writer.events.append({
        "id": "ek-dentist",
        "title": "Dentist",
        "start": tomorrow.isoformat(timespec="minutes"),
        "end": (tomorrow + timedelta(hours=1)).isoformat(timespec="minutes"),
        "notes": "", "invitees": [], "alarm_minutes": 0,
    })

    await rt.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "move the dentist appointment",
                                 "chat_id": 42, "message_id": 4})
    )

    pending = approval.list_pending(rt.db)
    assert pending and pending[0].kind == "edit_calendar_event", (
        "underspecified move should produce an edit proposal card; "
        f"got pending={[a.kind for a in pending]!r}, replies={replies[-2:]!r}"
    )
    payload = pending[0].payload
    assert payload.get("new_start"), "proposal card must carry a concrete target"
    assert rt.deps.calendar_writer.update_calls == [], (
        "nothing may be moved before approval"
    )
