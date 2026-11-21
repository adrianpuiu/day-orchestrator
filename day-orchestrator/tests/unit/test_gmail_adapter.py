"""Unit tests for the Gmail channel: parsing, drafts, sends, watcher, retries."""

from __future__ import annotations

import email.message
import email.policy
import smtplib
import threading
import time
from typing import Any

import pytest

from app.channels.gmail import GmailClient, GmailWatcher, html_to_text


def make_rfc822(
    body_plain: str | None = "Hello world",
    body_html: str | None = None,
    subject: str = "Hi",
    from_: str = "Alice <alice@example.com>",
    to: str = "owner@gmail.com",
    message_id: str = "<abc@example.com>",
    date: str = "Sat, 22 Nov 2026 09:00:00 +0000",
) -> bytes:
    """Build an RFC822 byte payload for fake IMAP fetches."""
    msg = email.message.EmailMessage(policy=email.policy.default)
    msg["From"] = from_
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = date
    if body_plain and body_html:
        msg.set_content(body_plain)
        msg.add_alternative(body_html, subtype="html")
    elif body_html:
        msg.set_content(body_html, subtype="html")
    else:
        msg.set_content(body_plain or "")
    return msg.as_bytes(policy=email.policy.SMTP)


class FakeIMAP:
    """In-memory IMAPClient double."""

    server_greeting = b"fake"

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self.appended: list[tuple] = []
        self.selected: str | None = None
        store["client"] = self

    def login(self, username: str, password: str) -> None:
        if self._store.get("fail_next_connect"):
            self._store["fail_next_connect"] -= 1
            raise ConnectionError("imap down")
        self._store["logins"] = [*self._store.get("logins", []), (username, password)]

    def select_folder(self, folder: str) -> None:
        self.selected = folder

    def search(self, criteria: Any) -> list[int]:
        return list(self._store.get("uids", []))

    def fetch(self, uids: Any, data: Any) -> dict[int, dict]:
        bodies: dict[int, bytes] = self._store["bodies"]
        return {uid: {b"RFC822": bodies[uid]} for uid in uids if uid in bodies}

    def append(self, folder: str, flags: list, date_time: Any, raw: bytes) -> list:
        self.appended.append((folder, flags, date_time, raw))
        new_uid = max(self._store.get("uids") or [0]) + 1
        self._store.setdefault("uids", []).append(new_uid)
        self._store["bodies"][new_uid] = raw
        return [new_uid]

    def idle(self) -> None:
        self._store["idle_on"] = True

    def idle_check(self, timeout: float | None = None) -> tuple:
        time.sleep(min(timeout or 0, 0.01))
        return ()

    def idle_done(self) -> None:
        self._store["idle_on"] = False


