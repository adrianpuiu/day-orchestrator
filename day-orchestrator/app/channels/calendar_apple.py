"""Apple Calendar adapter: reads the local Calendar store via EventKit (ADR-009).

Calendar.app already syncs the owner's iPhone accounts (iCloud/Google), so this
reads the same events with zero OAuth setup. macOS-only; imports EventKit
lazily so non-Mac hosts can still import the module.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# EKAuthorizationStatus: 0 not determined, 1 restricted, 2 denied, 3 authorized/full
_AUTHORIZED = 3
_DENIED_STATES = (1, 2)


class CalendarPermissionDenied(RuntimeError):
    """Calendar access denied or not granted — fix in System Settings."""


class AppleCalendarClient:
    """Reads macOS Calendar events; satisfies the calendar tool contract."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any] | None = None,
        auth_status_fn: Callable[..., int] | None = None,
    ) -> None:
        self._store_factory = store_factory  # test seam
        self._auth_status_fn = auth_status_fn
        self._store: Any = None

    def _get_store(self) -> Any:
        if self._store is not None:
            return self._store
        import EventKit as EK

        self._EK = EK
        store = (self._store_factory or (lambda: EK.EKEventStore.alloc().init()))()
        status = (self._auth_status_fn or _auth_status)(EK, store)
        if status in _DENIED_STATES:
            raise CalendarPermissionDenied(
                "Calendar access denied — grant it in System Settings → "
                "Privacy & Security → Calendars, then restart the agent"
            )
        if status != _AUTHORIZED:
            granted = _request_access(EK, store)
            if not granted:
                raise CalendarPermissionDenied(
                    "Calendar consent was not granted (timed out or declined)"
                )
        self._store = store
        return store

    def get_events(self, days: int) -> list[dict[str, Any]]:
        """Sync: events from now to now+days across all calendars, local ISO."""
        store = self._get_store()
        start = datetime.now().astimezone()
        end = start + timedelta(days=days)
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            _ns(start), _ns(end), None
        )
        events = store.eventsMatchingPredicate_(predicate) or []
        out: list[dict[str, Any]] = []
        for event in sorted(events, key=lambda ev: ev.startDate()):
            start_iso = _iso(event.startDate())
            end_raw = event.endDate()
            out.append(
                {
                    "start": start_iso,
                    "end": _iso(end_raw) if end_raw is not None else start_iso,
                    "summary": str(event.title() or "(untitled)"),
                    "location": str(event.location()) if event.location() else None,
                }
            )
        log.info("calendar: %d event(s) in the next %d day(s)", len(out), days)
        return out


def _auth_status(EK: Any, _store: Any) -> int:
    return EK.EKEventStore.authorizationStatusForEntityType_(EK.EKEntityTypeEvent)


def _request_access(EK: Any, store: Any) -> bool:
    """Blocks until the user answers the TCC prompt (up to 2 minutes)."""
    import threading

    done = threading.Event()
    state: dict[str, bool] = {}

    def handler(granted: bool, error: object) -> None:
        if error is not None:
            log.warning("calendar consent error: %s", error)
        state["granted"] = bool(granted)
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:  # pre-macOS-14 API
        store.requestAccessToEntityType_completion_(EK.EKEntityTypeEvent, handler)
    if not done.wait(timeout=120):
        log.error("calendar consent prompt timed out")
        return False
    return state.get("granted", False)


def _ns(dt: datetime) -> Any:
    from Foundation import NSDate

    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _iso(ns_date: Any) -> str:
    return datetime.fromtimestamp(ns_date.timeIntervalSince1970()).astimezone().isoformat(
        timespec="minutes"
    )
