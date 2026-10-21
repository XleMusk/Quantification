import json

import pandas as pd
import pytest

from trading.research.stockdb_score_batch_merge import FALSE_FLAGS, merge_batches


def batch(tmp_path, label, dates):
    provider = tmp_path / "provider"
    provider.mkdir(exist_ok=True)
    (provider / "result.json").write_text(json.dumps({"calendar": [20260105, 20260106]}))
    native = tmp_path / "native.json"
    native.write_text(json.dumps({"provider_view": str(provider), "symbols": 2, "partitions": [{"symbols": ["A", "B"]}]}))
    folder = tmp_path / label
    folder.mkdir()
    scores = pd.DataFrame([{"date": d, "symbol": s, "score_seed17": value, "score_seed29": -value,
                            "score_seed43": value * 2, "rank_mean": 999.0}
                           for d in dates for s, value in (("A", 1.), ("B", 2.))])
    path = folder / "scores.parquet"
    scores.to_parquet(path)
    record = {"status": "completed_research_only_dated_score_batch", "native_run": str(native),
              "model_run": str(tmp_path / "model.json"), "score_path": str(path), "score_rows": len(scores),
              "score_dates": dates, **{k: False for k in FALSE_FLAGS}}
    result = folder / "result.json"
    result.write_text(json.dumps(record))
    return result, path


def test_join_reuses_exact_raw_scores_and_local_ranks_without_modifying_sources(tmp_path):
    first, first_scores = batch(tmp_path, "first", ["2026-01-05"])
    second, second_scores = batch(tmp_path, "second", ["2026-01-06"])
    before = [p.read_bytes() for p in (first, first_scores, second, second_scores)]
    result = merge_batches([first, second], start="2026-01-05", end="2026-01-06", out=tmp_path / "joined")
    scores = pd.read_parquet(result["score_path"])
    assert len(scores) == 4 and result["inference_performed"] is False
    assert scores.loc[scores.symbol.eq("A"), "rank_mean"].tolist() == [2 / 3, 2 / 3]
    assert scores.loc[scores.symbol.eq("B"), "score_seed17"].tolist() == [2., 2.]
    assert before == [p.read_bytes() for p in (first, first_scores, second, second_scores)]


def test_overlapping_saved_dates_fail_without_selecting_or_creating_output(tmp_path):
    first, _ = batch(tmp_path, "first", ["2026-01-05"])
    second, _ = batch(tmp_path, "second", ["2026-01-05"])
    with pytest.raises(ValueError, match="Overlapping"):
        merge_batches([first, second], start="2026-01-05", end="2026-01-06", out=tmp_path / "joined")
    assert not (tmp_path / "joined").exists()
