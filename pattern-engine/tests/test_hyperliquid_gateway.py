"""HyperliquidGateway — shadow mode, strictly read-only.

Mirrors the real account via public `info` endpoints: an address is enough,
no key, no signing, no order path. The paper plane runs its signals and this
gateway answers "what does the account actually hold / what actually filled"
so the two can be reconciled every cycle. There is deliberately NO submit()
here: this class cannot trade, by construction.

Tests parse canned public-endpoint payloads offline.
"""

from __future__ import annotations

import pytest

from app.exec.hyperliquid_gateway import HyperliquidGateway

CLEARINGHOUSE = {
    "marginSummary": {"accountValue": "1234.56"},
    "assetPositions": [{
        "position": {"coin": "ETH", "szi": "0.5", "entryPx": "3000.0",
                     "unrealizedPnl": "12.5",
                     "leverage": {"value": "3"},
                     "liquidationPx": "2100.0"}}],
}
FILLS = [
    {"coin": "ETH", "side": "B", "sz": "0.5", "px": "3000.0",
     "closedPnl": "0.0", "time": 1760000000000, "dir": "Open Long"},
    {"coin": "BTC", "side": "S", "sz": "0.01", "px": "60000.0",
     "closedPnl": "-15.2", "time": 1759990000000, "dir": "Close Long"},
]
META = [{"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{"markPx": "3050.0"}, {"markPx": "61000.0"}]]


@pytest.fixture
def gw(monkeypatch):
    g = HyperliquidGateway("0x" + "aa" * 20)
    def fake_info(payload, api=None):
        return {"clearinghouseState": CLEARINGHOUSE,
                "userFills": FILLS,
                "metaAndAssetCtxs": META}[payload["type"]]
    from app.exec import hyperliquid_gateway as mod
    monkeypatch.setattr(mod, "_hl_info", fake_info)
    return g


def test_rejects_malformed_address():
    with pytest.raises(ValueError):
        HyperliquidGateway("not-an-address")


def test_no_order_submission_path_exists():
    g = HyperliquidGateway("0x" + "bb" * 20)
    assert not hasattr(g, "submit")
    assert not hasattr(g, "order")
    assert not hasattr(g, "place")


def test_positions_and_marks(gw):
    positions = gw.positions()
    assert positions == [{
        "coin": "ETH", "szi": 0.5, "entry_px": 3000.0, "mark_px": 3050.0,
        "unrealized_pnl": 12.5, "leverage": "3", "liquidation_px": "2100.0"}]


def test_snapshot_reconciliation_fields(gw):
    snap = gw.snapshot()
    assert snap["account_value"] == "1234.56"
    assert snap["positions"][0]["coin"] == "ETH"
    assert len(snap["recent_fills"]) == 2
    assert snap["recent_fills"][0]["coin"] == "ETH"   # newest first

def test_env_override_sets_hl_account(monkeypatch):
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "cc" * 20)
    from app.core.config import load_config
    cfg = load_config()
    assert cfg.hl_account == "0x" + "cc" * 20
