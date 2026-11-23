"""Live-trading guards — kill switch + human confirmation gate.

Pure code in the trading plane (the interview's rule: no LLM ever sits in
the decision path, and HITL here means a durable, inspectable state file —
not an agent tool-flow).

Durability model:
  - Kill switch: `var/KILL` file. Existing -> tripped. It wins over
    everything, always, and survives registry/json corruption because it is
    a bare file. Manual re-arm only.
  - Confirmation gate: `var/live_state.json` — armed flag, confirmed-order
  counter, pending intents. The first N live orders require explicit
  human approval (`runtime approve <id>`); approval executes the exact
  intent shown, through the same guarded path.
"""


import json
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_CONFIRM_FIRST_N = 5


class TradingHalted(RuntimeError):
    """Raised when a live order is attempted while halted (kill/not armed)."""


class KillSwitch:
    """File-based trip; existence check is atomic and corruption-proof."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def tripped(self) -> bool:
        return self.path.exists()

    def trip(self, reason: str = "manual") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            f"tripped {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"reason={reason}\n")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class ConfirmationGate:
    """Durable armed/pending state for the first-N-orders confirmation."""

    def __init__(self, path: Path, confirm_first_n: int = DEFAULT_CONFIRM_FIRST_N
                 ) -> None:
        self.path = path
        self.confirm_first_n = confirm_first_n
        self._state = self._load()

    # ---- state ---------------------------------------------------------

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                pass     # corrupt state fails SAFE: treat as unarmed
        return {"armed": False, "confirmed_orders": 0, "pending": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(self.path)      # atomic swap

    # ---- lifecycle -------------------------------------------------------

    @property
    def armed(self) -> bool:
        return bool(self._state.get("armed"))

    def arm(self) -> None:
        self._state["armed"] = True
        self._save()

    def disarm(self) -> None:
        self._state["armed"] = False
        self._save()

    # ---- confirmation flow -------------------------------------------------

    def needs_confirmation(self) -> bool:
        return self._state["confirmed_orders"] < self.confirm_first_n

    def enqueue(self, intent: dict) -> str:
        intent_id = intent["id"]
        self._state["pending"][intent_id] = {**intent,
                                             "enqueued_at": time.strftime(
                                                 "%Y-%m-%dT%H:%M:%SZ")}
        self._save()
        return intent_id

    def pending(self) -> dict:
        return dict(self._state["pending"])

    def take_approved(self, intent_id: str) -> dict | None:
        """Removes and returns the intent if pending; else None."""
        intent = self._state["pending"].pop(intent_id, None)
        if intent is not None:
            self._save()
        return intent

    def note_confirmed_order(self) -> int:
        self._state["confirmed_orders"] += 1
        self._save()
        return self._state["confirmed_orders"]

    # ---- the guarded send ------------------------------------------------

    def guarded_send(self, kill: KillSwitch, intent: dict,
                     send: Callable[[dict], dict],
                     approved: bool = False) -> dict:
        """Single entry point for anything that touches the exchange.

        Order of checks: kill switch (file, corruption-proof) -> armed ->
        confirmation policy. A blocked call never reaches `send`.
        `approved=True` marks an intent the human already explicitly OK'd
        (the `approve` CLI); kill/armed still apply.
        """
        if kill.tripped:
            raise TradingHalted("kill switch tripped (var/KILL) — "
                                "manual re-arm required")
        if not self.armed:
            raise TradingHalted("live gateway not armed — `runtime arm` first")
        if self.needs_confirmation() and not approved:
            iid = self.enqueue(intent)
            return {"status": "pending_confirmation", "intent_id": iid}
        result = send(intent)
        self.note_confirmed_order()
        return result
