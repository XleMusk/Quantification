import json

import numpy as np
import pandas as pd

from trading.research import stockdb_alpha158_lgbm_diagnostic as module


def test_frozen_predictions_preserve_seed_columns_and_rank(monkeypatch, tmp_path):
    columns = ["A", "B"]
    folder = tmp_path / "seed"
    folder.mkdir()
    pd.DataFrame({"train_median": [0.0, 1.0]}, index=columns).to_csv(folder / "preprocessing.csv")

    class FakeBooster:
        def __init__(self, model_file):
            assert model_file.endswith("model.txt")

        def feature_name(self):
            return columns

        def predict(self, frame, num_threads=2):
            return frame["A"].to_numpy() + frame["B"].to_numpy()

    (folder / "model.txt").write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(module.lgb, "Booster", FakeBooster)
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-10-13"), "000001.SZSE"),
         (pd.Timestamp("2026-10-13"), "000002.SZSE")], names=["date", "symbol"])
    frame = pd.DataFrame({"A": [1.0, 2.0], "B": [3.0, np.nan]}, index=index)

    result = module._frozen_predictions(frame, {17: folder, 29: folder, 43: folder}, columns)

    assert result.columns.tolist() == ["score_seed17", "score_seed29", "score_seed43", "rank_mean"]
    assert result[["score_seed17", "score_seed29", "score_seed43"]].notna().all().all()
    assert result.rank_mean.between(0.0, 1.0).all()


def test_load_eligibility_maps_stockdb_symbols_to_vt_symbols(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    pd.DataFrame({
        "symbol": ["sz.000001", "sh.600000", "sz.000002"],
        "observed_compatibility_known": [True, True, False],
        "observed_eligible_proxy": [True, False, True],
    }).to_csv(artifacts / "latest_eligibility_observation.csv", index=False)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "status": "completed_research_only_eligibility_compatibility",
        "execution_admitted": False,
        "latest_session": "2026-10-13",
        "artifacts": str(artifacts),
    }), encoding="utf-8")

    eligible, metadata = module._load_eligibility(receipt, pd.Timestamp("2026-10-13"))

    assert eligible.index.tolist() == ["000001.SZSE"]
    assert metadata["eligible_count"] == 1

