"""Parity lock: the live monitor path (PaperGateway + PatternMonitor) must
produce byte-identical trades to the backtest engine on the same history.
Two independent implementations of the same semantics — this test is the
anti-divergence contract for Phase 2."""

from pathlib import Path

import pandas as pd

from app.core import backtest
from app.core.indicators import with_indicators
from app.core.registry import Registry
from app.exec.paper import PaperGateway
from app.exec.risk import RiskManager
from app.strategies import pullback_resumption
from app.strategies.seeds import seed_specs
from app.workflows.monitor import PatternMonitor

FIXTURE = Path(__file__).parents[1] / "tests/fixtures/spy_daily.parquet"


def _registry(tmp_path, live_id: str) -> Registry:
    reg = Registry(tmp_path / "registry.db")
    for spec in seed_specs():
        status = "live" if spec.id == live_id else "retired"
        reg.conn.execute(
            "INSERT INTO strategies (id, market, timeframe, signal_fn, params,"
            " status, stats) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET status=excluded.status",
            (spec.id, spec.market, spec.timeframe, spec.signal_fn,
             __import__("json").dumps(spec.params), status,
             __import__("json").dumps(spec.stats)))
    reg.conn.commit()
    return reg


def _monitor(df, reg) -> PatternMonitor:
    return PatternMonitor(
        name="pe", registry=reg, gateway=PaperGateway(),
        risk=RiskManager(unit_equity_usd=100_000.0, risk_frac_per_trade=0.01),
        df=df)


def _assert_identical(engine_trades, gateway_closed):
    assert len(engine_trades) == len(gateway_closed), (
        f"trade count diverged: engine {len(engine_trades)} vs "
        f"gateway {len(gateway_closed)}")
    for i, (e, g) in enumerate(zip(engine_trades, gateway_closed)):
        for key in ("entry_ts", "exit_ts", "entry", "exit", "r"):
            assert e[key] == g[key], (
                f"trade {i} field {key!r}: engine {e[key]!r} vs gateway {g[key]!r}")


def test_parity_variant_b(tmp_path):
    df = with_indicators(pd.read_parquet(FIXTURE))
    engine = backtest.run(df, pullback_resumption.signal, target_mult=3.0,
                          trail_mult=None, time_stop_bars=10)
    mon = _monitor(df, _registry(tmp_path, "pullback_resumption_long_v1"))
    closed = mon.replay()
    _assert_identical(engine, closed)


def test_parity_variant_c(tmp_path):
    df = with_indicators(pd.read_parquet(FIXTURE))
    engine = backtest.run(df, pullback_resumption.signal, target_mult=None,
                          trail_mult=2.5, time_stop_bars=10)
    mon = _monitor(df, _registry(tmp_path, "pullback_resumption_long_trail_v1"))
    closed = mon.replay()
    _assert_identical(engine, closed)


def test_paper_dollar_accounting(tmp_path):
    """1% risk sizing: a full stop-out must lose ~risk_frac × unit equity."""
    df = with_indicators(pd.read_parquet(FIXTURE))
    mon = _monitor(df, _registry(tmp_path, "pullback_resumption_long_v1"))
    mon.replay()
    for t in mon.gateway.closed:
        if t["exit_kind"] == "stop":
            assert t["pnl_usd"] == pytest_approx(-1000.0, tol=25.0)
            break


def pytest_approx(expected, tol):
    class A:
        def __eq__(self, other):
            return abs(other - expected) <= tol
    return A()