class FakeSMTP:
    """In-memory SMTP double capturing sendmail payloads."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        store.setdefault("sent", [])

    def ehlo(self) -> None:
        pass

    def starttls(self, context: Any = None) -> None:
        pass

    def login(self, username: str, password: str) -> None:
        if self._store.get("smtp_fail_login"):
            raise smtplib.SMTPAuthenticationError(535, b"nope")
        self._store["smtp_logins"] = self._store.get("smtp_logins", 0) + 1

    def sendmail(self, from_: str, to: list, raw: bytes) -> None:
        if self._store.get("smtp_fail_send"):
            self._store["smtp_fail_send"] -= 1
            raise smtplib.SMTPRecipientsRefused({to[0]: (450, b"busy")})
        self._store["sent"].append((from_, to, raw))

    def quit(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture()
def gmail_store() -> dict[str, Any]:
    store: dict[str, Any] = {"uids": [], "bodies": {}}
    return store


@pytest.fixture()
def client(gmail_store: dict[str, Any]) -> GmailClient:
    return GmailClient(
        "owner@gmail.com",
        "app-password",
        imap_factory=lambda: FakeIMAP(gmail_store),
        smtp_factory=lambda: FakeSMTP(gmail_store),
    )


# --------------------------------------------------------------- parsing


def test_html_to_text_strips_tags() -> None:
    assert html_to_text("<p>Hello <b>world</b></p><script>bad()</script>") == "Hello world"


def test_summary_prefers_plain_over_html(gmail_store: dict[str, Any]) -> None:
    gmail_store["uids"] = [1]
    gmail_store["bodies"][1] = make_rfc822(
        body_plain="Plain body", body_html="<p>HTML body</p>"
    )
    summaries = client_summary(gmail_store)
    assert summaries[0]["snippet"] == "Plain body"
    assert summaries[0]["subject"] == "Hi"
    assert summaries[0]["from_"] == "Alice <alice@example.com>"
    assert summaries[0]["uid"] == "1"
    assert summaries[0]["message_id"] == "<abc@example.com>"
    assert summaries[0]["date"].startswith("2026-11-22")


def client_summary(store: dict[str, Any]) -> list[dict]:
    c = GmailClient(
        "owner@gmail.com",
        "pw",
        imap_factory=lambda: FakeIMAP(store),
        smtp_factory=lambda: FakeSMTP(store),
    )
    return c.fetch_recent(10)


def test_html_only_message_body_strips_tags(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    gmail_store["uids"] = [7]
    gmail_store["bodies"][7] = make_rfc822(
        body_plain=None, body_html="<div><p>Fancy <b>HTML</b></p></div>"
    )
    msg = client.get_message("7")
    assert msg["body"] == "Fancy HTML"
    assert msg["to"] == "owner@gmail.com"


def test_snippet_truncated_to_200_chars(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    gmail_store["uids"] = [2]
    gmail_store["bodies"][2] = make_rfc822("x" * 500)
    summary = client.fetch_recent(1)[0]
    assert len(summary["snippet"]) == 200


def test_fetch_recent_newest_first(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    for uid in (10, 11, 12):
        gmail_store["uids"].append(uid)
        gmail_store["bodies"][uid] = make_rfc822(f"body {uid}")
    uids = [s["uid"] for s in client.fetch_recent(2)]
    assert uids == ["12", "11"]


def test_get_message_unknown_uid_raises(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="uid"):
        client.get_message("999")


# --------------------------------------------------------------- drafts


def test_save_draft_appends_with_flags_and_headers(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    message_id = client.save_draft(
        "peer@example.com", "Re: Hi", "Draft body", in_reply_to="<abc@example.com>"
    )
    folder, flags, _dt, raw = gmail_store["client"].appended[0]
    assert folder == "[Gmail]/Drafts"
    assert flags == ["\\Draft"]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["Message-ID"] == message_id
    assert msg["In-Reply-To"] == "<abc@example.com>"
    assert msg["From"] == "owner@gmail.com"
    assert message_id.startswith("<") and "@gmail.com>" in message_id


def test_save_draft_without_reply_has_no_threading_headers(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    client.save_draft("peer@example.com", "Fresh", "Body")
    raw = gmail_store["client"].appended[0][3]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["In-Reply-To"] is None


# --------------------------------------------------------------- sends


def test_send_email_headers_and_payload(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    client.send_email("peer@example.com", "Re: Hi", "Send body", in_reply_to="<abc@example.com>")
    from_, to, raw = gmail_store["sent"][0]
    assert from_ == "owner@gmail.com"
    assert to == ["peer@example.com"]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["In-Reply-To"] == "<abc@example.com>"
    assert msg["Message-ID"]
    assert "Send body" in msg.get_content()


def test_send_email_retries_once_then_raises(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    gmail_store["smtp_fail_send"] = 2  # fail both attempts
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        client.send_email("peer@example.com", "S", "B")
    assert len(gmail_store["sent"]) == 0


def test_send_email_recovers_on_first_retry(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    gmail_store["smtp_fail_send"] = 1  # fail once, succeed on retry
    client.send_email("peer@example.com", "S", "B")
    assert len(gmail_store["sent"]) == 1


# --------------------------------------------------------------- reconnect


def test_imap_reconnect_retry_then_raises(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    gmail_store["fail_next_connect"] = 99  # every login fails
    with pytest.raises(ConnectionError):
        client.fetch_recent(5)


def test_imap_recovers_on_reconnect(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    gmail_store["fail_next_connect"] = 1  # first login fails, retry succeeds
    gmail_store["uids"] = [3]
    gmail_store["bodies"][3] = make_rfc822("recovered")
    summaries = client.fetch_recent(1)
    assert summaries[0]["uid"] == "3"


def test_password_never_in_exception(client: GmailClient, gmail_store: dict[str, Any]) -> None:
    gmail_store["fail_next_connect"] = 99
    with pytest.raises(Exception) as excinfo:
        client.fetch_recent(5)
    assert "app-password" not in str(excinfo.value)


# --------------------------------------------------------------- watcher


def test_watcher_fires_on_new_uids(gmail_store: dict[str, Any]) -> None:
    gmail_store["uids"] = [1]
    gmail_store["bodies"][1] = make_rfc822("first")
    received: list[list[dict]] = []
    done = threading.Event()

    def on_new(summaries: list[dict]) -> None:
        received.append(summaries)
        done.set()

    c = GmailClient(
        "owner@gmail.com",
        "pw",
        imap_factory=lambda: FakeIMAP(gmail_store),
        smtp_factory=lambda: FakeSMTP(gmail_store),
    )
    watcher = GmailWatcher(c, on_new, poll_seconds=30)
    watcher.poll_once()  # uid 1 unseen -> callback fires
    assert done.wait(2)
    assert received[0][0]["uid"] == "1"

    received.clear()
    watcher.poll_once()  # no new UIDs -> no callback
    assert not received

    gmail_store["uids"].append(2)
    gmail_store["bodies"][2] = make_rfc822("second")
    watcher.poll_once()
    assert received and received[0][0]["uid"] == "2"


def test_watcher_survives_callback_exception(gmail_store: dict[str, Any]) -> None:
    gmail_store["uids"] = [1]
    gmail_store["bodies"][1] = make_rfc822("boom")
    calls = {"n": 0}

    def on_new(_summaries: list[dict]) -> None:
        calls["n"] += 1
        raise RuntimeError("callback blew up")

    c = GmailClient(
        "owner@gmail.com",
        "pw",
        imap_factory=lambda: FakeIMAP(gmail_store),
        smtp_factory=lambda: FakeSMTP(gmail_store),
    )
    watcher = GmailWatcher(c, on_new, poll_seconds=30)
    with pytest.raises(RuntimeError):
        watcher.poll_once()
    assert calls["n"] == 1


def test_watcher_start_stop(gmail_store: dict[str, Any]) -> None:
    gmail_store["uids"] = [1]
    gmail_store["bodies"][1] = make_rfc822("idle")
    received: list[list[dict]] = []
    c = GmailClient(
        "owner@gmail.com",
        "pw",
        imap_factory=lambda: FakeIMAP(gmail_store),
        smtp_factory=lambda: FakeSMTP(gmail_store),
    )
    watcher = GmailWatcher(c, received.append, poll_seconds=1, idle_check_timeout=1)
    watcher.start()
    assert watcher._thread is not None and watcher._thread.daemon
    time.sleep(0.2)
    watcher.stop(timeout=5)
    assert watcher._thread is None
    assert gmail_store.get("idle_on") is False
