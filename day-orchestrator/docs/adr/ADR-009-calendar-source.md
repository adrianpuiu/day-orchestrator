# ADR-009: Calendar source — Apple EventKit local sync (no OAuth)

Date: 2026-11-22 · Status: accepted · Owner: agp

## Context

The Google Calendar path (ADR-002/003 era) required an OAuth client, consent
flow, and token management — the one setup step the owner never completed.
The owner pointed out their **Calendar.app already syncs with their iPhone**;
EventKit therefore exposes the same events locally with zero external setup.

## Decision

`app/channels/calendar_apple.py::AppleCalendarClient` reads the macOS Calendar
store via EventKit (pyobjc): all calendars, now → now+days, mapped to the same
contract dicts as the Google adapter. Selected by `config.toml`
(`calendar_backend = "apple"`, the new default); the Google adapter stays
available behind `calendar_backend = "google"` (ADR-003's evolution path keeps
it as the swap target if server-side features are ever needed).

Rules:
- Access is requested via `requestFullAccessToEvents` (TCC prompt; the CLI
  `auth-calendar` triggers it in the foreground with a 2-minute window).
  Denied/declined → `CalendarPermissionDenied` with fix instructions.
- Events map to `{start, end, summary, location}` in **local time**; the brief
  renders them through the same deterministic formatter as before.
- Import discipline unchanged: only this file imports `EventKit`
  (`test_send_path.py` + import-linter independence contract).

## Consequences

Positive: zero OAuth setup (the blocker is gone); on-device reads (privacy C9
win — calendar data no longer transits Google's API on behalf of the agent);
same events as the iPhone by construction (both sync from the same accounts).

Negative (accepted): **TCC grant is bound to the python binary** — the
launchd-spawned interpreter is the same path, so the foreground grant is
expected to carry over; if briefs ever report "calendar unavailable" without
a clear cause, re-run `auth-calendar` and check System Settings → Privacy &
Security → Calendars. pyobjc selector names differ from ObjC docs
(`startDate_endDate_`, not `endingDate_`) — caught live, fixed. Read-only;
calendar writes remain evolution step 4 (would reuse the approval gate).
