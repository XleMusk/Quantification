from hashlib import sha256
import json

import numpy as np
import pandas as pd
import pytest

from trading.research.financials import FIELDS, PERIODS
from trading.research.profitability_portfolios import profitability_pool, ranked_profitability, CASES
from trading.research.report_period_calendar import fixed_period, load_frozen_periods, period_observations


@pytest.mark.parametrize("day,period", [
    ("2025-01-02", "2024-09-30"), ("2025-04-30", "2024-09-30"),
    ("2025-05-01", "2025-03-31"), ("2025-08-31", "2025-03-31"),
    ("2025-09-01", "2025-06-30"), ("2025-10-31", "2025-06-30"),
    ("2025-11-01", "2025-09-30"), ("2026-03-31", "2025-09-30"),
])
def test_calendar_boundaries_do_not_follow_early_reports(day, period):
    assert fixed_period(day) == period


def reports():
    return pd.DataFrame([
        {"symbol": "a", "statDate": "2025-09-30", "pubDate": "2025-10-30", "roeAvg": 1., "gpMargin": 2.},
        {"symbol": "a", "statDate": "2025-12-31", "pubDate": "2026-03-01", "roeAvg": 99., "gpMargin": 99.},
        {"symbol": "b", "statDate": "2025-09-30", "pubDate": "2026-03-31", "roeAvg": 1., "gpMargin": 2.},
        {"symbol": "c", "statDate": "2025-09-30", "pubDate": "2025-10-30", "roeAvg": np.nan, "gpMargin": 2.},
    ])


def test_exact_older_quarter_survives_newer_report_and_late_missing_are_not_imputed():
    source = reports()
    before = source.copy(deep=True)
    current = period_observations(source, "2026-03-31")
    assert current.symbol.tolist() == ["a", "c"]
    assert current.source_stat_date.unique().tolist() == ["2025-09-30"]
    pool = pd.DataFrame({"symbol": list("abcd"), "volatility": [.1, .2, .3, .4], "momentum": [.4, .3, .2, .1]})
    frame, audit = profitability_pool(pool, current, "2026-03-31")
    assert frame.index.tolist() == ["a"] and frame.roeAvg_provider_value.iloc[0] == 1.
    assert audit["coverage"] == .25
    assert all([item["symbol"] for item in ranked_profitability(frame, case)] == ["a"] for case in CASES)
    assert period_observations(source, "2026-05-06").empty
    pd.testing.assert_frame_equal(source, before)


@pytest.mark.parametrize("kind", ["duplicate", "blank_pub", "future_stat", "invalid_day"])
def test_invalid_quarter_provenance_fails(kind):
    source = reports()
    day = "2026-03-31"
    if kind == "duplicate":
        source = pd.concat([source, source.iloc[:1]])
    elif kind == "blank_pub":
        source.loc[0, "pubDate"] = ""
    elif kind == "future_stat":
        source.loc[0, "statDate"] = "2026-09-30"
    else:
        day = "2027-01-01"
    with pytest.raises(ValueError):
        period_observations(source, day)


def test_reader_uses_frozen_receipts_not_mutable_collection_scope(tmp_path):
    collection = tmp_path / "financials"
    view = collection / "views" / "frozen"
    view.mkdir(parents=True)
    tasks = {}
    for year, quarter in PERIODS:
        raw = pd.DataFrame(columns=FIELDS)
        if (year, quarter) == (2025, 3):
            raw = pd.DataFrame([{**dict.fromkeys(FIELDS, ""), "code": "sh.600000", "statDate": "2025-09-30",
                                 "pubDate": "2025-10-30", "roeAvg": "1", "gpMargin": "2"}])
        relative = f"quarterly/{year}Q{quarter}.csv"
        path = collection / relative
        path.parent.mkdir(exist_ok=True)
        raw.to_csv(path, index=False)
        tasks[f"600000.SSE:{year}Q{quarter}"] = {"file": relative, "sha256": sha256(path.read_bytes()).hexdigest(),
            "rows": len(raw), "code": "sh.600000", "year": year, "quarter": quarter}
    (view / "source_receipts.json").write_text(json.dumps({"tasks": tasks}))
    (view / "source_plan.json").write_text(json.dumps({"periods": PERIODS, "symbols": [{"code": "sh.600000", "symbol": "600000.SSE"}]}))
    (view / "result.json").write_text(json.dumps({"status": "completed_publication_lagged_provider_view", "execution_admitted": False,
        "symbols": 1, "source_receipts_sha256": sha256((view / "source_receipts.json").read_bytes()).hexdigest()}))
    (collection / "receipts.json").write_text('{"status":"stopped_source_error","tasks":{}}')
    loaded = load_frozen_periods(view, ["2026-03-31"])
    assert loaded.symbol.tolist() == ["600000.SSE"] and loaded.roeAvg.iloc[0] == 1.
    (collection / "quarterly/2025Q3.csv").write_text("changed")
    with pytest.raises(ValueError, match="receipt changed"):
        load_frozen_periods(view, ["2026-03-31"])
