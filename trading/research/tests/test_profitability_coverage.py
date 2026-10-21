from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trading.research.profitability_portfolios import cohort_coverage


def inputs():
    dates = ["2025-04-30", "2025-05-30"]
    pools = pd.DataFrame({"date": [day for day in dates for _ in range(8)],
                          "symbol": list("abcdefgh") * 2})
    reports = pools.copy()
    reports["source_pub_date"] = ["2025-04-01"] * 8 + [""] * 8
    reports["source_stat_date"] = ["2024-12-31"] * 4 + ["2025-03-31"] * 4 + [""] * 8
    reports["roeAvg_provider_value"] = [1., 2., 3., 4., 5., 6., np.nan, np.nan] + [np.nan] * 8
    reports["gpMargin_provider_value"] = [1.] * 8 + [np.nan] * 8
    return pools, reports


def test_latest_sufficient_original_cohort_and_missing_month_are_preserved():
    pools, reports = inputs()
    before_pools, before_reports = deepcopy(pools), deepcopy(reports)
    audit, periods, membership = cohort_coverage(pools, reports, 3)
    first, second = audit.to_dict("records")
    assert first["common_financial_pool"] == 6 and first["coverage"] == .75
    assert first["latest_observed_stat_date"] == "2025-03-31" and first["latest_period_members"] == 2
    assert first["selected_stat_date"] == "2024-12-31" and first["selected_period_members"] == 4
    assert first["same_period_coverage"] == .5 and first["excluded_period_members"] == 2
    assert second["status"] == "insufficient_same_period_members" and second["selected_stat_date"] is None
    assert second["common_financial_pool"] == second["selected_period_members"] == 0
    assert membership.symbol.tolist() == list("abcd") and "profitability" not in membership
    assert periods.members.tolist() == [4, 2] and periods.selected.tolist() == [True, False]
    pd.testing.assert_frame_equal(pools, before_pools)
    pd.testing.assert_frame_equal(reports, before_reports)


def test_diagnostics_ignore_future_outcomes_and_input_order():
    pools, reports = inputs()
    expected = cohort_coverage(pools, reports, 3)
    actual = cohort_coverage(pools.iloc[::-1].assign(future_return=999), reports.iloc[::-1], 3)
    for left, right in zip(expected, actual):
        pd.testing.assert_frame_equal(left, right)


@pytest.mark.parametrize("invalid", ["duplicate", "same_day", "unknown_period", "future_period", "missing_publication"])
def test_invalid_joint_provenance_is_not_hidden_as_low_coverage(invalid):
    pools, reports = inputs()
    if invalid == "duplicate":
        reports = pd.concat([reports, reports.iloc[:1]], ignore_index=True)
    elif invalid == "same_day":
        reports.loc[0, "source_pub_date"] = "2025-04-30"
    elif invalid == "unknown_period":
        reports.loc[0, "source_stat_date"] = ""
    elif invalid == "future_period":
        reports.loc[0, "source_stat_date"] = "2025-06-30"
    else:
        reports.loc[0, "source_pub_date"] = ""
    with pytest.raises(ValueError):
        cohort_coverage(pools, reports, 3)
