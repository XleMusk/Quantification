import numpy as np
import pandas as pd
import pytest

from trading.research import stockdb_factor_lineage_compare as module


def _panel(values, columns):
    idx = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-09-30")], ["000001.SZSE", "000002.SZSE", "000003.SZSE"]],
        names=["date", "symbol"],
    )
    return pd.DataFrame(values, index=idx, columns=columns)


def test_compare_frames_reports_raw_and_oriented_rank_diagnostics():
    candidates = pd.DataFrame([
        {"factor": "Alpha158/A", "library": "Alpha158", "family": "A",
         "original_name": "A", "train_direction": "high"},
        {"factor": "Alpha158/B", "library": "Alpha158", "family": "B",
         "original_name": "B", "train_direction": "low"},
    ])
    old = _panel([[1.0, 10.0], [2.0, 20.0], [np.nan, 30.0]], candidates.factor.tolist())
    new = _panel([[2.0, 30.0], [4.0, 20.0], [6.0, 10.0]], candidates.factor.tolist())
    metrics, symbols, ranks = module.compare_frames(old, new, candidates)

    assert len(symbols) == 3
    assert set(ranks.index.get_level_values("lineage")) == {"old", "new"}
    a = metrics.set_index("factor").loc["Alpha158/A"]
    b = metrics.set_index("factor").loc["Alpha158/B"]
    assert a.old_finite == 2 and a.new_finite == 3 and a.pair_finite == 2
    assert b.old_finite == b.new_finite == b.pair_finite == 3
    assert a.raw_spearman == pytest.approx(1.0)
    assert b.raw_spearman == pytest.approx(-1.0)
    assert np.isfinite(a.rank_mae)
    assert np.isfinite(b.rank_mae)
