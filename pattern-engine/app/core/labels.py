"""ExcursionLabeler — MAE/MFE statistics per trade.

"How much should it back up against me if my entry is correct… a statistical
measurement of it should not go beyond this point if it's going to be a good
trade" (1:09:16-1:09:31).
"""

from __future__ import annotations

import pandas as pd


def excursions(df: pd.DataFrame, trades: list[dict]) -> list[dict]:
    """Per closed trade: maximum adverse/favorable excursion, in R units."""
    out: list[dict] = []
    for t in trades:
        i = int(df.index.get_loc(pd.Timestamp(t["entry_ts"])))
        j = int(df.index.get_loc(pd.Timestamp(t["exit_ts"])))
        seg = df.iloc[i + 1:j + 1]          # path after the signal-bar entry
        if len(seg) == 0:
            out.append({**t, "mae_r": 0.0, "mfe_r": 0.0})
            continue
        adverse = (t["entry"] - seg["Low"]).max()
        favorable = (seg["High"] - t["entry"]).max()
        out.append({**t,
                    "mae_r": float(adverse / t["stop_dist"]),
                    "mfe_r": float(favorable / t["stop_dist"])})
    return out


def good_trade_bound(labeled: list[dict]) -> float | None:
    """Largest adverse excursion survived by an eventual winner: beyond this,
    history says the trade was not a good one."""
    winners = [x["mae_r"] for x in labeled if x["r"] > 0]
    return max(winners) if winners else None


def label_report(labeled: list[dict]) -> dict:
    df = pd.DataFrame(labeled)
    if df.empty:
        return {"n": 0}
    return {
        "n": len(df),
        "winner_mae_max_r": round(float(df[df.r > 0].mae_r.max()), 3),
        "loser_mae_median_r": round(float(df[df.r <= 0].mae_r.median()), 3),
        "winner_mfe_median_r": round(float(df[df.r > 0].mfe_r.median()), 3),
        "loser_mfe_median_r": round(float(df[df.r <= 0].mfe_r.median()), 3),
    }
