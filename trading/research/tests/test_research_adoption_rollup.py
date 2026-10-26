import json

import pytest

from trading.research.research_adoption_rollup import build_rollup


def _write_receipt(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixtures(tmp_path):
    candidate = _write_receipt(tmp_path / "candidate.json", {
        "schema": "quantification_candidate_adoption_decision_v1",
        "execution_admitted": False, "paper_allowed": False, "promotion_allowed": False,
        "summary": {"continue_research_only": 1},
        "strategies": [{"strategy_id": "factor", "label": "Factor", "research_action": "CONTINUE_RESEARCH_ONLY", "strategy_validated": False, "decision_reason": "research only"}],
    })
    factor = _write_receipt(tmp_path / "factor.json", {
        "schema": "mature_factor_horizon_comparison_v1",
        "machine_status": {"execution_admitted": False, "paper_allowed": False, "promotion_allowed": False, "decision": "NO_ADOPTION_RESEARCH_ONLY"},
        "comparison": {
            "baseline": {"strategy_id": "base", "horizon_sessions": 20, "sessions": 3, "metrics": {"net_return": 0.1, "max_drawdown": -0.1, "sharpe_zero_rf_252": 1.0}},
            "variant": {"strategy_id": "h60", "horizon_sessions": 60, "sessions": 3, "metrics": {"net_return": 0.08, "max_drawdown": -0.11, "sharpe_zero_rf_252": 0.8}},
        },
        "metric_delta_variant_minus_baseline": {"net_return": -0.02, "max_drawdown": -0.01, "sharpe_zero_rf_252": -0.2},
    })
    shortline = _write_receipt(tmp_path / "shortline.json", {
        "schema": "quantification_shortline_adoption_gate_v1",
        "execution_admitted": False, "paper_allowed": False, "promotion_allowed": False,
        "models": [{"model_kind": "lr", "research_action": "CONTINUE_RESEARCH_ONLY", "replay_sessions": 10, "orders": 1, "fills": 2, "net_return": -0.01, "decision_reason": "gates open", "paper_allowed": False, "promotion_allowed": False}],
        "summary": {"models": 1}, "next_gates": ["forward"],
    })
    retained_joint = _write_receipt(tmp_path / "retained_joint.json", {
        "schema": "quantification_retained_joint_candidate_comparison_v1",
        "execution_admitted": False,
        "promotion_allowed": False,
        "incremental_decision": {
            "decision": "CONTINUE_RESEARCH_ONLY",
            "return_incremental_status": "NOT_COMPARABLE_PROJECTION_ONLY",
        },
        "joint_target": {
            "strategy_ids": ["trend", "ridge"],
            "decision_day": "2026-10-13",
            "target_count": 2,
            "paper_order_count": 2,
            "route_available_count": 2,
            "route_blocked_count": 0,
        },
    })
    return candidate, factor, shortline, retained_joint


def test_rollup_binds_all_three_research_routes(tmp_path):
    candidate, factor, shortline, retained_joint = _fixtures(tmp_path)
    result = build_rollup(candidate, factor, shortline, retained_joint)
    assert result["status"] == "COMPLETED_RESEARCH_ONLY_ADOPTION_ROLLUP"
    assert result["execution_admitted"] is False
    assert result["separate_gates"]["mature_factor_horizon"]["variant_minus_baseline"]["net_return"] == -0.02
    assert result["separate_gates"]["shortline_native_lr_lgbm"]["models"][0]["orders"] == 1
    assert result["separate_gates"]["retained_joint_portfolio"]["target_count"] == 2
    assert result["separate_gates"]["retained_joint_portfolio"]["return_incremental_status"] == "NOT_COMPARABLE_PROJECTION_ONLY"


def test_rollup_rejects_execution_boundary(tmp_path):
    candidate, factor, shortline, _retained_joint = _fixtures(tmp_path)
    payload = json.loads(shortline.read_text(encoding="utf-8"))
    payload["execution_admitted"] = True
    shortline.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="execution boundary"):
        build_rollup(candidate, factor, shortline)


def test_rollup_rejects_factor_adoption(tmp_path):
    candidate, factor, shortline, _retained_joint = _fixtures(tmp_path)
    payload = json.loads(factor.read_text(encoding="utf-8"))
    payload["machine_status"]["decision"] = "ADOPT"
    factor.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="no-adoption"):
        build_rollup(candidate, factor, shortline)
