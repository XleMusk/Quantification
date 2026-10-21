"""Date-local frozen ensemble ranks; no training or market-source access."""
from pathlib import Path
import json

import numpy as np
import pandas as pd

from trading.research import stockdb_alpha158_lgbm_diagnostic as diagnostic
from trading.research.stockdb_alpha158_lgbm_panel_replay import load_scores


def _models(tmp_path, monkeypatch):
    columns = ["seed17", "seed29", "seed43"]
    paths = {}
    for seed in diagnostic.SEEDS:
        folder = tmp_path / str(seed)
        folder.mkdir()
        pd.DataFrame({"train_median": [0.0] * 3}, index=columns).to_csv(folder / "preprocessing.csv")
        paths[seed] = folder

    class Booster:
        def __init__(self, model_file):
            self.column = diagnostic.SEEDS.index(int(Path(model_file).parent.name))

        def feature_name(self):
            return columns

        def predict(self, values, num_threads):
            return values.iloc[:, self.column].to_numpy()

    monkeypatch.setattr(diagnostic.lgb, "Booster", Booster)
    return paths, columns


def _frame(values):
    return pd.DataFrame(values, columns=["date", "symbol", "seed17", "seed29", "seed43"]).set_index(["date", "symbol"])


def test_later_session_additions_and_distribution_changes_do_not_change_earlier_rank(tmp_path, monkeypatch):
    paths, columns = _models(tmp_path, monkeypatch)
    earlier = _frame([
        ("2026-09-01", "A", 10.0, 0.0, 0.0),
        ("2026-09-01", "B", 0.0, 10.0, 10.0),
    ])
    later = _frame([
        ("2026-09-02", "X", 5.0, -5.0, -5.0),
        ("2026-09-02", "Y", 6.0, -6.0, -6.0),
    ])
    expected = diagnostic._frozen_predictions(earlier, paths, columns)
    for following in (later, later.mul(-1000), pd.concat([later, later.rename(index={"X": "Z", "Y": "W"}, level="symbol").mul(1000)])):
        observed = diagnostic._frozen_predictions(pd.concat([earlier, following]), paths, columns)
        pd.testing.assert_frame_equal(observed.loc[earlier.index], expected, check_exact=True)

    # The former whole-batch normalization really reverses this seed-conflict
    # example: this assertion keeps the regression from becoming a tautology.
    joint = pd.concat([earlier, later])
    old_ranks = joint.rank(pct=True).mean(axis=1).loc[earlier.index]
    assert old_ranks.idxmax()[-1] == "A"
    assert expected["rank_mean"].idxmax()[-1] == "B"


def test_single_date_prediction_and_ties_remain_exactly_the_same(tmp_path, monkeypatch):
    paths, columns = _models(tmp_path, monkeypatch)
    frame = _frame([
        ("2026-09-01", "A", 2.0, 1.0, 3.0),
        ("2026-09-01", "B", 2.0, 3.0, 1.0),
        ("2026-09-01", "C", 1.0, 2.0, 2.0),
    ])
    result = diagnostic._frozen_predictions(frame, paths, columns)
    expected = frame.rank(pct=True).mean(axis=1).rename("rank_mean")
    pd.testing.assert_series_equal(result["rank_mean"], expected, check_exact=True)
    np.testing.assert_array_equal(result[["score_seed17", "score_seed29", "score_seed43"]].to_numpy(), frame.to_numpy())


def _saved_batch(folder, frame):
    folder.mkdir()
    scores = frame.rename(columns={"seed17": "score_seed17", "seed29": "score_seed29", "seed43": "score_seed43"}).copy()
    scores["rank_mean"] = scores.rank(pct=True).mean(axis=1)
    score_path = folder / "dated_scores.parquet"
    scores.reset_index().to_parquet(score_path, index=False)
    record = {"status": "completed_research_only_dated_score_batch", "score_path": str(score_path)}
    record.update({key: False for key in ("model_refit", "future_labels_used_for_score", "execution_admitted",
        "strategy_validated", "ledger_mutated", "target_holdings_generated", "paper_orders_generated", "ths_payload_generated")})
    (folder / "result.json").write_text(json.dumps(record), encoding="utf-8")
    return folder / "result.json", score_path


def test_saved_legacy_batch_loader_corrects_cross_date_rank_and_leaves_payloads_unchanged(tmp_path):
    frame = _frame([
        ("2026-09-01", "A.SSE", 10.0, 0.0, 0.0),
        ("2026-09-01", "B.SSE", 0.0, 10.0, 10.0),
        ("2026-09-02", "X.SSE", 5.0, -5.0, -5.0),
        ("2026-09-02", "Y.SSE", 6.0, -6.0, -6.0),
    ])
    record, payload = _saved_batch(tmp_path / "legacy", frame)
    before = [path.read_bytes() for path in (record, payload)]
    legacy = pd.read_parquet(payload).query("date == '2026-09-01'")
    assert legacy.sort_values("rank_mean", ascending=False).iloc[0].symbol == "A.SSE"
    corrected, metadata = load_scores(record, top_per_day=2, start="2026-09-01", end="2026-09-01")
    assert corrected.iloc[0].symbol == "B.SSE"
    assert corrected.iloc[0].score == 5 / 6
    assert metadata["saved_rank_mean_used_for_selection"] is False
    assert metadata["model_refit"] is False
    assert metadata["saved_score_normalization_method"] == "whole_batch_cross_date_seed_percentile_rank_mean_v1"
    assert metadata["selection_score_normalization_method"] == diagnostic.SCORE_NORMALIZATION_METHOD
    assert [path.read_bytes() for path in (record, payload)] == before


def test_saved_loader_single_day_unchanged_and_later_rows_do_not_change_selection(tmp_path):
    earlier = _frame([
        ("2026-09-01", "A.SSE", 10.0, 0.0, 0.0),
        ("2026-09-01", "B.SSE", 0.0, 10.0, 10.0),
    ])
    later = _frame([
        ("2026-09-02", "X.SSE", -5000.0, 5000.0, 5000.0),
        ("2026-09-02", "Y.SSE", 6000.0, -6000.0, -6000.0),
        ("2026-09-03", "Z.SSE", 10.0, -5.0, 2000.0),
    ])
    single, single_payload = _saved_batch(tmp_path / "single", earlier)
    multi, _ = _saved_batch(tmp_path / "multi", pd.concat([earlier, later]))
    one, _ = load_scores(single, top_per_day=2)
    saved = pd.read_parquet(single_payload).set_index("symbol")
    pd.testing.assert_series_equal(one.set_index("symbol").score.sort_index(),
        saved.rank_mean.sort_index().rename("score"), check_exact=True)
    joined, _ = load_scores(multi, top_per_day=2)
    sliced, _ = load_scores(multi, top_per_day=2, start="2026-09-01", end="2026-09-01")
    pd.testing.assert_frame_equal(one, joined.loc[joined.date.eq("2026-09-01")].reset_index(drop=True), check_exact=True)
    pd.testing.assert_frame_equal(one, sliced, check_exact=True)
