"""LLM step factories. Fresh instances per pipeline call (ADK parent binding).

Model strings come from Config (ADR-003: affordability via flash-for-batch).
"""

from __future__ import annotations

from typing import Any, Literal

from google.adk.agents import Agent
from pydantic import BaseModel, Field

TRIAGE_INSTRUCTION = """You triage emails for a busy owner.

Emails (JSON array; empty means none): {overnight_emails}

For each email decide:
- importance: "high" (action needed today, or a real person pressing),
  "normal", or "low" (newsletters, notifications).
- category: needs_reply | meeting | newsletter | fyi | task | other
- summary: one sentence.

Judge only from what is in the emails. Do not invent deadlines."""

BRIEF_INSTRUCTION = """Compose the owner's morning brief in concise Markdown (<=250 words).

Inputs:
- Triaged overnight email (JSON): {triage_json}
- Calendar events (JSON): {calendar_events}
- Open tasks (JSON): {open_tasks}

Structure:
## Day plan
One short paragraph: what today looks like given the calendar and tasks.
## Needs attention
High-importance emails, one line each (subject — one-line why).
## Everything else
One line per remaining non-trivial email, or "Nothing else notable."

Facts only from the inputs. If a section has no items, write "Nothing."
Never invent times, names, or deadlines."""

TASK_EXTRACT_INSTRUCTION = """From the triaged emails below (JSON), extract concrete
actionable tasks. Use the add_task tool for each one: a short title and an ISO due
datetime when the email states one — never invent one. Emails: {triage_json}

Extract nothing for newsletters or pure notifications. If nothing is actionable,
do nothing and reply exactly: none."""

COORDINATOR_INSTRUCTION = """You are the owner's day-organizing assistant, reachable via
Telegram. You organize their day: email, calendar, tasks, reminders.

Rules:
- Be concise; markdown.
- Use the read tools to answer questions about email, calendar, and tasks.
- To send an email, use send_email — the owner approves in Telegram before it
  actually sends; tell them approval is pending.
- save_draft_reply only drafts; say so.
- notify_user pushes a Telegram message; do not spam it for replies to the owner."""


class TriageItem(BaseModel):
    uid: str = Field(description="The email uid from the input")
    importance: Literal["high", "normal", "low"]
    category: Literal["needs_reply", "meeting", "newsletter", "fyi", "task", "other"]
    summary: str = Field(description="One-sentence summary")


class TriageResult(BaseModel):
    items: list[TriageItem]


def make_triage_agent(model: str) -> Agent:
    return Agent(
        name="triage_agent",
        model=model,
        instruction=TRIAGE_INSTRUCTION,
        output_schema=TriageResult,
        output_key="triage_json",
        include_contents="none",
        generate_content_config=None,
    )


def make_planner_agent(model: str) -> Agent:
    return Agent(
        name="planner_agent",
        model=model,
        instruction=BRIEF_INSTRUCTION,
        output_key="brief_markdown",
        include_contents="none",
    )


def make_task_extractor(model: str, add_task_tool) -> Agent:
    return Agent(
        name="task_extractor",
        model=model,
        instruction=TASK_EXTRACT_INSTRUCTION,
        tools=[add_task_tool],
        include_contents="none",
    )


def make_coordinator(model: Any, tools: list) -> Agent:
    return Agent(
        name="day_coordinator",
        model=model,
        instruction=COORDINATOR_INSTRUCTION,
        tools=tools,
    )
