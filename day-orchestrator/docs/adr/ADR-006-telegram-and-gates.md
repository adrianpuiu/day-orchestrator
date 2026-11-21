# ADR-006: Telegram channel — aiogram long polling; enforcement of boundaries

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

Telegram is the only user channel (D2) and the approval surface (ADR-004). The process
must not open inbound listeners (C1: outbound-only networking).

## Decision

`aiogram` 3.x `Bot` with **long polling** (`delete_webhook` on start, polling task on
the shared asyncio loop). Proactive messages are ordinary `send_message` /
`edit_message_reply_markup` calls. Approval cards carry `callback_data="appr:<id>:<verdict>"`;
the callback handler resolves the approval row and pokes the runtime to resume blocked
runs. Free-text messages route to the W5 coordinator agent (ADK `Runner` over the
shared session service). The bot token lives in Keychain/.env (C8), never in chat or DB.

**Enforcement of module boundaries (CI):**
- `import-linter` layered contract: `core ← channels ← tools ← workflows ← app`;
  `channels` may not import `channels` or `workflows`; only `app` (composition root)
  imports across top-level packages.
- **Send-path gate**: only `app/channels/gmail.py` may import `smtplib`; only
  `app/core/tools.py` may call `approval.require_resolved`; tested by an AST scan test
  and the layer contract, not by review.

## Alternatives considered

- **Webhook mode** — rejected: needs a public HTTPS endpoint (C1 violation) even though
  Telegram webhooks are prettier for latency; polling latency (~1 s) is fine for +3
  timeliness.
- **raw Bot API over httpx** — rejected: aiogram gives router/callback-keyboard/FSM
  machinery we'd otherwise hand-roll (simplicity +3.5).

## Consequences

Positive: zero inbound surface; works behind NAT; one process for everything.
Negative (accepted): aiogram's polling loop owns reconnect logic; polling and the IMAP
IDLE thread both bridge into one loop (thread-safe event posting only via
`loop.call_soon_threadsafe`); Telegram rate limits apply to bursts — the notification
budget (C10) already caps volume.
