import pandas as pd

from trading.research import stockdb_factor_lineage_overlap as module


def _panel(path, values):
    index = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-09-30")], ["000001.SZSE", "000002.SZSE", "000003.SZSE"]],
        names=["date", "symbol"],
    )
    pd.DataFrame(values, index=index).to_parquet(path)


def test_overlap_reports_rank_and_top30_agreement(tmp_path):
    old = tmp_path / "old.parquet"
    new = tmp_path / "new.parquet"
    _panel(old, {"Alpha158/A": [1.0, 2.0, 3.0], "Alpha360/B": [4.0, 3.0, 2.0]})
    _panel(new, {"Alpha158/A": [2.0, 4.0, 6.0], "Alpha360/B": [8.0, 6.0, 4.0]})
    candidate = tmp_path / "candidate.csv"
    pd.DataFrame([
        {"factor": "Alpha158/A", "library": "Alpha158", "train_direction": "high"},
        {"factor": "Alpha360/B", "library": "Alpha360", "train_direction": "low"},
    ]).to_csv(candidate, index=False)
    summary, record = module.compare_feature_panels(old, new, candidate, "2026-09-30")
    assert record["common_symbols"] == 3
    assert record["finite_pair_ge_0_9"] == 2
    assert summary["top30_overlap"].tolist() == [1.0, 1.0]


def test_overlap_rejects_factor_identity_mismatch(tmp_path):
    old = tmp_path / "old.parquet"
    new = tmp_path / "new.parquet"
    _panel(old, {"Alpha158/A": [1.0, 2.0, 3.0], "Alpha360/B": [4.0, 3.0, 2.0]})
    _panel(new, {"Alpha158/A": [2.0, 4.0, 6.0], "Alpha360/B": [8.0, 6.0, 4.0]})
    candidate = tmp_path / "candidate.csv"
    pd.DataFrame([{"factor": "Alpha158/A", "library": "Alpha158", "train_direction": "high"}]).to_csv(candidate, index=False)
    try:
        module.compare_feature_panels(old, new, candidate, "2026-09-30")
    except ValueError as error:
        assert "factor set" in str(error)
    else:
        raise AssertionError("factor identity mismatch must be rejected")

