"""Gmail channel: IMAP/SMTP transport and IDLE watcher (ADR-002).

This module is the only project file allowed to import ``imapclient`` and
``smtplib``. Reads use IMAP (993/TLS, IDLE with poll fallback), draft creation
uses IMAP APPEND to ``[Gmail]/Drafts``, and approved sends go over SMTP
(587/STARTTLS). Auth is a Google app password; the password is never logged
and never embedded in exceptions.
"""

from __future__ import annotations

import email
import email.message
import email.policy
import email.utils
import logging
import re
import smtplib
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC
from email.header import decode_header, make_header
from html.parser import HTMLParser

from imapclient import IMAPClient

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
DRAFTS_FOLDER = "[Gmail]/Drafts"
SNIPPET_LENGTH = 200


class _TextExtractor(HTMLParser):
    """Collect text content from an HTML document, skipping script/style."""

    _SKIP: frozenset[str] = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._chunks.append(data)

    def text(self) -> str:
        """Join collected chunks with single spaces."""
        return " ".join("".join(self._chunks).split())


def html_to_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace into plain text."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed HTML: degrade to a crude tag strip rather than failing.
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())
    return parser.text()


class GmailClient:
    """Sync IMAP/SMTP transport implementing GmailClientProto."""

    def __init__(
        self,
        address: str,
        app_password: str,
        *,
        imap_factory: Callable[[], IMAPClient] | None = None,
        smtp_factory: Callable[[], smtplib.SMTP] | None = None,
    ) -> None:
        self._address = address
        self._password = app_password
        self._imap_factory = imap_factory or self._default_imap_factory
        self._smtp_factory = smtp_factory or self._default_smtp_factory
        self._imap: IMAPClient | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _default_imap_factory() -> IMAPClient:
        """Create a TLS IMAPClient bound to Gmail's IMAP endpoint."""
        return IMAPClient(
            IMAP_HOST, port=IMAP_PORT,
            ssl_context=ssl.create_default_context(), timeout=60,
        )

    @staticmethod
    def _default_smtp_factory() -> smtplib.SMTP:
        """Create an SMTP connection to Gmail's STARTTLS endpoint."""
        return smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)

    # ------------------------------------------------------------------ plumbing

    def _connect_imap(self) -> IMAPClient:
        """Connect, log in, and select INBOX (no retry; callers retry)."""
        return self._imap_once()

    def _imap_once(self) -> IMAPClient:
        """One connect + login + select-INBOX attempt, caching the client."""
        if self._imap is None:
            client = self._imap_factory()
            client.login(self._address, self._password)
            client.select_folder("INBOX")
            self._imap = client
        return self._imap

    def new_imap(self) -> IMAPClient:
        """Fresh connected IMAP client, NOT cached; the caller owns it.

        The IDLE watcher must never share the cached connection with
        request-path reads: commands issued while a connection sits in
        IDLE mode fail server-side (imapclient contract), so the watcher
        keeps its own connection per cycle.
        """
        client = self._imap_factory()
        client.login(self._address, self._password)
        client.select_folder("INBOX")
        return client

    def _imap_op(self, op: Callable[[IMAPClient], object]) -> object:
        """Run an IMAP operation with exactly one reconnect retry."""
        with self._lock:
            try:
                return op(self._connect_imap())
            except Exception:
                log.warning("imap operation failed; retrying once with reconnect")
                self._imap = None
                return op(self._connect_imap())

    def _connect_smtp(self) -> smtplib.SMTP:
        """Connect, STARTTLS, and log in over SMTP (no retry; callers retry)."""
        return self._smtp_once()

    def _smtp_once(self) -> smtplib.SMTP:
        """One connect + STARTTLS + login attempt on a fresh connection."""
        client = self._smtp_factory()
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        client.login(self._address, self._password)
        return client

    # ------------------------------------------------------------------ reads

    def fetch_recent(self, limit: int) -> list[dict]:
        """Fetch summaries of the newest INBOX messages, newest first."""
        uids = self._imap_op(lambda c: c.search(["ALL"]))
        assert isinstance(uids, list)
        newest = list(uids)[-limit:] if limit > 0 else []
        newest.reverse()
        if not newest:
            return []
        fetch_data: dict = self._imap_op(lambda c: c.fetch(newest, ["RFC822"]))
        return [
            self._summary(uid, msg)
            for uid, msg in self._sorted_messages(fetch_data, newest)
        ]

    def get_message(self, uid: str) -> dict:
        """Fetch one full message dict (summary fields plus body and to)."""
        fetch_data: dict = self._imap_op(
            lambda c: c.fetch([int(uid)], ["RFC822"])
        )
        raw = fetch_data.get(int(uid))
        if raw is None:
            raise ValueError(f"no message with uid {uid}")
        msg = email.message_from_bytes(raw[b"RFC822"], policy=email.policy.default)
        return {**self._summary(int(uid), raw), "body": self._body(msg), "to": str(msg["To"] or "")}

    # ------------------------------------------------------------------ writes

    def save_draft(
        self, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> str:
        """Append a draft to [Gmail]/Drafts and return its Message-ID."""
        message_id = self._make_message_id()
        msg = self._build_mime(to, subject, body, message_id, in_reply_to)
        raw = msg.as_bytes(policy=email.policy.SMTP)
        appended: list = self._imap_op(
            lambda c: c.append(DRAFTS_FOLDER, ["\\Draft"], None, raw)
        )
        log.info("draft appended uid=%s", appended)
        return message_id

    def send_email(
        self, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> None:
        """Send an email over SMTP with proper threading headers."""
        message_id = self._make_message_id()
        msg = self._build_mime(to, subject, body, message_id, in_reply_to)
        raw = msg.as_bytes(policy=email.policy.SMTP)

        def _send(client: smtplib.SMTP) -> None:
            client.sendmail(self._address, [to], raw)

        self._smtp_op(_send)
        log.info("email sent to=%s message_id=%s", to, message_id)

    def _smtp_op(self, op: Callable[[smtplib.SMTP], None]) -> None:
        """Run an SMTP operation on a fresh connection; retry once, then raise."""
        try:
            client = self._smtp_once()
        except Exception:
            log.warning("smtp connection failed; retrying once")
            client = self._smtp_once()
        try:
            op(client)
        except Exception:
            log.warning("smtp operation failed; retrying once with fresh connection")
            client = self._smtp_once()
            op(client)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()

    # ------------------------------------------------------------------ helpers

    def _make_message_id(self) -> str:
        """Return a uuid-based Message-ID in the account's domain."""
        domain = self._address.rsplit("@", 1)[-1]
        return f"<{uuid.uuid4()}@{domain}>"

    def _build_mime(
        self,
        to: str,
        subject: str,
        body: str,
        message_id: str,
        in_reply_to: str | None,
    ) -> email.message.Message:
        """Build a text/plain MIME message with threading headers."""
        msg = email.message.EmailMessage()
        msg["From"] = self._address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        return msg

    @staticmethod
    def _sorted_messages(
        fetch_data: dict, order: list[int]
    ) -> list[tuple[int, dict]]:
        """Return (uid, fetch dict) pairs in the requested UID order."""
        pairs: list[tuple[int, dict]] = []
        for uid in order:
            data = fetch_data.get(uid)
            if data is not None:
                pairs.append((uid, data))
        return pairs

    def _summary(self, uid: int, fetch_dict: dict) -> dict:
        """Build the contract summary dict from one fetched RFC822 body."""
        raw: bytes = fetch_dict[b"RFC822"]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        from_ = str(msg["From"] or "")
        try:
            from_ = str(make_header(decode_header(from_)))
        except Exception:
            pass
        subject = str(msg["Subject"] or "")
        try:
            subject = str(make_header(decode_header(subject)))
        except Exception:
            pass
        snippet = self._body(msg)[:SNIPPET_LENGTH]
        return {
            "uid": str(uid),
            "message_id": str(msg["Message-ID"] or ""),
            "subject": subject,
            "from_": from_,
            "snippet": snippet,
            "date": self._date_iso(msg),
        }

    @staticmethod
    def _date_iso(msg: email.message.Message) -> str:
        """Render the Date header as an ISO-8601 UTC string."""
        raw = msg["Date"]
        if not raw:
            return ""
        try:
            dt = email.utils.parsedate_to_datetime(str(raw))
        except (TypeError, ValueError):
            return ""
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()

    @staticmethod
    def _body(msg: email.message.Message) -> str:
        """Prefer text/plain; fall back to HTML with tags stripped."""
        plain: str | None = None
        html: str | None = None
        for part in msg.walk():
            if part.get_content_maintype() != "text" or part.is_multipart():
                continue
            try:
                content = part.get_content()
            except Exception:
                continue
            if part.get_content_subtype() == "plain":
                plain = content if plain is None else f"{plain}\n{content}"
            elif html is None:
                html = content
        if plain:
            return " ".join(plain.split())
        if html:
            return html_to_text(html)
        return ""


class GmailWatcher:
    """Thread-based near-push watcher using IMAP IDLE with poll fallback."""

    def __init__(
        self,
        client: GmailClient,
        on_new_messages: Callable[[list[dict]], None],
        poll_seconds: int,
        *,
        idle_check_timeout: int = 1740,
        max_batch: int = 20,
        on_persistent_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._on_new_messages = on_new_messages
        self._poll_seconds = poll_seconds
        self._idle_check_timeout = idle_check_timeout
        self._max_batch = max_batch
        self._on_persistent_failure = on_persistent_failure
        self._seen: set[int] = set()
        # Set when the baseline seed failed: the first poll must then treat
        # the whole inbox as a fresh baseline instead of replaying history.
        self._baseline_pending = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the daemon watcher thread (baseline seeds inside it)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="gmail-watcher", daemon=True
        )
        self._thread.start()

    def _seed_baseline(self) -> None:
        """Mark current INBOX uids seen so a fresh start doesn't replay history."""
        try:
            client = self._client.new_imap()
            try:
                uids = client.search(["ALL"])
                if isinstance(uids, list):
                    self._seen.update(uids)
                log.info("watcher baseline: %d uid(s) marked seen", len(self._seen))
            finally:
                try:
                    client.logout()
                except Exception:
                    log.debug("baseline logout failed", exc_info=True)
        except Exception:
            self._baseline_pending = True
            log.exception("watcher baseline seed failed; first poll re-baselines")

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the watcher to stop and join its thread."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def poll_once(self, client: IMAPClient | None = None) -> None:
        """Check INBOX for unseen UIDs and notify about new summaries.

        Uses a dedicated connection when none is given (never the shared
        request-path one) and caps the per-batch notify count so a large
        backlog cannot flood the triage pipeline in one shot.
        """
        own = client is None
        conn = client if client is not None else self._client.new_imap()
        try:
            uids = conn.search(["ALL"])
            assert isinstance(uids, list)
            if self._baseline_pending:
                # Seed failed earlier: treat current uids as the baseline.
                self._seen.update(uids)
                self._baseline_pending = False
            new = sorted(u for u in uids if u not in self._seen)
            if not new:
                return
            # Cap the batch; older unseen mail drains in later cycles
            # (only the fetched batch is marked seen).
            batch = new[-self._max_batch:]
            fetch_data: dict = conn.fetch(batch, ["RFC822"])
            summaries = [
                self._client._summary(uid, data)
                for uid, data in fetch_data.items()
            ]
            self._seen.update(batch)
            if summaries:
                self._on_new_messages(summaries)
        finally:
            if own:
                try:
                    conn.logout()
                except Exception:
                    log.debug("poll logout failed", exc_info=True)

    # ------------------------------------------------------------------ loop


    def _run(self) -> None:
        """Baseline seed, then IDLE loop with poll fallback; never raises."""
        self._seed_baseline()
        backoff = 1.0
        streak = 0
        alerted = False
        while not self._stop.is_set():
            try:
                self._idle_cycle()
                backoff = 1.0
                streak = 0
                alerted = False
            except Exception:
                streak += 1
                log.exception("watcher cycle failed; backing off")
                if (
                    self._on_persistent_failure is not None
                    and streak >= 10
                    and not alerted
                ):
                    alerted = True  # one-shot per streak, not per failure
                    try:
                        self._on_persistent_failure(
                            f"{streak} consecutive failed watcher cycles"
                        )
                    except Exception:
                        log.exception("persistent-failure callback failed")
                self._stop.wait(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)

    def _idle_cycle(self) -> None:
        """One IDLE cycle on a dedicated, cycle-owned connection."""
        client = self._client.new_imap()
        try:
            try:
                client.idle()
            except Exception:
                log.warning("IDLE refused; degrading to poll-only cycle", exc_info=True)
                self._stop.wait(min(self._poll_seconds, 60.0))
                self.poll_once(client)
                return
            try:
                deadline = time.monotonic() + self._poll_seconds
                while not self._stop.is_set() and time.monotonic() < deadline:
                    remaining = min(
                        self._idle_check_timeout, deadline - time.monotonic()
                    )
                    if remaining <= 0:
                        break
                    responses = client.idle_check(timeout=remaining)
                    if responses:
                        break
            finally:
                try:
                    client.idle_done()
                except Exception:
                    log.debug("idle_done failed", exc_info=True)
            # Poll strictly AFTER idle_done: any command issued while the
            # connection is in IDLE mode fails server-side.
            self.poll_once(client)
        finally:
            try:
                client.logout()
            except Exception:
                log.debug("watcher logout failed", exc_info=True)
