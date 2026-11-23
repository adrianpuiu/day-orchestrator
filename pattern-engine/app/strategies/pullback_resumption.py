"""Seed strategy: pullback-resumption in a strong trend (long-only).

"Best risk/reward in the book" (0:42): higher-timeframe trend + pullback,
resumption bar confirms. Exact rules of the verified SPY backtest
(raschke-strategy/strategy.py, B/C long-only variants).
"""

from __future__ import annotations

import pandas as pd

from ..core.types import Signal


def signal(df: pd.DataFrame, i: int, strategy_id: str = "pullback_resumption_long",
           target_mult: float | None = 3.0, trail_mult: float | None = None,
           time_stop_bars: int = 10, adx_min: float = 30.0,
           pullback_mult: float = 0.25, window: int = 5,
           ema_slope_bars: int = 10) -> Signal | None:
    """Evaluates the completed bar at integer index i (no lookahead).

    Parameter space for the research loop (defaults = the verified variant):
    adx_min in [25, 45], pullback_mult in [0.1, 0.5], window in [3, 7].
    """
    if i < max(15, window):
        return None
    row = df.iloc[i]
    O, H, L, C = (df[k].values for k in ("Open", "High", "Low", "Close"))
    pulled_back = (min(L[max(i - (window - 1), 0):i + 1])
                   <= row.ema + pullback_mult * row.atr)
    long_sig = (row.adx > adx_min
                and df.ema.iat[i] > df.ema.iat[max(i - ema_slope_bars, 0)]
                and pulled_back
                and C[i] > max(H[i - 1], O[i])
                and C[i] > row.ema)
    if not long_sig:
        return None
    entry = float(C[i])
    stop = entry - row.atr                      # 1-ATR stop (unit risk)
    if entry - stop > 2.5 * row.atr:            # defensive; cannot trigger at 1 ATR
        return None
    target = None if target_mult is None else entry + target_mult * row.atr
    return Signal(
        strategy_id=strategy_id, ts=df.index[i], side=1,
        entry=entry, stop=float(stop), target=float(target) if target else None,
        trail_mult=trail_mult, time_stop_bars=time_stop_bars, stop_dist=float(row.atr),
        reason=(f"adx={row.adx:.1f}>{adx_min} ema rising, pullback touched "
                f"ema+{pullback_mult * row.atr:.2f}, resumption close {entry:.2f} "
                f"> prev high {H[i - 1]:.2f}"),
    )
