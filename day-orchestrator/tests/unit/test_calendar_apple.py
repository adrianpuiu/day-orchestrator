"""Apple Calendar adapter tests: mapping, window, permission paths — no EventKit."""

from __future__ import annotations

import pytest

from app.channels.calendar_apple import (
    AppleCalendarClient,
    CalendarPermissionDenied,
)


class FakeNSDate:
    def __init__(self, epoch: float) -> None:
        self._epoch = epoch

    def timeIntervalSince1970(self) -> float:
        return self._epoch

    def __lt__(self, other: FakeNSDate) -> bool:
        return self._epoch < other._epoch


class FakeEKEvent:
    def __init__(self, start: float, end: float, title: str, location=None) -> None:
        self._start = FakeNSDate(start)
        self._end = FakeNSDate(end)
        self._title = title
        self._location = location

    def startDate(self):
        return self._start

    def endDate(self):
        return self._end

    def title(self):
        return self._title

    def location(self):
        return self._location

    def setTitle_(self, title) -> None:
        self._title = title

    def setStartDate_(self, date) -> None:
        self._start = date

    def setEndDate_(self, date) -> None:
        self._end = date

    def setCalendar_(self, calendar) -> None:
        self._calendar = calendar

    def eventIdentifier(self) -> str:
        return self._identifier


class FakeStore:
    def __init__(self, events) -> None:
        self._events = events
        self.captured_window: tuple[float, float] | None = None

    def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, calendars):
        self.captured_window = (start.timeIntervalSince1970(), end.timeIntervalSince1970())
        return ("predicate",)

    def eventsMatchingPredicate_(self, predicate) -> list:
        return self._events


def make_client(store, auth_status: int = 3) -> AppleCalendarClient:
    return AppleCalendarClient(
        store_factory=lambda: store,
        auth_status_fn=lambda EK, _store: auth_status,
    )


def test_maps_events_to_contract_dicts() -> None:
    store = FakeStore(
        [
            FakeEKEvent(1795439400.0, 1795441200.0, "Standup"),
            FakeEKEvent(1795480800.0, 1795486200.0, "Dentist", location="Vasile Lupu 12"),
        ]
    )
    client = make_client(store)
    events = client.get_events(2)
    assert events[0]["summary"] == "Standup"
    assert events[0]["start"].endswith(("+02:00", "+03:00")) or ":" in events[0]["start"]
    assert events[1]["location"] == "Vasile Lupu 12"


def test_requests_two_day_window_ending_now(tmp_path) -> None:
    store = FakeStore([])
    client = make_client(store)
    events = client.get_events(2)
    assert events == []
    start, end = store.captured_window
    assert 0 < end - start <= 2 * 86400 + 60  # ~2 days


def test_denied_calendar_raises_permission_error(tmp_path) -> None:
    client = make_client(FakeStore([]), auth_status=2)
    with pytest.raises(CalendarPermissionDenied, match="System Settings"):
        client.get_events(1)


def test_undetermined_prompts_and_grants(tmp_path) -> None:
    calls: list[str] = []

    class PromptingStore(FakeStore):
        def requestFullAccessToEventsWithCompletion_(self, handler) -> None:
            calls.append("prompt")
            handler(True, None)

    client = make_client(PromptingStore([]), auth_status=0)
    assert client.get_events(2) == []
    assert calls == ["prompt"]


def test_denied_prompt_raises(tmp_path) -> None:
    class DecliningStore(FakeStore):
        def requestFullAccessToEventsWithCompletion_(self, handler) -> None:
            handler(False, None)

    client = make_client(DecliningStore([]), auth_status=0)
    with pytest.raises(CalendarPermissionDenied, match="consent"):
        client.get_events(1)


class WritingStore:
    """Captures the create_event flow."""

    def __init__(self) -> None:
        self.saved: dict | None = None

    def defaultCalendarForNewEvents(self):
        return object()  # opaque calendar handle

    def eventWithEventStore_(self, store):
        return FakeEKEvent(0, 0, "")

    def saveEvent_span_commit_error_(self, event, span, commit, error):
        self.saved = {
            "title": event.title(),
            "start": event.startDate().timeIntervalSince1970(),
            "end": event.endDate().timeIntervalSince1970(),
        }
        event._identifier = "ek-123"
        return True, None


def test_create_event_maps_and_saves() -> None:
    from datetime import datetime

    store = WritingStore()
    client = AppleCalendarClient(
        store_factory=lambda: store,
        auth_status_fn=lambda EK, _store: 3,
        event_factory=lambda s: store.eventWithEventStore_(s),
    )
    event_id = client.create_event(
        "Plată factură curent", "2026-11-23T10:30", "2026-11-23T11:00"
    )

    assert event_id == "ek-123"
    assert store.saved is not None
    assert store.saved["title"] == "Plată factură curent"
    expected = datetime.fromisoformat("2026-11-23T10:30").astimezone().timestamp()
    assert abs(store.saved["start"] - expected) < 1
    expected_end = datetime.fromisoformat("2026-11-23T11:00").astimezone().timestamp()
    assert abs(store.saved["end"] - expected_end) < 1


def test_create_event_without_writable_calendar() -> None:
    class NoCalStore(WritingStore):
        def defaultCalendarForNewEvents(self):
            return None

    no_cal = NoCalStore()
    client = AppleCalendarClient(
        store_factory=lambda: no_cal,
        auth_status_fn=lambda EK, _store: 3,
        event_factory=lambda s: no_cal.eventWithEventStore_(s),
    )
    with pytest.raises(CalendarPermissionDenied, match="writable"):
        client.create_event("x", "2026-11-23T10:30", "2026-11-23T11:00")
