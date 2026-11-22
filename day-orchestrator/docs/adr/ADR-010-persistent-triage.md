# ADR-010: Persistent triage — `email_triage` is the source of truth

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

Follow-up sweeps read `memory["last_triage"]` — a session-memory value
**replaced by every incoming batch**, so the overnight triage (W1, up to 50
emails — the batch that matters most) never reached the 13:00/18:00 sweep.
Session state also evaporates with the session.

## Decision

- New table `email_triage` (uid PK, summary, category, importance,
  created_at). **INSERT OR IGNORE: first-seen classification wins** —
  re-triage never rewrites history.
- `PersistTriage` — a deterministic (no-LLM) pipeline stage — persists right
  after the TriageAgent in **both** W1 and W2.
- Consumers: W3 sweep (needs_reply ≤7 days, replied-filter via the
  `in_reply_to` email_sent audit, budgeted) and the W1 brief
  ("## Awaiting your reply" section, copy-exact).
- `memory["last_triage"]` is retired.

## Consequences

Bounded spam: the 7-day window + per-category budgets cap the nudges; there
is no per-email dismissal UI yet (replying removes the thread; a "handled"
flag is the natural next step if needed). Triage classifications are
immutable once written — reclassification would need a new ADR.
