"""Typed in-process event bus (C6).

Modules communicate through this bus; adapters never call adapters. Handler
exceptions are contained (logged, other handlers still run) — one failing
subscriber must not silence the morning brief.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Event type constants (the vocabulary of the bus)
GMAIL_NEW_MESSAGES = "gmail.new_messages"
TELEGRAM_COMMAND = "telegram.command"
TELEGRAM_APPROVAL = "telegram.approval"
APPROVAL_RESOLVED = "approval.resolved"
REMINDER_DUE = "reminder.due"

Handler = Callable[["Event"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subs[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        for handler in list(self._subs.get(event.type, ())):
            try:
                await handler(event)
            except Exception:  # containment is the point
                log.exception("event handler failed type=%s", event.type)

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, event: Event) -> None:
        """Bridge from non-asyncio threads (IMAP IDLE) onto the main loop."""
        asyncio.run_coroutine_threadsafe(self.publish(event), loop)
