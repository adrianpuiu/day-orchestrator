# ADR-003: ADK Python + DatabaseSessionService on the shared SQLite file

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

Framework is Google ADK (Python ≥2.12). The system runs workflows W1–W4 headlessly
(scheduler/mail triggers) and W5 interactively (Telegram commands). Sessions must persist
across restarts (architecture.md C2: SQLite-only persistence, reliability +2.5).

## Decision

One SQLite database file holds **everything**: ADK session tables via
`DatabaseSessionService("sqlite:///<data_dir>/day.db")` and the app's own tables
(approvals, action_log, tasks, memory) created by the same process. The composition root
(`app/runtime.py`) builds the session service once and passes it to every `Runner`.

Model routing per agent (affordability +3): triage batches run a flash-class model
(`gemini-3.8-flash` default), the planner runs a stronger model; both are config values
(`config.toml`), not literals in agent code.

## Alternatives considered

- **InMemorySessionService** — rejected: loses W4 approval-blocked runs on restart.
- **VertexAiSessionService** — rejected: GCP dependency contradicts C1/local-only.
- **Separate DB files** — rejected: one file is one backup, one WAL, one integrity story.

## Consequences

Positive: crash-safe workflow pause/resume; zero infra; a single copyable state artifact.
Negative (accepted): single-writer discipline required (one process; SQLite WAL allows
concurrent readers); SQLAlchemy is already an ADK dependency so no new weight; DB
migrations are manual for v1 (single user, disposable dev state — a `schema_version`
row guards future migrations).
