"""Deterministic pipelines W1-W3 (architecture.md): glue agents have no LLM.

Fresh pipeline instances per run (ADK agents bind to one parent).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from ..channels.calendar import CalendarNotConfigured
from ..core import actionlog, budget, tasks
from ..core.config import Config
from ..core.textfmt import escape_html, markdown_to_telegram_html
from ..tools import ToolDeps, make_tools
from . import agents as wf_agents

log = logging.getLogger(__name__)


def _extract_json(value: Any) -> dict[str, Any]:
    """Tolerant parse of model text → JSON dict: strips fences, finds the object."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if "```" in text:  # drop markdown fences, keep the densest fenced block
        blocks = [b.strip() for b in text.split("```")]
        fenced = [b for b in blocks if b.startswith(("{", "[", "json"))]
        if fenced:
            text = min(fenced, key=len).removeprefix("json").strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return parsed if isinstance(parsed, dict) else {"items": parsed}
    return {}


class LoadState(BaseAgent):
    """Runs an async loader, writes the returned dict into session state."""

    loader: Any = None  # async () -> dict[str, Any]

    async def _run_async_impl(self, ctx: InvocationContext):
        data = await self.loader()
        for key, value in data.items():
            ctx.session.state[key] = value
        yield Event(author=self.name)


class PushText(BaseAgent):
    """Pipeline tail: push a state text to Telegram under the daily budget (C10)."""

    deps: Any = None
    cfg: Any = None
    text_key: str = "brief_markdown"
    category: str = "briefing"

    async def _run_async_impl(self, ctx: InvocationContext):
        text = ctx.session.state.get(self.text_key) or ""
        if not str(text).strip():
            actionlog.log(self.deps.db, "pipeline", "brief_empty", "system",
                          detail={"category": self.category})
            yield Event(author=self.name)
            return
        limit = self.cfg.budgets.get(self.category, 1)
        if not budget.allow(self.deps.db, self.category, limit, self.cfg.timezone):
            actionlog.log(self.deps.db, "pipeline", "notify_budget_blocked", "notify",
                          detail={"category": self.category})
            yield Event(author=self.name)
            return
        try:
            await self.deps.notifier.send(
                markdown_to_telegram_html(str(text)), self.category
            )
        except Exception:
            budget.refund(self.deps.db, self.category, self.cfg.timezone)
            raise
        yield Event(author=self.name)


class PersistTriage(BaseAgent):
    """Deterministic: writes triage results to email_triage (first-seen wins).
    Both pipelines run it right after the TriageAgent; the sweep and the brief
    read the table, never session memory."""

    deps: Any = None

    async def _run_async_impl(self, ctx: InvocationContext):
        from app.core.db import persist_triage

        triage = _extract_json(ctx.session.state.get("triage_json"))
        items = triage.get("items", []) if isinstance(triage, dict) else []
        new = persist_triage(self.deps.db, items)
        if new:
            actionlog.log(self.deps.db, "pipeline", "triage_persisted", "system",
                          detail={"new": new})
        yield Event(author=self.name)


class AlertHigh(BaseAgent):
    """W2 tail: push a digest of high-importance mail."""

    deps: Any = None
    cfg: Any = None

    async def _run_async_impl(self, ctx: InvocationContext):
        triage = _extract_json(ctx.session.state.get("triage_json"))
        items = triage.get("items", []) if isinstance(triage, dict) else []
        high = [i for i in items if i.get("importance") == "high"]
        if not high:
            yield Event(author=self.name)
            return
        limit = self.cfg.budgets.get("triage_alerts", 5)
        if not budget.allow(self.deps.db, "triage_alerts", limit, self.cfg.timezone):
            actionlog.log(self.deps.db, "pipeline", "notify_budget_blocked", "notify",
                          detail={"category": "triage_alerts"})
            yield Event(author=self.name)
            return
        lines = ["<b>High-importance mail:</b>"]
        for item in high[:5]:
            summary = escape_html(str(item.get("summary", "")))
            lines.append(f"• {summary}")
        try:
            await self.deps.notifier.send("\n".join(lines), "triage_alerts")
        except Exception:
            budget.refund(self.deps.db, "triage_alerts", self.cfg.timezone)
            raise
        yield Event(author=self.name)


class RefreshTaskLines(BaseAgent):
    """W1, after task extraction: the brief lists the tasks as they are now —
    including the ones this run just extracted from the overnight mail."""

    deps: Any = None
    cfg: Any = None

    async def _run_async_impl(self, ctx: InvocationContext):
        ctx.session.state["task_lines"] = tasks.as_brief_lines(
            tasks.list_open_bucketed(self.deps.db, self.cfg.timezone)
        )
        yield Event(author=self.name)


def _brief_window_start(db) -> datetime:
    """A brief covers mail received since the previous brief went out (so a
    quiet day or downtime never repeats or drops mail); without an earlier
    brief, the last 24 hours."""
    row = db.query_one(
        "SELECT ts FROM action_log WHERE action='brief_sent' "
        "ORDER BY seq DESC LIMIT 1"
    )
    if row is not None:
        try:
            start = datetime.fromisoformat(row["ts"])
            return start if start.tzinfo else start.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC) - timedelta(hours=24)


