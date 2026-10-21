import pandas as pd
import pytest

from trading.research.shortline_split import closed_label_window, purge_training_labels


def test_purge_training_labels_excludes_runs_ending_after_fold_boundary():
    frame = pd.DataFrame(
        {
            "event_id": ["closed", "crossing", "missing", "outside"],
            "date": ["2024-12-20", "2024-12-25", "2024-12-26", "2025-01-02"],
            "run_end_date": ["2024-12-24", "2025-01-02", None, "2025-01-03"],
        }
    )
    kept, stats = purge_training_labels(frame, "2024-01-01", "2024-12-31")
    assert kept["event_id"].tolist() == ["closed"]
    assert stats == {"candidate_rows": 3, "purged_rows": 2, "kept_rows": 1}


def test_purge_training_labels_requires_label_end_column():
    with pytest.raises(KeyError, match="run_end_date"):
        purge_training_labels(pd.DataFrame({"date": ["2024-01-01"]}), "2024-01-01", "2024-12-31")


def test_closed_label_window_purges_evaluation_rows_beyond_test_end():
    frame = pd.DataFrame(
        {"event_id": ["inside", "continuing"], "date": ["2025-12-20", "2025-12-31"],
         "run_end_date": ["2025-12-24", "2026-01-02"]}
    )
    kept, stats = closed_label_window(frame, "2025-01-01", "2025-12-31")
    assert kept["event_id"].tolist() == ["inside"]
    assert stats["purged_rows"] == 1

