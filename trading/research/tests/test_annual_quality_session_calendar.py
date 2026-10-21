"""The next exchange day confirms scheduling without manufacturing a quote."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode, SignalFrame, SignalUnavailable
from trading.engine import advance
from trading.ledger import new_state
from trading.research import annual_quality_portfolios as quality
from trading.research.prospective_research import incoming_dates
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


D, E, F = "2026-10-15", "2026-10-16", "2026-10-19"
A = "600001.SSE"


def calendar(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}), encoding="utf-8")
    return path


def source():
    return SimpleNamespace(calendar=[D, E], day_index={D: 0, E: 1},
        manifest={"end": E, "physical_rows": 2}, rows={D: {A: {"close": 10}}, E: {A: {"close": 11}}},
        closes=pd.DataFrame({A: [10., 11.]}, index=[D, E]))


def test_latest_physical_close_becomes_processable_without_a_future_bar(tmp_path):
    base = source()
    path = calendar(tmp_path)
    old_rows, old_index, old_manifest = deepcopy(base.rows), deepcopy(base.day_index), deepcopy(base.manifest)
    old_prices = base.closes.copy(deep=True)
    current = datetime.fromisoformat("2026-10-18T18:30:00+08:00")
    assert incoming_dates(base.calendar, D, D, current) == []
    binding, observed = quality.confirm_session_successor(base, path)
    assert incoming_dates(base.calendar, D, D, current) == [E]
    assert base.calendar == [D, E, F] and F not in base.day_index and F not in base.rows
    assert base.rows == old_rows and base.day_index == old_index and base.manifest == old_manifest
    pd.testing.assert_frame_equal(base.closes, old_prices)
    assert observed["successor_raw_bar_present"] is False and observed["price_rows_added"] == 0
    assert binding["method_id"] == quality.CALENDAR_CONFIRMATION_METHOD
    with pytest.raises(ValueError, match="real cutoff"):
        quality.confirm_session_successor(base, path)


@pytest.mark.parametrize("case", ["missing_session", "wrong_cutoff", "already_priced_successor"])
def test_bad_calendar_or_synthetic_source_label_is_rejected_before_change(tmp_path, case):
    base = source()
    if case == "missing_session":
        base.calendar = ["2026-10-14", E]
    elif case == "wrong_cutoff":
        base.manifest["end"] = D
    else:
        base.day_index[F] = 2
    before = deepcopy(base.calendar)
    with pytest.raises(ValueError, match="source session|successor label"):
        quality.confirm_session_successor(base, calendar(tmp_path))
    assert base.calendar == before


def test_shared_engine_queues_last_close_target_for_official_next_day_without_executing_it(tmp_path):
    bars = pd.DataFrame([{"date": day, "symbol": A, "open": 10., "high": 10.2,
        "low": 9.8, "close": 10., "preclose_for_limits": 10.,
        "volume": 1_000_000., "amount": 10_000_000., "is_st": False} for day in [D, E]])
    base = ShortlinePanelFeed(bars, {}, [A])
    base.day_index = {D: 0, E: 1}
    base.manifest = {"end": E}
    base.instruments[A]["ipoDate"] = "1999-01-01"
    quality.confirm_session_successor(base, calendar(tmp_path))
    class CloseOnlyFeed:
        # Like the annual signal producer, this uses D's saved features. The
        # unrelated shortline-v2 fixture deliberately requires E raw and is
        # therefore unsuitable for checking calendar-only confirmation.
        def __getattr__(self, key):
            return getattr(base, key)

        def signal_frame(self, day, config, context):
            return (SignalFrame.from_legacy([{"symbol": A, "score": 1.}], day,
                "synthetic_calendar_only", context) if day == E
                else SignalUnavailable("synthetic_no_close_decision"))

    feed = CloseOnlyFeed()
    config = _config(100000, 5)
    context = RunContext("synthetic_last_close_calendar", TradingMode.REPLAY)
    state = new_state(config.initial_cash)
    for index, day in enumerate([D, E]):
        advance(state, feed, day, config, index, context=context)
    assert state["day"] == E and state["pending_targets"] == {A: .16}
    assert not state["orders"] and not state["fills"]
    assert F not in base.rows and F not in base.day_index
    targets = [row for row in state["events"] if row["kind"] == "targets_generated"]
    assert targets[-1]["execution_not_before"] == F
