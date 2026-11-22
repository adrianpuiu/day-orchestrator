"""Unit tests for the Telegram channel adapter (no network — fake Bot injected)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.types import Chat, Message, Update, User

from app.channels.telegram import TelegramChannel
from app.core.events import (
    TELEGRAM_APPROVAL,
    TELEGRAM_COMMAND,
    Event,
    EventBus,
)


class FakeMessage:
    """Minimal response object returned by the fake Bot's send methods."""

    def __init__(self, message_id: int, chat_id: int) -> None:
        self.message_id = message_id
        self.chat = Chat(id=chat_id, type="private")


class FakeBot:
    """In-memory aiogram Bot stand-in; records every call, sends nothing."""

    def __init__(self, chat_id: int = 42) -> None:
        self.id = 12345
        self.chat_id = chat_id
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.deleted_webhook: dict[str, bool] | None = None
        self.session = self

    async def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self.deleted_webhook = {"drop_pending_updates": drop_pending_updates}

    async def send_message(self, **kwargs: Any) -> FakeMessage:
        self.sent.append(kwargs)
        self._next_id = getattr(self, "_next_id", 100) + 1
        return FakeMessage(self._next_id, self.chat_id)

    async def edit_message_text(self, **kwargs: Any) -> FakeMessage:
        self.edits.append(kwargs)
        return FakeMessage(kwargs["message_id"], self.chat_id)

    async def close(self) -> None:  # session.close() in stop()
        self.closed = True


class FakeCallback:
    """Minimal callback_query stand-in with an answer() spy."""

    def __init__(self, data: str | None, message: FakeMessage) -> None:
        self._data = data
        self._message = message
        self.answered = False

    @property
    def data(self) -> str | None:
        return self._data

    @property
    def message(self) -> FakeMessage:
        return self._message

    async def answer(self) -> None:
        self.answered = True


def make_update(message: Message) -> Update:
    return Update(update_id=1, message=message)


def make_user_message(text: str) -> Message:
    return Message(
        message_id=7,
        date=datetime(2026, 11, 22, tzinfo=UTC),
        chat=Chat(id=123, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Owner"),
        text=text,
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def channel(bus: EventBus) -> TelegramChannel:
    return TelegramChannel(
        "TOKEN", bus, owner_chat_id=42, bot_factory=lambda token: FakeBot()
    )


async def collect(bus: EventBus, event_type: str) -> list[Event]:
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    bus.subscribe(event_type, handler)
    return seen


async def test_message_handler_publishes_command_event(
    bus: EventBus, channel: TelegramChannel
) -> None:
    seen = await collect(bus, TELEGRAM_COMMAND)
    await channel._dispatcher.feed_update(
        channel._bot, make_update(make_user_message("/today"))
    )
    assert len(seen) == 1
    assert seen[0].type == TELEGRAM_COMMAND
    assert seen[0].payload == {"text": "/today", "chat_id": 123, "message_id": 7}


async def test_message_handler_ignores_non_text_updates(
    bus: EventBus, channel: TelegramChannel
) -> None:
    seen = await collect(bus, TELEGRAM_COMMAND)
    msg = Message(
        message_id=7,
        date=datetime(2026, 11, 22, tzinfo=UTC),
        chat=Chat(id=123, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Owner"),
        text=None,
    )
    await channel._dispatcher.feed_update(channel._bot, make_update(msg))
    assert seen == []


async def test_callback_query_publishes_approval_event(
    bus: EventBus, channel: TelegramChannel
) -> None:
    seen = await collect(bus, TELEGRAM_APPROVAL)
    callback = FakeCallback(
        "appr:abc-1:approved", FakeMessage(message_id=55, chat_id=123)
    )
    await channel._router.callback_query.trigger(callback)  # type: ignore[arg-type]
    assert len(seen) == 1
    assert seen[0].payload == {
        "approval_id": "abc-1",
        "verdict": "approved",
        "message_id": 55,
        "chat_id": 123,
    }


@pytest.mark.parametrize("verdict", ["approved", "rejected", "edit"])
async def test_all_verdicts_published(
    bus: EventBus, channel: TelegramChannel, verdict: str
) -> None:
    seen = await collect(bus, TELEGRAM_APPROVAL)
    callback = FakeCallback(f"appr:id:{verdict}", FakeMessage(9, 1))
    await channel._router.callback_query.trigger(callback)  # type: ignore[arg-type]
    assert seen[0].payload["verdict"] == verdict


async def test_unrelated_callback_answered_but_not_published(
    bus: EventBus, channel: TelegramChannel
) -> None:
    seen = await collect(bus, TELEGRAM_APPROVAL)
    callback = FakeCallback("other:data:x", FakeMessage(9, 1))
    await channel._router.callback_query.trigger(callback)  # type: ignore[arg-type]
    assert callback.answered
    assert seen == []


async def test_send_message_returns_ids_and_uses_markdown(
    bus: EventBus, channel: TelegramChannel
) -> None:
    result = await channel.send_message("Brief", "email_digest")
    bot = channel._bot
    assert isinstance(bot, FakeBot)
    assert result == {"message_id": 101, "chat_id": 42}
    assert bot.sent[0]["parse_mode"] == "HTML"
    assert bot.sent[0]["reply_markup"] is None


async def test_send_approval_card_button_layout(
    bus: EventBus, channel: TelegramChannel
) -> None:
    message_id = await channel.send_approval_card("abc-1", "Send report?")
    bot = channel._bot
    assert isinstance(bot, FakeBot)
    assert message_id == 101
    assert bot.sent[0]["parse_mode"] == "HTML"
    keyboard = bot.sent[0]["reply_markup"].inline_keyboard[0]
    assert [(btn.text, btn.callback_data) for btn in keyboard] == [
        ("Approve", "appr:abc-1:approved"),
        ("Reject", "appr:abc-1:rejected"),
        ("Edit", "appr:abc-1:edit"),
    ]


async def test_edit_message_text_passes_through(
    bus: EventBus, channel: TelegramChannel
) -> None:
    await channel.edit_message_text(55, "*updated*")
    bot = channel._bot
    assert isinstance(bot, FakeBot)
    assert bot.edits == [
        {
            "chat_id": 42,
            "message_id": 55,
            "text": "*updated*",
            "parse_mode": "HTML",
            "reply_markup": None,
        }
    ]


async def test_start_drops_webhook_and_stop_closes_session(
    bus: EventBus, channel: TelegramChannel
) -> None:
    bot = channel._bot
    assert isinstance(bot, FakeBot)
    await channel.start()
    assert bot.deleted_webhook == {"drop_pending_updates": True}
    assert channel._poll_task is not None
    await asyncio.sleep(0)
    await channel.stop()
    assert channel._poll_task is None
    assert bot.closed


