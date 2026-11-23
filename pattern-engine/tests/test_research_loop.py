"""Research-loop tests with a scripted LLM: the LLM proposes, code decides.

Covers: fence-tolerant parsing, gate accept/reject, clamping to parameter
bounds, LoopAgent promote-escalation, and registry persistence.
"""

from pathlib import Path

import pandas as pd
import pytest
from google.adk.models.base_llm import BaseLlm
from google.genai import types as gtypes
from pydantic import ConfigDict, Field

from app.core import backtest
from app.core.indicators import with_indicators
from app.core.registry import Registry
from app.research import gate
from app.research.agents import SweepAndGrade, _clamp
from app.research.extract import extract_json
from app.strategies import pullback_resumption
from app.strategies.seeds import seed_specs

FIXTURE = Path(__file__).parents[1] / "tests/fixtures/spy_daily.parquet"


class ScriptedLlm(BaseLlm):
    """Stands in for ds41: returns queued text, then 'done' forever."""

    model_config = ConfigDict(extra="allow")
    model: str = "scripted"
    replies: list = Field(default_factory=list)
    cursor: int = 0

    async def generate_content_async(self, llm_request, **kwargs):
        from google.adk.models.llm_response import LlmResponse
        text = (self.replies[self.cursor]
                if self.cursor < len(self.replies) else "done")
        self.cursor += 1
        yield LlmResponse(content=gtypes.Content(
            role="model", parts=[gtypes.Part(text=text)]))


def test_extract_json_fences_and_prose():
    assert extract_json('junk ```json\n{"a": 1}\n``` more') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": "x}y"}} here') == {"a": {"b": "x}y"}}
    assert extract_json("no json at all") is None
    assert extract_json("") is None


def test_clamp_bounds():
    out = _clamp({"adx_min": 99, "window": 1, "target_mult": 50,
                  "rogue_key": 1, "time_stop_bars": 9})
    assert out["adx_min"] == 45.0
    assert out["window"] == 3
    assert out["target_mult"] == 5.0
    assert out["time_stop_bars"] == 9
    assert "rogue_key" not in out
    assert out["trail_mult"] is None


def test_gate_rejects_small_sample():
    verdict = gate.evaluate([{"r": 1.0}] * 10)
    assert not verdict.promoted
    assert any("n=" in f for f in verdict.failures)


def test_gate_requires_fold_consistency():
    # 40 strong winners all in one epoch -> folds inconsistent or fails gate
    trades = ([{"r": 2.0, "entry_ts": pd.Timestamp("2020-01-01")}] * 35
              + [{"r": -1.0, "entry_ts": pd.Timestamp("2021-01-01")}] * 5)
    verdict = gate.evaluate_walkforward(trades)
    stats = verdict.stats
    assert stats["pf"] > 1.25
    assert "fold_avg_r" in stats


def test_incumbent_passes_gate():
    df = with_indicators(pd.read_parquet(FIXTURE))
    trades = backtest.run(df, pullback_resumption.signal, target_mult=3.0,
                          trail_mult=None, time_stop_bars=10)
    verdict = gate.evaluate_walkforward(trades)
    assert verdict.promoted, verdict.failures



def test_gate_rejects_one_epoch_wonder():
    # PF and avgR fine, but ~90% of gross positive R sits in one fold.
    trades = ([{"r": 0.05, "entry_ts": pd.Timestamp("2000-01-01")}] * 34
              + [{"r": 20.0, "entry_ts": pd.Timestamp("2024-01-01")}]
              + [{"r": -1.0, "entry_ts": pd.Timestamp("2024-06-01")}] * 3)
    verdict = gate.evaluate_walkforward(trades)
    assert not verdict.promoted
    assert any("one-epoch" in f for f in verdict.failures)

@pytest.fixture
def df():
    return with_indicators(pd.read_parquet(FIXTURE))


