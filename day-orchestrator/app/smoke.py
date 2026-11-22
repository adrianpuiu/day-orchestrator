"""Offline end-to-end proof of the approval gate (ADR-004).

Fake channels + a scripted LLM that calls `send_email`. The run pauses on the
approval gate, the owner "approves" via the bus, the run resumes, and the fake
SMTP transport records the send. No network, no real model.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gtypes
from pydantic import Field

from .core import actionlog, approval, memory
from .core.config import Config
from .core.events import TELEGRAM_APPROVAL, TELEGRAM_COMMAND, Event
from .core.secrets import InMemoryBackend, Secrets
from .runtime import Runtime

log = logging.getLogger(__name__)


class ScriptedLlm(BaseLlm):
    """Yields queued Contents in order; then a plain 'done' text forever."""

    model_config = {"extra": "allow"}
    model: str = "scripted"
    responses: list[Any] = Field(default_factory=list)
    cursor: int = 0

    async def generate_content_async(self, llm_request, **kwargs):
        async for response in self._generate_content_async_impl(llm_request):
            yield response

    async def _generate_content_async_impl(self, llm_request):
        if self.cursor < len(self.responses):
            content = self.responses[self.cursor]
            self.cursor += 1
        else:
            content = gtypes.Content(parts=[gtypes.Part.from_text(text="done")])
        yield LlmResponse(content=content)


class FakeGmail:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.msgs: list[dict[str, Any]] = []

    def fetch_recent(self, limit: int) -> list[dict[str, Any]]:
        return self.msgs[:limit]

    def get_message(self, uid: str) -> dict[str, Any]:
        for m in self.msgs:
            if m["uid"] == uid:
                return m
        raise KeyError(uid)

    def save_draft(self, to, subject, body, in_reply_to=None) -> str:
        return "draft-1"

    def send_email(self, to, subject, body, in_reply_to=None) -> None:
        self.sent.append(
            {"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to}
        )


class FakeTelegram:
    def __init__(self) -> None:
        self.cards: list[dict[str, Any]] = []
        self.messages: list[str] = []
        self.edits: list[str] = []

    def set_owner_chat_id(self, chat_id: int | None) -> None:
        pass

    def start_typing(self) -> Any:
        return asyncio.create_task(asyncio.sleep(3600))

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, text: str, category: str, reply_markup=None) -> dict:
        return await self.send_message(text, category, reply_markup)

    async def send_message(self, text: str, category: str, reply_markup=None) -> dict:
        self.messages.append(text)
        return {"message_id": len(self.messages), "chat_id": 42}

    async def send_approval_card(self, approval_id: str, summary_text: str) -> int:
        self.cards.append({"id": approval_id, "text": summary_text})
        return 100 + len(self.cards)

    async def edit_message_text(self, message_id, text, reply_markup=None) -> None:
        self.edits.append(text)


class FakeCalendar:
    """Records reads and approval-gated writes."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fetched: list[int] = []
        self.update_calls: list[str] = []
        self.access_status_value = 3  # preflight tests override to 0

    def access_status(self) -> int:
        return self.access_status_value

    def get_events(self, days: int) -> list[dict]:
        self.fetched.append(days)
        return list(self.events)

    def create_event(self, title: str, start: str, end: str,
                     notes: str = "", invitees: list[str] | None = None,
                     alarm_minutes: int = 0, recurrence: str = "",
                     location: str = "",
                     lat: float | None = None,
                     lon: float | None = None) -> str:
        event_id = f"ek-{len(self.events) + 1}"
        self.events.append({"id": event_id, "title": title, "start": start,
                            "end": end, "notes": notes,
                            "invitees": invitees or [],
                            "alarm_minutes": alarm_minutes,
                            "recurrence": recurrence,
                            "location": location, "lat": lat, "lon": lon})
        return event_id

    def geocode(self, query: str, lat=None, lon=None) -> list[dict]:
        return [{"name": query, "address": f"{query} (validated)",
                 "lat": lat or 46.57, "lon": lon or 26.90}]

    def get_event_meta(self, event_id: str) -> dict | None:
        return next((e for e in self.events if e["id"] == event_id), None)

    def update_event(self, event_id: str, title=None, start=None, end=None) -> dict:
        self.update_calls.append(event_id)
        ev = self.get_event_meta(event_id)
        if ev is None:
            raise RuntimeError(f"event {event_id} not found")
        for key, value in (("title", title), ("start", start), ("end", end)):
            if value:
                ev[key] = value
        return {"event_id": event_id, "title": ev["title"]}

    def delete_event(self, event_id: str) -> dict:
        ev = self.get_event_meta(event_id)
        if ev is None:
            raise RuntimeError(f"event {event_id} not found")
        self.events.remove(ev)
        return {"deleted": event_id, "title": ev["title"]}


