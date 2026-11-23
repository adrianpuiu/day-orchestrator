"""Regression lock: the ported engine must reproduce the session-verified
backtest numbers on the locked fixture (cross-checked trade-for-trade against
the original raschke-strategy engine on the same data)."""

from pathlib import Path

import pandas as pd
import pytest

from app.core import backtest
from app.core.indicators import with_indicators
from app.strategies import pullback_resumption

FIXTURE = Path(__file__).parents[1] / "tests/fixtures/spy_daily.parquet"


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    return with_indicators(pd.read_parquet(FIXTURE))


def test_engine_reference_variant_b(df):
    trades = backtest.run(df, pullback_resumption.signal, target_mult=3.0,
                          trail_mult=None, time_stop_bars=10)
    stats = backtest.summarize(trades)
    assert stats["n"] == 71
    assert stats["pf"] == pytest.approx(1.4, abs=0.01)
    assert stats["avg_r"] == pytest.approx(0.268, abs=0.005)


def test_engine_reference_variant_c(df):
    trades = backtest.run(df, pullback_resumption.signal, target_mult=None,
                          trail_mult=2.5, time_stop_bars=10)
    stats = backtest.summarize(trades)
    assert stats["n"] == 58
    assert stats["pf"] == pytest.approx(1.45, abs=0.01)
    assert stats["avg_r"] == pytest.approx(0.296, abs=0.005)


def test_no_lookahead_signal_never_uses_future_bars(df):
    """Signal at i must not change when future bars are truncated."""
    cut = len(df) - 40
    full_sig = pullback_resumption.signal(df, cut)
    trunc = with_indicators(df.iloc[:cut + 1])
    trunc_sig = pullback_resumption.signal(trunc, cut)
    if full_sig is None:
        assert trunc_sig is None
        return
    assert trunc_sig is not None
    assert full_sig.entry == trunc_sig.entry
    assert full_sig.reason == trunc_sig.reason
