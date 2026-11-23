"""LiveGateway — the gated order path (Step 2.2).

Subclasses PaperGateway so position/exit semantics are *the verified ones*
(stop-first, gap fills at open, trail geometry, time stop — the parity-locked
state machine). Only the fill boundary changes: instead of a simulated fill,
every entry/exit routes through `guarded_send` (kill switch -> armed ->
confirmation gate) and then to Hyperliquid via the official SDK, signed with
the API-wallet key, acting on the main account address.

The private key lives in .env (HL_API_SECRET) and is read here, once, at
first send — never in session state, never in the registry, never logged.

With no key configured the gateway still constructs (planning/preflight)
but every send raises: credential absence is a hard stop, not a silent
fallback to paper.
"""

from __future__ import annotations

import logging
import os

from ..core.types import Fill, Signal
from .guards import ConfirmationGate, KillSwitch
from .paper import PaperGateway

log = logging.getLogger(__name__)

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"


class MissingApiKey(RuntimeError):
    """Raised when an order path is reached without HL_API_SECRET."""


def _make_exchange(api_key: str, account_address: str, testnet: bool):
    from eth_account import Account
    from hyperliquid.exchange import Exchange

    wallet = Account.from_key(api_key)
    return Exchange(wallet, base_url=TESTNET if testnet else MAINNET,
                    account_address=account_address or None)


def _default_coin(strategy_id: str) -> str:
    return strategy_id.split(":", 1)[0].split("@", 1)[0]


class LiveGateway(PaperGateway):
    """Same state machine as paper; fills are real (guarded) or blocked."""

    def __init__(self, kill: KillSwitch, gate: ConfirmationGate,
                 account_address: str, cost_bp_side: float = 0.5,
                 testnet: bool = False, exchange_factory=_make_exchange,
                 coin_of=None) -> None:
        super().__init__(cost_bp_side=cost_bp_side)
        self.kill = kill
        self.gate = gate
        self.account_address = account_address
        self.testnet = testnet
        self._exchange_factory = exchange_factory
        self._exchange = None          # lazy: built on first real send
        self.coin_of = coin_of or _default_coin

    # ---- exchange plumbing ------------------------------------------------

    def _ex(self):
        if self._exchange is None:
            api_key = os.environ.get("HL_API_SECRET", "")
            if not api_key:
                raise MissingApiKey(
                    "HL_API_SECRET not set — no credentials, no orders "
                    "(hard stop; there is no paper fallback on this path)")
            self._exchange = self._exchange_factory(
                api_key, self.account_address, self.testnet)
        return self._exchange

    def _send(self, intent: dict) -> dict:
        ex = self._ex()
        if intent["kind"] == "entry":
            resp = ex.market_open(intent["coin"], intent["side"] > 0,
                                  intent["qty"])
        else:
            resp = ex.market_close(intent["coin"], intent["qty"])
        status = str((resp or {}).get("status", ""))
        if not (status == "ok" or status.startswith("ok")
                or status == "filled"):
            raise RuntimeError(f"exchange rejected order: {resp}")
        return {"status": "sent", "response": resp, "intent": intent["id"]}

    # ---- the fill boundary (everything else inherits paper semantics) ----

    def submit(self, sig: Signal, qty: float) -> Fill | None:
        if self.position is not None:
            return None
        coin = self.coin_of(sig.strategy_id)
        intent = {"id": f"entry:{sig.strategy_id}:{sig.ts}", "kind": "entry",
                  "coin": coin, "side": sig.side, "qty": qty,
                  "price_hint": sig.entry, "reason": sig.reason}
        result = self.gate.guarded_send(self.kill, intent, self._send)
        if result.get("status") == "pending_confirmation":
            log.warning("[live] entry pending confirmation: %s "
                        "(approve via `runtime approve %s`)",
                        intent["id"], result["intent_id"])
            return None
        return super().submit(sig, qty)     # record the local book as filled

    def _exit(self, ts, price: float, kind: str) -> Fill:
        pos = self.position
        assert pos is not None
        intent = {"id": f"exit:{pos.signal.strategy_id}:{ts}", "kind": "exit",
                  "coin": self.coin_of(pos.signal.strategy_id),
                  "side": -pos.signal.side, "qty": pos.qty,
                  "price_hint": price, "reason": kind}
        result = self.gate.guarded_send(self.kill, intent, self._send)
        if result.get("status") == "pending_confirmation":
            # An exit awaiting confirmation stays open locally and retries on
            # the next bar — never silently drop risk management.
            log.warning("[live] exit pending confirmation: %s", intent["id"])
            return None                     # type: ignore[return-value]
        return super()._exit(ts, price, kind)

    # ---- certification helper ------------------------------------------------

    def preflight(self) -> dict:
        """Everything a human checks before arming: guards + credentials."""
        return {
            "kill_tripped": self.kill.tripped,
            "armed": self.gate.armed,
            "confirm_first_n": self.gate.confirm_first_n,
            "confirmed_orders": self.gate._state["confirmed_orders"],
            "pending": len(self.gate.pending()),
            "testnet": self.testnet,
            "account_address": self.account_address,
            "api_key_present": bool(os.environ.get("HL_API_SECRET")),
        }
