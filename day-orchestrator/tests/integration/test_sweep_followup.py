"""Sweep reads the email_triage table: awaiting-reply push, replied filter."""

from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path

from app.core.config import Config
from app.core.db import persist_triage
from app.core.secrets import InMemoryBackend, Secrets
from app.runtime import Runtime
from app.smoke import FakeCalendar, FakeGmail, FakeTelegram


def _runtime(tmp_path: Path) -> tuple[Runtime, FakeTelegram]:
    cfg = dataclasses.replace(Config.load(), data_dir=Path(tempfile.mkdtemp()))
    telegram = FakeTelegram()
    rt = Runtime(
        cfg=cfg,
        secrets=Secrets(InMemoryBackend()),
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )
    rt.subscribe()
    return rt, telegram


async def test_sweep_pushes_unanswered_needs_reply(tmp_path: Path) -> None:
    rt, telegram = _runtime(tmp_path)
    persist_triage(rt.db, [
        {"uid": "<await@x>", "summary": "Offer expiring Friday",
         "category": "needs_reply", "importance": "normal"},
    ])

    await rt.run_sweep("sweep-test-1")

    assert any("Awaiting your reply" in m for m in telegram.messages), (
        f"sweep must push awaiting-reply lines; got {telegram.messages!r}"
    )
    await rt.shutdown()


async def test_sweep_skips_replied_emails(tmp_path: Path) -> None:
    rt, telegram = _runtime(tmp_path)
    persist_triage(rt.db, [
        {"uid": "<replied@x>", "summary": "Already answered",
         "category": "needs_reply", "importance": "normal"},
    ])
    rt.gmail.msgs.append({
        "uid": "<replied@x>", "message_id": "<replied@x>",
        "subject": "Already answered", "snippet": "...",
        "from_": "a@x", "date": "2026-11-22T10:00:00+00:00",
    })
    rt.db.execute(
        "INSERT INTO action_log (ts, actor, action, effect_class, detail) "
        "VALUES (datetime('now'), 'gate', 'email_sent', 'send', ?)",
        (json.dumps({"to": "a@x", "subject": "Re", "in_reply_to": "<replied@x>"}),),
    )

    await rt.run_sweep("sweep-test-2")

    assert not any("Awaiting your reply" in m for m in telegram.messages), (
        "replied threads must not resurface in the sweep"
    )
    await rt.shutdown()
