"""Transient model-error classification for the runtime retry wrapper."""

from __future__ import annotations

from app.runtime import _transient_model_error


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
