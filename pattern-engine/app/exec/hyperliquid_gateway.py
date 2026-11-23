"""HyperliquidGateway — shadow mode, strictly read-only.

Mirrors the real account via public `info` endpoints: an address is enough,
no key, no signing, no order path. The paper plane runs its signals and this
gateway answers "what does the account actually hold / what actually filled"
so the two can be reconciled every cycle. There is deliberately NO submit()
here: this class cannot trade, by construction.
"""

from __future__ import annotations

import time

import requests

from ..core.hyperliquid import HL_API


def _hl_info(payload: dict, api: str = HL_API) -> object:
    r = requests.post(f"{api}/info", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


class HyperliquidGateway:
    """Read-only account mirror (address-only auth)."""

    def __init__(self, address: str, api: str = HL_API) -> None:
        if not address or not address.startswith("0x") or len(address) != 42:
            raise ValueError(f"not a valid account address: {address!r}")
        self.address = address.lower()
        self.api = api

    # ---- account state ----------------------------------------------------

    def clearinghouse_state(self) -> dict:
        return _hl_info({"type": "clearinghouseState", "user": self.address},
                        self.api)   # type: ignore[return-value]

    def positions(self) -> list[dict]:
        """Open perp positions with mark prices attached."""
        state = self.clearinghouse_state()
        marks = self.marks()
        out: list[dict] = []
        for ap in state.get("assetPositions", []):
            p = ap.get("position", {})
            coin = p.get("coin", "?")
            out.append({
                "coin": coin,
                "szi": float(p.get("szi", 0) or 0),          # signed size
                "entry_px": float(p.get("entryPx") or 0),
                "mark_px": marks.get(coin),
                "unrealized_pnl": float(p.get("unrealizedPnl") or 0),
                "leverage": p.get("leverage", {}).get("value", "?"),
                "liquidation_px": p.get("liquidationPx"),
            })
        return out

    def fills(self, limit: int = 20) -> list[dict]:
        rows = _hl_info({"type": "userFills", "user": self.address}, self.api)
        if not isinstance(rows, list):
            return []
        rows = sorted(rows, key=lambda f: f.get("time", 0), reverse=True)
        return rows[:limit]

    def marks(self) -> dict[str, float]:
        meta = _hl_info({"type": "metaAndAssetCtxs"}, self.api)
        if not isinstance(meta, list) or len(meta) < 2:
            return {}
        universe, ctxs = meta[0]["universe"], meta[1]
        out: dict[str, float] = {}
        for i, asset in enumerate(universe):
            if i < len(ctxs) and ctxs[i].get("markPx"):
                out[asset["name"]] = float(ctxs[i]["markPx"])
        return out

    # ---- shadow artifact ---------------------------------------------------

    def snapshot(self) -> dict:
        """Everything a shadow report needs, one call."""
        state = self.clearinghouse_state()
        return {
            "address": self.address,
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "account_value": state.get("marginSummary", {}).get("accountValue"),
            "margin_summary": state.get("marginSummary", {}),
            "positions": self.positions(),
            "recent_fills": [
                {"coin": f.get("coin"), "side": f.get("side"),
                 "sz": f.get("sz"), "px": f.get("px"),
                 "closed_pnl": f.get("closedPnl"),
                 "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.gmtime(f.get("time", 0) / 1000)),
                 "dir": f.get("dir")}
                for f in self.fills()],
        }
