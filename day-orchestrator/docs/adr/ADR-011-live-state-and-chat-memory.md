# ADR-011: Live-state snapshot pinned to the request; chat conversations end after silence

Date: 2026-11-26 · Status: accepted · Owner: agp

## Context

The coordinator's answers must describe state that changes between turns: events
are edited from the phone, cards are tapped, tasks are completed, the date rolls
over. Until 2026-11-26 it had one source for that — its own earlier messages in
a single endless Telegram session per chat.

- 2026-11-26 01:25: "HI" was answered with "azi ai cafea cu Diana la 22:00 si
  vizionare cu Max la 23:00", labelled confirmed — the calendar list of
  2026-11-22, recited from four days earlier.
- Within one conversation: an event removed between two turns came back from
  the previous answer.

Measured on the LAN model
(`tests/eval/test_live_agent.py::test_coordinator_answers_from_live_state`,
8 runs per variant): snapshot in the system prompt → 2/8; the same snapshot
pinned to the end of the request → 8/8.

## Decision

1. Every coordinator turn takes a **live-state snapshot** before the model runs
   (`_live_state_before_turn`, rendered by `core/agenda.py::live_state`): the
   calendar for the next `CALENDAR_DAYS` days, the pending approval cards, the
   open tasks, and the clock. A calendar that cannot be read makes the snapshot
   say so — the coordinator must not fill the gap from memory.
2. The snapshot is **the last content of every request** (`pin_live_state`,
   wired as the coordinator's `before_model_callback`): after the owner's
   message, or after a tool result mid-turn. The system prompt only names it.
   The model reads what it said a minute ago as the fresher fact; the system's
   reading has to be the last word before the answer.
3. A chat **conversation ends after 8 hours of silence**
   (`Runtime._chat_session_id`): the next message opens a fresh ADK session
   (ADR-003 keeps every session in SQLite). Chat history is context for the
   exchange at hand; durable state lives in the calendar, the approval rows and
   the tasks. A paused run resumes in its own session (ADR-004), whatever the
   chat is doing meanwhile.

## Alternatives considered

- **A rule in the instruction ("never repeat an earlier list")** — kept as
  wording, rejected as the mechanism: with the snapshot in the system prompt
  the stale event still came back in 6 of 8 runs.
- **`include_contents="none"` plus a hand-written transcript** — rejected:
  re-implements ADK's turn assembly to gain what item 2 already gives.
- **Daily session rollover** — rejected for silence-based: a chat that
  continues past midnight keeps its thread, and one that went cold at 15:00 is
  not revived at 23:00.
- **Trimming or rewriting the stored history** — rejected: edits ADK-owned
  session events to argue with a prompt; the snapshot plus short conversations
  leave the history nothing stale to add.

## Consequences

Positive: calendar, pending-card and task claims come from a reading taken in
the same turn; a cold conversation is no longer in the request, so nothing
day-old can be presented as current.
Negative (accepted): every coordinator model call carries the snapshot; a
follow-up typed the next morning has no antecedent for "it" — the owner names
the event, or the coordinator reads it from the snapshot's pending cards and
calendar.
