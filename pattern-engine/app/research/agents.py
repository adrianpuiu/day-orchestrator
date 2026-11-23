"""Research plane (Phase 3): the strategy-regeneration loop.

"Constantly coming up with new strategies" (1:17:34) with LLMs as
components only (1:15:18 "throw ChatGPT and Claude out the window… use parts
of them for a component"):

  VariantProposer  (LlmAgent, ds41 via LiteLLM) — reads registry stats,
                     proposes ONE bounded parameter variant as strict JSON.
  SweepAndGrade    (BaseAgent, pure code) — fence-tolerant parse, verified
                     engine walk-forward, rule-graded gate (research.gate),
                     registry upsert. Escalates on promotion (stops the loop).

The LLM never grades, never sizes, never trades.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from pydantic import PrivateAttr

from ..core import backtest
from ..core.config import Config
from ..core.models import resolve_model
from ..core.registry import Registry, StrategySpec
from ..strategies import pullback_resumption
from . import gate
from .extract import extract_json

PARAM_BOUNDS = {
    "adx_min": (25.0, 45.0),
    "pullback_mult": (0.10, 0.50),
    "window": (3, 7),
    "target_mult": (2.0, 5.0),
    "time_stop_bars": (5, 15),
}
ALLOWED_KEYS = set(PARAM_BOUNDS) | {"trail_mult"}


def _clamp(params: dict) -> dict:
    out: dict = {}
    for key, value in params.items():
        if key not in ALLOWED_KEYS or value is None:
            continue              # nulls (LLMs emit them) fall back to defaults
        if key == "trail_mult":
            out[key] = min(max(float(value), 2.0), 3.5)
            continue
        lo, hi = PARAM_BOUNDS[key]
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue              # non-numeric: drop, defaults apply
        if math.isnan(v) or math.isinf(v):
            continue              # NaN/inf: drop
        out[key] = type(lo)(min(max(v, lo), hi))
    if "target_mult" not in out and "trail_mult" not in out:
        out["target_mult"] = 3.0
    out.setdefault("trail_mult", None)
    return out


PROPOSER_INSTRUCTION = """You are a strategy-research assistant. You see the
performance stats of live trading strategies and must propose exactly ONE
variant to test next: a small perturbation of the parameter space, never a
new paradigm.

Parameter bounds: adx_min 25-45, pullback_mult 0.10-0.50, window 3-7,
target_mult 2-5 (or trail_mult 2.0-3.5 instead of target_mult),
time_stop_bars 5-15. Timeframe: "d" (daily) or "8h" (intraday, HL-native —
only meaningful for crypto-style 24/7 markets).

Output ONLY a JSON object, no markdown fences, no prose:
{"id": "pullback_resumption_long_v2", "rationale": "one sentence",
 "timeframe": "d", "params": {"adx_min": 32, "pullback_mult": 0.2, "window": 5,
            "target_mult": 3.5, "trail_mult": null, "time_stop_bars": 10}}

Live strategies and their stats:
{registry_state}
"""


class VariantProposer(Agent):
    """LlmAgent: grunt work only — turns registry stats into one variant."""

    def __init__(self, cfg: Config, name: str = "variant_proposer") -> None:
        super().__init__(
            name=name,
            model=resolve_model(cfg),
            description="Proposes one bounded strategy variant as JSON.",
            instruction=PROPOSER_INSTRUCTION,
            output_key="proposal",
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
        )


class SweepAndGrade(BaseAgent):
    """Pure code: parse proposal, sweep the verified engine, apply the
    dual-window gate (long research history + HL-native confirmation)."""

    _registry: Registry = PrivateAttr()
    # windows: {"d": {"research": df_long, "confirm": df_hl},
    #          "8h": {...}} — only present timeframes are sweepable.
    _windows: dict = PrivateAttr()
    _market: str = PrivateAttr()

    def __init__(self, registry: Registry, windows: dict,
                 name: str = "sweep_and_grade", market: str = "SPY") -> None:
        super().__init__(name=name)
        self._registry = registry
        self._windows = windows
        self._market = market

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        proposal_raw = ctx.session.state.get("proposal", "")
        proposal = extract_json(proposal_raw if isinstance(proposal_raw, str)
                                else str(proposal_raw))
        if proposal is None or "params" not in proposal:
            yield Event(
                author=self.name,
                content=_text("proposal unparseable; retrying"),
                actions=EventActions(state_delta={"grade": "unparseable proposal"}))
            return
        params = _clamp(proposal.get("params", {}))
        spec_id = str(proposal.get("id", "candidate_v1"))[:80]
        tf = str(proposal.get("timeframe", "d"))
        tf_key = "d" if tf in ("d", "1d", "daily") else tf
        if tf_key not in self._windows:         # unknown -> default daily
            tf_key = "d"
        tf_store = "1d" if tf_key == "d" else tf_key   # registry vocabulary
        win = self._windows[tf_key]
        trades = await asyncio.to_thread(
            backtest.run, win["research"], pullback_resumption.signal, **params)
        verdict = gate.evaluate_walkforward(trades)
        confirm_stats: dict = {}
        confirm_ok = True
        if verdict.promoted:
            confirm_trades = await asyncio.to_thread(
                backtest.run, win["confirm"], pullback_resumption.signal,
                **params)
            confirm_ok, confirm_stats = gate.evaluate_confirm(confirm_trades)
        # Promotion policy: pass the research gate AND the confirmation window
        # AND not be dominated by a live sibling (same market+timeframe) with
        # higher avg_r — a worse variant must not steal trades (first-signal-wins).
        siblings = [s for s in self._registry.live_specs()
                    if s.signal_fn == "pullback_resumption.signal"
                    and s.market == self._market and s.timeframe == tf_store]
        dominated = any((s.stats.get("avg_r") or -9.9)
                        > verdict.stats.get("avg_r", -9.9) for s in siblings)
        status = ("live" if (verdict.promoted and confirm_ok and not dominated)
                  else "candidate")
        self._registry.upsert(StrategySpec(
            id=spec_id, market=self._market, timeframe=tf_store,
            params=params, status=status,
            stats={**verdict.stats, **confirm_stats,
                   "rationale": str(proposal.get("rationale", ""))[:200]}))
        grade_text = (f"{verdict} | confirm={confirm_stats or 'skipped'} "
                      f"| timeframe={tf_store} "
                      f"| promotion={'live' if status == 'live' else 'no'}"
                      f"{' (dominated by incumbent)' if dominated else ''}")
        delta = {"grade": grade_text, "last_spec_id": spec_id}
        if status == "live":
            yield Event(author=self.name, content=_text(grade_text),
                        actions=EventActions(state_delta=delta, escalate=True))
        else:
            yield Event(author=self.name, content=_text(grade_text),
                        actions=EventActions(state_delta=delta))


def _text(s: str):
    from google.genai import types as gtypes
    return gtypes.Content(role="agent", parts=[gtypes.Part(text=s)])


def build_research_loop(cfg: Config, registry: Registry,
                        windows: dict, max_iterations: int = 3,
                        market: str = "SPY") -> LoopAgent:
    return LoopAgent(
        name="research_loop",
        sub_agents=[VariantProposer(cfg),
                    SweepAndGrade(registry, windows, market=market)],
        max_iterations=max_iterations,
    )
