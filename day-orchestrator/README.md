# Day Orchestrator

Single-user agent orchestrator that organizes your day: Gmail (IMAP/SMTP) + Telegram,
Google ADK (Python). Runs on your Mac; outbound-only networking; email sends and
calendar writes are approval-gated via Telegram and audited in SQLite.

Docs: [`../docs/architecture.md`](../docs/architecture.md) (constraint set) ·
[`docs/adr/`](docs/adr/) (ADR-001…010).

## Why we built it

Two problems, one inverted trust model.

**The morning scramble.** Overnight email, a calendar that lives on the phone,
tasks scattered across notes. We wanted one 07:00 Telegram brief that merges
the triaged inbox into concrete tasks, the day's calendar into a schedule, and
a short plan — written by an agent that reads the mail but is not allowed to
act on it.

**Assistants you can't trust.** Cloud assistants read your mail and act with
no receipt. Day Orchestrator inverts that: it runs **on your Mac**
(outbound-only networking, no public endpoints), keeps everything in **one
auditable SQLite file**, and **cannot send an email or write a calendar event
without you tapping Approve** in Telegram. Every transition lands in an
append-only action log; an at-most-once latch makes double-apply impossible;
identical duplicate requests collapse onto a single approval card; a newer
move or delete of the same event retires its earlier pending card; a failed
action after approval pushes an explicit ❌ instead of failing silently, and
every card ends on what actually happened (`✅ created`, `❌ failed`).

Design rules that followed (full reasoning in
[`../docs/architecture.md`](../docs/architecture.md), decisions in
`docs/adr/`):

- One process, one machine, outbound-only networking — nothing exposed to break in.
- The LLM runs only inside agent steps; adapters and the approval gate are
  deterministic code.
- Calendar reads and writes go through Apple EventKit inside a proper
  `.app` bundle (the TCC identity Apple requires) — no OAuth, your
  credentials never leave the machine; geocoding uses Apple Maps under the
  same identity.
- The gate is the product: pause → card → Approve/Reject/Edit → at-most-once
  apply → explicit ❌ on failure.
- The morning brief is idempotent: it covers mail received since the previous
  brief, keeps one task per email however often that mail is read again, and
  never revives a task the owner finished. Timed tasks stand in the Schedule
  next to calendar events, marked `(task)`.
- The coordinator answers about the calendar, pending cards and tasks from a
  snapshot read at the start of each turn and pinned to the end of the
  request — never from its own earlier messages. A chat conversation ends after
  a night of silence; the calendar, the cards and the tasks carry the state.

## Calendar & Maps (Apple EventKit)

The calendar adapter (ADR-009 + addenda) talks to Calendar.app through
EventKit inside the `DayOrchestrator.app` bundle — the same events you see on
your iPhone, no OAuth, no cloud.

| Capability | Example on Telegram |
|---|---|
| Read (all calendars) | „ce am mâine?" |
| Create with notes, location, alarm, recurrence | „pune sală luni și joi la 18:00, alarmă cu 30 min înainte" |
| **Geocoded locations** | „dentist la Str. Exemplu 10, București" → Apple Maps validates the address and attaches coordinates (travel-time on iPhone) |
| Move / rename / delete | „mută dentistul de joi" → proposes a conflict-free slot → card → Approve |
| Writable target calendar | `calendar_target = "Home"` in config.toml |

Everything is approval-gated and audited (`calendar_event_created` /
`calendar_event_updated` / `calendar_event_deleted`). Implementation notes and
the macOS TCC story live in
[`docs/adr/ADR-009-calendar-source.md`](docs/adr/ADR-009-calendar-source.md).

## Install (macOS, ~5 minutes)

```bash
git clone https://github.com/adrianpuiu/day-orchestrator.git && cd day-orchestrator
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv is missing
uv run python -m app.cli install
```

The installer walks through: dependencies → config (from `config.toml.example`,
Gemini API by default) → secrets (Telegram token, Gmail app password — input
hidden) → calendar consent (one macOS dialog) → the always-on launchd service.
Then run `uv run python -m app.cli pair` and send `/start <code>` (the
printed one-time code) to your bot on Telegram — done. Tomorrow's brief
arrives at
07:00; anything important emails push immediately.

## Layout

```
app/
  agent.py        ADK entrypoint (W5 coordinator for `adk web` debugging)
  runtime.py      composition root: adapters, scheduler, runners, bus, approval resume
  cli.py          setup + ops CLI (secrets, calendar consent, run, smoke)
  smoke.py        offline proof of the approval gate (no network, no real model)
  tools.py        typed tool layer: read | draft | send | notify; gated email +
                  calendar effects (create/edit/delete events, alarms, recurrence,
                  geocoded locations)
  core/           config, secrets, db (incl. email_triage), event bus, approval
                  gate, scheduler, budget, human date formatting
  channels/       gmail (IMAP IDLE + SMTP), telegram (long polling),
                  calendar (EventKit read+write via .app bundle, geocode)
  workflows/      W1 briefing · W2 triage · W3 follow-up sweep (LLM only in agent steps)
ops/launchd/      KeepAlive wrapper for always-on operation
DayOrchestrator.app/   bundle wrapper: TCC identity for EventKit + geocoding
tests/            unit (fakes) + integration (gate proofs, RUN_MAC EventKit probes)
```

