"""Saved-only realization arithmetic; no market, Engine, or account mutation."""
from copy import deepcopy

import pytest

from trading.portfolio_realization import analyze


def diagnostic(day, symbol="000001.SZSE", **changes):
    return {
        "day": day, "kind": "target_execution_diagnostic", "symbol": symbol,
        "target_weight": .4, "open_equity_cents": 1_000_000,
        "engine_mark": 1000, "price_unit": "cent", "has_execution_open": True,
        "engine_target_quantity": 400, "held_after": 400,
        "mechanical_reason": "new_entry", "constraint_flags": [], **changes,
    }


def close(day, **changes):
    return {"date": day, "equity": 10_000., "cash": 2000., "receivables": 0.,
            "stale_symbols": [], **changes}


def test_cent_and_mill_values_use_same_equity_and_negative_gap_is_preserved():
    day = "2026-01-05"
    events = [
        diagnostic(day, held_after=300, constraint_flags=["exposure_caps"]),
        diagnostic(day, "518880.SSE", engine_mark=5000, price_unit="mill",
                   engine_target_quantity=800, held_after=1200,
                   constraint_flags=["prior_volume_participation", "prior_volume_participation"]),
    ]
    state, daily = {"events": events}, [close(day, cash=1000.)]
    original = deepcopy((state, daily))
    result = analyze({"gross_limit": .8}, state, daily)
    row, summary = result["sessions"][0], result["summary"]
    assert row["target_adopted_gross"] == pytest.approx(.8)
    assert row["lot_target_gross"] == pytest.approx(.8)
    assert row["realized_reference_gross"] == pytest.approx(.9)
    assert row["lot_to_realized_gap"] == pytest.approx(-.1)
    assert summary["configured_gross_limit"] == .8  # A cap never replaces actual holdings.
    assert summary["mean_realized_reference_gross"] == pytest.approx(.9)
    assert row["constraint_reasons"]["prior_volume_participation"] == 1
    assert all(reason["day_count"] == 1 for reason in result["reason_counts"])
    assert (state, daily) == original


def test_unknown_weights_duplicate_symbols_equity_and_units_do_not_become_zero():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    events = [
        diagnostic(days[0], target_weight=None),
        diagnostic(days[1]), diagnostic(days[1]),
        diagnostic(days[2], price_unit="yuan", engine_mark=0),
        diagnostic(days[3]),
        diagnostic(days[3], "000002.SZSE", open_equity_cents=999_999),
        diagnostic(days[4], engine_target_quantity=None),
    ]
    result = analyze({"gross_limit": .8}, {"events": events}, [close(day) for day in days])
    rows = result["sessions"]
    assert rows[0]["target_adopted_gross"] is None
    assert rows[0]["lot_target_gross"] == pytest.approx(.4)
    assert rows[1]["target_adopted_gross"] is None
    assert rows[1]["realized_reference_gross"] is None
    assert "duplicate_symbol_diagnostics_no_last_row_selection" in rows[1]["warnings"]
    assert rows[2]["lot_target_gross"] is None
    assert rows[3]["lot_target_gross"] is None
    assert "opening_equity_unknown_or_inconsistent" in rows[3]["warnings"]
    assert rows[4]["lot_target_gross"] is None
    assert rows[4]["realized_reference_gross"] == pytest.approx(.4)
    assert result["summary"]["matched_sessions"] == 0
    assert result["summary"]["mean_adopted_target_gross"] is None


def test_means_share_dates_and_old_reference_or_no_trace_never_implies_zero():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    events = [
        diagnostic(days[0], engine_target_quantity=300, held_after=200),
        diagnostic(days[1], target_weight=.8, has_execution_open=False),
        diagnostic(days[3], target_weight=.2),
    ]
    daily = [
        close(days[0], cash="5000", equity="10000", receivables="0", stale_symbols="[]",
              events=[events[0]]),  # Embedded event is the saved duplicate, not another trace.
        close(days[1], cash=1000.), close(days[2], cash=3000., stale_symbols=["000001.SZSE"]),
        close(days[3], receivables=None),
    ]
    result = analyze({"gross_limit": .8}, {"events": events}, daily)
    summary, rows = result["summary"], {row["date"]: row for row in result["sessions"]}
    assert summary["committed_sessions"] == 4
    assert summary["trace_sessions"] == 3
    assert summary["matched_dates"] == [days[0]]
    assert summary["mean_adopted_target_gross"] == pytest.approx(.4)
    assert summary["mean_lot_target_gross"] == pytest.approx(.3)
    assert summary["mean_realized_reference_gross"] == pytest.approx(.2)
    assert summary["mean_close_gross_on_matched_sessions"] == pytest.approx(.5)
    assert summary["mean_close_gross_all_saved_sessions"] == pytest.approx(.7)
    assert summary["close_gross_known_sessions"] == 3
    assert summary["stale_valuation_sessions"] == 1
    assert rows[days[0]]["diagnostic_rows"] == 1
    assert rows[days[1]]["realized_reference_gross"] is None
    assert rows[days[2]]["target_adopted_gross"] is None
    assert rows[days[2]]["diagnostic_rows"] == 0
    assert rows[days[3]]["close_gross"] is None