def _received_since(
    emails: list[dict[str, Any]], start: datetime
) -> list[dict[str, Any]]:
    """Emails received at or after `start`. Undated mail cannot be placed in
    a window and is left out."""
    kept: list[dict[str, Any]] = []
    for email in emails:
        try:
            received = datetime.fromisoformat(str(email.get("date") or ""))
        except ValueError:
            continue
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        if received >= start:
            kept.append(email)
    return kept


def _local(value: Any) -> datetime:
    """Local wall clock of an ISO datetime; a naive value is local time."""
    return datetime.fromisoformat(str(value)).astimezone()


def _timed(due: Any) -> datetime | None:
    """Local datetime of a due value that names a time of day. Date-only dues
    parse as midnight and are not schedule slots (the fmt_dt rule)."""
    if not due:
        return None
    try:
        moment = datetime.fromisoformat(str(due))
    except ValueError:
        return None
    if moment.hour == moment.minute == 0:
        return None
    return moment.astimezone()


def _schedule_lines(
    events: list[dict[str, Any]],
    buckets: dict[str, list[dict[str, Any]]],
    notice: str = "",
) -> str:
    """The day in chronological order as brief-ready markdown lines: calendar
    events plus the tasks that name a time today or tomorrow, marked
    '(task)' — they are not in the calendar. A degraded-calendar `notice`
    stays on the first line."""
    from app.core.humanize import fmt_dt, fmt_span

    unplaced = datetime.max.replace(tzinfo=UTC)  # all-day/undated sorts last
    slots: list[tuple[datetime, str]] = []
    for event in events:
        try:
            span, start = fmt_span(event["start"], event["end"]), _local(event["start"])
        except (KeyError, TypeError, ValueError):
            span, start = "all-day", unplaced
        line = f"- {span} {event.get('summary') or '(untitled)'}"
        if event.get("location"):
            line += f" · {event['location']}"
        slots.append((start, line))
    for name in ("today", "tomorrow"):
        for task in buckets.get(name, ()):
            start = _timed(task.get("due"))
            if start is not None:
                slots.append(
                    (start, f"- {fmt_dt(task['due'])} {task['title']} (task)")
                )
    slots.sort(key=lambda slot: slot[0])
    lines = ([notice] if notice else []) + [line for _, line in slots]
    if not slots and not notice:
        lines.append("No events or timed tasks.")
    return "\n".join(lines)


def briefing_loader(deps: ToolDeps, cfg: Config) -> Callable[[], Awaitable[dict[str, str]]]:
    async def loader() -> dict[str, str]:
        emails = await asyncio.to_thread(deps.gmail.fetch_recent, cfg.max_overnight_emails)
        emails = _received_since(emails, _brief_window_start(deps.db))
        buckets = tasks.list_open_bucketed(deps.db, cfg.timezone)
        events: list[dict[str, Any]] = []
        notice = ""
        try:
            events = await asyncio.to_thread(deps.calendar.get_events, 2)
        except CalendarNotConfigured:
            notice = "Calendar not configured."
        except Exception:
            log.warning("calendar unavailable; briefing degrades", exc_info=True)
            notice = "Calendar temporarily unavailable."
        return {
            "overnight_emails": json.dumps(emails),
            "schedule_lines": _schedule_lines(events, buckets, notice),
            "task_lines": tasks.as_brief_lines(buckets),
            "needs_reply_lines": _needs_reply_lines(deps.db),
        }

    return loader


def _needs_reply_lines(db) -> str:
    """Pre-rendered 'Awaiting your reply' lines (needs_reply, last 7 days)."""
    from app.core.db import needs_reply_open

    items = needs_reply_open(db, days=7, limit=5)
    if not items:
        return "None."
    return "\n".join(f"- {item['summary']}" for item in items)


def incoming_loader(emails: list[dict[str, Any]]) -> Callable[[], Awaitable[dict[str, str]]]:
    async def loader() -> dict[str, str]:
        return {"overnight_emails": json.dumps(emails)}

    return loader


def _tool_named(tools: list, name: str):
    for tool in tools:
        if tool.name == name:
            return tool
    raise LookupError(f"tool {name!r} not in toolset")


def make_briefing_pipeline(deps: ToolDeps, cfg: Config) -> SequentialAgent:
    """W1 Morning brief: fetch → triage → task extraction → plan → push."""
    tools = make_tools(deps)
    return SequentialAgent(
        name="briefing_pipeline",
        sub_agents=[
            LoadState(name="load_briefing_inputs", loader=briefing_loader(deps, cfg)),
            wf_agents.make_triage_agent(cfg),
            PersistTriage(name="persist_triage", deps=deps),
            wf_agents.make_task_extractor(cfg, _tool_named(tools, "add_task_from_email")),
            RefreshTaskLines(name="refresh_task_lines", deps=deps, cfg=cfg),
            wf_agents.make_planner_agent(cfg),
            PushText(name="push_briefing", deps=deps, cfg=cfg),
        ],
    )


def make_incoming_pipeline(
    deps: ToolDeps, cfg: Config, emails: list[dict[str, Any]]
) -> SequentialAgent:
    """W2 New-mail triage: triage → persist + alert on high importance."""
    return SequentialAgent(
        name="incoming_pipeline",
        sub_agents=[
            LoadState(name="load_incoming", loader=incoming_loader(emails)),
            wf_agents.make_triage_agent(cfg),
            PersistTriage(name="persist_triage", deps=deps),
            AlertHigh(name="alert_high", deps=deps, cfg=cfg),
        ],
    )
