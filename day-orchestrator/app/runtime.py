"""Composition root (C6): wires adapters, scheduler, runners, and the bus.

The only module allowed to import across all top-level packages (ADR-006
layer contract). Owns pairing, approval resolution, and paused-run resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types as gtypes

from .channels.calendar import CalendarClient
from .channels.gmail import GmailClient, GmailWatcher
from .channels.telegram import TelegramChannel
from .core import actionlog, approval, budget, memory
from .core.config import PROJECT_ROOT, Config
from .core.db import Database
from .core.events import (
    GMAIL_NEW_MESSAGES,
    REMINDER_DUE,
    TELEGRAM_APPROVAL,
    TELEGRAM_COMMAND,
    Event,
    EventBus,
)
from .core.scheduler import EffectScheduler
from .core.secrets import Secrets, default_secrets
from .core.textfmt import escape_html, markdown_to_telegram_html
from .tools import ToolDeps, make_tools
from .workflows import pipelines
from .workflows.agents import make_coordinator

log = logging.getLogger(__name__)

APP_NAME = "app"


def _acquire_instance_lock(data_dir: Path, blocking: bool = False):
    """Non-blocking by default; returns the open lock file or None if held.

    A second instance parks on the blocking acquire instead of exiting, so
    launchd KeepAlive never thrashes while the primary is healthy.
    """
    import fcntl

    fh = (data_dir / ".lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0))
    except BlockingIOError:
        fh.close()
        return None
    fh.write(str(__import__("os").getpid()))
    fh.flush()
    return fh


class NullNotifier:
    """Used when no Telegram token is configured; briefs stay in the log."""

    async def send(self, text: str, category: str) -> dict:
        log.info("notify (no channel): category=%s text=%.200s", category, text)
        return {"message_id": None}


class Runtime:
    def __init__(
        self,
        cfg: Config | None = None,
        secrets: Secrets | None = None,
        *,
        gmail: GmailClient | None = None,
        calendar: CalendarClient | None = None,
        telegram: TelegramChannel | None = None,
        coordinator_model: object | None = None,
    ) -> None:
        self.cfg = cfg or Config.load()
        self.secrets = secrets or default_secrets(PROJECT_ROOT)
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.cfg.db_path)
        self.db.init_schema()
        self.session_service = DatabaseSessionService(
            f"sqlite+aiosqlite:///{self.cfg.db_path}"
        )
        self.bus = EventBus()
        self.loop: asyncio.AbstractEventLoop | None = None

        if calendar is not None:
            self.calendar = calendar
        elif self.cfg.calendar_backend == "apple":
            from .channels.calendar_apple import AppleCalendarClient

            self.calendar = AppleCalendarClient()
        else:
            self.calendar = CalendarClient(
                self.cfg.data_dir / "calendar_token.json",
                self.cfg.data_dir / "calendar_client_secret.json",
            )
        self.gmail = gmail or GmailClient(
            self.secrets.require("GMAIL_ADDRESS"),
            self.secrets.require("GMAIL_APP_PASSWORD"),
        )
        token = self.secrets.get("TELEGRAM_BOT_TOKEN")
        self.telegram = telegram or (
            TelegramChannel(token, self.bus) if token else None
        )
        self.notifier = self.telegram or NullNotifier()
        if self.telegram is not None:
            self.telegram.set_owner_chat_id(memory.get_value(self.db, "paired_chat"))

        self.deps = ToolDeps(
            db=self.db,
            gmail=self.gmail,
            calendar=self.calendar,
            notifier=self.notifier,
            budgets=self.cfg.budgets,
            timezone=self.cfg.timezone,
            card_sender=self._send_approval_card if self.telegram else None,
            calendar_writer=(
                self.calendar
                if self.cfg.calendar_backend == "apple"
                else None  # google adapter is read-only
            ),
        )

        self.scheduler = EffectScheduler(
            self.cfg.timezone,
            handlers={"brief": self.run_briefing, "sweep": self.run_sweep},
            bus=self.bus,
        )
        self.scheduler.set_database(self.db)

        tools = make_tools(self.deps)
        # None → resolve from cfg (local OpenAI-compatible or Gemini per config);
        # tests/smoke may inject an ADK model object (ScriptedLlm) instead.
        self._coordinator_model = coordinator_model
        coordinator = make_coordinator(self._coordinator_model or self.cfg, tools)
        self.coordinator_app = App(
            name=APP_NAME,
            root_agent=coordinator,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        self._coordinator_runner: Runner | None = None
        self._watcher: GmailWatcher | None = None
        self._stop = asyncio.Event()

    # ---------- lifecycle ----------

    def subscribe(self) -> None:
        self.bus.subscribe(GMAIL_NEW_MESSAGES, self.on_new_mail)
        self.bus.subscribe(TELEGRAM_COMMAND, self.on_command)
        self.bus.subscribe(TELEGRAM_APPROVAL, self.on_approval)
        self.bus.subscribe(REMINDER_DUE, self.on_reminder)

    async def start(self) -> None:
        lock = _acquire_instance_lock(self.cfg.data_dir)
        if lock is None:
            log.warning(
                "another instance holds the lock; parking until it exits"
            )
            lock = _acquire_instance_lock(self.cfg.data_dir, blocking=True)
        self._lock_file = lock
        self.subscribe()
        self.loop = asyncio.get_running_loop()
        self.scheduler.ensure_daily("brief", self.cfg.brief_time)
        for t in self.cfg.sweep_times:
            self.scheduler.ensure_daily("sweep", t)
        await self.scheduler.start()
        if self.telegram is not None:
            await self.telegram.start()
        else:
            log.warning("TELEGRAM_BOT_TOKEN unset; running without the user channel")
        self._watcher = GmailWatcher(
            self.gmail,
            on_new_messages=self._on_new_messages_threadsafe,
            poll_seconds=self.cfg.gmail_poll_seconds,
        )
        self._watcher.start()
        log.info("runtime started (db=%s)", self.cfg.db_path)
        await self._preflight_calendar_consent()
        await self._stop.wait()
        await self.shutdown()

    async def _preflight_calendar_consent(self) -> None:
        """One-time informational push when the agent's calendar access is
        ungranted (TCC keys consent per process context; ADR-009)."""
        if self.cfg.calendar_backend != "apple":
            return
        if not hasattr(self.calendar, "access_status"):
            return
        try:
            if self.calendar.access_status() != 3:
                await self.notifier.send(
                    "Calendar access isn't granted to the background service. "
                    "Fix once: System Settings → Privacy & Security → Calendars "
                    "→ enable the python entry. If no dialog ever appears, tell "
                    "the operator — macOS may be suppressing it.",
                    "triage_alerts",
                )
        except Exception:
            log.exception("calendar consent preflight failed")

    async def shutdown(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        if self.telegram is not None:
            await self.telegram.stop()
        await self.scheduler.stop()
        self.db.close()
        if getattr(self, "_lock_file", None) is not None:
            import fcntl

            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
        log.info("runtime stopped")

    def request_stop(self) -> None:
        self._stop.set()

    def _on_new_messages_threadsafe(self, emails: list[dict]) -> None:
        if self.loop is not None and emails:
            self.bus.publish_threadsafe(
                self.loop, Event(GMAIL_NEW_MESSAGES, {"messages": emails})
            )

    async def _send_approval_card(self, row: approval.Approval) -> int:
        assert self.telegram is not None
        p = row.payload
        if row.kind == "create_calendar_event":
            summary = (
                "<b>Create this calendar event?</b>\n"
                f"{escape_html(str(p.get('title', '')))}\n"
                f"{escape_html(str(p.get('start', '')))} → "
                f"{escape_html(str(p.get('end', '')))}"
            )
        else:
            summary = (
                "<b>Send this email?</b>\n"
                f"To: {escape_html(str(p.get('to', '')))}\n"
                f"Subject: {escape_html(str(p.get('subject', '')))}\n\n"
                f"{escape_html(str(p.get('body', ''))[:800])}"
            )
        return await self.telegram.send_approval_card(row.id, summary)

    # ---------- pipelines ----------

    async def _run_agent(
        self, root_agent, user_id: str, session_id: str, message: gtypes.Content
    ) -> str:
        """Runs one agent invocation on its own persisted session; returns final text."""
        app = App(
            name=APP_NAME,
            root_agent=root_agent,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        runner = Runner(app=app, session_service=self.session_service)
        try:
            await self.session_service.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
        except Exception:
            pass  # session exists (e.g. persistent Telegram threads)
        final = ""
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=message
        ):
            if (
                event.is_final_response()
                and event.content
                and event.content.parts
            ):
                final = "".join(
                    part.text or "" for part in event.content.parts
                ).strip()
        return final

    async def run_briefing(self, dedup_key: str) -> None:
        if not actionlog.claim(self.db, dedup_key, "scheduler"):
            return
        pipeline = pipelines.make_briefing_pipeline(self.deps, self.cfg)
        session_id = f"brief-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        try:
            await self._run_agent(
                pipeline,
                "system:briefing",
                session_id,
                gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="run")]),
            )
            actionlog.log(self.db, "pipeline", "brief_sent", "system",
                          detail={"dedup_key": dedup_key})  # key lives in claim row (UNIQUE)
        except Exception:
            log.exception("briefing failed (%s) — gap is visible in action log", dedup_key)
            actionlog.log(self.db, "pipeline", "brief_failed", "system",
                          detail={"dedup_key": dedup_key})
            try:
                # the failed run never consumed its budget slot — use it to tell you
                if budget.allow(self.db, "briefing",
                                self.cfg.budgets.get("briefing", 1), self.cfg.timezone):
                    await self.notifier.send(
                        "⚠️ Morning brief failed to run — check "
                        "~/Library/Application Support/day-orchestrator/logs",
                        "briefing",
                    )
            except Exception:
                log.exception("brief-failure notify also failed")

    async def run_sweep(self, dedup_key: str) -> None:
        if not actionlog.claim(self.db, dedup_key, "scheduler"):
            return
        last = memory.get_value(self.db, "last_triage") or {}
        items = [i for i in last.get("items", []) if i.get("category") == "needs_reply"]
        if not items:
            return
        replied = self._replied_message_ids()
        awaiting = [i for i in items if not self._is_replied(i, replied)]
        if not awaiting:
            return
        limit = self.cfg.budgets.get("triage_alerts", 5)
        if not budget.allow(self.db, "triage_alerts", limit, self.cfg.timezone):
            return
        lines = ["<b>Awaiting your reply:</b>"]
        for item in awaiting[:5]:
            lines.append(f"• {escape_html(str(item.get('summary', '')))}")
        await self.notifier.send("\n".join(lines), "triage_alerts")
        actionlog.log(self.db, "pipeline", "sweep_notified", "notify",
                      detail={"dedup_key": dedup_key, "count": len(awaiting)})

    def _replied_message_ids(self) -> set[str]:
        rows = self.db.query(
            "SELECT detail FROM action_log WHERE action='email_sent' AND detail IS NOT NULL"
        )
        out: set[str] = set()
        for row in rows:
            try:
                ref = json.loads(row["detail"]).get("in_reply_to")
                if ref:
                    out.add(ref)
            except json.JSONDecodeError:
                continue
        return out

    def _is_replied(self, item: dict, replied: set[str]) -> bool:
        uid = str(item.get("uid", ""))
        try:
            msg = self.gmail.get_message(uid)
        except Exception:
            return False
        return str(msg.get("message_id", "")) in replied

    # ---------- bus handlers ----------

    async def on_new_mail(self, event: Event) -> None:
        emails = event.payload.get("messages", [])
        if not emails:
            return
        pipeline = pipelines.make_incoming_pipeline(self.deps, self.cfg, emails)
        session_id = f"incoming-{uuid.uuid4().hex[:10]}"
        await self._run_agent(
            pipeline,
            "system:incoming",
            session_id,
            gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="run")]),
        )

    async def on_command(self, event: Event) -> None:
        if self.telegram is None:
            return
        chat_id = event.payload["chat_id"]
        text = str(event.payload["text"]).strip()

        paired = memory.get_value(self.db, "paired_chat")
        if paired is None:
            if text.startswith("/start"):
                memory.set_value(self.db, "paired_chat", chat_id)
                self.telegram.set_owner_chat_id(chat_id)
                await self.telegram.send_message(
                    "Paired. This chat is now the owner channel.", "reminders"
                )
            return
        if chat_id != paired:
            log.warning("unpaired chat %s attempted command; ignored", chat_id)
            return

        edit_key = f"edit_pending:{chat_id}"
        pending_edit = memory.get_value(self.db, edit_key)
        if pending_edit:
            row = approval.get(self.db, str(pending_edit))
            memory.set_value(self.db, edit_key, None)
            if row is not None and row.state == "pending":
                payload = dict(row.payload)
                payload["body"] = text
                approval.update_payload(self.db, row.id, payload)
                await self.telegram.send_message(
                    "Draft updated — approve on the card.", "reminders"
                )
                return

        session_id = f"tg-{chat_id}"
        typing = self.telegram.start_typing()
        try:
            reply = await self._run_agent(
                self.coordinator_app.root_agent,
                f"tg:{chat_id}",
                session_id,
                gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=text)]),
            )
        except Exception:
            log.exception("coordinator failed for chat %s", chat_id)
            await self.telegram.send_message(
                "⚠️ Something broke while answering — the failure is in the logs.",
                "reminders",
            )
            return
        finally:
            typing.cancel()
        if reply:
            await self.telegram.send_message(markdown_to_telegram_html(reply), "reminders")

    async def on_approval(self, event: Event) -> None:
        approval_id = str(event.payload["approval_id"])
        verdict = str(event.payload["verdict"])
        chat_id = event.payload.get("chat_id")
        if verdict == "edit":
            row = approval.get(self.db, approval_id)
            if row is not None and row.state == "pending" and self.telegram:
                memory.set_value(self.db, f"edit_pending:{chat_id}", approval_id)
                await self.telegram.send_message(
                    "Reply with the corrected email body.", "reminders"
                )
            return
        mapped = {"approved": "approved", "rejected": "rejected"}.get(verdict)
        if mapped is None:
            return
        try:
            row = approval.resolve(self.db, approval_id, mapped, f"telegram:{verdict}")
            actionlog.log(
                self.db, "gate",
                "send_rejected" if mapped == "rejected" else "send_approved",
                "send", approval_id=row.id,
                detail={"via": f"telegram:{verdict}"},
            )
        except approval.ApprovalError as exc:
            log.warning("approval resolve failed: %s", exc)
            return
        confirmed = mapped == "approved"
        resumed = await self.resume_for_approval(row, confirmed)
        if self.telegram is not None and row.telegram_message_id:
            state = "approved — sending" if confirmed else "rejected"
            note = "" if resumed else "\n(paused run not found; nothing was sent)"
            try:
                await self.telegram.edit_message_text(
                    row.telegram_message_id,
                    f"{escape_html(str(row.payload.get('subject', '')))}\n[{state}{note}]",
                    reply_markup=None,
                )
            except Exception:
                log.exception("failed to update approval card")

    async def resume_for_approval(self, row: approval.Approval, confirmed: bool) -> bool:
        """Resumes the paused run: FunctionResponse for adk_request_confirmation."""
        if not row.session_ref:
            return False
        coords = json.loads(row.session_ref)
        user_id, session_id = coords["user_id"], coords["session_id"]
        session = await self.session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if session is None:
            log.error("approval %s: session %s not found", row.id, session_id)
            return False
        fc_id = self._find_pending_confirmation(session)
        if fc_id is None:
            log.error("approval %s: no pending confirmation in %s", row.id, session_id)
            return False
        root = self._root_for_user(user_id)
        await self._run_agent(
            root,
            user_id,
            session_id,
            gtypes.Content(
                role="user",
                parts=[
                    gtypes.Part(
                        function_response=gtypes.FunctionResponse(
                            id=fc_id,
                            name="adk_request_confirmation",
                            response={
                                "confirmed": confirmed,
                                "payload": {"approval_id": row.id},
                            },
                        )
                    )
                ],
            ),
        )
        return True

    def _root_for_user(self, user_id: str):
        if user_id.startswith("tg:"):
            return self.coordinator_app.root_agent
        if user_id == "system:briefing":
            return pipelines.make_briefing_pipeline(self.deps, self.cfg)
        if user_id == "system:incoming":
            return pipelines.make_incoming_pipeline(self.deps, self.cfg, [])
        raise ValueError(f"no root agent mapping for user {user_id!r}")

    def _find_pending_confirmation(self, session) -> str | None:
        responded: set[str] = set()
        pending: str | None = None
        for ev in session.events or ():
            content = ev.content
            if content is None or not content.parts:
                continue
            for part in content.parts:
                fr = part.function_response
                if fr is not None and fr.name == "adk_request_confirmation":
                    responded.add(fr.id or "")
                fc = part.function_call
                if fc is not None and fc.name == "adk_request_confirmation":
                    pending = fc.id
        return pending if pending and pending not in responded else None

    async def on_reminder(self, event: Event) -> None:
        text = escape_html(str(event.payload.get("text", "")))
        await self.notifier.send(f"⏰ {text}", "reminders")
        actionlog.log(self.db, "scheduler", "reminder_fired", "notify",
                      detail={"id": event.payload.get("id")})
