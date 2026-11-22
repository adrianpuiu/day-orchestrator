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
import time
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
    p_create.add_argument("--location", default="")
    p_create.add_argument(
        "--lat", type=float, default=None,
        help="with --lon: attach structured location (travel-time aware)",
    )
    p_create.add_argument("--lon", type=float, default=None)
    sub.add_parser("status")
    p_geo = sub.add_parser("geocode")
    p_geo.add_argument("--query", required=True)
    p_geo.add_argument("--lat", type=float, default=None)
    p_geo.add_argument("--lon", type=float, default=None)
    p_upd = sub.add_parser("update-event")
    p_upd.add_argument("--id", required=True)
    p_upd.add_argument("--title")
    p_upd.add_argument("--start")
    p_upd.add_argument("--end")
    p_del = sub.add_parser("delete-event")
    p_del.add_argument("--id", required=True)
    p_meta = sub.add_parser("get-event")
    p_meta.add_argument("--id", required=True)
    p_create.add_argument(
        "--recurrence", default="",
        help="'daily' or 'weekly:MO,TU,WE,TH,FR,SA,SU' (repeat forever)",
    )
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

    if args.cmd == "geocode":
        import CoreLocation
        from AppKit import NSApplication
        from Foundation import NSDefaultRunLoopMode, NSRunLoop

        NSApplication.sharedApplication().finishLaunching()
        geocoder = CoreLocation.CLGeocoder.alloc().init()
        done: list = []

        def _done(placemarks, err) -> None:
            done.append((placemarks, err))

        geocoder.geocodeAddressString_completionHandler_(args.query, _done)
        runloop = NSRunLoop.currentRunLoop()
        deadline = time.time() + 15
        while time.time() < deadline and not done:
            runloop.runMode_beforeDate_(
                NSDefaultRunLoopMode,
                __import__("Foundation", fromlist=["NSDate"]).NSDate.dateWithTimeIntervalSinceNow_(0.1),
            )
        if not done:
            _die("geocoder did not answer in 15s", 1)
        placemarks, err = done[0]
        if err is not None:
            _die(f"geocoder error: {err}", 1)
        items = []
        for pm in (placemarks or [])[:5]:
            coord = pm.location().coordinate()
            parts = [
                str(x) for x in (
                    pm.name(), pm.thoroughfare(), pm.subThoroughfare(),
                    pm.locality(), pm.administrativeArea(), pm.country(),
                ) if x
            ]
            items.append({
                "name": str(pm.name() or ""),
                "address": ", ".join(parts),
                "lat": coord.latitude,
                "lon": coord.longitude,
            })
        print(json.dumps({"results": items}))
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
                    "id": str(ev.eventIdentifier()),
                    "start": start.isoformat(timespec="minutes"),
                    "end": end_dt.isoformat(timespec="minutes"),
                    "summary": str(ev.title() or "(untitled)"),
                    "location": str(ev.location()) if ev.location() else None,
                }
            )
        print(json.dumps({"events": out}))
        return

    if args.cmd == "get-event":
        ev = store.eventWithIdentifier_(args.id)
        if ev is None:
            _die(f"event {args.id} not found", 1)
        start = datetime.fromtimestamp(
            ev.startDate().timeIntervalSince1970()).astimezone()
        print(json.dumps({
            "id": args.id,
            "title": str(ev.title() or "(untitled)"),
            "start": start.isoformat(timespec="minutes"),
            "calendar": str(ev.calendar().title() or ""),
        }))
        return

    if args.cmd == "update-event":
        ev = store.eventWithIdentifier_(args.id)
        if ev is None:
            _die(f"event {args.id} not found", 1)
        if args.title:
            ev.setTitle_(args.title)
        if args.start:
            ev.setStartDate_(
                EK_NSDATE_from(datetime.fromisoformat(args.start).astimezone())
            )
        if args.end:
            ev.setEndDate_(
                EK_NSDATE_from(datetime.fromisoformat(args.end).astimezone())
            )
        ok, error = store.saveEvent_span_commit_error_(
            ev, EK.EKSpanThisEvent, True, None
        )
        if not ok:
            _die(f"EventKit update failed: {error}", 1)
        print(json.dumps({"event_id": args.id, "title": str(ev.title() or "")}))
        return

    if args.cmd == "delete-event":
        ev = store.eventWithIdentifier_(args.id)
        if ev is None:
            _die(f"event {args.id} not found", 1)
        # FutureEvents: for recurring events this removes this and all future
        # occurrences (the whole series from now on); for single events it is
        # exactly that event. Edits keep EKSpanThisEvent (move one occurrence).
        ok, error = store.removeEvent_span_commit_error_(
            ev, EK.EKSpanFutureEvents, True, None
        )
        if not ok:
            _die(f"EventKit delete failed: {error}", 1)
        print(json.dumps({"deleted": args.id, "title": str(ev.title() or "")}))
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
    if args.location:
        event.setLocation_(args.location)
    if args.lat is not None and args.lon is not None:
        import CoreLocation

        structured = EK.EKStructuredLocation.locationWithTitle_(
            args.location or args.title
        )
        structured.setGeoLocation_(
            CoreLocation.CLLocation.alloc().initWithLatitude_longitude_(
                args.lat, args.lon
            )
        )
        event.setStructuredLocation_(structured)
    if args.alarm_minutes > 0:
        event.addAlarm_(
            EK.EKAlarm.alarmWithRelativeOffset_(-args.alarm_minutes * 60.0)
        )
    if args.recurrence:
        rule = _recurrence_rule(args.recurrence)
        if rule is None:
            _die(f"unsupported recurrence {args.recurrence!r}", 1)
        event.setRecurrenceRule_(rule)
    ok, error = store.saveEvent_span_commit_error_(
        event, EK.EKSpanThisEvent, True, None
    )
    if not ok:
        _die(f"EventKit save failed: {error}", 1)
    print(json.dumps({"event_id": str(event.eventIdentifier())}))


def _recurrence_rule(spec: str):
    """'daily' or 'weekly:MO,TU,WE,TH,FR,SA,SU' -> EKRecurrenceRule or None."""
    import EventKit as EK

    spec = spec.strip().lower()
    weekday_codes = {"su": "SU", "mo": "MO", "tu": "TU", "we": "WE",
                     "th": "TH", "fr": "FR", "sa": "SA"}
    if spec == "daily":
        rrule = "FREQ=DAILY"
    elif spec.startswith("weekly:"):
        codes = [c.strip() for c in spec.split(":", 1)[1].split(",") if c.strip()]
        days = [weekday_codes.get(c) for c in codes]
        if not days or None in days:
            return None
        rrule = "FREQ=WEEKLY;BYDAY=" + ",".join(days)
    else:
        return None
    return EK.EKRecurrenceRule.alloc().initWithRRULEString_(rrule)


def EK_NSDATE_from(dt: datetime):
    from Foundation import NSDate

    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


if __name__ == "__main__":
    main()
