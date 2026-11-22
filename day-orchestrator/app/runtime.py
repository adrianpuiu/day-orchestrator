"""Composition root (C6): wires adapters, scheduler, runners, and the bus.

The only module allowed to import across all top-level packages (ADR-006
layer contract). Owns pairing, approval resolution, and paused-run resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
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


def _transient_model_error(exc: Exception) -> bool:
    """True for model-endpoint hiccups worth retrying (connection/timeout/5xx)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "connection error", "connection reset", "timed out", "timeout",
            "internal server error", "rate limit", "apiconnectionerror",
            "service unavailable",
        )
    )


class _AttemptFailure(Exception):
    """Wraps an agent-attempt exception together with its produced-event count."""

    def __init__(self, produced: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.produced = produced
        self.cause = cause


async def _retry_async(
    attempt: Callable[[], Awaitable[tuple[str, int]]],
    attempts: int = 3,
    backoff: float = 2.0,
) -> tuple[str, int]:
    """Runs `attempt`, retrying transient model failures that produced nothing."""
    last: _AttemptFailure | None = None
    for i in range(attempts):
        try:
            return await attempt()
        except _AttemptFailure as failure:
            last = failure
            if (
                i < attempts - 1
                and failure.produced == 0
                and _transient_model_error(failure.cause)
            ):
                log.warning(
                    "transient model error (attempt %d/%d): %s; retrying",
                    i + 1, attempts, failure.cause,
                )
                await asyncio.sleep(backoff * (i + 1))
                continue
            raise failure.cause from None
    raise last.cause if last else RuntimeError("no attempts")


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

            self.calendar = AppleCalendarClient(target_calendar=self.cfg.calendar_target)
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
        # One-time pairing code: a rotated code stored by `app.cli
        # reset-pairing`, else the tail of the bot token; readable only on
        # the owner's Mac. First-/start alone no longer wins.
        override = memory.get_value(self.db, "pairing_code")
        self._pairing_code = override or (token[-8:] if token else None)
        self.telegram = telegram or (
            TelegramChannel(token, self.bus) if token else None
        )
        self.notifier = self.telegram or NullNotifier()
        if self.telegram is not None:
            self.telegram.set_owner_chat_id(memory.get_value(self.db, "paired_chat"))
        # Per-chat serialization: Telegram updates arrive as concurrent
        # tasks; commands and approval callbacks for one chat must not
        # interleave (double-tap Approve, burst messages).
        self._chat_locks: dict[int, asyncio.Lock] = {}

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
        # Stored bare: _run_agent builds a fresh resumable App per invocation,
        # so a never-executed wrapper here would only suggest a second path.
        self.coordinator = coordinator
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

    def _render_card(self, row: approval.Approval) -> str:
        """Owner-facing card text for an approval row (single renderer)."""
        p = row.payload
        if row.kind == "create_calendar_event":
            from .core.humanize import fmt_dt

            summary = (
                "<b>Create this calendar event?</b>\n"
                f"{escape_html(str(p.get('title', '')))}\n"
                f"{escape_html(fmt_dt(p.get('start', '')))} → "
                f"{escape_html(fmt_dt(p.get('end', '')))}"
            )
            notes = str(p.get("notes") or "")
            invitees = p.get("invitees") or []
            location = str(p.get("location") or "")
            if notes:
                if len(notes) > 200:
                    summary += (
                        f"\n{escape_html(notes[:200])}"
                        f"\n(+{len(notes) - 200} more characters — "
                        "the full notes are written to the event)"
                    )
                else:
                    summary += f"\n{escape_html(notes)}"
            if location:
                summary += f"\nWhere: {escape_html(location)}"
            if invitees:
                summary += f"\nAttendees: {escape_html(', '.join(map(str, invitees)))}"
            alarm = int(p.get("alarm_minutes") or 0)
            if alarm:
                summary += f"\nAlarm: {alarm} min before"
            if p.get("recurrence"):
                summary += f"\nRepeats: {escape_html(str(p['recurrence']))}"
        elif row.kind == "edit_calendar_event":
            from .core.humanize import fmt_dt

            changes = ", ".join(
                f"{k}: {escape_html(str(v))}"
                for k, v in (("new start", fmt_dt(p["new_start"]) if p.get("new_start") else None),
                             ("new end", fmt_dt(p["new_end"]) if p.get("new_end") else None),
                             ("new title", p.get("new_title")))
                if v
            )
            summary = (
                "<b>Edit this calendar event?</b>\n"
                f"{escape_html(str(p.get('old_title', '')))}\n"
                f"change: {changes or 'nothing'}"
            )
        elif row.kind == "delete_calendar_event":
            from .core.humanize import fmt_dt

            summary = (
                "<b>Delete this calendar event?</b>\n"
                f"{escape_html(str(p.get('old_title', '')))}\n"
                f"{escape_html(fmt_dt(p.get('old_start', '')))}"
            )
        else:
            body = str(p.get("body", ""))
            if len(body) > 800:
                shown = (
                    escape_html(body[:800])
                    + f"\n(+{len(body) - 800} more characters — "
                      "the full text is what gets sent)"
                )
            else:
                shown = escape_html(body)
            summary = (
                "<b>Send this email?</b>\n"
                f"To: {escape_html(str(p.get('to', '')))}\n"
                f"Subject: {escape_html(str(p.get('subject', '')))}\n\n"
                f"{shown}"
            )
        return summary

    async def _send_approval_card(self, row: approval.Approval) -> int:
        assert self.telegram is not None
        return await self.telegram.send_approval_card(row.id, self._render_card(row))

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

        async def attempt() -> tuple[str, int]:
            local_final, produced = "", 0
            try:
                async for event in runner.run_async(
                    user_id=user_id, session_id=session_id, new_message=message
                ):
                    produced += 1
                    if (
                        event.is_final_response()
                        and event.content
                        and event.content.parts
                    ):
                        local_final = "".join(
                            part.text or "" for part in event.content.parts
                        ).strip()
            except Exception as exc:
                raise _AttemptFailure(produced, exc) from exc
            return local_final, produced

        final, _produced = await _retry_async(attempt)
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
        from .core.db import needs_reply_open

        items = needs_reply_open(self.db, days=7, limit=10)
        if not items:
            return
        replied = self._replied_message_ids()
        awaiting = []
        for i in items:
            if not await self._is_replied(i, replied):
                awaiting.append(i)
        if not awaiting:
            return
        limit = self.cfg.budgets.get("triage_alerts", 5)
        if not budget.allow(self.db, "triage_alerts", limit, self.cfg.timezone):
            return
        rank = {"high": 0, "normal": 1, "low": 2}
        awaiting.sort(key=lambda i: rank.get(str(i.get("importance", "normal")), 1))
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

    async def _is_replied(self, item: dict, replied: set[str]) -> bool:
        uid = str(item.get("uid", ""))
        try:
            msg = await asyncio.to_thread(self.gmail.get_message, uid)
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
        chat_id = int(event.payload["chat_id"])
        text = str(event.payload["text"]).strip()
        paired = memory.get_value(self.db, "paired_chat")
        if paired is None:
            # Fail closed after a live `reset-pairing`: drop any stale
            # in-memory owner the channel still holds.
            self.telegram.set_owner_chat_id(None)
        elif chat_id != paired:
            return  # unpaired chat: no lock, no reply, no footprint
        async with self._lock_for(chat_id):
            await self._handle_command(chat_id, text)

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    EDIT_TTL_SECONDS = 600

    def _load_edit_pending(self, key: str) -> dict | None:
        """Read the edit marker; expire it after EDIT_TTL_SECONDS."""
        raw = memory.get_value(self.db, key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if (
                isinstance(data, dict)
                and time.time() - float(data.get("ts", 0)) <= self.EDIT_TTL_SECONDS
            ):
                return data
        except (json.JSONDecodeError, TypeError, ValueError):
            memory.set_value(self.db, key, None)  # garbage marker: clear it
            return None
        memory.set_value(self.db, key, None)  # expired: don't eat the next message
        return None

    async def _handle_command(self, chat_id: int, text: str) -> None:
        paired = memory.get_value(self.db, "paired_chat")
        if paired is None:
            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                code = parts[1].strip() if len(parts) > 1 else ""
                expected = (
                    memory.get_value(self.db, "pairing_code") or self._pairing_code
                )
                if expected and code == expected:
                    memory.set_value(self.db, "paired_chat", chat_id)
                    self.telegram.set_owner_chat_id(chat_id)
                    await self.telegram.send_message(
                        "Paired. This chat is now the owner channel.", "reminders"
                    )
                else:
                    try:
                        await self.telegram.reply_to(
                            chat_id,
                            "Pairing requires a one-time code. On the Mac "
                            "run: uv run python -m app.cli pair",
                        )
                    except Exception:
                        log.exception("failed to answer wrong pairing code")
            return
        if chat_id != paired:
            log.warning("unpaired chat %s attempted command; ignored", chat_id)
            return

        edit_key = f"edit_pending:{chat_id}"
        pending_edit = self._load_edit_pending(edit_key)
        if pending_edit:
            if text == "/cancel":
                memory.set_value(self.db, edit_key, None)
                await self.telegram.send_message("Edit cancelled.", "reminders")
                return
            row = approval.get(self.db, str(pending_edit["id"]))
            memory.set_value(self.db, edit_key, None)
            if row is not None and row.state == "pending":
                payload = dict(row.payload)
                payload["body"] = text
                approval.update_payload(self.db, row.id, payload)
                fresh = approval.get(self.db, row.id)
                if fresh is not None and fresh.telegram_message_id:
                    try:
                        await self.telegram.refresh_approval_card(
                            fresh.telegram_message_id,
                            self._render_card(fresh),
                            fresh.id,
                        )
                    except Exception:
                        log.exception("failed to refresh approval card after edit")
                await self.telegram.send_message(
                    "Draft updated — approve on the card.", "reminders"
                )
                return

        session_id = f"tg-{chat_id}"
        typing = self.telegram.start_typing()
        try:
            reply = await self._run_agent(
                self.coordinator,
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
        paired = memory.get_value(self.db, "paired_chat")
        if paired is None:
            # Fail closed: no pairing -> no approvals, and drop any stale
            # in-memory owner the channel still holds.
            if self.telegram is not None:
                self.telegram.set_owner_chat_id(None)
            log.warning("approval event with no pairing; dropped")
            return
        if chat_id is None or chat_id != paired:
            log.warning("approval event from non-paired chat %s; dropped", chat_id)
            return
        async with self._lock_for(int(chat_id)):
            await self._handle_approval(approval_id, verdict, chat_id)

    async def _handle_approval(
        self, approval_id: str, verdict: str, chat_id
    ) -> None:
        if verdict == "edit":
            row = approval.get(self.db, approval_id)
            if row is not None and row.state == "pending" and self.telegram:
                if row.kind != "send_email":
                    await self.telegram.send_message(
                        "Only email drafts can be edited here — reject the "
                        "card and ask for a new one instead.",
                        "reminders",
                    )
                    return
                memory.set_value(
                    self.db,
                    f"edit_pending:{chat_id}",
                    json.dumps({"id": approval_id, "ts": time.time()}),
                )
                await self.telegram.send_message(
                    "Reply with the corrected email body (or /cancel).",
                    "reminders",
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
        try:
            resumed = await self.resume_for_approval(row, confirmed)
        except Exception:
            log.exception("resume failed for approval %s", row.id)
            actionlog.log(
                self.db, "gate", "send_failed", "send", approval_id=row.id,
                detail={"stage": "resume"},
            )
            if self.telegram is not None:
                try:
                    await self.telegram.send_message(
                        f"❌ Approved action ({escape_html(row.kind)}) could not "
                        "run — resuming the agent failed. It will NOT be "
                        "retried automatically; ask again if still needed.",
                        "triage_alerts",
                    )
                except Exception:
                    log.exception("resume-failure notify also failed")
            resumed = False
        if self.telegram is not None and row.telegram_message_id:
            state = "approved — sending" if confirmed else "rejected"
            if not resumed:
                state += (
                    " — FAILED to resume; nothing was sent"
                    if confirmed
                    else "\n(paused run not found; nothing was sent)"
                )
            try:
                await self.telegram.edit_message_text(
                    row.telegram_message_id,
                    f"{self._render_card(row)}\n[{state}]",
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
            return self.coordinator
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
