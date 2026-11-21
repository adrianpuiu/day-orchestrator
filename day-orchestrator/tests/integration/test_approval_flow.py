"""Integration proof of the approval gate (ADR-004): pause → approve → send,
and pause → reject → nothing sent. Fully offline (fake channels, scripted LLM).
"""

from __future__ import annotations

from pathlib import Path

from app.smoke import run_gate_scenario


async def test_approval_gate_approve_then_send(tmp_path: Path) -> None:
    result = await run_gate_scenario(tmp_path, verdict="approved")

    assert result["approval_state"] == "approved"
    assert result["sent"], "approved email must actually send via the transport"
    sent = result["sent"][0]
    assert sent["to"] == "bob@example.com"
    assert sent["subject"] == "Lunch on Tuesday?"

    actions = {row["action"] for row in result["actions"]}
    assert "email_sent" in actions, "every send must land in the action log"


async def test_approval_gate_reject_never_sends(tmp_path: Path) -> None:
    result = await run_gate_scenario(tmp_path, verdict="rejected")

    assert result["approval_state"] == "rejected"
    assert result["sent"] == [], "rejected email must never reach the transport"

    actions = {row["action"] for row in result["actions"]}
    assert "send_rejected" in actions
    assert "email_sent" not in actions