async def run_gate_scenario(
    tmp: Path,
    verdict: str = "approved",
    function_call: tuple[str, dict] = (
        "send_email",
        {
            "to": "bob@example.com",
            "subject": "Lunch on Tuesday?",
            "body": "Hi Bob — Tuesday 12:30 works?",
        },
    ),
) -> dict[str, Any]:
    """Runs a gated tool call → gate → verdict → outcome. Returns evidence."""
    name, args = function_call
    cfg = Config(data_dir=tmp)
    gmail = FakeGmail()
    telegram = FakeTelegram()
    calendar = FakeCalendar()
    runtime = Runtime(
        cfg=cfg,
        secrets=Secrets(InMemoryBackend()),
        gmail=gmail,  # type: ignore[arg-type]
        calendar=calendar,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        coordinator_model=ScriptedLlm(
            responses=[
                gtypes.Content(
                    parts=[
                        gtypes.Part(
                            function_call=gtypes.FunctionCall(
                                id="fc_send_1", name=name, args=args,
                            )
                        )
                    ]
                ),
                gtypes.Content(parts=[gtypes.Part.from_text(text="asked Bob.")]),
            ]
        ),
    )
    runtime.subscribe()
    memory.set_value(runtime.db, "paired_chat", 42)

    await runtime.bus.publish(
        Event(TELEGRAM_COMMAND, {"text": "email bob about lunch", "chat_id": 42,
                                 "message_id": 1})
    )
    pending = approval.list_pending(runtime.db)
    assert len(pending) == 1, "run should pause with exactly one pending approval"
    assert len(telegram.cards) == 1, "owner should see an approval card"
    assert gmail.sent == [], "nothing may send before approval"

    await runtime.bus.publish(
        Event(TELEGRAM_APPROVAL, {"approval_id": pending[0].id,
                                  "verdict": verdict, "chat_id": 42,
                                  "message_id": 2})
    )

    after = approval.get(runtime.db, pending[0].id)
    log_rows = actionlog.recent(runtime.db, limit=10)
    await runtime.shutdown()
    return {
        "approval_state": after.state if after else None,
        "card": telegram.cards[0],
        "card_edit": telegram.edits,
        "sent": gmail.sent,
        "events": calendar.events,
        "actions": [dict(r) for r in log_rows],
    }


async def run_smoke() -> None:
    """Prints the offline gate proof for the owner."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = await run_gate_scenario(Path(d))
    print("\n=== SMOKE: approval gate (offline) ===")
    print(f"card shown:  {result['card']['text'][:60]!r}…")
    print(f"card edited: {result['card_edit']}")
    print(f"approval:    {result['approval_state']}")
    print(f"smtp sends:  {result['sent']}")
    print("audit trail (newest first):")
    for row in result["actions"]:
        print(f"  {row['ts']} {row['actor']:9} {row['action']:16} "
              f"{row['effect_class']:7} {row['dedup_key'] or row['approval_id'] or ''}")


if __name__ == "__main__":
    asyncio.run(run_smoke())
