"""Seed strategies + signal-function resolution (strategies layer)."""

from __future__ import annotations

from ..core.registry import StrategySpec
from . import pullback_resumption

SIGNAL_FNS = {
    "pullback_resumption.signal": pullback_resumption.signal,
}


def evaluate(spec: StrategySpec, df, i: int):
    fn = SIGNAL_FNS[spec.signal_fn]
    return fn(df, i, strategy_id=spec.id, **spec.params)


def seed_specs() -> list[StrategySpec]:
    """Verified variants (1-ATR stop): B target/10-bar PF 1.40 avgR +0.27,
    C 2.5-ATR trail PF 1.45 avgR +0.30 on the locked fixture; cross-checked
    trade-for-trade against the original raschke-strategy engine."""
    return [
        StrategySpec(
            id="pullback_resumption_long_v1",
            params={"target_mult": 3.0, "trail_mult": None, "time_stop_bars": 10},
            status="live",
            stats={"n": 71, "pf": 1.40, "avg_r": 0.268},
        ),
        StrategySpec(
            id="pullback_resumption_long_trail_v1",
            params={"target_mult": None, "trail_mult": 2.5, "time_stop_bars": 10},
            status="live",
            stats={"n": 58, "pf": 1.45, "avg_r": 0.296},
        ),
    ]
