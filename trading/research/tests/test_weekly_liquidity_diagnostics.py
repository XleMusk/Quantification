"""Pure diagnostic membership and missingness, using synthetic data only."""
import numpy as np
import pandas as pd
import pytest

from trading.research.weekly_liquidity_diagnostics import ENDPOINTS, evaluate


D1, D2 = "2026-01-09", "2026-01-16"


def source(rows=125, *, day=D1, group=1, offset=0):
    index = pd.MultiIndex.from_tuples([(day, f"{offset+i:06d}.SZSE") for i in range(rows)],
        names=["date", "symbol"])
    return pd.DataFrame(dict(reversal_score=np.arange(rows, dtype=float),
        turn_group=group, turn63_percent=float(group)), index=index)


def labels(frame):
    values = pd.Series(np.arange(len(frame), dtype=float) / 1000, index=frame.index)
    return {endpoint: values.copy() for endpoint in ENDPOINTS}


def row(result, stratum="T1", endpoint="h5", day=D1, *, field="daily"):
    return next(r for r in result[field] if r["stratum"] == stratum and r["endpoint"] == endpoint and r["date"] == day)


def test_original_quintile_members_survive_missing_future_label_without_reranking():
    frame = source()
    target = labels(frame)
    for endpoint in ENDPOINTS:
        target[endpoint] = target[endpoint].drop(frame.index[-1])
    result = evaluate(frame, target, [D1])
    for endpoint in ENDPOINTS:
        group = row(result, endpoint=endpoint, field="groups")
        assert group["q5_members"] == 25
        assert group["q5_observed_labels"] == 24 and group["q5_missing_labels"] == 1
        assert group["q5_member_symbols"] == frame.index.get_level_values("symbol")[-25:].tolist()
        assert row(result, endpoint=endpoint)["pairs"] == 124
        assert row(result, endpoint=endpoint)["rank_ic"] == pytest.approx(1.)


def test_constant_ties_preserve_unsplit_average_rank_and_null_metrics():
    frame = source(120)
    frame["reversal_score"] = 7.
    result = evaluate(frame, labels(frame), [D1])
    group = row(result, field="groups")
    assert group["maximum_score_tie_count"] == 120
    assert group["q1_members"] == 0 and group["q5_members"] == 0
    assert row(result)["rank_ic"] is None and row(result)["q5_minus_q1"] is None
    assert row(result)["reason"] == "constant_reversal_scores"


def test_late_pairing_uses_only_same_D_both_finite_and_keeps_missing_original_D():
    frame = pd.concat([source(120, day=day, group=group, offset=group*1000)
                       for day in (D1, D2) for group in (1, 5)])
    target = labels(frame)
    late = frame.reversal_score.copy()
    high = frame.turn_group.eq(5)
    late.loc[high] *= -1
    late.loc[high & (frame.index.get_level_values("date") == D2)] = np.nan
    target["late5_21"] = late
    result = evaluate(frame, target, [D1, D2])
    paired = result["paired_late_daily"]
    assert [r["date"] for r in paired] == [D1, D2]
    assert paired[0]["delta_IC"] == pytest.approx(2.)
    assert paired[1]["delta_IC"] is None
    summary = next(r for r in result["paired_late_summaries"] if r["scope"] == "all")
    assert summary["same_D_valid_IC_dates"] == [D1]
    assert summary["mean_delta_IC"] == pytest.approx(2.)
    assert row(result, stratum="T5", endpoint="late5_21")["rank_ic"] == pytest.approx(-1.)
    assert row(result, stratum="T5", endpoint="h21")["rank_ic"] == pytest.approx(1.)


def test_insufficient_samples_and_absent_source_date_are_null_not_zero():
    frame = source(99)
    result = evaluate(frame, labels(frame), [D1, D2])
    assert row(result)["pairs"] == 99
    assert row(result)["rank_ic"] is None and row(result)["q5_minus_q1"] is None
    assert row(result)["reason"] == "insufficient_finite_label_pairs"
    absent = row(result, day=D2)
    assert absent["pool_members"] is None and absent["pairs"] is None
    assert absent["rank_ic"] is None and absent["q5_minus_q1"] is None
    assert len(result["daily"]) == 2 * 6 * 3
