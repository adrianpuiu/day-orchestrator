"""PaperGateway — position state machine with the verified engine's fill rules.

The gateway and core.backtest are two independent implementations of the same
conservative semantics; tests lock them equal. Engine semantics (which this
must reproduce):
  - entry fills at the signal bar's close (market-on-close, implementable live)
  - exits are evaluated from the NEXT bar; the entry bar never exits
  - a bar touching both stop and target counts as stop (conservative)
  - gap through the stop fills at the open
  - target-mode: time stop exits at the close of bar entry+t_stop_bars
  - trail-mode: Wilder trail (2.5 ATR from extreme, ATR frozen at signal),
    NO time stop — engine C variant runs until trailed out
"""

from __future__ import annotations

from ..core.types import Fill, Position, Signal


class PaperGateway:
    def __init__(self, cost_bp_side: float = 0.5) -> None:
        self.cost_bp_side = cost_bp_side
        self.position: Position | None = None
        self.fills: list[Fill] = []
        self.closed: list[dict] = []

    @property
    def flat(self) -> bool:
        return self.position is None

    def submit(self, sig: Signal, qty: float) -> Fill | None:
        """Fills immediately at the signal close (one position at a time)."""
        if self.position is not None:
            return None
        pos = Position(signal=sig, qty=qty, extreme=sig.entry, stop=sig.stop,
                       trail_atr=sig.stop_dist if sig.trail_mult is not None else 0.0)
        fill = Fill(kind="entry", ts=sig.ts, price=sig.entry, qty=qty,
                    reason=sig.reason)
        pos.entry_fill = fill
        self.position = pos
        self.fills.append(fill)
        return fill

    def _exit(self, ts, price: float, kind: str) -> Fill:
        pos = self.position
        assert pos is not None and pos.entry_fill is not None
        fill = Fill(kind=kind, ts=ts, price=price, qty=pos.qty)
        self.fills.append(fill)
        gross = pos.signal.side * (price - pos.entry_fill.price)
        cost_usd = 2 * self.cost_bp_side * 1e-4 * pos.entry_fill.price * pos.qty
        self.closed.append({
            "strategy_id": pos.signal.strategy_id,
            "entry_ts": pos.entry_fill.ts, "exit_ts": ts, "side": pos.signal.side,
            "entry": pos.entry_fill.price, "exit": price,
            "stop_dist": pos.signal.stop_dist,
            "r": float(gross / pos.signal.stop_dist),   # gross R (engine parity)
            "pnl_usd": float(gross * pos.qty - cost_usd),
            "cost_usd": round(cost_usd, 2),
            "qty": pos.qty,
            "exit_kind": kind,
        })
        self.position = None
        return fill

    def on_bar(self, ts, open_: float, high: float, low: float,
               close: float) -> list[Fill]:
        """Advances one completed bar after entry. Returns fills produced."""
        pos = self.position
        if pos is None or pos.entry_fill is None:
            return []
        sig = pos.signal
        out: list[Fill] = []
        pos.bars_held += 1
        if sig.side == 1:
            pos.extreme = max(pos.extreme, high)
            if sig.trail_mult is not None:
                pos.stop = max(pos.stop, pos.extreme - sig.trail_mult * pos.trail_atr)
            if low <= pos.stop:  # stop first (conservative), gap fills at open
                kind = "trail" if sig.trail_mult is not None else "stop"
                out.append(self._exit(ts, min(open_, pos.stop), kind))
                return out
            if sig.target is not None and high >= sig.target:
                out.append(self._exit(ts, max(open_, sig.target), "target"))
                return out
            if sig.trail_mult is None and pos.bars_held >= sig.time_stop_bars:
                out.append(self._exit(ts, close, "time"))
        return out
