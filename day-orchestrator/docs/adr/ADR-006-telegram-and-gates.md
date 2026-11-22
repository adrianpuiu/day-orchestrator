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

## Amendment (2026-11-22, as-built)

- **HTML parse mode, chunked sends**: MarkdownV2 with unescaped brief text
  400s; sends are HTML, `split_message` chunks at line boundaries under the
  4096-char cap (keyboard rides the last chunk) so a long brief can never be
  rejected whole.
- **Owner-chat ownership**: the channel holds `owner_chat_id`, injected by the
  runtime (pairing seeded from `memory.paired_chat`, or set live at `/start`);
  every bot call targets it. aiogram requires `chat_id` on every call — the
  original implementation omitted it and fakes hid the bug until first live send.
- **Typing indicator**: `start_typing()` repeats the chat action every 4 s for
  the duration of an agent turn; cancelled when the reply (or failure note) lands.
- **Visible failures**: a failing coordinator turn answers `⚠️ …` instead of
  silence; LiteLLM retries (attempts=3, ADR-007) absorb model-server blips.
- Enforcement gates live in `tests/unit/test_send_path.py` (AST scans: transport
  imports, `approval.require_resolved` only in `app/tools.py`) plus the
  import-linter contracts; layer order as-built is
  `core ← channels ← tools ← workflows ← app`.

## Amendment (2026-11-23, as-built)

Pairing requires a one-time code: `/start <code>` where the code is the bot
token's tail by default, rotated to a random value by `app.cli reset-pairing`
(read live by the daemon on each `/start`). First-/start alone no longer
pairs a chat; callbacks and approval events are dropped unless they come from
the paired chat (fail-closed when no pairing exists). `send_message` retries
once on rate-limit (honoring `retry_after`) and network errors; notification
budget slots are refunded when a push fails.
