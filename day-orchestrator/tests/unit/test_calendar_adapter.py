"""Unit tests for the read-only Google Calendar adapter (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

import app.channels.calendar as cal
from app.channels.calendar import CalendarClient, CalendarNotConfigured


class FakeCreds:
    """Minimal stand-in for google.oauth2.credentials.Credentials."""

    def __init__(self, *, valid=True, expired=False, refresh_token="r"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.token = "tok"
        self.token_uri = "https://oauth2/token"
        self.client_id = "cid"
        self.client_secret = "sec"
        self.scopes = cal.SCOPES
        self.expiry = datetime.now() + timedelta(hours=1)
        self.refreshed = 0

    def refresh(self, request):
        self.refreshed += 1
        self.valid = True
        self.expired = False


class FakeFlow:
    instances: ClassVar[list[FakeFlow]] = []

    def __init__(self, path):
        self.path = path
        self.creds = FakeCreds()
        type(self).instances.append(self)

    def run_local_server(self, port=0):
        return self.creds


class FakeRequest:
    pass


def _fake_service(items, calls):
    class Events:
        def list(self, **kwargs):
            calls.append(kwargs)

            class Exec:
                def execute(self):
                    return {"items": items}

            return Exec()

        def __call__(self):
            return self

    class Service:
        @property
        def events(self):
            return Events()

    return Service()


def _patch_auth(monkeypatch, creds):
    holder = type(
        "C", (), {"from_authorized_user_file": staticmethod(lambda *a, **k: creds)}
    )
    monkeypatch.setattr(cal, "Credentials", holder)
    monkeypatch.setattr(cal, "Request", FakeRequest)


def test_window_and_mapping(tmp_path: Path, monkeypatch):
    """get_events queries now→now+days and maps items to contract dicts."""
    calls: list[dict] = []
    items = [
        {
            "id": "1",
            "status": "cancelled",
            "start": {"dateTime": "2026-11-23T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-23T11:00:00+01:00"},
            "summary": "Cancelled meeting",
        },
        {
            "id": "2",
            "start": {"dateTime": "2026-11-23T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-23T11:00:00+01:00"},
            "summary": "Standup",
            "location": "Zoom",
        },
        {
            "id": "3",
            "start": {"date": "2026-11-24"},
            "end": {"date": "2026-11-25"},
            "summary": "All day trip",
            "location": None,
        },
        {
            "id": "4",
            "start": {"dateTime": "bogus"},
            "end": {"date": "2026-11-26"},
            "summary": "Broken",
        },
    ]
    service = _fake_service(items, calls)
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    _patch_auth(monkeypatch, FakeCreds())
    client = CalendarClient(token_path, service_factory=lambda creds: service)
    events = client.get_events(3)

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["singleEvents"] is True
    assert kwargs["orderBy"] == "startTime"
    tmin = datetime.fromisoformat(kwargs["timeMin"])
    tmax = datetime.fromisoformat(kwargs["timeMax"])
    assert timedelta(days=3) - timedelta(minutes=1) <= tmax - tmin <= timedelta(days=3)
    assert tmin <= datetime.now().astimezone() + timedelta(seconds=1)

    # Cancelled skipped, malformed skipped, contract shape preserved.
    assert [e["summary"] for e in events] == ["Standup", "All day trip"]
    assert events[0] == {
        "start": "2026-11-23T10:00:00+01:00",
        "end": "2026-11-23T11:00:00+01:00",
        "summary": "Standup",
        "location": "Zoom",
    }
    # All-day event maps to its date at local 00:00.
    assert events[1]["start"].endswith("T00:00:00")
    assert events[1]["location"] is None


def test_token_refresh_path(tmp_path: Path, monkeypatch):
    """Expired saved token is refreshed and rewritten to disk."""
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    creds = FakeCreds(valid=False, expired=True)
    _patch_auth(monkeypatch, creds)
    calls: list[dict] = []
    service = _fake_service([], calls)
    client = CalendarClient(token_path, service_factory=lambda _creds: service)
    client.get_events(1)

    assert creds.refreshed == 1
    saved = json.loads(token_path.read_text())
    assert saved["token"] == "tok"
    assert saved["scopes"] == cal.SCOPES


def test_interactive_consent_via_flow_factory(tmp_path: Path):
    """No token but client secret present: flow runs once, token is saved."""
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}")
    token_path = tmp_path / "token.json"
    calls: list[dict] = []
    service = _fake_service([], calls)
    FakeFlow.instances = []
    client = CalendarClient(
        token_path,
        secret,
        service_factory=lambda _creds: service,
        flow_factory=FakeFlow,
    )
    client.get_events(1)

    assert len(FakeFlow.instances) == 1
    assert token_path.exists()
    assert json.loads(token_path.read_text())["token"] == "tok"


def test_not_configured(tmp_path: Path):
    """No token and no client secret raises CalendarNotConfigured."""
    client = CalendarClient(tmp_path / "missing-token.json", None)
    with pytest.raises(CalendarNotConfigured):
        client.get_events(1)


def test_not_configured_when_secret_missing(tmp_path: Path):
    """A client_secret_path that does not exist also raises."""
    client = CalendarClient(
        tmp_path / "missing-token.json", tmp_path / "no-such-secret.json"
    )
    with pytest.raises(CalendarNotConfigured):
        client.get_events(1)
