# ADR-002: Email transport — IMAP/SMTP, app-password auth first

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

Gmail access is owner-mandated as IMAP/SMTP (not the Gmail API). IMAP exposes less than
the API: labels appear as IMAP folders under `[Gmail]/`, push is IDLE-only (no
server-side push), and quota/limits differ. Auth options: app password (requires
2-Step Verification) or OAuth 2.0 XOAUTH2 (requires a Google Cloud OAuth client even for
personal use).

## Decision

Use IMAP (993/TLS, IMAPClient, IDLE + 3-min poll fallback) for reads and draft creation
(APPEND to `[Gmail]/Drafts` with `\Draft`), SMTP (587 STARTTLS) for approved sends.
Authenticate with a **Google app password** for v1. The adapter sits behind one narrow
client interface (`GmailClient` protocol) so an XOAUTH2 or Gmail-API implementation can
replace it without touching tools or workflows (architecture.md evolution step 3).

## Alternatives considered

- **Gmail API** — rejected for v1: owner-mandated transport; API needs an OAuth client +
  consent screen and still wouldn't change the capability set meaningfully at +3
  extensibility.
- **XOAUTH2 now** — rejected for v1: setup cost (OAuth client, consent flow, token
  refresh) buys robustness we don't yet need on a single-user box; the protocol is
  supported by IMAPClient/SMTP via `AUTH XOAUTH2`.

## Consequences

Positive: five-minute credential setup (2FA → app password), no OAuth client to
maintain, drafts visible in the normal Gmail UI (sendable from the phone).
Negative (accepted): Google may tighten app-password support — migration path is the
client interface, not a rewrite; IDLE can drop silently — poll fallback compensates;
no API-level label mutations — labels are approximated by moving messages between
IMAP folders only if a future need arises.
