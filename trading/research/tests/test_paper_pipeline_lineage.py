import json

import pytest

from trading.research.paper_pipeline_lineage import VERIFIED, build_lineage


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_lineage_rejects_client_interaction(tmp_path):
    post = _write(tmp_path / "post.json", {"status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1", "execution_admitted": False, "broker_interacted": False, "ths_interacted": False, "persistent_ledger_mutated": False})
    fixed = _write(tmp_path / "fixed.json", {"schema": "stockdb_paper_target_order_package_v1", "execution_admitted": False, "client_interacted": False, "submitted": False, "ledger_persisted": False})
    joint = _write(tmp_path / "joint.json", {"execution_admitted": False, "broker_interacted": False, "ths_interacted": False, "persistent_ledger_mutated": False})
    ths = _write(tmp_path / "ths.json", {"execution_admitted": False, "client_interacted": True, "submitted": False, "ledger_mutated": False})
    with pytest.raises(ValueError, match="client_interacted"):
        build_lineage(post, fixed, joint, ths, tmp_path / "out.json")


def test_lineage_builds_saved_stage_summary(tmp_path):
    snapshot = _write(tmp_path / "snapshot.json", {})
    candidate = _write(tmp_path / "candidate.json", {"candidates": []})
    files = {name: _write(tmp_path / name, "x") for name in ("fixed_targets.csv", "fixed_orders.csv", "fixed_account.json", "joint_targets.csv", "joint_orders.csv")}
    joint_account = _write(tmp_path / "joint_account.json", {"initial_cash_rmb": 100, "cash_after_modeled_fills_rmb": 90, "equity_at_observed_marks_rmb": 99, "positions": {"000001.SZSE": 100}})
    post = _write(tmp_path / "post.json", {"status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1", "execution_admitted": False, "broker_interacted": False, "ths_interacted": False, "persistent_ledger_mutated": False, "close_date": "2026-01-01", "execution_day": "2026-01-02", "settlement_day": "2026-01-05", "input_fingerprint": "fp", "snapshot": {"path": str(snapshot)}, "candidate_source_manifest": {"path": str(candidate)}, "snapshot_coverage": {"status": "COMPLETE"}})
    fixed = _write(tmp_path / "fixed.json", {"schema": "stockdb_paper_target_order_package_v1", "strategy_id": "fixed_strategy", "execution_admitted": False, "client_interacted": False, "submitted": False, "ledger_persisted": False, "target_count": 1, "paper_order_count": 1, "artifacts": {"target_holdings": str(files["fixed_targets.csv"]), "order_plan": str(files["fixed_orders.csv"]), "projected_account": str(files["fixed_account.json"])}})
    joint = _write(tmp_path / "joint.json", {"execution_admitted": False, "broker_interacted": False, "ths_interacted": False, "persistent_ledger_mutated": False, "paper_package": {"result": {"schema": "stockdb_paper_target_order_package_v1", "target_count": 1, "paper_order_count": 1, "artifacts": {"target_holdings": str(files["joint_targets.csv"]), "order_plan": str(files["joint_orders.csv"]), "projected_account": str(joint_account)}}}})
    ths = _write(tmp_path / "ths.json", {"execution_admitted": False, "client_interacted": False, "submitted": False, "ledger_mutated": False, "status": "READBACK_VERIFIED_SYNTHETIC_ONLY"})
    result = build_lineage(post, fixed, joint, ths, tmp_path / "out.json")
    assert result["status"] == VERIFIED
    assert result["stages"]["broker_adapter"]["status"] == "REFERENCE_PAYLOAD_ONLY"
    assert result["stages"]["signal"]["strategies"] == ["fixed_strategy"]
    assert result["stages"]["signal"]["candidate_count"] == 0
    assert result["stages"]["cash_position"]["position_count"] == 1
