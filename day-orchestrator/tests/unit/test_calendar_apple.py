"""Apple Calendar client tests: helper protocol — no EventKit, no subprocess."""

from __future__ import annotations

import pytest

from app.channels.calendar_apple import AppleCalendarClient, CalendarPermissionDenied


def ok_get(days: int) -> dict:
    return {
        "events": [
            {
                "start": "2026-11-23T10:30",
                "end": "2026-11-23T11:00",
                "summary": "Plată factură curent",
                "location": None,
            }
        ]
    }


def test_get_events_passes_through_contract() -> None:
    calls: list[list[str]] = []

    client = AppleCalendarClient(
        runner=lambda args: (calls.append(args), ok_get(2))[1]
    )
    events = client.get_events(2)

    assert calls == [["get-events", "--days", "2"]]
    assert events[0]["summary"] == "Plată factură curent"


def test_create_event_returns_helper_id() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> dict:
        calls.append(args)
        return {"event_id": "ek-123"}

    client = AppleCalendarClient(runner=runner)
    event_id = client.create_event(
        "Cafea cu Diana", "2026-11-23T17:30", "2026-11-23T18:30",
        notes="prima cafea", invitees=["diana@example.com"],
    )

    assert event_id == "ek-123"
    assert calls == [[
        "create-event", "--title", "Cafea cu Diana",
        "--start", "2026-11-23T17:30", "--end", "2026-11-23T18:30",
        "--notes", "prima cafea",
        "--invitees", "diana@example.com",
    ]]


def test_create_event_omits_empty_extras() -> None:
    captured: list[list[str]] = []

    client = AppleCalendarClient(
        runner=lambda args: (captured.append(args), {"event_id": "ek-1"})[1]
    )
    client.create_event("x", "2026-11-23T10:30", "2026-11-23T11:00")

    assert captured == [[
        "create-event", "--title", "x",
        "--start", "2026-11-23T10:30", "--end", "2026-11-23T11:00",
    ]]


def test_permission_exit_maps_to_denied() -> None:
    def runner(args: list[str]) -> dict:
        raise CalendarPermissionDenied(
            "calendar consent not granted (status=0). If no dialog appeared, "
            "macOS suppressed it"
        )

    client = AppleCalendarClient(runner=runner)
    with pytest.raises(CalendarPermissionDenied, match="suppressed"):
        client.get_events(1)


def test_access_status_passive() -> None:
    client = AppleCalendarClient(runner=lambda args: {"status": 3})
    assert client.access_status() == 3


def test_helper_binary_exists_and_is_executable() -> None:
    from app.channels.calendar_apple import HELPER

    assert HELPER.exists(), f"bundle helper missing: {HELPER}"
    assert HELPER.stat().st_mode & 0o111, "helper not executable"
