"""Preflight consent push: warns once when the agent's calendar access is
ungranted; stays silent when granted. Offline (FakeCalendar)."""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from app.core import memory
from app.core.config import Config
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import FakeCalendar, FakeGmail, FakeTelegram


def _runtime(calendar: FakeCalendar) -> tuple[Runtime, FakeTelegram]:
    cfg = dataclasses.replace(Config.load(), data_dir=Path(tempfile.mkdtemp()))
    telegram = FakeTelegram()
    rt = Runtime(
        cfg=cfg,
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=calendar,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )
    memory.set_value(rt.db, "paired_chat", 42)
    return rt, telegram


async def test_preflight_pushes_when_ungranted() -> None:
    calendar = FakeCalendar()
    calendar.access_status_value = 0
    rt, telegram = _runtime(calendar)

    await rt._preflight_calendar_consent()

    assert telegram.messages, "ungranted access must trigger the info push"
    assert "Calendar access" in telegram.messages[0]


async def test_preflight_silent_when_granted() -> None:
    calendar = FakeCalendar()
    calendar.access_status_value = 3
    rt, telegram = _runtime(calendar)

    await rt._preflight_calendar_consent()

    assert telegram.messages == [], "granted access must not push"
