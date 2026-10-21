from trading.research_diagnostics import _research_comparison_eligible


def test_independent_sample_is_comparable_without_selection_eligibility():
    assert _research_comparison_eligible({
        "family": "stockdb_independent_sample_compare",
        "independent_sample": True,
        "prior_forward_overlap": False,
        "provider_complete_paired_rows": 15,
    }) is True
    assert _research_comparison_eligible({
        "family": "stockdb_independent_sample_compare",
        "independent_sample": False,
        "prior_forward_overlap": True,
        "provider_complete_paired_rows": 15,
    }) is False


def test_stability_requires_non_overlapping_paired_windows():
    assert _research_comparison_eligible({
        "family": "stockdb_independent_window_stability",
        "non_overlapping": True,
        "window_count": 5,
        "paired_rows_all_windows": 15,
    }) is True
    assert _research_comparison_eligible({
        "family": "stockdb_independent_window_stability",
        "non_overlapping": True,
        "window_count": 1,
        "paired_rows_all_windows": 15,
    }) is False
