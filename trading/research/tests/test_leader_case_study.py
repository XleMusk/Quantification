"""Small behavior checks for date sampling and honest case boundaries."""
from copy import deepcopy
import pandas as pd

from trading.research.leader_case_study import EVENT_FIELDS, _markdown, select_samples, summarize_case


def event(symbol="001231.SZSE", **changes):
    values = {key: 0 for key in EVENT_FIELDS}
    return {**values, "date": "2026-01-05", "symbol": symbol, "stage": 2, "board": "main", "is_st": False,
        "amount": 30_000_000, "price_reference_status": "match_within_1pct", "low": 9., **changes}


def daily(day, *, close=10., pct=0., low=9.):
    return {"date": day, "open": close, "high": close, "low": low, "close": close,
        "pct_chg": pct, "volume": 1000., "amount": 10000.}


def markdown_for(summary):
    return _markdown({"display_name": "sample", "symbol": "001231.SZSE", "event_day": "2026-01-05",
        "window_start": "2026-01-05", "window_end": "2026-01-08",
        "information_through_E": {"selected_event": event(close=10.)}, "after_E_observed_path": summary,
        "daily_rows": [], "phase_evidence": [], "derived_at": "2026-01-09T16:00:00+08:00",
        "source": {"snapshot_recorded_created_at": "2026-01-09T15:30:00+08:00",
            "annotations_file": "annotations.csv", "snapshot_manifest": "manifest.json"}, "json_file": "case.json"})


def test_first5_selection_ignores_future_success_eligibility_and_future_append():
    rows = [event(f"{i:06}.SZSE", hit_5=i % 2, research_eligible=False, run_max_stage_observed=i) for i in range(1, 8)]
    initial = select_samples(pd.DataFrame(rows), "2026-01-05")
    altered = deepcopy(rows)
    for row in altered:
        row.update(hit_5=1, research_eligible=True, run_max_stage_observed=99)
    altered.append(event("000000.SZSE", date="2026-01-06"))
    assert select_samples(pd.DataFrame(altered), "2026-01-05") == initial
    assert [row["symbol"] for row in initial] == [f"{i:06}.SZSE" for i in range(1, 6)]
    assert not {"hit_5", "research_eligible", "run_max_stage_observed"}.intersection(initial[0])


def test_positive_break_is_not_decline_and_missing_row_not_suspension():
    rows = [daily("2026-01-05", close=10., pct=10.), daily("2026-01-06", close=10.5, pct=5.)]
    result = summarize_case(rows, event(), ["2026-01-05", "2026-01-06", "2026-01-07"])
    assert result["first_observed_close_limit_break_day"] == "2026-01-06"
    assert result["first_break_closes_price_down"] is False
    assert result["exact_expected_missing_raw_days"] == ["2026-01-07"]
    assert result["later_daily_path"][-1]["suspension_inferred"] is False
    assert result["first_close_below_fixed_event_low_status"] == "unknown"
    assert "准确首次收盘跌破日 **未知**" in markdown_for(result)
    assert "窗口内未见" not in markdown_for(result)


def test_fixed_E_low_is_not_refreshed_from_later_low_or_future_labels():
    rows = [daily("2026-01-05", close=10., low=9.), daily("2026-01-06", close=9.5, low=5.),
            daily("2026-01-07", close=8.9, low=8.)]
    result = summarize_case(rows, event(hit_5=1), ["2026-01-05", "2026-01-06", "2026-01-07"])
    assert result["fixed_event_setup_low"] == 9.
    assert result["first_close_below_fixed_event_low"] == "2026-01-07"
    assert result["first_observed_close_below_fixed_event_low"] == "2026-01-07"
    assert result["first_close_below_fixed_event_low_status"] == "known_first"
    # A later missing slot cannot undo an already exact first breach.
    trailing_gap = summarize_case(rows, event(), ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
    assert trailing_gap["first_close_below_fixed_event_low"] == "2026-01-07"
    assert trailing_gap["first_close_below_fixed_event_low_status"] == "known_first"


def test_earlier_missing_slot_keeps_first_breach_unknown_but_retains_observed_date():
    rows = [daily("2026-01-05"), daily("2026-01-07", close=8.9, low=8.)]
    result = summarize_case(rows, event(), ["2026-01-05", "2026-01-06", "2026-01-07"])
    assert result["first_observed_close_below_fixed_event_low"] == "2026-01-07"
    assert result["first_close_below_fixed_event_low"] is None
    assert result["first_close_below_fixed_event_low_status"] == "unknown"
    assert result["later_daily_path"][0]["below_fixed_event_low"] is None
    rendered = markdown_for(result)
    assert "首次已观察到收盘跌破为 **2026-01-07**" in rendered
    assert "准确首次收盘跌破日 **未知**" in rendered
    assert "窗口内未见" not in rendered


def test_complete_window_without_breach_is_explicit_complete_none():
    rows = [daily("2026-01-05"), daily("2026-01-06", close=9.5), daily("2026-01-07", close=9.)]
    result = summarize_case(rows, event(), ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"])
    # A missing E-before row does not affect completeness of the E-after comparison.
    assert result["exact_expected_missing_raw_days"] == ["2026-01-02"]
    assert result["first_observed_close_below_fixed_event_low"] is None
    assert result["first_close_below_fixed_event_low"] is None
    assert result["first_close_below_fixed_event_low_status"] == "complete_none"
    assert "E后准确比较窗完整，窗口内未见收盘跌破" in markdown_for(result)
    assert "准确首次收盘跌破日 **未知**" not in markdown_for(result)
