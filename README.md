# Day Orchestrator

A single-user AI agent that organizes your day — Gmail, Telegram and Apple
Calendar — and **cannot act without your approval**. Runs entirely on your Mac.

| Where | What |
|---|---|
| [`day-orchestrator/`](day-orchestrator/) | **The project**: full README (why + install in ~5 min), source, tests, ADRs |
| [`docs/architecture.md`](docs/architecture.md) | Constraint-driven architecture: drivers → style → workflows → stack → evolution |
| [`day-orchestrator/docs/adr/`](day-orchestrator/docs/adr/) | 10 ADRs — every decision and its trade-offs |

## Highlights

- **Morning brief at 07:00** on Telegram: overnight email triaged into tasks,
  today's calendar, a short plan.
- **Calendar & Maps**: full calendar CRUD through Apple EventKit (create /
  edit / delete, alarms, recurrence, notes, invitees) — locations validated by
  Apple Maps geocoding and attached with coordinates. No OAuth; consent is one
  macOS dialog, granted to a signed `.app` bundle identity.
- **The approval gate is the product**: email sends and calendar writes pause
  the agent and raise an Approve/Reject/Edit card in Telegram. Free text never
  authorizes; every action lands in an append-only audit log.
- **Private by design**: outbound-only networking, one auditable SQLite file,
  no cloud service reading your mail.

## Try it

```bash
git clone https://github.com/adrianpuiu/day-orchestrator.git
cd day-orchestrator
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv is missing
uv run python -m app.cli install                  # guided: deps, secrets, consent, launchd
```

macOS only (EventKit + launchd). See
[`day-orchestrator/README.md`](day-orchestrator/README.md) for the full story
and [`docs/architecture.md`](docs/architecture.md) for the reasoning.
