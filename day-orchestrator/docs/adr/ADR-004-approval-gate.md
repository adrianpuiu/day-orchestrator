# ADR-004: Approval gate — SQLite state machine + Telegram inline keyboard

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

Email autonomy is read+draft-only (driver D4): nothing sends without owner approval.
Auditability (+4, critical) requires every send to trace to a recorded approval.
The owner approves from their phone via Telegram. A workflow can block for **hours**
(on the phone, asleep), so the pause must survive process restarts.

## Decision

`send`-class effects flow through an **approval row** in SQLite:
`pending → approved | rejected | cancelled` (+ `superseded` when a newer draft replaces
an older one for the same thread). State transitions happen only in
`app/core/approval.py` behind explicit functions; no other module mutates approval rows.

1. Tool creates an approval row and calls `tool_context.request_confirmation(...)`,
   pausing the ADK run (resumability enabled; ADR-003 persistence keeps it durable).
2. Telegram adapter renders a card with inline buttons (Approve / Reject / Edit) bound
   to the approval id.
3. The callback handler resolves the row (Approve/Reject) or updates the draft payload
   (Edit) — never sends directly.
4. The runtime resumes the paused run; the tool re-executes, checks the row, and either
   sends (SMTP) and appends to `action_log`, or returns `rejected`.

**Free-text authorization is forbidden**: the word "yes" in chat never resolves an
approval; only callbacks bound to a row id do. There is exactly one send path
(`tools.send_email` → gmail adapter), enforced by CI gates (ADR-006).

## Alternatives considered

- **Parse chat text for approval** — rejected: unauditable, fragile, and the classic
  social-engineering hole.
- **ADK `require_confirmation` alone** — rejected as the record of truth: confirmation
  state lives in session events; we need an independent, queryable audit row.
- **Web approval UI** (like ambient-expense-agent) — rejected: requires an inbound HTTP
  listener, violating C1. Telegram inline keyboards give the same UX outbound-only.

## Consequences

Positive: durable across restarts; append-only `action_log` gives the +4 audit story;
the same gate serves future send-class effects (calendar writes) unchanged.
Negative (accepted): every send costs a round-trip — that friction is the product (D4);
Edit flow needs a small keyed conversation state in the Telegram adapter.