## Setup (manual — the installer does all of this for you; secrets never go through chat)

```bash
cd day-orchestrator
uv sync

# 1. Gemini API key for the agent models (or GOOGLE_CLOUD_* for Vertex)
$EDITOR .env            # set GEMINI_API_KEY=...

# 2. Secrets into the project .env (git-ignored; Keychain is the fallback)
uv run python -m app.cli set-secret TELEGRAM_BOT_TOKEN   # BotFather token
uv run python -m app.cli set-secret GMAIL_ADDRESS
uv run python -m app.cli set-secret GMAIL_APP_PASSWORD   # needs Google 2FA, see ADR-002

# 3. Calendar access — one macOS consent prompt, no Google OAuth needed
#    (reads Calendar.app, which syncs with your iPhone; ADR-009)
uv run python -m app.cli auth-calendar
#    If briefs ever say "calendar unavailable": System Settings → Privacy &
#    Security → Calendars → allow the python binary.

# 4. Pair Telegram: `uv run python -m app.cli pair`, then send /start <code>
uv run python -m app.cli run

# Always-on (survives sleep/restart, catch-up on wake):
# the installer generates + bootstraps the launchd plist with real paths;
# it is idempotent - re-run it any time paths or config change.
uv run python -m app.cli install
launchctl kickstart -k gui/$(id -u)/com.agp.day-orchestrator
```

Optional `config.toml` (project root) overrides defaults — copy
`config.toml.example` if you start without the installer. Model serving: leave
`model_api_base` empty for Gemini model strings, or point it at any
OpenAI-compatible server (e.g. a LAN inference box) and use bare model ids:

```toml
model_api_base = ""            # empty = Gemini; or e.g. "http://192.168.1.20:8000/v1"
triage_model = "ds41"
planner_model = "ds41"
coordinator_model = "ds41"
brief_time = "07:00"
sweep_times = ["13:00", "18:00"]
budgets = { briefing = 1, triage_alerts = 5, reminders = 20 }
calendar_backend = "apple"     # "apple" (EventKit via bundle) | "google" (read-only)
calendar_target = ""           # "" = macOS default-for-new-events calendar
```

Structured output is instruction-driven JSON with tolerant parsing (no
`response_format`), so any engine works; tool calling must be supported by the
engine for the coordinator and task extraction.

Owner-facing date format is `DD/MM/YYYY - HH:MM` (brief lines, approval cards,
chat); internals stay ISO.

## Verify

```bash
uv run pytest -q            # 109 tests: gate, bus, stores, adapters (fakes), gate
                            # E2E, dedup, geocode args, preflight, retry loop,
                            # approval cards (multi-card resume, outcome footer,
                            # supersede), brief (one task per email, schedule,
                            # mail window)
uv run lint-imports         # layer + channel-independence contracts (ADR-006)
uv run ruff check .
uv run python -m app.smoke  # prints the offline approval-gate proof
RUN_LIVE=1 uv run pytest tests/eval -q   # agent evals on the configured model (15):
#   triage judgment (OTP=low, invoice=urgent, meeting=needs_reply, persisted to
#   email_triage), brief structure (sections, awaiting-reply, carry-over format,
#   no invented times), rubric-judged readability, multi-turn session
#   continuity, coordinator tool routing: task add, email through the gate,
#   calendar create/edit/delete through the gate, live calendar read for state
#   questions, slot proposal for underspecified moves. Nightly at 03:30 via
#   launchd; Telegram alert on failure.
RUN_MAC=1 uv run pytest tests/integration/test_helper_live.py -q
#   real EventKit probes through DayOrchestrator.app: TCC status, reads,
#   create with alarm + structured location, series rename override,
#   series delete; self-cleaning.
```

## Trust boundary

`send_email` and the calendar writes (`add_calendar_event`,
`edit_calendar_event`, `delete_calendar_event`) are gated paths: an approval
row in SQLite, a paused run (ADK resumability), an Approve/Reject/Edit card in
Telegram. Free text never authorizes; every transition lands in the
append-only `action_log`, and an at-most-once latch makes an approved row
impossible to apply twice (identical duplicate calls collapse onto the open
card). A failed effect after approval pushes an explicit ❌ to the owner.
Enforced by `tests/unit/test_send_path.py` + import-linter, not by review.
