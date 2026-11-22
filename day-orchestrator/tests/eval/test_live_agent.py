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
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from app.core import approval, memory, tasks
from app.core.config import Config
from app.core.events import TELEGRAM_COMMAND, Event
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import FakeCalendar, FakeGmail, FakeTelegram

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE"),
    reason="live model evals: set RUN_LIVE=1",
)


def make_email(uid: str, subject: str, snippet: str, from_: str = "a@b.com") -> dict:
    return {
        "uid": uid, "message_id": f"<{uid}@x>", "subject": subject,
        "from_": from_, "snippet": snippet,
        "date": "2026-11-21T20:00:00+00:00",
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

    triage = memory.get_value(rt.db, "last_triage") or {}
    items = triage.get("items", [])
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
