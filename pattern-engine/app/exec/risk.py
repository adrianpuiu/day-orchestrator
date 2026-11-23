"""RiskManager — all deterministic, no LLM anywhere in this path.

Her rules (DESIGN.md traceability):
  - even bet size: risk a fixed fraction per trade, unit per $100k (0:43)
  - process degradation (data quality, missed runs) -> cut size (0:48:43)
  - system drawdown within binomial bounds -> hold size (0:49:34)
  - session/news blackout windows (1:09:00) — pluggable calendar,
    conservative default: never black out (no calendar source wired yet)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class BlackoutCalendar:
    """Pluggable news/macro blackout dates. Default: none (documented choice —
    wire a real economic-calendar source before live trading)."""

    dates: set[date] = field(default_factory=set)

    def is_blackout(self, d: date) -> bool:
        return d in self.dates


@dataclass
class RiskManager:
    unit_equity_usd: float = 100_000.0
    risk_frac_per_trade: float = 0.01
    missed_run_limit: int = 2
    calendar: BlackoutCalendar = field(default_factory=BlackoutCalendar)

    missed_runs: int = 0
    degraded: bool = False

    def note_missed_run(self) -> None:
        self.missed_runs += 1
        if self.missed_runs > self.missed_run_limit:
            self.degraded = True

    def note_clean_run(self) -> None:
        self.missed_runs = 0
        self.degraded = False

    def allows_entry(self, d: date) -> bool:
        return not self.calendar.is_blackout(d)

    def size_multiplier(self) -> float:
        """Process degradation halves size; expectancy drawdown deliberately
        does NOT (system school: 'ready to spring')."""
        return 0.5 if self.degraded else 1.0

    def size(self, stop_dist: float) -> float:
        """Shares/contracts so that |entry-stop| risks risk_frac × unit equity,
        times the process multiplier. Even bet: independent of conviction."""
        if stop_dist <= 0:
            return 0.0
        base = self.risk_frac_per_trade * self.unit_equity_usd / stop_dist
        return base * self.size_multiplier()
