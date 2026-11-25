"""The morning brief as the owner reads it (W1): one task per email no matter
how often the same mail is read, tasks extracted by the brief shown in that
brief, timed tasks in the schedule, and mail scoped to the window since the
previous brief.

Reproduces the 2026-11-25 live brief: 19 tasks of which 13 were re-extractions
of the same emails, "Schedule: No events" over four timed commitments, and
the day's new tasks missing until the next morning. Offline: fake channels,
scripted LLM.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from google.genai import types as gtypes

from app.core import actionlog, tasks
from app.core.config import Config
from app.core.db import Database
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import (
    FakeCalendar,
    FakeGmail,
    FakeTelegram,
    RecordingLlm,
    system_instruction,
)
from app.workflows import pipelines
from app.workflows.pipelines import _schedule_lines, briefing_loader

EMPTY_BUCKETS = {"overdue": [], "today": [], "tomorrow": [], "later": [], "someday": []}


# --- task store: one task per source email -----------------------------------


def test_add_with_source_ref_never_duplicates_or_resurrects(tmp_path) -> None:
    db = Database(tmp_path / "tasks.db")
    db.init_schema()

    first = tasks.add(db, "Pay invoice #4471", source="email", source_ref="17")
    again = tasks.add(db, "Pay invoice #4471 (overdue)", source="email",
                      source_ref="17")
    assert first["created"] is True and again["created"] is False
    assert again["id"] == first["id"], "the same email must fold onto its task"
    assert len(tasks.list_open(db)) == 1

    tasks.complete(db, first["id"])
    repeated = tasks.add(db, "Pay invoice #4471", source="email", source_ref="17")
    assert repeated["created"] is False and repeated["status"] == "done"
    assert tasks.list_open(db) == [], "a finished task must not come back"

    # Without a source the store has nothing to key on: every add is a task.
    assert tasks.add(db, "Buy milk")["created"] is True
    assert tasks.add(db, "Buy milk")["created"] is True
    db.close()


# --- schedule: calendar events and timed tasks in one chronological list -----


def _at(day: date, hour: int, minute: int) -> str:
    """Local-wall-clock ISO value on `day` (what the calendar helper and the
    coordinator tools hand over)."""
    local = datetime.now().astimezone()
    return local.replace(year=day.year, month=day.month, day=day.day,
                         hour=hour, minute=minute).isoformat(timespec="minutes")


def test_schedule_merges_timed_tasks_between_events() -> None:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    fmt = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731
    buckets = {
        "overdue": [{"title": "Fix CI", "due": _at(today - timedelta(days=1), 9, 30)}],
        "today": [
            {"title": "Plătește factura la curent", "due": _at(today, 10, 30)},
            # A date-only due is not a slot in the day (midnight = date only).
            {"title": "Renew Anthropic key", "due": _at(today, 0, 0)},
            {"title": "someday item"},
        ],
        "tomorrow": [{"title": "Call the dentist", "due": _at(tomorrow, 14, 15)}],
        "later": [{"title": "Far off", "due": _at(day_after, 9, 0)}],
        "someday": [],
    }
    events = [
        {"id": "ek-1", "start": _at(today, 12, 0), "end": _at(today, 12, 30),
         "summary": "Spălat pe dinți", "location": "Str. Exemplu 10"},
        {"id": "ek-2", "start": _at(tomorrow, 23, 0), "end": _at(day_after, 0, 0),
         "summary": "Vizionare apartament", "location": None},
    ]

    assert _schedule_lines(events, buckets).split("\n") == [
        f"- {fmt(today)} - 10:30 Plătește factura la curent (task)",
        f"- {fmt(today)} - 12:00 → 12:30 Spălat pe dinți · Str. Exemplu 10",
        f"- {fmt(tomorrow)} - 14:15 Call the dentist (task)",
        f"- {fmt(tomorrow)} - 23:00 → {fmt(day_after)} - 00:00 Vizionare apartament",
    ], "carried, date-only and later items are not slots of today or tomorrow"


def test_schedule_degrades_honestly() -> None:
    today = date.today()
    due = _at(today, 9, 30)
    timed = {**EMPTY_BUCKETS, "today": [{"title": "Fix CI", "due": due}]}

    assert _schedule_lines([], EMPTY_BUCKETS) == "No events or timed tasks."
    notice = "Calendar temporarily unavailable."
    assert _schedule_lines([], EMPTY_BUCKETS, notice) == notice
    assert _schedule_lines([], timed, notice).split("\n") == [
        notice, f"- {today:%d/%m/%Y} - 09:30 Fix CI (task)",
    ], "timed tasks still show when the calendar is down"
    # A malformed or all-day event never breaks the list; it sorts last.
    all_day = {"summary": "Sărbătoare", "location": None}
    assert _schedule_lines([all_day], timed) == (
        f"- {today:%d/%m/%Y} - 09:30 Fix CI (task)\n- all-day Sărbătoare"
    )


# --- the brief on fakes -------------------------------------------------------


def _mail(uid: str, age: timedelta | None) -> dict:
    received = "" if age is None else (datetime.now(UTC) - age).isoformat()
    return {"uid": uid, "message_id": f"<{uid}@x>", "subject": f"mail {uid}",
            "from_": "a@b.com", "snippet": "...", "date": received}


def _runtime(tmp_path: Path) -> tuple[Runtime, Any, FakeTelegram]:
    telegram = FakeTelegram()
    rt = Runtime(
        cfg=Config(data_dir=tmp_path),
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )
    rt.subscribe()
    return rt, rt.gmail, telegram


async def test_brief_covers_mail_since_the_previous_brief(tmp_path) -> None:
    rt, gmail, _ = _runtime(tmp_path)
    gmail.msgs = [
        _mail("1", timedelta(hours=1)),
        _mail("2", timedelta(days=3)),  # an earlier brief already covered it
        _mail("3", None),  # undated: cannot be placed in a window
    ]

    loader = briefing_loader(rt.deps, rt.cfg)
    state = await loader()
    assert [e["uid"] for e in json.loads(state["overnight_emails"])] == ["1"]

    sent = datetime.now(UTC) - timedelta(hours=2)
    actionlog.log(rt.db, "pipeline", "brief_sent", "system", detail={"dedup_key": "k"})
    rt.db.execute("UPDATE action_log SET ts=? WHERE action='brief_sent'",
                  (sent.isoformat(timespec="seconds"),))
    gmail.msgs.insert(0, _mail("4", timedelta(minutes=30)))

    state = await loader()
    assert [e["uid"] for e in json.loads(state["overnight_emails"])] == ["4", "1"]
    await rt.shutdown()


def _scripted_agents(monkeypatch, llm: RecordingLlm) -> None:
    """The three LLM steps of the brief on the scripted model; the glue between
    them stays real."""
    from google.adk.agents import Agent

    def script(name, instruction, **kwargs):
        return Agent(name=name, model=llm, instruction=instruction,
                     include_contents="none", **kwargs)

    monkeypatch.setattr(
        pipelines.wf_agents, "make_triage_agent",
        lambda cfg: script("triage_agent", pipelines.wf_agents.TRIAGE_INSTRUCTION,
                           output_key="triage_json", generate_content_config=None),
    )
    monkeypatch.setattr(
        pipelines.wf_agents, "make_task_extractor",
        lambda cfg, tool: script("task_extractor",
                                 pipelines.wf_agents.TASK_EXTRACT_INSTRUCTION,
                                 tools=[tool]),
    )
    monkeypatch.setattr(
        pipelines.wf_agents, "make_planner_agent",
        lambda cfg: script("planner_agent", pipelines.wf_agents.BRIEF_INSTRUCTION,
                           output_key="brief_markdown"),
    )


def _text(text: str) -> gtypes.Content:
    return gtypes.Content(parts=[gtypes.Part.from_text(text=text)])


def _one_brief_of_script(uid: str, title: str) -> list[gtypes.Content]:
    """triage → extractor asks for the task → extractor stops → planner."""
    return [
        _text(json.dumps({"items": [
            {"uid": uid, "importance": "normal", "category": "task",
             "summary": "Invoice #4471 is 7 days overdue"},
        ]})),
        gtypes.Content(parts=[gtypes.Part(function_call=gtypes.FunctionCall(
            id=f"fc_{uid}", name="add_task_from_email",
            args={"email_id": uid, "title": title},
        ))]),
        _text("none"),
        _text("## Day plan\nPay the bill."),
    ]


async def test_tasks_extracted_by_the_brief_appear_in_it_and_never_twice(
    tmp_path, monkeypatch
) -> None:
    title = "Pay invoice #4471"
    llm = RecordingLlm(responses=3 * _one_brief_of_script("17", title))
    _scripted_agents(monkeypatch, llm)
    rt, gmail, telegram = _runtime(tmp_path)

    async def brief(key: str) -> str:
        gmail.msgs = [_mail("17", timedelta(hours=2))]
        await rt.run_briefing(key)
        (planner,) = [r for r in llm.requests if "morning brief" in system_instruction(r)]
        return system_instruction(planner)

    first = await brief("brief:one")
    assert f"- [someday] {title}" in first, (
        "a task extracted moments earlier must be in the brief that extracted it"
    )
    assert "Pay the bill." in telegram.messages[0]
    assert [t["title"] for t in tasks.list_open(rt.db)] == [title]

    # Same mail again (re-run, catch-up after downtime): the extractor asks for
    # the same task and the store answers with the one it already keeps.
    llm.requests.clear()
    await brief("brief:two")
    assert [t["title"] for t in tasks.list_open(rt.db)] == [title]

    # The owner finished it; the next brief must not bring it back.
    tasks.complete(rt.db, tasks.list_open(rt.db)[0]["id"])
    llm.requests.clear()
    await brief("brief:three")
    assert tasks.list_open(rt.db) == []
    await rt.shutdown()

