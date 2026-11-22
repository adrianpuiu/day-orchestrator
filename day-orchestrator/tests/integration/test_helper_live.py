"""Real-mac EventKit probes through the bundled helper (RUN_MAC=1).

These exercise what the offline suite cannot: the actual EventKit save path,
structured locations, alarms and TCC-gated reads. They self-clean (every probe
event is deleted by identifier at the end).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RUN_MAC"),
        reason="real-mac EventKit probe: set RUN_MAC=1",
    ),
    pytest.mark.skipif(
        __import__("sys").platform != "darwin",
        reason="macOS only",
    ),
]

HELPER = Path(__file__).resolve().parents[2] / (
    "DayOrchestrator.app/Contents/MacOS/DayOrchestrator"
)


def _run(*args: str) -> dict:
    proc = subprocess.run([str(HELPER), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"helper failed: {proc.stderr.strip()}"
    return json.loads(proc.stdout)


def _store():
    import EventKit

    store = EventKit.EKEventStore.alloc().init()
    done = __import__("threading").Event()
    state: dict[str, bool] = {}

    def handler(granted, error) -> None:
        state["granted"] = bool(granted)
        done.set()

    if EventKit.EKEventStore.authorizationStatusForEntityType_(
        EventKit.EKEntityTypeEvent
    ) != 3:
        store.requestFullAccessToEventsWithCompletion_(handler)
        done.wait(120)
    return store


def _slot(offset_hours: int) -> tuple[str, str]:
    now = datetime.now().astimezone()
    start = (now + timedelta(hours=offset_hours)).isoformat(timespec="minutes")
    end = (now + timedelta(hours=offset_hours + 1)).isoformat(timespec="minutes")
    return start, end


def test_helper_status_reports_authorized() -> None:
    assert _run("status")["status"] == 3, (
        "bundle calendar access not granted - run auth-calendar first"
    )


def test_get_events_roundtrip() -> None:
    result = _run("get-events", "--days", "2")
    assert isinstance(result["events"], list)


def test_create_with_alarm_structured_location_selfclean() -> None:
    import EventKit

    start, end = _slot(4)
    out = _run(
        "create-event", "--title", "zz-runmac-probe", "--calendar", "Home",
        "--location", "Strada Exemplu 10, București",
        "--lat", "44.426765", "--lon", "26.102464",
        "--alarm-minutes", "30", "--start", start, "--end", end,
    )
    event_id = out["event_id"]
    store = _store()
    ev = store.eventWithIdentifier_(event_id)
    assert ev is not None, "created event missing from the store"
    try:
        assert ev.title() == "zz-runmac-probe"
        assert ev.location() == "Strada Exemplu 10, București"
        structured = ev.structuredLocation()
        assert structured is not None and structured.title() == (
            "Strada Exemplu 10, București"
        )
        geo = structured.geoLocation()
        lat, lon = geo.coordinate()
        assert abs(lat - 44.426765) < 1e-5 and abs(lon - 26.102464) < 1e-5
        alarms = ev.alarms() or []
        assert any(a.relativeOffset() == -1800.0 for a in alarms), (
            "30-minute alarm missing"
        )
    finally:
        store.removeEvent_span_commit_error_(
            ev, EventKit.EKSpanThisEvent, True, None
        )
    assert store.eventWithIdentifier_(event_id) is None, "probe must self-clean"


def test_update_and_delete_series_lifecycle() -> None:

    from Foundation import NSDate

    start, end = _slot(5)
    out = _run(
        "create-event", "--title", "zz-runmac-series", "--calendar", "Home",
        "--recurrence", "weekly:MO,TH", "--start", start, "--end", end,
    )
    event_id = out["event_id"]
    store = _store()
    try:
        upd = _run("update-event", "--id", event_id, "--title", "zz-runmac-renamed")
        assert upd["title"] == "zz-runmac-renamed"

        # ThisEvent edit on a recurring master = one-occurrence override:
        # exactly one occurrence carries the new title, the rest keep the old.
        now = datetime.now().astimezone()
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(
            NSDate.dateWithTimeIntervalSince1970_(now.timestamp()),
            NSDate.dateWithTimeIntervalSince1970_(
                (now + timedelta(days=14)).timestamp()
            ),
            None,
        )
        titles = [e.title() for e in (store.eventsMatchingPredicate_(pred) or [])
                  if e.title() and "zz-runmac" in e.title()]
        assert titles.count("zz-runmac-renamed") == 1, (
            f"exactly one occurrence must be renamed: {titles}"
        )
        assert "zz-runmac-series" in titles, "remaining occurrences must stay"
    finally:
        _run("delete-event", "--id", event_id)

    # FutureEvents delete removes the master, all occurrences and overrides.
    now = datetime.now().astimezone()
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        NSDate.dateWithTimeIntervalSince1970_(now.timestamp()),
        NSDate.dateWithTimeIntervalSince1970_(
            (now + timedelta(days=14)).timestamp()
        ),
        None,
    )
    leftovers = [e.title() for e in (store.eventsMatchingPredicate_(pred) or [])
                 if e.title() and "zz-runmac" in e.title()]
    assert not leftovers, f"series must be fully gone: {leftovers}"
