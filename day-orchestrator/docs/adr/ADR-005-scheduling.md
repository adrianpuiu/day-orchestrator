# ADR-005: Scheduling — APScheduler on SQLite, idempotent catch-up

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

The Mac sleeps; the process restarts (launchd KeepAlive, ADR-006 ops wrapper). Briefs
and sweeps must fire once — never zero times without a trace, never twice (reliability
+2.5; a double send destroys trust like an unapproved send).

## Decision

APScheduler 3.x `AsyncIOScheduler` with `SQLAlchemyJobstore` pointed at the shared
SQLite file. Every scheduled **effect** carries a deterministic job id = dedup key
(e.g. `brief-2026-11-22-morning`). Effect handlers check/claim the dedup key in
`action_log` (unique constraint) before doing work, so:

- normal fire → claimed, runs once;
- restart/wake after a missed tick (within `misfire_grace_time`, `coalesce=True`) →
  runs once, then claims;
- process restarts between claim and effect completion → the unclaimed key reruns
  (at-least-once effects must be idempotent; email reads/triage are, sends are
  approval-gated anyway).

Reminders are persisted rows with their own fire-time jobs recreated from rows on
startup (jobstore is a cache, the row is the truth).

## Alternatives considered

- **OS cron / launchd StartCalendarInterval per job** — rejected: N processes, no
  shared state, no catch-up semantics beyond "run if window missed", loses in-process
  bus access.
- **APScheduler 4.x** — rejected for v1: 3.x AsyncIOScheduler is the boring, documented
  path; 4's scheduler-on-store semantics differ enough to matter under sleep/wake.

## Consequences

Positive: brief ≤2 min punctuality when awake; ≤2 min catch-up after wake; no double
fires across restarts.
Negative (accepted): missed effects older than the grace window are skipped by design —
the morning brief for "yesterday" never fires at noon; the handler logs the skip to
`action_log` so the gap is visible (observability +2).
