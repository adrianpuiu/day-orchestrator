"""Offline tests for the tolerant model-JSON parser and the IDLE fallback."""

from __future__ import annotations

from typing import Any

from app.channels.gmail import GmailClient, GmailWatcher
from app.workflows.pipelines import _extract_json


def test_extract_json_plain_dict() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_dict() -> None:
    assert _extract_json('```json\n{"items": [{"uid": "7"}]}\n```') == {
        "items": [{"uid": "7"}]
    }


def test_extract_json_prose_wrapped() -> None:
    assert _extract_json('Sure! Here it is: {"a": {"b": 2}} hope that helps') == {
        "a": {"b": 2}
    }


def test_extract_json_array_becomes_items() -> None:
    out = _extract_json('[{"uid": "1"}, {"uid": "2"}]')
    assert isinstance(out, dict)
    assert out.get("items") == [{"uid": "1"}, {"uid": "2"}]


def test_extract_json_garbage_and_none() -> None:
    assert _extract_json("no json here at all") == {}
    assert _extract_json(None) == {}
    assert _extract_json(42) == {}


def _msg(message_id: str) -> bytes:
    return (
        f"From: x@y.z\r\nSubject: hello\r\nMessage-ID: <{message_id}>\r\n"
        "Date: Mon, 23 Nov 2026 08:00:00 +0000\r\n"
        "Content-Type: text/plain\r\n\r\nbody here\r\n"
    ).encode()


class _RefusingIdleIMAP:
    """FakeIMAP double whose server refuses IDLE (some proxies do)."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def login(self, u: str, p: str) -> None: ...
    def select_folder(self, f: str) -> None: ...
    def logout(self) -> None: ...

    def search(self, criteria: Any) -> list[int]:
        return list(self._store.get("uids", []))

    def fetch(self, uids: list[int], parts: Any) -> dict[int, dict]:
        bodies: dict[int, bytes] = self._store["bodies"]
        return {uid: {b"RFC822": bodies[uid]} for uid in uids if uid in bodies}

    def idle(self) -> None:
        raise RuntimeError("IDLE not supported")


class _WorkingIdleIMAP(_RefusingIdleIMAP):
    """Server double with working IDLE: the happy path must also reach the poll."""

    def idle(self) -> None: ...

    def idle_check(self, timeout: float) -> list:
        return [(b"1", b"EXISTS")]

    def idle_done(self) -> None: ...


def _watcher(store: dict[str, Any], got: list, imap_cls) -> GmailWatcher:
    client = GmailClient("a@b.c", "pw", imap_factory=lambda: imap_cls(store))
    return GmailWatcher(client, got.append, poll_seconds=0)


def test_idle_refusal_degrades_to_poll() -> None:
    """IDLE unsupported must degrade to polling, not silent death."""
    store = {"uids": [1], "bodies": {1: _msg("m1@x")}}
    got: list = []
    _watcher(store, got, _RefusingIdleIMAP)._idle_cycle()
    assert len(got) == 1, "poll fallback must discover and deliver new mail"


def test_successful_idle_cycle_still_polls() -> None:
    """After a successful IDLE window the cycle must still poll and deliver."""
    store = {"uids": [1], "bodies": {1: _msg("m2@x")}}
    got: list = []
    _watcher(store, got, _WorkingIdleIMAP)._idle_cycle()
    assert len(got) == 1, "happy-path IDLE cycle must end with the poll"