async def test_sweep_and_grade_promotes_when_undominated(tmp_path, df):
    proposal = ('Here you go:\n```json\n{"id": "pb_long_v2", '
                '"rationale": "slightly stricter trend filter", '
                '"params": {"adx_min": 32, "pullback_mult": 0.2, "window": 5, '
                '"target_mult": 3.5, "trail_mult": null, "time_stop_bars": 10}}\n```')
    registry = Registry(tmp_path / "registry.db")   # empty: no incumbent
    sg = SweepAndGrade(registry, df)
    escalations = []
    async for event in _run_agent(sg, {"proposal": proposal}):
        if event.actions and event.actions.escalate:
            escalations.append(event)
    assert escalations, "undominated promotion must escalate (stop the loop)"
    row = registry.conn.execute(
        "SELECT status FROM strategies WHERE id='pb_long_v2'").fetchone()
    assert row and row[0] == "live"


async def test_sweep_and_grade_dominated_variant_stays_candidate(tmp_path, df):
    proposal = ('{"id": "pb_long_v3", "rationale": "worse on purpose", '
                '"params": {"adx_min": 32, "pullback_mult": 0.2, "window": 5, '
                '"target_mult": 3.5, "trail_mult": null, "time_stop_bars": 10}}')
    registry = Registry(tmp_path / "registry.db")
    from app.core.registry import StrategySpec
    registry.upsert(StrategySpec(            # unbeatable incumbent
        id="pb_long_king", status="live", stats={"avg_r": 9.99}))
    sg = SweepAndGrade(registry, df)
    escalated = False
    async for event in _run_agent(sg, {"proposal": proposal}):
        if event.actions and event.actions.escalate:
            escalated = True
    assert not escalated, "dominated variant must not escalate"
    row = registry.conn.execute(
        "SELECT status FROM strategies WHERE id='pb_long_v3'").fetchone()
    assert row and row[0] == "candidate"

async def test_full_loop_scripted_llm_proposes_and_code_grades(tmp_path, df):
    """End-to-end LoopAgent: ScriptedLlm proposes, engine sweeps, gate rules."""
    from google.adk.agents import LlmAgent, LoopAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    registry = Registry(tmp_path / "registry.db")
    for spec in seed_specs():
        registry.upsert(spec)
    proposer = LlmAgent(
        name="proposer", model=ScriptedLlm(replies=[
            ('```json\n{"id": "pb_v_scripted", "rationale": "tighter adx", '
            '"params": {"adx_min": 33, "pullback_mult": 0.22, "window": 4, '
            '"target_mult": 3.2, "trail_mult": null, "time_stop_bars": 9}}\n```')
        ]),
        description="propose", instruction="propose {registry_state}",
        output_key="proposal",
        disallow_transfer_to_parent=True, disallow_transfer_to_peers=True)
    loop = LoopAgent(name="rl", sub_agents=[proposer,
                                            SweepAndGrade(registry, df)],
                     max_iterations=2)
    svc = InMemorySessionService()
    session = await svc.create_session(
        app_name="t", user_id="u",
        state={"registry_state": registry.as_dicts()})
    runner = Runner(agent=loop, app_name="t", session_service=svc)
    graded = False
    async for event in runner.run_async(
            user_id="u", session_id=session.id,
            new_message=gtypes.Content(role="user",
                                       parts=[gtypes.Part(text="research")])):
        if (event.author == "sweep_and_grade" and event.actions
                and event.actions.state_delta
                and event.actions.state_delta.get("grade")):
            graded = True
    assert graded, "sweep_and_grade must have run and graded"
    row = registry.conn.execute(
        "SELECT id, status FROM strategies WHERE id='pb_v_scripted'").fetchone()
    assert row and row[1] in ("candidate", "live")

async def test_sweep_and_grade_unparseable_keeps_loop_alive(tmp_path, df):
    registry = Registry(tmp_path / "registry.db")
    sg = SweepAndGrade(registry, df)
    events = [e async for e in _run_agent(sg, {"proposal": "no json here"})]
    assert events and not any(e.actions and e.actions.escalate for e in events)


async def _run_agent(agent, state: dict):
    """Drives a BaseAgent directly through an in-memory session."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    svc = InMemorySessionService()
    session = await svc.create_session(app_name="t", user_id="u", state=state)
    runner = Runner(agent=agent, app_name="t", session_service=svc)
    async for event in runner.run_async(
            user_id="u", session_id=session.id,
            new_message=gtypes.Content(role="user",
                                       parts=[gtypes.Part(text="go")])):
        yield event
