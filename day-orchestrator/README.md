# Day Orchestrator

Single-user agent orchestrator that organizes your day: Gmail (IMAP/SMTP) + Telegram,
Google ADK (Python). Runs on your Mac; outbound-only networking; every email send is
approval-gated via Telegram and audited in SQLite.

Docs: [`docs/architecture.md`](docs/architecture.md) (constraint set) ·
[`docs/adr/`](docs/adr/) (ADR-001…006).

## Layout

```
app/
  agent.py        ADK entrypoint (W5 coordinator for `adk web` debugging)
  runtime.py      composition root: adapters, scheduler, runners, bus, approval resume
  cli.py          setup + ops CLI (secrets, calendar consent, run, smoke)
  smoke.py        offline proof of the approval gate (no network, no real model)
  tools.py        typed tool layer: read | draft | send | notify; single send path
  core/           config, secrets, db, event bus, approval gate, scheduler, budget
  channels/       gmail (IMAP IDLE + SMTP), telegram (long polling), calendar (read-only)
  workflows/      W1 briefing · W2 triage · W3 sweep pipelines (LLM only in agent steps)
ops/launchd/      KeepAlive wrapper for always-on operation
tests/            unit (fakes, no network) + integration (approval-gate proofs)
```

## Setup (owner runs these — secrets never go through chat)

```bash
cd day-orchestrator
uv sync

# 1. Gemini API key for the agent models (or GOOGLE_CLOUD_* for Vertex)
$EDITOR .env            # set GEMINI_API_KEY=...

# 2. Secrets into macOS Keychain
uv run python -m app.cli set-secret TELEGRAM_BOT_TOKEN   # BotFather token
uv run python -m app.cli set-secret GMAIL_ADDRESS
uv run python -m app.cli set-secret GMAIL_APP_PASSWORD   # needs Google 2FA, see ADR-002

# 3. One-time calendar consent (opens browser; needs OAuth client secret JSON)
uv run python -m app.cli auth-calendar --client-secret ~/Downloads/client_secret.json

# 4. Pair Telegram: run the app, send /start from your chat
uv run python -m app.cli run

# Always-on (survives sleep/restart, catch-up on wake):
cp ops/launchd/com.agp.day-orchestrator.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agp.day-orchestrator.plist
```

Optional `config.toml` (project root) overrides defaults: `brief_time`,
`sweep_times`, models, `gmail_poll_seconds`, `budgets` (notification caps), `timezone`.

## Verify

```bash
uv run pytest -q            # 41 tests: gate, bus, stores, adapters (fakes), gate E2E
uv run lint-imports         # layer + channel-independence contracts (ADR-006)
uv run ruff check .
uv run python -m app.smoke  # prints the offline approval-gate proof
```

## Trust boundary

`send_email` is the only send path. It creates an approval row in SQLite, pauses the
run (ADK resumability), and shows an Approve/Reject/Edit card in Telegram. Free text
never authorizes; every transition lands in the append-only `action_log`. Enforced by
`tests/unit/test_send_path.py` + import-linter, not by review.
