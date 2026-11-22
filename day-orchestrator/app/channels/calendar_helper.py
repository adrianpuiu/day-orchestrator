"""Calendar helper CLI — runs inside the DayOrchestrator.app bundle.

EventKit TCC consent needs a stable bundle identity; bare binaries spawned by
launchd get their prompts silently suppressed (observed 2026-11-22). The app
bundle gives macOS something to attribute the grant to, so the consent dialog
appears once and the toggle lands in System Settings.

Protocol: JSON on stdout, exit 0 ok / 2 permission / 1 other.
  get-events  --days N
  create-event --title T --start ISO --end ISO
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta


def _die(message: str, code: int) -> None:
    print(json.dumps({"error": message}), file=sys.stderr)
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(prog="calendar-helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_get = sub.add_parser("get-events")
    p_get.add_argument("--days", type=int, required=True)
    p_create = sub.add_parser("create-event")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--start", required=True)
    p_create.add_argument("--end", required=True)
    p_create.add_argument("--notes", default="")
    p_create.add_argument(
        "--invitees", default="",
        help="comma-separated emails; recorded in the event notes (EventKit "
             "has no public API for real attendees)",
    )
    p_create.add_argument(
        "--calendar", default="",
        help="target calendar title; empty = system default for new events",
    )
    p_create.add_argument(
        "--alarm-minutes", type=int, default=0,
        help="fire an alert N minutes before the event (0 = no alarm)",
    )
    sub.add_parser("status")
    args = parser.parse_args()

    import EventKit as EK

    if args.cmd == "status":
        print(
            json.dumps(
                {
                    "status": int(
                        EK.EKEventStore.authorizationStatusForEntityType_(
                            EK.EKEntityTypeEvent
                        )
                    )
                }
            )
        )
        return

    store = EK.EKEventStore.alloc().init()
    status = EK.EKEventStore.authorizationStatusForEntityType_(EK.EKEntityTypeEvent)
    if status == 2:  # denied
        _die("calendar access denied in System Settings", 2)
    if status != 3:  # not determined
        import threading

        done = threading.Event()
        state: dict[str, bool] = {}

        def handler(granted: bool, error: object) -> None:
            state["granted"] = bool(granted)
            done.set()

        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(handler)
        else:
            store.requestAccessToEntityType_completion_(EK.EKEntityTypeEvent, handler)
        if not done.wait(timeout=120) or not state.get("granted"):
            _die("calendar consent not granted (timed out or declined)", 2)

    if args.cmd == "get-events":
        from Foundation import NSDate

        now = datetime.now().astimezone()
        end = now + timedelta(days=args.days)
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            NSDate.dateWithTimeIntervalSince1970_(now.timestamp()),
            NSDate.dateWithTimeIntervalSince1970_(end.timestamp()),
            None,
        )
        events = sorted(
            store.eventsMatchingPredicate_(predicate) or [],
            key=lambda ev: ev.startDate().timeIntervalSince1970(),
        )
        out = []
        for ev in events:
            start = datetime.fromtimestamp(
                ev.startDate().timeIntervalSince1970()
            ).astimezone()
            end_ns = ev.endDate()
            end_dt = (
                datetime.fromtimestamp(end_ns.timeIntervalSince1970()).astimezone()
                if end_ns is not None
                else start
            )
            out.append(
                {
                    "start": start.isoformat(timespec="minutes"),
                    "end": end_dt.isoformat(timespec="minutes"),
                    "summary": str(ev.title() or "(untitled)"),
                    "location": str(ev.location()) if ev.location() else None,
                }
            )
        print(json.dumps({"events": out}))
        return

    # create-event
    calendar = None
    if args.calendar:
        for c in store.calendars() or []:
            if c.title() == args.calendar and c.allowsContentModifications():
                calendar = c
                break
        if calendar is None:
            available = ", ".join(
                sorted({c.title() for c in (store.calendars() or [])})
            )
            _die(f"calendar {args.calendar!r} not found or read-only; "
                 f"available: {available}", 1)
    if calendar is None:
        calendar = store.defaultCalendarForNewEvents()
    if calendar is None:
        _die("no writable calendar found", 2)
    event = EK.EKEvent.eventWithEventStore_(store)
    event.setTitle_(args.title)
    notes = args.notes
    invitees = [e.strip() for e in args.invitees.split(",") if e.strip()]
    if invitees:
        bad = [e for e in invitees if "@" not in e]
        if bad:
            _die(f"invalid invitee emails: {', '.join(bad)}", 1)
        notes = (notes + "\n\n" if notes else "") + "Attendees: " + ", ".join(invitees)
    if notes:
        event.setNotes_(notes)
    event.setStartDate_(
        EK_NSDATE_from(datetime.fromisoformat(args.start).astimezone())
    )
    event.setEndDate_(EK_NSDATE_from(datetime.fromisoformat(args.end).astimezone()))
    event.setCalendar_(calendar)
    if args.alarm_minutes > 0:
        event.addAlarm_(
            EK.EKAlarm.alarmWithRelativeOffset_(-args.alarm_minutes * 60.0)
        )
    ok, error = store.saveEvent_span_commit_error_(
        event, EK.EKSpanThisEvent, True, None
    )
    if not ok:
        _die(f"EventKit save failed: {error}", 1)
    print(json.dumps({"event_id": str(event.eventIdentifier())}))


def EK_NSDATE_from(dt: datetime):
    from Foundation import NSDate

    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


if __name__ == "__main__":
    main()
