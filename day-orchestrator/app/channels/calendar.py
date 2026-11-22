"""Google Calendar read-only client (scope calendar.readonly).

Import discipline: this is the ONLY project file allowed to import the
Google API/auth client libraries. Read-only: write support arrives with
architecture.md evolution step 4.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class CalendarNotConfigured(RuntimeError):
    """Raised when no saved token exists and no client secret is available."""


def _default_service_factory(creds: Credentials) -> Any:
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_iso(value: str) -> datetime:
    """Parse an RFC3339 timestamp, tolerating the trailing Z form."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _event_start(item: dict[str, Any], key: str) -> datetime:
    """Extract the start or end of an event; all-day events use date at 00:00."""
    raw = item.get(key) or {}
    if "dateTime" in raw:
        return _parse_iso(raw["dateTime"])
    # All-day event: bare date, interpreted at local midnight.
    return datetime.fromisoformat(raw["date"] + "T00:00:00")


class CalendarClient:
    """Provide read-only access to the user's primary Google Calendar."""

    def __init__(
        self,
        token_path: Path,
        client_secret_path: Path | None = None,
        *,
        service_factory: Callable[[Credentials], Any] | None = None,
        flow_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Store auth paths and injectable factories without touching disk."""
        self._token_path = Path(token_path)
        self._client_secret_path = (
            Path(client_secret_path) if client_secret_path is not None else None
        )
        self._service_factory = service_factory or _default_service_factory
        self._flow_factory = flow_factory or (
            lambda path: InstalledAppFlow.from_client_secrets_file(path, SCOPES)
        )
        self._service: Any | None = None

    def _authenticate(self) -> Any:
        """Load, refresh, or interactively obtain OAuth credentials once."""
        creds: Credentials | None = None
        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._token_path), SCOPES
            )
            if not creds.valid and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Calendar OAuth token")
                creds.refresh(Request())
                self._save_token(creds)
        elif (
            self._client_secret_path is not None
            and self._client_secret_path.exists()
        ):
            logger.info(
                "Running one-time interactive Calendar consent "
                "(only via `auth-calendar`)"
            )
            flow = self._flow_factory(str(self._client_secret_path))
            creds = flow.run_local_server(port=0)
            self._save_token(creds)
        else:
            raise CalendarNotConfigured(
                "No saved Calendar token and no client secret; run "
                "`auth-calendar` after providing the OAuth client secret."
            )
        self._service = self._service_factory(creds)
        return self._service

    def _save_token(self, creds: Credentials) -> None:
        """Persist the (possibly refreshed) token to token_path."""
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
        }
        self._token_path.write_text(json.dumps(payload), encoding="utf-8")

    def _get_service(self) -> Any:
        """Return the cached API service, authenticating lazily."""
        if self._service is None:
            self._authenticate()
        return self._service

    def access_status(self) -> int:
        """Passive: OAuth consent is established at setup, so always authorized."""
        return 3

    def get_event_meta(self, event_id: str) -> dict[str, Any] | None:
        return None  # read-only backend

    def get_events(self, days: int) -> list[dict[str, Any]]:
        """Return primary-calendar events from now through now + days."""
        now = datetime.now().astimezone()
        service = self._get_service()
        response = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=days)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )
        events: list[dict[str, Any]] = []
        for item in response.get("items", []):
            if item.get("status") == "cancelled":
                continue
            try:
                start = _event_start(item, "start")
                end = _event_start(item, "end")
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed event %s: %s", item.get("id"), exc)
                continue
            events.append(
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "summary": item.get("summary") or "(untitled)",
                    "location": item.get("location"),
                }
            )
        return events
