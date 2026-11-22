"""Apple Calendar client — shells out to the bundled EventKit helper.

Direct EventKit calls from launchd-spawned python get their TCC prompts
silently suppressed (observed 2026-11-22: granted=False returned instantly,
even after `tccutil reset Calendar`). The DayOrchestrator.app wrapper gives
macOS a stable bundle identity to attribute the consent to, so the dialog
appears once and the toggle lands in System Settings (ADR-009 addendum).
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HELPER = PROJECT_ROOT / "DayOrchestrator.app" / "Contents" / "MacOS" / "DayOrchestrator"


class CalendarPermissionDenied(RuntimeError):
    pass


class AppleCalendarClient:
    """Reads and writes macOS Calendar events via the bundled helper."""

    def __init__(
        self,
        *,
        runner: Callable[[list[str]], dict[str, Any]] | None = None,
        target_calendar: str = "",
    ) -> None:
        self._runner = runner  # test seam
        self._target = target_calendar  # empty = system default

    def _run(self, args: list[str]) -> dict[str, Any]:
        if self._runner is not None:
            return self._runner(args)
        proc = subprocess.run(
            [str(HELPER), *args], capture_output=True, text=True, timeout=150
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        message = proc.stderr.strip() or f"calendar helper exited {proc.returncode}"
        if proc.returncode == 2:
            raise CalendarPermissionDenied(message)
        raise RuntimeError(message)

    def get_events(self, days: int) -> list[dict[str, Any]]:
        """Sync: events from now to now+days across all calendars, local ISO."""
        data = self._run(["get-events", "--days", str(days)])
        events = data.get("events", [])
        log.info("calendar: %d event(s) in the next %d day(s)", len(events), days)
        return events

    def create_event(
        self,
        title: str,
        start_iso: str,
        end_iso: str,
        notes: str = "",
        invitees: list[str] | None = None,
        alarm_minutes: int = 0,
        recurrence: str = "",
        location: str = "",
        lat: float | None = None,
        lon: float | None = None,
    ) -> str:
        """Write: create an event in the default calendar; returns its id.

        Invitees land in the notes (EventKit has no public attendee API);
        real invitations go through the gated send_email tool.
        alarm_minutes > 0 attaches an alert N minutes before the start.
        recurrence: 'daily' or 'weekly:MO,TU,...' (see helper).
        """
        args = ["create-event", "--title", title, "--start", start_iso, "--end", end_iso]
        if self._target:
            args += ["--calendar", self._target]
        if alarm_minutes > 0:
            args += ["--alarm-minutes", str(alarm_minutes)]
        if recurrence:
            args += ["--recurrence", recurrence]
        if notes:
            args += ["--notes", notes]
        if invitees:
            args += ["--invitees", ",".join(invitees)]
        if location:
            args += ["--location", location]
        if lat is not None and lon is not None:
            args += ["--lat", str(lat), "--lon", str(lon)]
        data = self._run(args)
        return str(data["event_id"])

    def geocode(
        self, query: str, lat: float | None = None, lon: float | None = None
    ) -> list[dict[str, Any]]:
        """Passive: Apple Maps geocoding; top placemarks with coordinates."""
        args = ["geocode", "--query", query]
        if lat is not None and lon is not None:
            args += ["--lat", str(lat), "--lon", str(lon)]
        data = self._run(args)
        return data.get("results", [])

    def get_event_meta(self, event_id: str) -> dict[str, Any] | None:
        """Passive: title/start/calendar of one event; None if missing."""
        try:
            return self._run(["get-event", "--id", event_id])
        except RuntimeError:
            return None

    def update_event(
        self,
        event_id: str,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Write: change title and/or start/end of one event."""
        args = ["update-event", "--id", event_id]
        if title:
            args += ["--title", title]
        if start:
            args += ["--start", start]
        if end:
            args += ["--end", end]
        return self._run(args)

    def delete_event(self, event_id: str) -> dict[str, Any]:
        """Write: delete one event."""
        return self._run(["delete-event", "--id", event_id])

    def access_status(self) -> int:
        """0 not determined, 2 denied, 3 authorized. Passive; prompts nothing."""
        return int(self._run(["status"])["status"])
