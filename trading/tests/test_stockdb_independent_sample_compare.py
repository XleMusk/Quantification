from trading.research.stockdb_independent_sample_compare import classify_sample_window


def test_current_forward_diagnostic_is_not_independent():
    assert classify_sample_window("2026-09-23", "2026-09-24", "2026-10-09") == (False, True)


def test_non_overlapping_holdout_is_independent():
    assert classify_sample_window("2025-01-27", "2025-02-05", "2025-09-23") == (True, False)
