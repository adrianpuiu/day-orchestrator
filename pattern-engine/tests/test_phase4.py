"""Phase 4 tests: journal persistence, decay watcher, report, scheduler jobs."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.core.config import Config
from app.core.journal import JournalStore
from app.core.registry import Registry, StrategySpec
from app.core.report import write_report
from app.research.decay import evaluate_decay, get_streak
from app.strategies.seeds import seed_specs

FIXTURE = Path(__file__).parents[1] / "tests/fixtures/spy_daily.parquet"


@pytest.fixture
def df():
    return pd.read_parquet(FIXTURE)


def test_journal_append_is_idempotent(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    trades = [{"strategy_id": "s1", "entry_ts": "2026-01-05 00:00:00",
               "exit_ts": "2026-01-09 00:00:00", "side": 1, "entry": 100.0,
               "exit": 103.0, "r": 0.9, "pnl_usd": 900.0,
               "exit_kind": "target"}]
    store.append(trades)
    store.append(trades)          # daemon restart replays: must not duplicate
    assert store.summary()["trades"] == 1
    assert store.recent()[0]["strategy_id"] == "s1"
    store.close()


def test_decay_two_strikes_retires(tmp_path, df):
    registry = Registry(tmp_path / "registry.db")
    # Tight pullback zone + loose ADX: verified to lose on the trailing
    # window (sweep: avg_r -0.08, n=23); premise re-asserted below.
    registry.upsert(StrategySpec(
        id="decay_victim", status="live",
        params={"target_mult": 3.0, "trail_mult": None, "time_stop_bars": 15,
                "adx_min": 25.0, "pullback_mult": 0.1, "window": 5}))
    first = evaluate_decay(registry, df)
    w = first["windows"]["decay_victim"]
    assert w["avg_r"] < 0, f"fixture premise changed: {w}"
    assert first["strikes"]["decay_victim"] == 1
    assert "decay_victim" not in first["retired"]
    assert get_streak(registry, "decay_victim") == 1
    second = evaluate_decay(registry, df)
    assert "decay_victim" in second["retired"]
    statuses = dict(registry.conn.execute(
        "SELECT id, status FROM strategies").fetchall())
    assert statuses["decay_victim"] == "retired"


def test_decay_healthy_or_quiet_never_retires(tmp_path, df):
    registry = Registry(tmp_path / "registry.db")
    for spec in seed_specs():
        registry.upsert(spec)
    result = evaluate_decay(registry, df)
    for spec in seed_specs():
        assert spec.id not in result["retired"]
        assert result["windows"][spec.id]["verdict"] in (
            "healthy",) or "no signal" in result["windows"][spec.id]["verdict"]


def test_report_written_with_sections(tmp_path):
    registry = Registry(tmp_path / "registry.db")
    for spec in seed_specs():
        registry.upsert(spec)
    journal = JournalStore(tmp_path / "journal.db")
    journal.append([{"strategy_id": "pullback_resumption_long_v1",
                     "entry_ts": "2026-01-05 00:00:00",
                     "exit_ts": "2026-01-09 00:00:00", "side": 1,
                     "entry": 100.0, "exit": 103.0, "r": 0.9,
                     "pnl_usd": 900.0, "exit_kind": "target"}])
    path = write_report(tmp_path / "reports", registry, journal,
                        decay={"windows": {"x": {"n": 5, "avg_r": 0.1,
                                                 "verdict": "healthy"}},
                               "retired": []},
                        open_position="flat")
    text = path.read_text()
    assert "pattern-engine report" in text
    assert "pullback_resumption_long_v1" in text
    assert "Recent trades" in text and "Decay watch" in text
    assert "open position: flat" in text
    journal.close()


def test_serve_registers_all_jobs(tmp_path):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import app.runtime as rt

    created: dict = {}

    class FakeScheduler(AsyncIOScheduler):
        def add_job(self, *_args, **kwargs):
            created[kwargs.get("id")] = kwargs

        def start(self):
            created["__started"] = True

    with patch("apscheduler.schedulers.asyncio.AsyncIOScheduler", FakeScheduler), \
            patch("asyncio.Event") as ev:
        ev.return_value.wait = AsyncMock(side_effect=asyncio.CancelledError)
        cfg = Config(db_dir=str(tmp_path / "var"))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(rt.run_serve(cfg))
    assert {"eod_run", "decay_run", "research_run", "report_run",
            "__started"} <= set(created)
