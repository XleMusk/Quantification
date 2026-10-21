from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.engine import advance
from trading.research.holding_paths import cohort_labels, held_sessions, selected_momentum_count, selection_rows
from trading.research.tests import test_replay_resume
from trading.research.turnover_attribution import project_orders
from trading.ledger import new_state


def fixture():
    config = replace(StrategyConfig(), holdings=2, retain_rank=3)
    ranks = pd.DataFrame([{"date": "2026-01-05", "symbol": symbol, "rank": rank,
                           "score": 4 - rank, "source_pub_date": "2025-10-30",
                           "source_stat_date": "2025-09-30", "momentum": -.1}
                          for rank, symbol in enumerate(("A", "B", "C"), 1)])
    target = {"kind": "targets_generated", "day": "2026-01-05", "signal_date": "2026-01-05",
              "execution_not_before": "2026-01-06", "targets": {"A": 0., "C": .1}}
    return config, ranks, target


def test_buffer_displaces_head_zero_budget_is_not_missing_or_filled():
    config, ranks, target = fixture()
    rows = selection_rows([target], ranks, {"2026-01-05": {"C": 100}}, [], config).set_index("symbol")
    assert rows.at["C", "selection_reason"] == "retained_rank_buffer"
    assert rows.at["B", "selection_reason"] == "unselected_head_displaced_by_retention"
    assert rows.at["A", "explicit_zero_budget"]
    assert rows.at["A", "trace_status"] == "missing_or_overridden_attempt"
    assert pd.isna(rows.at["A", "held_after"])
    bad = deepcopy(target)
    bad["targets"] = {"A": .1, "B": .1}
    with pytest.raises(ValueError, match="membership"):
        selection_rows([bad], ranks, {"2026-01-05": {"C": 100}}, [], config)
    ranks.loc[0, "source_pub_date"] = "2026-01-05"
    with pytest.raises(ValueError, match="lagged"):
        selection_rows([target], ranks, {"2026-01-05": {"C": 100}}, [], config)


def test_share_path_carries_only_past_changes_and_counts_stale_sessions():
    marks = [{"date": day, "holdings": count, "stale_symbols": stale} for day, count, stale in
             [("2026-01-05", 0, []), ("2026-01-06", 1, []), ("2026-01-07", 1, ["A"])]]
    rows = held_sessions(marks, {"2026-01-06": {"A": 100}})
    assert rows.date.tolist() == ["2026-01-06", "2026-01-07"]
    assert rows.quantity.tolist() == [100, 100] and rows.stale_valuation.sum() == 1
    assert held_sessions([{**m, "holdings": str(m["holdings"])} for m in marks],
                         {"2026-01-06": {"A": 100}}).equals(rows)
    with pytest.raises(ValueError, match="successful"):
        held_sessions(marks, {"2026-01-08": {"A": 100}})
    with pytest.raises(ValueError, match="count"):
        held_sessions(marks, {})


def test_existing_projection_observes_decision_after_real_saved_fills_without_mutation():
    feed = test_replay_resume.ReplayResumeTests().feed()
    config = StrategyConfig(initial_cash=140000, rebalance_sessions=1)
    state = new_state(config.initial_cash)
    advance(state, feed, feed.calendar[0], config, 0)
    advance(state, feed, feed.calendar[1], config, 1)
    before = deepcopy(state)
    decisions, daily = {}, {}
    project_orders(state, {}, decision_holdings=decisions, daily_holdings=daily)
    assert decisions[feed.calendar[0]] == {}
    assert decisions[feed.calendar[1]]["600000.SSE"] > 0
    assert daily[feed.calendar[1]] == decisions[feed.calendar[1]]
    assert state == before


def test_fixed_factor_paths_keep_report_guard_and_unknown_momentum():
    config, ranks, target = fixture()
    ranks = ranks.drop(columns=["source_pub_date", "source_stat_date", "momentum"])
    with pytest.raises(ValueError, match="report provenance"):
        selection_rows([target], ranks, {"2026-01-05": {"C": 100}}, [], config)
    rows = selection_rows([target], ranks, {"2026-01-05": {"C": 100}}, [], config, financial_ranks=False)
    assert rows.set_index("symbol").at["C", "selection_reason"] == "retained_rank_buffer"
    assert selected_momentum_count(rows.loc[rows.selected_slot]) is None


def test_month_end_cohorts_keep_missing_outcomes_zero_budgets_and_retained_members():
    from trading.research.stockdb_factor_diagnostics import BLEND
    config, ranks, target = fixture()
    ranks["security_identity"] = ranks.symbol
    decisions = selection_rows([target], ranks, {"2026-01-05": {"C": 100}}, [], config)
    index = pd.MultiIndex.from_product([[pd.Timestamp("2026-01-05")], ["A", "B", "C", "D"]],
                                      names=["date", "symbol"])
    scores = pd.DataFrame({BLEND: [3., 2., 1., 0.], "log_float_mv_candidate": [4., 3., 2., 1.]}, index=index)
    labels = pd.Series([.1, .2, float("nan"), -.1], index=index)
    rows = cohort_labels(scores, labels, decisions, ranks, BLEND, config).set_index("cohort")
    assert rows.at["common_pool", "members"] == 4 and rows.at["common_pool", "missing_labels"] == 1
    assert rows.at["top20_head", "members"] == 2 and rows.at["top20_head", "mean_adjusted_open_label_h20"] == pytest.approx(.15)
    assert rows.at["positive_selected_targets", "members"] == 1
    assert rows.at["positive_selected_targets", "missing_labels"] == 1
    assert pd.isna(rows.at["positive_selected_targets", "mean_adjusted_open_label_h20"])
    assert rows.at["positive_entry_holdings", "members"] == 0
    assert rows.at["retained_rank_buffer", "members"] == 1
    assert rows.at["displaced_head", "members"] == 1
    assert rows.at["positive_selected_targets", "oriented_rank_log_float_mv_candidate"] == .5
    ranks.loc[0, "score"] += 1
    with pytest.raises(ValueError, match="fixed source scores"):
        cohort_labels(scores, labels, decisions, ranks, BLEND, config)
