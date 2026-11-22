"""Typed tool layer (C3): the only way an LLM reaches the world.

Every tool declares an effect class (`__effect__`); `send`-class tools execute
only against a resolved approval row (ADR-004). The LLM never sees credentials:
adapters are injected as structural protocols, wired by the composition root.
Only this module may call `approval.require_resolved` (CI gate, ADR-006).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from google.adk.tools import FunctionTool, ToolContext

from .core import actionlog, approval, budget, tasks
from .core.approval import ApprovalPending
from .core.db import Database
from .core.textfmt import escape_html

log = logging.getLogger(__name__)


class Effect(StrEnum):
    READ = "read"
    DRAFT = "draft"
    SEND = "send"
    NOTIFY = "notify"


def effect(e: Effect) -> Callable:
    def decorate(fn: Callable) -> Callable:
        fn.__effect__ = e  # type: ignore[attr-defined]
        return fn

    return decorate


# --- structural adapter contracts (implemented in app/channels, injected at runtime) ---


class GmailClientProto(Protocol):
    def fetch_recent(self, limit: int) -> list[dict[str, Any]]: ...
    def get_message(self, uid: str) -> dict[str, Any]: ...
    def save_draft(
        self, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> str: ...
    def send_email(
        self, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> None: ...


class CalendarClientProto(Protocol):
    def get_events(self, days: int) -> list[dict[str, Any]]: ...


class NotifierProto(Protocol):
    async def send(self, text: str, category: str) -> dict[str, Any]: ...


@dataclass
class ToolDeps:
    db: Database
    gmail: GmailClientProto
    calendar: CalendarClientProto
    notifier: NotifierProto
    budgets: dict[str, int]
    timezone: str = "local"
    # Composition-root injected: async (Approval) -> telegram message id.
    card_sender: Any = None
    # Apple backend only: async-safe object with create_event(title, start, end).
    calendar_writer: Any = None


def make_tools(deps: ToolDeps) -> list[FunctionTool]:
    """Build the tool set bound to runtime services. Composition-root only."""

    # ---- read tools ----

    @effect(Effect.READ)
    async def list_recent_emails(limit: int = 10) -> dict[str, Any]:
        """Lists recent inbox emails with id, subject, sender, snippet.

        Args:
            limit: Maximum number of emails to return (1-50).
        """
        limit = max(1, min(int(limit), 50))
        msgs = deps.gmail.fetch_recent(limit)
        return {"status": "success", "emails": msgs}

    @effect(Effect.READ)
    async def read_email(uid: str) -> dict[str, Any]:
        """Returns one full email (headers + body text) by id.

        Args:
            uid: The email id as returned by list_recent_emails.
        """
        return {"status": "success", **deps.gmail.get_message(uid)}

    @effect(Effect.READ)
    async def get_upcoming_events(days: int = 2) -> dict[str, Any]:
        """Lists calendar events for today and the next days.

        Args:
            days: How many days ahead to include (1-7).
        """
        try:
            events = deps.calendar.get_events(max(1, min(days, 7)))
        except Exception:
            log.exception("calendar unavailable; degrading brief")
            events = []
        return {"status": "success", "events": events}

    @effect(Effect.READ)
    async def list_tasks() -> dict[str, Any]:
        """Lists all open tasks; each has a bucket: overdue | today | tomorrow | later | someday."""
        today = tasks.local_today(deps.timezone)
        rows = tasks.list_open(deps.db)
        return {
            "status": "success",
            "tasks": [{**row, "bucket": tasks.bucket(row, today, deps.timezone)
                       } for row in rows],
        }

    @effect(Effect.READ)
    async def list_pending_approvals() -> dict[str, Any]:
        """Lists send actions awaiting owner approval (live state — the only
        source of truth; conversation memory about old approvals is stale)."""
        rows = approval.list_pending(deps.db)
        return {
            "status": "success",
            "note": (
                "Only these rows await approval. Approvals not listed here are "
                "already resolved (used, failed, or rejected) — never point the "
                "owner at them; create a fresh request instead. Approval happens "
                "ONLY via the Approve/Reject buttons on the Telegram card; chat "
                "text never approves anything."
            ),
            "approvals": [
                {"id": a.id, "kind": a.kind, "state": a.state, "payload": a.payload}
                for a in rows
            ],
        }

    # ---- write tools (non-send) ----

    @effect(Effect.DRAFT)
    async def save_draft_reply(uid: str, body: str) -> dict[str, Any]:
        """Saves a reply draft in the Gmail Drafts folder (never sends).

        Args:
            uid: The email id being replied to.
            body: The reply body text.
        """
        msg = deps.gmail.get_message(uid)
        draft_id = deps.gmail.save_draft(
            to=msg["from_"], subject="Re: " + msg["subject"], body=body,
            in_reply_to=msg.get("message_id"),
        )
        actionlog.log(deps.db, "tool", "draft_saved", "draft", detail={"uid": uid})
        return {"status": "success", "draft_id": draft_id}

    @effect(Effect.NOTIFY)
    async def add_task(title: str, due: str | None = None) -> dict[str, Any]:
        """Adds a task, optionally with an ISO 8601 due datetime.

        Args:
            title: Short task description.
            due: Optional ISO 8601 due datetime, e.g. 2026-11-25T14:00.
        """
        task = tasks.add(deps.db, title, source="tool", due=due)
        return {"status": "success", "task": task}

    @effect(Effect.READ)
    async def complete_task(task_id: str) -> dict[str, Any]:
        """Marks a task done by id.

        Args:
            task_id: The task id from list_tasks.
        """
        return {"status": "success", "result": tasks.complete(deps.db, task_id)}

    @effect(Effect.NOTIFY)
    async def notify_user(text: str, category: str = "triage_alerts") -> dict[str, Any]:
        """Pushes a message to the owner's Telegram. Subject to the daily budget.

        Args:
            text: Message text (markdown).
            category: Budget category: 'briefing' | 'triage_alerts' | 'reminders'.
        """
        limit = deps.budgets.get(category)
        if limit is None:
            return {"status": "error", "error": f"unknown category {category!r}"}
        if not budget.allow(deps.db, category, limit, deps.timezone):
            actionlog.log(
                deps.db, "tool", "notify_budget_blocked", "notify",
                detail={"category": category},
            )
            return {"status": "suppressed", "reason": "daily budget exhausted"}
        result = await deps.notifier.send(text, category)
        return {"status": "success", "message_id": result.get("message_id")}

    # ---- approval-gated effect path (ADR-004): email + calendar writes ----

    async def _gated(
        tool_context: ToolContext,
        kind: str,
        payload: dict[str, Any],
        hint: str,
        execute: Callable[[approval.Approval], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Shared approval flow: pause on first run, act on approved resume.

        `execute` runs the real effect only after the gate resolves approved,
        behind an at-most-once latch (ADR-004).
        """
        state_key = f"appr:{tool_context.function_call_id}"
        approval_id = tool_context.state.get(state_key)

        if tool_context.tool_confirmation is None:
            # First execution: create the approval row and pause the run.
            existing = approval.find_open_same(deps.db, kind, payload)
            if existing is not None:
                log.info("duplicate %s request; open approval %s already covers it",
                         kind, existing.id)
                return {
                    "status": "duplicate",
                    "approval_id": existing.id,
                    "note": "an identical request already has an open approval card",
                }
            ic = tool_context.get_invocation_context()
            session_ref = json.dumps({"user_id": ic.user_id, "session_id": ic.session.id})
            row = approval.create(deps.db, kind, payload,
                                  session_ref=session_ref)
            tool_context.state[state_key] = row.id
            if deps.card_sender is not None:
                try:
                    message_id = await deps.card_sender(row)
                    approval.attach_telegram_message(deps.db, row.id, message_id)
                except Exception:
                    log.exception("failed to send approval card; gate stays in DB")
            tool_context.request_confirmation(hint=hint, payload={"approval_id": row.id})
            return {
                "status": "awaiting_approval",
                "approval_id": row.id,
            }

        # Resumed execution: the gate decides, never the chat text.
        if approval_id is None:
            return {"status": "error", "error": "approval row lost; refusing to send"}
        if not tool_context.tool_confirmation.confirmed:
            row = approval.get(deps.db, approval_id)
            if row is not None and row.state == "pending":
                row = approval.resolve(deps.db, approval_id, "rejected", "adk:denied")
                actionlog.log(deps.db, "gate", "send_rejected", "send", approval_id=row.id)
            return {"status": "rejected", "approval_id": approval_id}

        try:
            row = approval.require_resolved(deps.db, approval_id)
        except ApprovalPending:
            return {"status": "awaiting_approval", "approval_id": approval_id}
        if row.state != "approved":
            actionlog.log(deps.db, "gate", "send_refused", "send", approval_id=row.id,
                          detail={"state": row.state})
            return {"status": "rejected", "approval_id": approval_id}

        # At-most-once latch: an approved row must never send twice, no matter
        # how many times the run resumes.
        if not actionlog.claim(deps.db, f"send:{approval_id}", "gate"):
            return {"status": "already_sent", "approval_id": approval_id}

        # The row's payload is authoritative: the owner may have edited the
        # draft while the run was paused (ADR-004 Edit).
        try:
            return await execute(row)
        except Exception as exc:
            log.exception("approved effect failed (approval %s)", row.id)
            actionlog.log(deps.db, "gate", "send_failed", "send", approval_id=row.id)
            try:
                await deps.notifier.send(
                    f"❌ Approved action ({escape_html(row.kind)}) failed: "
                    f"{escape_html(str(exc)[:200])} — it will NOT be retried "
                    "automatically.",
                    "triage_alerts",
                )
            except Exception:
                log.exception("send-failure notify also failed")
            return {"status": "send_failed", "approval_id": row.id}

    @effect(Effect.SEND)
    async def send_email(
        tool_context: ToolContext,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Sends an email. REQUIRES owner approval via Telegram before sending.

        The run pauses until the owner approves or rejects in Telegram.

        Args:
            to: Recipient address.
            subject: Subject line.
            body: Plain-text body.
            in_reply_to: Optional Message-ID being replied to.
        """

        async def execute(row: approval.Approval) -> dict[str, Any]:
            final = dict(row.payload)
            deps.gmail.send_email(
                to=final["to"], subject=final["subject"], body=final["body"],
                in_reply_to=final.get("in_reply_to"),
            )
            actionlog.log(
                deps.db, "gate", "email_sent", "send", approval_id=row.id,
                detail={"to": final["to"], "subject": final["subject"]},
            )
            return {"status": "sent", "approval_id": row.id}

        payload = {
            "to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to,
        }
        return await _gated(
            tool_context, "send_email", payload, hint=f"Send email to {to}?",
            execute=execute,
        )

    @effect(Effect.SEND)
    async def add_calendar_event(
        tool_context: ToolContext,
        title: str,
        start: str,
        end: str,
        notes: str | None = None,
        invitees: list[str] | None = None,
        alarm_minutes: int = 30,
    ) -> dict[str, Any]:
        """Creates an event in the owner's Apple Calendar (syncs to iPhone).
        REQUIRES owner approval via Telegram before writing.

        Args:
            title: Event title.
            start: ISO datetime, e.g. 2026-11-23T10:30.
            end: ISO datetime for the event end.
            notes: Optional description text.
            invitees: Optional email list; recorded in the notes (EventKit has
                no public attendee API). To send real invitations, also call
                send_email to each invitee after this is approved.
            alarm_minutes: Alert N minutes before the start (default 30;
                0 disables the alarm).
        """

        async def execute(row: approval.Approval) -> dict[str, Any]:
            final = dict(row.payload)
            event_id = deps.calendar_writer.create_event(
                final["title"], final["start"], final["end"],
                notes=final.get("notes") or "",
                invitees=final.get("invitees") or None,
                alarm_minutes=int(final.get("alarm_minutes") or 0),
            )
            actionlog.log(
                deps.db, "gate", "calendar_event_created", "send", approval_id=row.id,
                detail=final,
            )
            return {"status": "created", "event_id": event_id, "approval_id": row.id}

        payload = {
            "title": title, "start": start, "end": end,
            "notes": notes, "invitees": invitees,
            "alarm_minutes": max(0, int(alarm_minutes)),
        }
        summary = title + (f" — {notes[:80]}" if notes else "")
        return await _gated(
            tool_context, "create_calendar_event", payload,
            hint=f"Create calendar event {summary!r}?",
            execute=execute,
        )

    return [
        FunctionTool(list_recent_emails),
        FunctionTool(read_email),
        FunctionTool(get_upcoming_events),
        FunctionTool(list_tasks),
        FunctionTool(list_pending_approvals),
        FunctionTool(save_draft_reply),
        FunctionTool(add_task),
        FunctionTool(complete_task),
        FunctionTool(notify_user),
        FunctionTool(send_email),
        FunctionTool(add_calendar_event),
    ]
