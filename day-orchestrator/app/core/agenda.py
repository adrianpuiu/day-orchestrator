"""Live state for the coordinator: what is in the calendar, which requests
await approval, which tasks are open — rendered at the start of every chat
turn so the model answers from the system, never from its own earlier
messages (2026-11-26: a "HI" was answered with the calendar list of four
days before, labelled "confirmed")."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import approval, tasks
from .db import Database
from .humanize import fmt_dt, fmt_span

CALENDAR_DAYS = 7  # window of the snapshot


def _span(start: Any, end: Any) -> str:
    """'DD/MM/YYYY - HH:MM → HH:MM' (the full end date when days differ)."""
    try:
        return fmt_span(start, end)
    except (TypeError, ValueError):
        return f"{fmt_dt(start)} → {fmt_dt(end)}"


def _request_line(row: approval.Approval) -> str:
    """One pending request as the owner reads it on its card, in plain text."""
    p = row.payload
    if row.kind == "create_calendar_event":
        return (
            f'create event "{p.get("title", "")}" — '
            f"{_span(p.get("start"), p.get("end"))}"
        )
    if row.kind == "edit_calendar_event":
        changes = ", ".join(filter(None, (
            f"start {fmt_dt(p["new_start"])}" if p.get("new_start") else "",
            f"end {fmt_dt(p["new_end"])}" if p.get("new_end") else "",
            f'title "{p["new_title"]}"' if p.get("new_title") else "",
        )))
        return f'edit "{p.get("old_title", "")}": {changes or "nothing"}'
    if row.kind == "delete_calendar_event":
        return f'delete "{p.get("old_title", "")}" ({fmt_dt(p.get("old_start", ""))})'
    return f'{row.kind} to {p.get("to", "")}: {p.get("subject", "")}'


def live_state(
    db: Database,
    events: list[dict[str, Any]] | None,
    now: datetime,
    tz: str = "local",
) -> str:
    """The coordinator's ground truth for one turn, as plain-text lines.

    `events` are the calendar events from `now` on; None when the calendar
    could not be read — the snapshot then says so instead of staying silent.
    """
    horizon = now + timedelta(days=CALENDAR_DAYS)
    lines = [f"Calendar, {now:%d/%m/%Y} → {horizon:%d/%m/%Y}:"]
    if events is None:
        lines.append("- unavailable right now. Say so; never list events from memory.")
    elif not events:
        lines.append("- no events.")
    for event in events or ():
        try:
            span = fmt_span(event["start"], event["end"])
        except (KeyError, TypeError, ValueError):
            span = "all-day"
        where = f" · {event['location']}" if event.get("location") else ""
        lines.append(f"- {span} {event.get('summary') or '(untitled)'}{where}")

    pending = approval.list_pending(db)
    lines.append("Requests awaiting approval:" if pending
                 else "Requests awaiting approval: none.")
    lines.extend(f"- [{row.id}] {_request_line(row)}" for row in pending)

    lines.append("Open tasks:")
    lines.extend(tasks.as_brief_lines(tasks.list_open_bucketed(db, tz)).split("\n"))
    return "\n".join(lines)
