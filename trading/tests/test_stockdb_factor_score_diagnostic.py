import json
from pathlib import Path

import pytest

from trading.research import stockdb_factor_score_diagnostic as module


def _run(tmp_path, **updates):
    feature = tmp_path / "features.parquet"
    feature.write_bytes(b"placeholder")
    record = {
        "status": "completed_stockdb_factor_features",
        "execution_admitted": False,
        "strategy_validated": False,
        "model_trained": False,
        "ranking_generated": False,
        "paper_orders_generated": False,
        "target_holdings_generated": False,
        "result": {"feature_path": str(feature), "factors": 190},
    }
    record.update(updates)
    path = tmp_path / "run.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_feature_run_gate_accepts_research_only_artifact(tmp_path):
    path = _run(tmp_path)
    resolved, record = module._resolve_feature_run(path)
    assert resolved == path.resolve()
    assert record["result"]["factors"] == 190


def test_feature_run_gate_rejects_rank_or_execution_flags(tmp_path):
    path = _run(tmp_path, ranking_generated=True)
    with pytest.raises(ValueError, match="non-research flag"):
        module._resolve_feature_run(path)

