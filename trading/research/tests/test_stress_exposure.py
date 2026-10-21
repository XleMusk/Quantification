import pandas as pd
import pytest

from trading.research.stress_exposure import exposure_rows, stress_dates, summarize


def _daily():
    return pd.DataFrame([
        {"date": "2026-01-01", "equity": 100.0, "cash": 20.0, "holdings": 2, "stale_symbols": "[]"},
        {"date": "2026-01-02", "equity": 90.0, "cash": 20.0, "holdings": 2, "stale_symbols": "[]"},
        {"date": "2026-01-03", "equity": 95.0, "cash": 20.0, "holdings": 2, "stale_symbols": "['B']"},
    ])


def test_stress_exposure_keeps_unknown_marks_and_asof_holdings():
    daily = _daily()
    snapshots = {"2026-01-01": {"A": 5, "B": 5}}
    raw = {"A": {"close": 10.0}, "B": {"close": 8.0}}
    rows = exposure_rows(daily, snapshots, lambda day: raw)
    worst = rows.loc[rows.date == "2026-01-02"]
    assert worst.known_mark.all()
    stale = rows.loc[rows.date == "2026-01-03"]
    assert bool(stale.loc[stale.symbol == "B", "known_mark"].iloc[0]) is False
    assert pd.isna(stale.loc[stale.symbol == "B", "marked_value_cny"].iloc[0])


def test_summary_does_not_turn_exposure_into_validation():
    daily = _daily()
    selected = stress_dates(daily, limit=1)
    rows = exposure_rows(daily, {"2026-01-01": {"A": 5}}, lambda day: {"A": {"close": 10.0}}, limit=1)
    _, _, summary = summarize(rows, selected)
    assert summary["execution_admitted"] is False
    assert summary["strategy_validated"] is False


def test_stress_limit_is_bounded():
    with pytest.raises(ValueError, match="limit"):
        stress_dates(_daily(), 0)
