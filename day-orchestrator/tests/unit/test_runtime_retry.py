"""Transient model-error classification and the real retry loop."""

from __future__ import annotations

import pytest

from app.runtime import _AttemptFailure, _retry_async, _transient_model_error


def test_connection_errors_are_transient() -> None:
    assert _transient_model_error(
        RuntimeError("litellm.InternalServerError: OpenAIException - Connection error.")
    )
    assert _transient_model_error(TimeoutError("request timed out"))


def test_non_transient_errors_are_not_retried() -> None:
    assert not _transient_model_error(ValueError("bad arguments"))
    assert not _transient_model_error(
        RuntimeError("Calendar consent not granted (status=0)")
    )


async def _attempt_fail(produced: int, exc: Exception):
    raise _AttemptFailure(produced, exc)


async def test_retry_loop_recovers_transient_first_attempt() -> None:
    calls = 0

    async def attempt() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            await _attempt_fail(0, RuntimeError("Connection error"))
        return "done", 1

    final, produced = await _retry_async(attempt, backoff=0)
    assert (final, produced, calls) == ("done", 1, 2)


async def test_retry_loop_gives_up_after_attempts() -> None:
    calls = 0

    async def attempt() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        await _attempt_fail(0, RuntimeError("Connection error"))

    with pytest.raises(RuntimeError, match="Connection error"):
        await _retry_async(attempt, backoff=0)
    assert calls == 3


async def test_retry_loop_skips_when_events_were_produced() -> None:
    calls = 0

    async def attempt() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        await _attempt_fail(2, RuntimeError("Connection error"))
        return "", 0

    with pytest.raises(RuntimeError, match="Connection error"):
        await _retry_async(attempt, backoff=0)
    assert calls == 1, "a run that already produced events must not retry"


async def test_retry_loop_never_retries_non_transient() -> None:
    calls = 0

    async def attempt() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        await _attempt_fail(0, RuntimeError("Calendar consent not granted"))

    with pytest.raises(RuntimeError, match="consent"):
        await _retry_async(attempt, backoff=0)
    assert calls == 1
