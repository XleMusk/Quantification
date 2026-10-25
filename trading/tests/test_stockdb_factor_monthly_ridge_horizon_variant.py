import json

import pytest

from trading.research import stockdb_factor_monthly_ridge_horizon_variant as module


def test_variant_is_fixed_and_research_only():
    assert module.HORIZON == 60
    assert module.PARENT_HORIZON == 20
    assert module.VARIANT_ID == "mature_factor_balanced_ridge_h60"


def test_replay_requires_the_prespecified_run(tmp_path):
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"variant_id": "other", "horizon_sessions": 20}), encoding="utf-8")
    with pytest.raises(ValueError, match="prespecified 60-session"):
        module.replay_variant(run, output=tmp_path / "out")


def test_replay_variant_binds_unique_receipt_and_safety_flags(monkeypatch, tmp_path):
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"variant_id": module.VARIANT_ID, "horizon_sessions": module.HORIZON}), encoding="utf-8")
    observed = {}

    def fake_replay(ridge_run, snapshot, **kwargs):
        observed["ridge_run"] = ridge_run
        observed["snapshot"] = snapshot
        return {"status": "completed_research_only_mature_factor_replay", "metrics": {"net_return": 0.0}}

    def fake_write(result, out, **kwargs):
        observed["result"] = result
        observed["out"] = out
        observed["receipt_name"] = kwargs["receipt_name"]
        return {"result": str(out / "result.json")}

    monkeypatch.setattr(module, "replay", fake_replay)
    monkeypatch.setattr(module, "write_result", fake_write)
    result_path, result = module.replay_variant(run, snapshot=tmp_path / "snapshot", output=tmp_path / "replay")
    assert observed["ridge_run"] == run.resolve()
    assert observed["receipt_name"] == module.REPLAY_RECEIPT
    assert result["variant_id"] == module.VARIANT_ID
    assert result["horizon_sessions"] == module.HORIZON
    assert result["execution_admitted"] is False
    assert result["strategy_validated"] is False
    assert result_path == (tmp_path / "replay" / "result.json").resolve()
