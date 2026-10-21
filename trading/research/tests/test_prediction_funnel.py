import numpy as np
import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.research.factors import LABEL
from trading.research.prediction_funnel import buy_matches, common_anchors, group_stats, ranked_day, stale_days, target_events


def test_missing_head_label_does_not_promote_lower_score_and_ties_are_stable():
    score = pd.Series([3., 3., 1.], index=pd.Index(["b", "a", "c"], name="symbol"))
    labels = pd.DataFrame({LABEL: [.2, np.nan, .9], "entry_date": pd.Timestamp("2026-01-06"),
                           "exit_date": pd.Timestamp("2026-01-13")}, index=score.index)
    ranked = ranked_day(score, labels)
    assert ranked.index.tolist() == ["a", "b", "c"]
    head = group_stats(ranked.iloc[:1])
    assert head["selected"] == 1 and head["missing"] == 1
    assert head["observed_mean_forward_return"] is None and head["complete_mean_forward_return"] is None
    incomplete = group_stats(ranked.iloc[:2], pd.Series({"a": .1, "b": .2}))
    assert incomplete["observed_mean_forward_return"] == pytest.approx(.2)
    assert incomplete["observed_weight_fraction"] == pytest.approx(2 / 3)
    assert incomplete["complete_mean_forward_return"] is None


def test_buy_join_never_uses_the_same_days_new_close_decision():
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    events = [{"signal_date": dates[0], "execution_not_before": dates[1], "targets": {"a": .16}},
              {"signal_date": dates[1], "execution_not_before": dates[2], "targets": {"b": .16}}]
    account = {"orders": {"a": {"id": "a", "day": dates[1], "symbol": "a", "side": "buy", "filled": 100, "notional": 100000}},
               "fills": {"1": {"order_id": "a", "qty": 100, "day": dates[2]}}}
    matched = buy_matches(account, events, dates).iloc[0]
    assert matched.target_matched and matched.signal_date == dates[0]
    assert matched.first_fill_day == dates[2]
    account["orders"]["a"]["symbol"] = "b"
    assert not buy_matches(account, events, dates).iloc[0].target_matched


def test_terminal_target_keeps_fallback_but_does_not_add_a_calendar_session():
    dates = ["2026-09-30"]
    event = {"kind": "targets_generated", "day": dates[0], "signal_date": dates[0],
             "execution_not_before": "2026-10-01", "targets": {"a": .16}}
    result = target_events({"events": [event]}, dates, StrategyConfig())
    assert result == [event] and result[0]["execution_not_before"] not in dates
    event["execution_not_before"] = dates[0]
    with pytest.raises(ValueError, match="next-session timing"):
        target_events({"events": [event]}, dates, StrategyConfig())


def test_saved_stale_symbols_use_existing_csv_python_list_encoding():
    assert stale_days(pd.Series(["[]", "['000001.SZSE']", "['a', 'b']"])) == 2
    with pytest.raises(ValueError, match="stale-valuation"):
        stale_days(pd.Series(["{'a': 1}"]))


def test_case_comparison_uses_intersection_of_complete_head_and_target_dates():
    rows = [{"date": day, "cadence": "rebalance", "group": group,
             "complete_mean_forward_return": .1, "observed_mean_forward_return": .1}
            for day in ["2026-01-05", "2026-01-12"] for group in ["head5", "actual_targets", "all_pool"]]
    first, second = pd.DataFrame(rows), pd.DataFrame(rows)
    second.loc[second.date.eq("2026-01-12") & second.group.eq("actual_targets"), "complete_mean_forward_return"] = np.nan
    result, labels = common_anchors({"a": first, "b": second})
    assert result["dates"] == ["2026-01-05"] and result["excluded_anchor_dates"] == ["2026-01-12"]
    assert len(labels) == 2 and labels.date.nunique() == 1
