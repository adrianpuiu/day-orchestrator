"""DataQualityGate and RiskManager unit tests."""

from datetime import date

import pandas as pd
import pytest

from app.core.market import quality_gate, repair_ohlc
from app.exec.risk import BlackoutCalendar, RiskManager


def _frame():
    idx = pd.bdate_range("2020-01-01", periods=400)
    return pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
        "Volume": 1000,
    }, index=idx)


def test_clean_frame_passes():
    assert quality_gate(_frame()).ok


def test_ohlc_violation_flagged_and_repaired():
    df = _frame()
    df.iloc[10, df.columns.get_loc("High")] = 90.0     # High below Low
    assert not quality_gate(df).ok
    fixed = repair_ohlc(df)
    assert quality_gate(fixed).ok


def test_gross_calendar_hole_flagged():
    df = _frame()
    df = df.drop(df.index[100:112])                     # 12 sessions gone
    report = quality_gate(df)
    assert not report.ok
    assert any("calendar hole" in p for p in report.problems)


def test_holidays_not_flagged():
    df = _frame()
    df = df.drop(df.index[[50, 51, 52]])                # long weekend
    assert quality_gate(df).ok


def test_duplicates_flagged():
    df = pd.concat([_frame(), _frame().iloc[[5]]]).sort_index()
    assert not quality_gate(df).ok


def test_risk_sizing_even_bet():
    risk = RiskManager(unit_equity_usd=100_000.0, risk_frac_per_trade=0.01)
    assert risk.size(stop_dist=2.0) == pytest.approx(500.0)
    assert risk.size(stop_dist=0.5) == pytest.approx(2000.0)
    assert risk.size(stop_dist=0.0) == 0.0


def test_process_degradation_halves_size_then_recovers():
    risk = RiskManager(missed_run_limit=2)
    risk.note_missed_run()
    risk.note_missed_run()
    risk.note_missed_run()
    assert risk.degraded
    assert risk.size_multiplier() == 0.5
    risk.note_clean_run()
    assert risk.size_multiplier() == 1.0


def test_blackout_calendar_blocks_entry():
    risk = RiskManager(calendar=BlackoutCalendar(dates={date(2026, 12, 25)}))
    assert not risk.allows_entry(date(2026, 12, 25))
    assert risk.allows_entry(date(2026, 12, 26))
