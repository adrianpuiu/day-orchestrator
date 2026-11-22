# ADR-008: Credential storage — git-ignored `.env` is canonical, Keychain fallback

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

C8 originally preferred macOS Keychain with `.env` as fallback. After the
owner pasted credentials into chat (and asked for file-based storage), the
trade-off was made explicit and decided: **visibility and migrability beat
encryption-at-rest for a single-user machine.**

## Decision

Credentials (`TELEGRAM_BOT_TOKEN`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`,
future OAuth tokens) live in the project `.env`, which is **git-ignored**
(`day-orchestrator/.gitignore`) and therefore never committed. Precedence in
`core/secrets.py::Secrets` is **`.env` → Keychain → process env** — the order
is the contract. `Secrets.set()` writes the canonical (first) backend;
`app.cli set-secret` writes `.env`. Keychain copies were deleted at the flip
so exactly one source of truth exists.

## Hard rule (the reason this ADR exists)

**No credential is ever hardcoded** — not in source, config, docs, DB rows,
logs, or prompts. The only permitted locations are this `.env` (ignored), the
fallback Keychain, and process env. Enforced socially today; the AST-based
send-path gate (`test_send_path.py`) is the precedent for mechanical checks if
drift appears.

## Consequences

Positive: five-minute setup on a new machine (`git clone` + copy `.env` +
`uv sync`); secrets inspectable without `security` incantations; launchd works
unchanged (same user session).

Negative (accepted): `.env` is plaintext on disk — protection is file
permissions, not Keychain encryption; `chmod 600` is recommended; rotation of
the chat-pasted credentials (app password, bot token) remains advisable since
they exist in a chat transcript.
