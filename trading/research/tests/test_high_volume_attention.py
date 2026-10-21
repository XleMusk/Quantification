from types import SimpleNamespace

import numpy as np
import pandas as pd

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.research.high_volume_attention import (
    AttentionFeed, config, forward_observations, summarize_observations, paired_price_observations,
)


def test_known_empty_exits_but_missing_batch_preserves():
    own_config = config()
    ctx = RunContext("attention_test", TradingMode.REPLAY)
    feed = AttentionFeed(SimpleNamespace(), {
        "2024-01-29": {"status": "complete", "targets": {"high": []}},
        "2024-01-30": {"status": "incomplete", "reason": "common_pool_activity_rank_unknown"},
    }, "high", own_config)
    empty = feed.signal_frame("2024-01-29", own_config, ctx)
    assert not isinstance(empty, SignalUnavailable)
    assert empty.signals == ()
    assert isinstance(feed.signal_frame("2024-01-30", own_config, ctx), SignalUnavailable)
    assert isinstance(feed.signal_frame("2024-01-31", own_config, ctx), SignalUnavailable)


def test_next_open_twenty_session_endpoint_and_censoring():
    days = pd.bdate_range("2024-01-02", periods=23).strftime("%Y-%m-%d").tolist()
    symbol = "600000.SSE"
    rows = {d: {symbol: {"open": 10.0 + i, "close": 10.0 + i,
        "volume": 100, "tradestatus": 1}} for i, d in enumerate(days)}
    adjusted = pd.DataFrame({symbol: [2*(10+i) for i in range(23)]}, index=days)
    base = SimpleNamespace(calendar=days, day_index={d:i for i,d in enumerate(days)},
        closes=adjusted, rows=rows)
    frame = pd.DataFrame([{"date": days[0], "symbol": symbol}, {"date": days[3], "symbol": symbol}])
    result = forward_observations(base, frame)
    assert result.loc[0, "entry_day"] == days[1]
    assert result.loc[0, "exit_day"] == days[21]
    assert abs(result.loc[0, "forward20_price_return"] - (31/11-1)) < 1e-12
    assert np.isnan(result.loc[1, "forward20_price_return"])
    assert result.loc[1, "endpoint_reason"] == "after_fixed_source_cutoff"


def test_partial_endpoint_basket_never_becomes_complete_or_zero():
    rows = [{"date": "2024-01-29", "activity_group": "high",
        "forward20_price_return": value,
        **{"selected_"+a: a in ("all", "high") for a in ("all", "high", "normal", "low")}}
        for value in (.1, np.nan)]
    summaries = summarize_observations(pd.DataFrame(rows))
    high = next(r for r in summaries if r["period"] == "all" and r["scope"] == "selected20" and r["arm"] == "high")
    assert high["formation_rows"] == 2 and high["endpoint_unknown"] == 1
    assert high["day_equal_price_return"] == .1
    assert high["complete_basket_dates"] == 0
    assert high["complete_basket_day_equal_price_return"] is None
    low = next(r for r in summaries if r["period"] == "all" and r["scope"] == "selected20" and r["arm"] == "low")
    assert low["day_equal_price_return"] is None


def test_comparison_pairs_actual_dates_not_different_known_subsets():
    frame = pd.DataFrame([
        dict(date=day, activity_group=group, forward20_price_return=value,
            selected_all=True, selected_high=group=="high", selected_normal=group=="normal", selected_low=False)
        for day,group,value in [("2024-01-29","high",.1),("2024-01-29","normal",.3),
            ("2024-02-27","high",np.nan),("2024-02-27","normal",.9)]])
    paired = [r for r in paired_price_observations(frame)
        if r["scope"] == "selected20" and r["contrast"] == "high_minus_all"]
    assert abs(paired[0]["conditional_same_date_difference"] + .1) < 1e-12
    assert abs(paired[0]["complete_basket_same_date_difference"] + .1) < 1e-12
    assert paired[1]["conditional_same_date_difference"] is None
    assert paired[1]["control_count"] == 2 and paired[1]["control_known"] == 1


def test_nonempty_group_labels_do_not_enter_numeric_signal_contract():
    row = dict(symbol="600000.SSE", score=.1, momentum=.1, volatility=.2,
        median_amount=30000000., activity_rank=50., activity_group="high",
        activity_status="known_50_exact_active_sessions")
    batch = {"status":"complete", "targets":{"high":[row]}}
    feed = AttentionFeed(SimpleNamespace(), {"2024-01-29":batch}, "high", config())
    result = feed.signal_frame("2024-01-29", config(), RunContext("projection", TradingMode.REPLAY))
    assert result.ranked_candidates()[0]["score"] == .1
    assert result.ranked_candidates()[0]["activity_rank"] == 50.
    assert "activity_status" not in dict(result.signals[0].diagnostics)
    assert batch["targets"]["high"][0] == row
