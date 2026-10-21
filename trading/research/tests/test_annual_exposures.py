from copy import deepcopy

import pandas as pd
import pytest

from trading.research.annual_exposures import (FINANCIAL_LABELS, compare_profiles, exposure_profile,
                                             industry_metadata)


def annual_rows():
    rows = []
    for index, label in enumerate([sorted(FINANCIAL_LABELS)[0], "technology", "technology", None]):
        row = {"symbol": str(index)}
        for report in ("balance", "income", "cashflow"):
            row.update({report + "_industry_name_source": label,
                        report + "_notice_at": "2026-04-30",
                        report + "_retrieved_at": "2026-10-04T20:00:00+08:00"})
        rows.append(row)
    rows[1]["income_industry_name_source"] = "utilities"
    rows[2]["cashflow_retrieved_at"] = "2026-10-06T20:00:00+08:00"
    return pd.DataFrame(rows)


def test_metadata_requires_matching_known_labels_and_actual_receipt_time():
    result = industry_metadata(annual_rows(), "2026-10-05T06:00:00+08:00")
    assert result.industry_status.tolist() == ["consistent_source_metadata", "conflicting_statement_labels",
                                              "metadata_not_available_asof", "missing_statement_label"]
    assert result.iloc[0].financial_source_label == True
    assert result.source_industry.iloc[1:].isna().all()
    assert result.financial_source_label.iloc[1:].isna().all()
    assert not result.industry_independently_verified.any()
    with pytest.raises(ValueError, match="timezone"):
        industry_metadata(annual_rows(), "2026-10-05")


def test_integer_capacity_drift_preserves_original_targets_and_unknowns():
    members = pd.DataFrame({"symbol": ["a", "b", "c"], "source_industry": ["tech", "energy", None],
                            "financial_source_label": [False, False, None],
                            "quality": [2., -1., 0.]}).set_index("symbol")
    target, sized = {"a": .4, "b": .4}, {"a": .3, "b": .1}
    original = deepcopy(target)
    result = compare_profiles(members, target, sized)
    assert result["target"]["effective_names"] == 2
    assert result["saved_capacity"]["effective_names"] == pytest.approx(1.6)
    assert result["stock_normalized_weight_distance"] == pytest.approx(.25)
    assert result["gross_difference"] == pytest.approx(-.4)
    assert result["saved_capacity"]["largest_known_industry"]["stock_share"] == pytest.approx(.75)
    assert result["saved_capacity"]["weighted_original_diagnostics"]["quality"] == pytest.approx(1.25)
    assert target == original
    unknown = exposure_profile(members, {"c": .2})
    assert unknown["unknown_industry_gross"] == .2
    assert unknown["largest_known_industry"] is None and unknown["known_industries"] == 0
    assert unknown["industries"][0]["source_industry"] is None


def test_unknown_sizing_identity_or_negative_weight_is_not_imputed():
    members = pd.DataFrame({"symbol": ["a"], "source_industry": [None],
                            "financial_source_label": [None]}).set_index("symbol")
    with pytest.raises(ValueError, match="identity"):
        exposure_profile(members, {"b": .2})
    with pytest.raises(ValueError, match="nonnegative"):
        exposure_profile(members, {"a": -.2})
    empty = exposure_profile(members, {"a": 0.})
    assert empty["gross"] == 0 and empty["effective_names"] is None
