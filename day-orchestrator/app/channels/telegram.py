"""Telegram channel: aiogram 3.x long polling adapter (ADR-006, no webhook).

Bridges the Telegram Bot API and the in-process event bus: inbound user
messages become TELEGRAM_COMMAND events, approval-button callbacks become
TELEGRAM_APPROVAL events, and outbound notifications / approval cards go
through the Bot. No authorization here — the runtime owns chat pairing;
no budgets here — enforcement is upstream (C10).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.events import (
    TELEGRAM_APPROVAL,
    TELEGRAM_COMMAND,
    Event,
    EventBus,
)

log = logging.getLogger(__name__)

_CALLBACK_RE = re.compile(
    r"^appr:(?P<approval_id>[^:]+):(?P<verdict>approved|rejected|edit)$"
)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Splits at line boundaries under Telegram's 4096-char cap; huge lines hard-cut.

    Sections are self-contained blocks, so a line-boundary split never breaks an
    HTML tag in practice.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for part in text.splitlines(keepends=True):
        while len(part) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(part[:limit])
            part = part[limit:]
        if len(current) + len(part) > limit:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks or [text]


class TelegramChannel:
    """Bridge Telegram Bot API traffic onto the core event bus."""

    def __init__(
        self,
        bot_token: str,
        bus: EventBus,
        *,
        owner_chat_id: int | None = None,
        bot_factory: Any = None,
    ) -> None:
        """Store dependencies and build the bot, dispatcher, and router."""
        self._bus = bus
        self._owner_chat_id = owner_chat_id  # set at runtime from pairing (C6)
        factory = bot_factory or (lambda token: Bot(token))
        self._bot = factory(bot_token)
        self._dispatcher = Dispatcher()
        self._router = Router()
        self._dispatcher.include_router(self._router)
        self._register_handlers(self._router)
        self._poll_task: asyncio.Task[None] | None = None

    def set_owner_chat_id(self, chat_id: int | None) -> None:
        """Destination for proactive pushes; runtime owns pairing and calls this."""
        self._owner_chat_id = chat_id

    def _target_chat_id(self) -> int:
        if self._owner_chat_id is None:
            raise ValueError(
                "no paired chat yet — send /start to the bot from your Telegram"
            )
        return self._owner_chat_id

    def _register_handlers(self, router: Router) -> None:
        """Attach the message and callback-query handlers to the router."""

        @router.message()
        async def on_message(message: Message) -> None:
            """Publish every text message as a TELEGRAM_COMMAND event."""
            if message.text is None:
                return
            await self._bus.publish(
                Event(
                    TELEGRAM_COMMAND,
                    {
                        "text": message.text,
                        "chat_id": message.chat.id,
                        "message_id": message.message_id,
                    },
                )
            )

        @router.callback_query()
        async def on_callback(callback: CallbackQuery) -> None:
            """Publish approval-button callbacks as TELEGRAM_APPROVAL events."""
            await callback.answer()
            data = callback.data or ""
            match = _CALLBACK_RE.match(data)
            if match is None or callback.message is None:
                return
            await self._bus.publish(
                Event(
                    TELEGRAM_APPROVAL,
                    {
                        "approval_id": match.group("approval_id"),
                        "verdict": match.group("verdict"),
                        "message_id": callback.message.message_id,
                        "chat_id": callback.message.chat.id,
                    },
                )
            )

    async def start(self) -> None:
        """Drop stale webhook state and begin long polling in a background task."""
        await self._bot.delete_webhook(drop_pending_updates=True)
        self._poll_task = asyncio.create_task(
            self._dispatcher.start_polling(
                self._bot,
                handle_signals=False,  # we own the loop and signal handling
            )
        )
        log.info("telegram polling started")

    async def stop(self) -> None:
        """Cancel the polling task and close the bot session."""
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._bot.session.close()
        log.info("telegram polling stopped")

    def start_typing(self) -> asyncio.Task:
        """Repeat the typing chat action until cancelled (Telegram shows ~5s each)."""
        async def _loop() -> None:
            try:
                chat_id = self._target_chat_id()
                while True:
                    await self._bot.send_chat_action(chat_id=chat_id, action="typing")
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("typing indicator failed", exc_info=True)

        return asyncio.create_task(_loop())

    async def send(self, text: str, category: str) -> dict[str, Any]:
        """NotifierProto entry (category unused here; budget enforced upstream)."""
        return await self.send_message(text, category)

    async def send_message(
        self,
        text: str,
        category: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> dict[str, Any]:
        """Send a Telegram-HTML notification to the paired chat (NotifierProto).

        Chunks at line boundaries under the 4096-char API cap; the keyboard, if
        any, rides on the last chunk.
        """
        chunks = split_message(text)
        message = None
        for i, chunk in enumerate(chunks):
            markup = reply_markup if i == len(chunks) - 1 else None
            message = await self._bot.send_message(
                chat_id=self._target_chat_id(),
                text=chunk,
                parse_mode="HTML",
                reply_markup=markup,
            )
        return {"message_id": message.message_id, "chat_id": message.chat.id}

    async def send_approval_card(self, approval_id: str, summary_text: str) -> int:
        """Send an inline approval card with approve/reject/edit buttons."""
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Approve", callback_data=f"appr:{approval_id}:approved"
                    ),
                    InlineKeyboardButton(
                        text="Reject", callback_data=f"appr:{approval_id}:rejected"
                    ),
                    InlineKeyboardButton(
                        text="Edit", callback_data=f"appr:{approval_id}:edit"
                    ),
                ]
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._target_chat_id(),
            text=summary_text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return message.message_id

    async def edit_message_text(
        self,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Edit an already-sent message in place."""
        await self._bot.edit_message_text(
            chat_id=self._target_chat_id(),
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
