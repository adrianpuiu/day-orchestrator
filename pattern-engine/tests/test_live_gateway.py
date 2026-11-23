"""Live order-path guards: kill switch, confirmation gate, LiveGateway parity.

All exchange interaction is mocked — these tests prove the GUARDS, never the
network. The locked parity suite (test_paper_parity.py) already proves the
state machine; LiveGateway must produce identical books when sends succeed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.types import Signal
from app.exec.guards import ConfirmationGate, KillSwitch, TradingHalted
from app.exec.live_gateway import LiveGateway
from app.exec.paper import PaperGateway


def _signal(ts, entry=100.0, strategy_id="pb_test") -> Signal:
    return Signal(strategy_id=strategy_id, ts=ts, side=1, entry=entry,
                  stop=98.0, target=None, trail_mult=None,
                  time_stop_bars=10, stop_dist=2.0, reason="test")


@pytest.fixture
def guards(tmp_path):
    kill = KillSwitch(tmp_path / "KILL")
    gate = ConfirmationGate(tmp_path / "live_state.json", confirm_first_n=2)
    gate.arm()
    return kill, gate



class RecordingGateway(LiveGateway):
    """Mock transport: records sends, never touches the network."""

    def _send(self, intent):
        self.sent = getattr(self, "sent", []) + [intent]
        return {"status": "sent", "response": {"status": "ok"}}


def test_kill_switch_blocks_before_anything(guards):
    kill, gate = guards
    kill.trip()
    gw = RecordingGateway(kill, gate, "0x" + "aa" * 20)
    with pytest.raises(TradingHalted):
        gate.guarded_send(kill, {"id": "x"}, gw._send)
    assert not getattr(gw, "sent", [])


def test_unarmed_blocks_even_with_kill_cleared(guards):
    kill, gate = guards
    kill.clear()
    gate.disarm()
    gw = RecordingGateway(kill, gate, "0x" + "aa" * 20)
    with pytest.raises(TradingHalted):
        gate.guarded_send(kill, {"id": "x"}, gw._send)


def test_first_n_orders_enqueue_instead_of_sending(guards):
    kill, gate = guards
    gw = RecordingGateway(kill, gate, "0x" + "aa" * 20,
                          coin_of=lambda sid: "ETH")
    sig = _signal(pd.Timestamp("2026-01-05"))
    fill = gw.submit(sig, 0.5)
    assert fill is None                        # nothing filled
    assert not getattr(gw, "sent", [])         # nothing sent
    assert gw.flat
    pending = gate.pending()
    assert len(pending) == 1
    iid = next(iter(pending))
    assert pending[iid]["coin"] == "ETH" and pending[iid]["qty"] == 0.5


def test_approval_sends_the_exact_intent(guards):
    kill, gate = guards
    gw = RecordingGateway(kill, gate, "0x" + "aa" * 20,
                          coin_of=lambda sid: "ETH")
    gw.submit(_signal(pd.Timestamp("2026-01-05")), 0.5)
    (iid,) = list(gate.pending())
    intent = gate.take_approved(iid)
    result = gate.guarded_send(kill, intent, gw._send, approved=True)
    assert result["status"] == "sent"
    assert gw.sent[0]["id"] == iid              # exactly what was shown


def test_after_n_confirmed_orders_sends_directly(guards):
    kill, gate = guards                          # confirm_first_n = 2
    gw = RecordingGateway(kill, gate, "0x" + "aa" * 20)
    gate.note_confirmed_order()
    gate.note_confirmed_order()
    fill = gw.submit(_signal(pd.Timestamp("2026-01-05")), 0.5)
    assert fill is not None                     # local book filled
    assert len(gw.sent) == 1 and gw.sent[0]["kind"] == "entry"


def test_corrupt_state_fails_safe_unarmed(tmp_path):
    p = tmp_path / "live_state.json"
    p.write_text("{not json")
    gate = ConfirmationGate(p, confirm_first_n=2)
    assert not gate.armed                       # corrupt -> unarmed, safe


def test_missing_api_key_is_a_hard_stop(guards, monkeypatch):
    kill, gate = guards
    monkeypatch.delenv("HL_API_SECRET", raising=False)
    gate.note_confirmed_order()
    gate.note_confirmed_order()
    gw = LiveGateway(kill, gate, "0x" + "aa" * 20,
                     coin_of=lambda sid: "ETH")
    with pytest.raises(Exception, match="HL_API_SECRET"):
        gw.submit(_signal(pd.Timestamp("2026-01-05")), 0.5)


def test_live_matches_paper_when_sends_succeed(guards):
    """Parity: with the gate spent (past confirm_n) and sends mocked ok,
    LiveGateway produces the same closed book as PaperGateway."""
    kill, gate = guards
    for _ in range(gate.confirm_first_n):
        gate.note_confirmed_order()
    lg = RecordingGateway(kill, gate, "0x" + "aa" * 20)
    pg = PaperGateway()
    for i, ts in enumerate(pd.bdate_range("2026-01-05", periods=8)):
        sig = _signal(ts, entry=100.0 + i)
        lg.submit(sig, 1.0)
        pg.submit(sig, 1.0)
        lg.on_bar(ts, open_=100.0 + i, high=103.0, low=99.0, close=102.0)
        pg.on_bar(ts, open_=100.0 + i, high=103.0, low=99.0, close=102.0)
    assert len(lg.closed) == len(pg.closed)
    for lc, pc in zip(lg.closed, pg.closed):
        assert lc["r"] == pc["r"] and lc["entry"] == pc["entry"]


def test_key_matches_wallet_catches_wrong_key(monkeypatch):
    from eth_account import Account

    from app.exec.live_gateway import _key_matches_wallet

    key = Account.create()
    monkeypatch.setenv("HL_API_SECRET", key.key.hex())
    monkeypatch.setenv("HL_API_WALLET", "0x" + "11" * 20)
    assert _key_matches_wallet() is False          # wrong key -> False
    monkeypatch.setenv("HL_API_WALLET", key.address)
    assert _key_matches_wallet() is True
    monkeypatch.delenv("HL_API_SECRET")
    assert _key_matches_wallet() is None           # check not applicable
