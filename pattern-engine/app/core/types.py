"""Shared value types (layer 0: imported by strategies/exec/workflows)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Signal:
    """A quantified pattern firing — produced by code, never an LLM."""

    strategy_id: str
    ts: object                 # pandas Timestamp of the completed signal bar
    side: int                  # +1 long / -1 short
    entry: float
    stop: float
    target: float | None       # None + trail_mult -> trailing exit
    trail_mult: float | None   # ATR multiple for Wilder-style trail
    time_stop_bars: int
    stop_dist: float           # |entry - stop|: the R denominator
    reason: str                # human-readable rule trace


@dataclass(frozen=True)
class Fill:
    kind: str                  # "entry" | "stop" | "target" | "trail" | "time"
    ts: object
    price: float
    qty: float
    reason: str = ""

@dataclass
class Position:
    signal: Signal
    qty: float = 0.0
    entry_fill: Fill | None = None
    extreme: float = 0.0       # highest high (long) / lowest low (short) since entry
    bars_held: int = 0
    stop: float = 0.0          # current (possibly trailed) stop
    trail_atr: float = 0.0     # ATR frozen at signal for the Wilder trail

@dataclass(frozen=True)
class QualityReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    bars_checked: int = 0
