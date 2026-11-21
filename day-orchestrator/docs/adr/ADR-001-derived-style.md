# ADR-001: Derived style — Gated Proactive Monolith

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

Single-user day organizer. Drivers, capability targets, and the full constraint set are
derived in [docs/architecture.md](../architecture.md). This ADR records the decision and
its consequences at ADR granularity.

## Decision

Adopt the **Gated Proactive Monolith** derived style: Modular Monolith base (single
process, domain-partitioned modules, SQLite), the Orchestrated Event-Driven mediator
constraint applied in-process (deterministic workflow pipelines; SQLite as the persistent
"queue", no broker), a uniform typed tool layer between the LLM and the world
(`read | draft | send` effect classes), and a database-backed approval gate as the single
trust boundary.

## Alternatives considered

- **Multi-service decomposition** — rejected: scalability target is −2 (one user); the
  distributed tax buys nothing here. Revisit only via architecture.md evolution path.
- **ADK built-in tool confirmation only** (no own approval store) — rejected as the sole
  mechanism: approval must survive restarts and be auditable independently of session
  events. ADK resumability is used *for* pause/resume (ADR-004), but the approval record
  is ours.
- **Big Ball of Mud** — rejected: evolvability +3 target and CI-enforced boundaries are
  cheap at this size; mud is not.

## Consequences

Positive: one deploy unit, one state file, one send path, cheap evolution to Docker.
Negative (accepted): shared failure domain; no redundancy (state = one copyable file);
approval round-trip on every send; module boundaries must be enforced (import-linter,
ADR-005 CI gates) or drift to mud.
