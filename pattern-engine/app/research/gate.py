"""Rule-graded promotion gate — the Evaluator. Code decides; LLM never grades.

Two windows:
  - research window (long Yahoo proxy history): the full statistical gate
  - confirmation window (HL-native recent data): directional check on the
    instrument we'd actually trade

Promotion requires passing BOTH plus non-domination vs live siblings.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

GATE = {
    "min_pf": 1.25,
    "min_avg_r": 0.10,
    "max_dd_r": 15.0,       # in R units on the closed-trade equity curve
    "min_trades": 30,
    "folds": 5,
    # Walk-forward policy targets one-epoch wonders, not uniform epochs:
    # the verified SPY incumbent's fold avgR is [0.11, 0.16, 0.09, -0.05, 1.04]
    # — epoch-lumpy by nature ("trend followers have miserable years").
    # Require edge in >=3 folds and no single fold > 80% of gross positive R.
    "min_folds_positive": 3,
    "max_fold_share": 0.80,
}

CONFIRM_MIN_TRADES = 10


@dataclass
class Grade:
    promoted: bool
    stats: dict
    failures: list[str]

    def __str__(self) -> str:
        verdict = "PROMOTED" if self.promoted else "rejected"
        return f"{verdict}: {self.stats} failures={self.failures}"


def _max_dd_r(rs: pd.Series) -> float:
    eq = rs.cumsum()
    return float((eq - eq.cummax()).min()) if len(eq) else 0.0


def evaluate(trades: list[dict]) -> Grade:
    failures: list[str] = []
    if len(trades) < GATE["min_trades"]:
        failures.append(f"n={len(trades)} < {GATE['min_trades']}")
        return Grade(False, {"n": len(trades)}, failures)
    rs = pd.Series([t["r"] for t in trades])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    avg_r = float(rs.mean())
    dd = _max_dd_r(rs)
    stats = {"n": len(trades), "pf": round(pf, 2), "avg_r": round(avg_r, 3),
             "max_dd_r": round(dd, 1)}
    if pf < GATE["min_pf"]:
        failures.append(f"pf={pf:.2f} < {GATE['min_pf']}")
    if avg_r < GATE["min_avg_r"]:
        failures.append(f"avg_r={avg_r:.3f} < {GATE['min_avg_r']}")
    if abs(dd) > GATE["max_dd_r"]:
        failures.append(f"max_dd_r={dd:.1f} exceeds {GATE['max_dd_r']}")
    return Grade(not failures, stats, failures)


def evaluate_walkforward(trades: list[dict]) -> Grade:
    """Full gate: overall stats + per-fold OOS consistency."""
    base = evaluate(trades)
    if not base.promoted:
        return base
    if not trades:
        return Grade(False, base.stats, ["no trades"])
    entries = pd.Series([t["entry_ts"] for t in trades])
    fold_labels = pd.qcut(entries.rank(method="first"), GATE["folds"],
                          labels=False)
    fold_avgs = [pd.Series([t["r"] for t, f in zip(trades, fold_labels)
                            if f == k]).mean() for k in range(GATE["folds"])]
    positive_folds = sum(1 for a in fold_avgs if a > 0)
    fold_r = [sum(t["r"] for t, f in zip(trades, fold_labels) if f == k)
              for k in range(GATE["folds"])]
    gross_positive = sum(r for r in fold_r if r > 0)
    best_share = (max(fold_r) / gross_positive) if gross_positive > 0 else 1.0
    failures = list(base.failures)
    if positive_folds < GATE["min_folds_positive"]:
        failures.append(
            f"positive folds={positive_folds} < {GATE['min_folds_positive']}")
    if best_share > GATE["max_fold_share"]:
        failures.append(
            f"best fold carries {best_share:.0%} of gross positive R "
            f"> {GATE['max_fold_share']:.0%} (one-epoch wonder)")
    stats = {**base.stats,
             "fold_avg_r": [round(float(a), 3) for a in fold_avgs]}
    return Grade(not failures, stats, failures)


def evaluate_confirm(trades: list[dict]) -> tuple[bool, dict]:
    """Confirmation window (HL-native recent data): the sample is inherently
    small (~3y daily), so the bar is directional, not statistical — the full
    gate ran on the long research window; this asks only 'does the edge also
    hold, weakly, on the instrument we'd actually trade'."""
    n = len(trades)
    avg_r = sum(t["r"] for t in trades) / n if n else 0.0
    ok = n >= CONFIRM_MIN_TRADES and avg_r > 0
    return ok, {"confirm_n": n, "confirm_avg_r": round(avg_r, 3)}
