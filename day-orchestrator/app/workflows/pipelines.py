"""Deterministic pipelines W1-W3 (architecture.md): glue agents have no LLM.

Fresh pipeline instances per run (ADK agents bind to one parent).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from ..channels.calendar import CalendarNotConfigured
from ..core import actionlog, budget, memory, tasks
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
        await self.deps.notifier.send(
            markdown_to_telegram_html(str(text)), self.category
        )
        yield Event(author=self.name)


class AlertHigh(BaseAgent):
    """W2 tail: store triage results; push a digest of high-importance mail."""

    deps: Any = None
    cfg: Any = None

    async def _run_async_impl(self, ctx: InvocationContext):
        triage = _extract_json(ctx.session.state.get("triage_json"))
        memory.set_value(self.deps.db, "last_triage", triage)
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
        await self.deps.notifier.send("\n".join(lines), "triage_alerts")
        yield Event(author=self.name)


def _format_events(events: list[dict[str, Any]]) -> str:
    """Renders calendar events as brief-ready markdown lines (local time)."""
    from app.core.humanize import fmt_span

    lines: list[str] = []
    for event in events:
        try:
            span = fmt_span(event["start"], event["end"])
        except (KeyError, TypeError, ValueError):
            span = "all-day"
        line = f"- {span} {event.get('summary') or '(untitled)'}"
        if event.get("location"):
            line += f" · {event['location']}"
        lines.append(line)
    return "\n".join(lines) if lines else "No events."


def briefing_loader(deps: ToolDeps, cfg: Config) -> Callable[[], Awaitable[dict[str, str]]]:
    async def loader() -> dict[str, str]:
        emails = await asyncio.to_thread(deps.gmail.fetch_recent, cfg.max_overnight_emails)
        try:
            events = await asyncio.to_thread(deps.calendar.get_events, 2)
            calendar_lines = _format_events(events)
        except CalendarNotConfigured:
            calendar_lines = "Calendar not configured."
        except Exception:
            log.warning("calendar unavailable; briefing degrades", exc_info=True)
            calendar_lines = "Calendar temporarily unavailable."
        return {
            "overnight_emails": json.dumps(emails),
            "calendar_lines": calendar_lines,
            "task_lines": tasks.as_brief_lines(
                tasks.list_open_bucketed(deps.db, cfg.timezone)
            ),
        }

    return loader


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
            wf_agents.make_task_extractor(cfg, _tool_named(tools, "add_task")),
            wf_agents.make_planner_agent(cfg),
            PushText(name="push_briefing", deps=deps, cfg=cfg),
        ],
    )


def make_incoming_pipeline(
    deps: ToolDeps, cfg: Config, emails: list[dict[str, Any]]
) -> SequentialAgent:
    """W2 New-mail triage: triage → store + alert on high importance."""
    return SequentialAgent(
        name="incoming_pipeline",
        sub_agents=[
            LoadState(name="load_incoming", loader=incoming_loader(emails)),
            wf_agents.make_triage_agent(cfg),
            AlertHigh(name="alert_high", deps=deps, cfg=cfg),
        ],
    )
