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


def test_rollup_binds_completed_d0_d1_without_promoting_it(tmp_path):
    candidate, factor, shortline, retained_joint = _fixtures(tmp_path)
    d0_d1 = _write_receipt(tmp_path / "d0_d1.json", {
        "schema": "stockdb_next_close_d0_d1_contract_v1",
        "status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "close_date": "2026-10-26",
        "snapshot_coverage": {
            "status": "COMPLETE", "requested_symbol_count": 40,
            "available_symbol_count": 40, "missing_symbols": [],
        },
        "fixed": {"strategy_id": "low_volatility_252_20", "target_count": 20,
                  "paper_order_count": 20},
        "candidate_batch": {"candidate_count": 1},
        "joint_paper": {"paper_order_count": 22},
        "persistent_fixed_daily_account_scope": "direct_terminal_account_input",
    })
    result = build_rollup(candidate, factor, shortline, retained_joint, d0_d1)
    latest = result["separate_gates"]["latest_d0_d1_paper"]
    assert latest["close_date"] == "2026-10-26"
    assert latest["available_symbol_count"] == 40
    assert latest["joint_paper_order_count"] == 22
    assert result["next_actions"][0].startswith("D0/D1 close 2026-10-26")
    assert result["execution_admitted"] is False


def test_rollup_rejects_incomplete_d0_d1_coverage(tmp_path):
    candidate, factor, shortline, _retained_joint = _fixtures(tmp_path)
    d0_d1 = _write_receipt(tmp_path / "d0_d1.json", {
        "schema": "stockdb_next_close_d0_d1_contract_v1",
        "status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
        "execution_admitted": False, "broker_interacted": False,
        "ths_interacted": False, "persistent_ledger_mutated": False,
        "snapshot_coverage": {"status": "COMPLETE", "missing_symbols": ["000001.SZSE"]},
        "fixed": {}, "candidate_batch": {}, "joint_paper": {},
    })
    with pytest.raises(ValueError, match="coverage"):
        build_rollup(candidate, factor, shortline, d0_d1_path=d0_d1)


def test_rollup_binds_current_window_without_promotion(tmp_path):
    candidate, factor, shortline, retained_joint = _fixtures(tmp_path)
    current_window = _write_receipt(tmp_path / "current_window.json", {
        "schema": "quantification_current_window_strategy_comparison_v1",
        "status": "COMPLETED_RESEARCH_ONLY_CURRENT_WINDOW_COMPARISON",
        "execution_admitted": False, "strategy_validated": False,
        "ledger_mutated": False, "broker_or_ths_interacted": False,
        "promotion_allowed": False, "stockdb_snapshot": "snapshot-v1",
        "common_window": {"start": "2026-08-31", "end": "2026-10-26", "sessions": 35},
        "strategies": [
            {"id": "fixed", "label": "Fixed", "execution_admitted": False,
             "strategy_validated": False, "research_decision": "CONTINUE_RESEARCH_ONLY",
             "metrics": {"net_return": 0.01, "max_drawdown": -0.02, "sharpe_zero_rf_252": 0.8, "orders": 2, "fills": 3, "fees_rmb": 1.0}},
            {"id": "candidate", "label": "Candidate", "execution_admitted": False,
             "strategy_validated": False, "research_decision": "STOP_RESEARCH_ONLY",
             "metrics": {"net_return": -0.02, "max_drawdown": -0.03, "sharpe_zero_rf_252": -0.5, "orders": 4, "fills": 5, "fees_rmb": 2.0}},
        ],
    })
    result = build_rollup(candidate, factor, shortline, retained_joint,
                          current_window_path=current_window)
    latest = result["separate_gates"]["latest_current_window"]
    assert latest["common_window"]["sessions"] == 35
    assert latest["strategies"][1]["research_decision"] == "STOP_RESEARCH_ONLY"
    assert result["promotion_allowed"] is False
    assert result["next_actions"][1].startswith("Current-window 2026-08-31")


def test_rollup_rejects_current_window_boundary_crossing(tmp_path):
    candidate, factor, shortline, _retained_joint = _fixtures(tmp_path)
    current_window = _write_receipt(tmp_path / "current_window.json", {
        "schema": "quantification_current_window_strategy_comparison_v1",
        "status": "COMPLETED_RESEARCH_ONLY_CURRENT_WINDOW_COMPARISON",
        "execution_admitted": True, "strategy_validated": False,
        "ledger_mutated": False, "broker_or_ths_interacted": False,
        "promotion_allowed": False, "common_window": {"sessions": 35},
        "stockdb_snapshot": "snapshot-v1", "strategies": [],
    })
    with pytest.raises(ValueError, match="current-window execution boundary"):
        build_rollup(candidate, factor, shortline, current_window_path=current_window)
