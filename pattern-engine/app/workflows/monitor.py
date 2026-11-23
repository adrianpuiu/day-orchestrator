"""PatternMonitor — the ADK trading plane. Zero LLM calls.

"An agent that can work a particular pattern for me" (0:26:15): each live
strategy in the registry is a pattern worker; this BaseAgent drives them all
over completed bars, routes signals through RiskManager + PaperGateway, and
journals closes. One shared code path serves live runs (newest completed bar)
and replay (whole history) — engine parity is tested, not hoped for.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import field

import pandas as pd
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types as gtypes
from pydantic import ConfigDict

from ..core import backtest, labels, market
from ..core.indicators import with_indicators
from ..core.registry import Registry
from ..core.types import QualityReport
from ..exec.paper import PaperGateway
from ..exec.risk import RiskManager
from ..strategies import seeds


class PatternMonitor(BaseAgent):
    registry: Registry
    gateway: PaperGateway
    risk: RiskManager
    df: pd.DataFrame
    cursor: int = 0            # index of the next bar to process
    journal: list = field(default_factory=list)
    last_quality: QualityReport | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, name: str, registry: Registry, gateway: PaperGateway,
                 risk: RiskManager, df: pd.DataFrame) -> None:
        super().__init__(name=name, registry=registry, gateway=gateway,
                         risk=risk, df=with_indicators(df))
        self.journal = []

    # ---- bar processing (shared by live + replay) ------------------------

    def process_bar(self, i: int) -> list[dict]:
        """Advances to completed bar i: manage open position, then evaluate
        new signals on this bar's close."""
        bar = self.df.iloc[i]
        self.gateway.on_bar(bar.name, float(bar.Open), float(bar.High),
                            float(bar.Low), float(bar.Close))
        newly_closed: list[dict] = []
        if len(self.gateway.closed) > len(self.journal):
            newly_closed = self.gateway.closed[len(self.journal):]
            self.journal = list(self.gateway.closed)
        # Engine semantics: i = j + 1 after an exit — no new signal on the
        # bar that closed a position (conservative, matches verified edge).
        if self.gateway.flat and not newly_closed and self.risk.allows_entry(
                self.df.index[i].date()):
            for spec in self.registry.live_specs():
                sig = seeds.evaluate(spec, self.df, i)
                if sig is None:
                    continue
                qty = self.risk.size(sig.stop_dist)
                if qty > 0:
                    self.gateway.submit(sig, qty)
                    break               # one position at a time (all-in)
        self.cursor = i + 1
        return newly_closed

    # ---- drivers ---------------------------------------------------------

    def replay(self, start: int = 15, end: int | None = None) -> list[dict]:
        end = len(self.df) if end is None else end
        for i in range(start, end):
            self.process_bar(i)
        return list(self.gateway.closed)

    async def run_once(self) -> str:
        """Live step: ingest latest EOD, quality-gate, process newest bar."""
        report = market.quality_gate(self.df)
        self.last_quality = report
        if not report.ok:
            self.risk.note_missed_run()
            return (f"data quality FAILED ({'; '.join(report.problems)}); "
                    f"process breaker at missed_runs={self.risk.missed_runs}")
        self.risk.note_clean_run()
        self.process_bar(len(self.df) - 1)
        self._refresh_stats()
        return (f"bar {self.df.index[-1].date()} processed; "
                f"closed={len(self.gateway.closed)} open="
                f"{0 if self.gateway.flat else 1}")

    def _refresh_stats(self) -> None:
        if not self.gateway.closed:
            return
        by_id: dict[str, list[dict]] = {}
        for t in self.gateway.closed:
            by_id.setdefault(t["strategy_id"], []).append(t)
        for sid, trades in by_id.items():
            self.registry.update_stats(sid, backtest.summarize(trades))

    # ---- ADK BaseAgent ---------------------------------------------------

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        summary = await self.run_once()
        ctx.session.state["journal_size"] = len(self.gateway.closed)
        ctx.session.state["flat"] = self.gateway.flat
        ctx.session.state["cursor"] = self.cursor
        yield Event(author=self.name, content=gtypes.Content(
            role="agent", parts=[gtypes.Part(text=summary)]))


def excursion_report_for(monitor: PatternMonitor) -> dict:
    labeled = labels.excursions(monitor.df, monitor.gateway.closed)
    return labels.label_report(labeled)
