from trading.research.admission_gate import evaluate


def _inputs():
    return {
        "status": "waiting_for_new_complete_snapshot",
        "fresh_holdout_claimed": False,
        "execution_admitted": False,
    }, {
        "status": "completed_saved_account_concentration",
        "stale_valuation_days": 33,
        "blocked_attempts": {"limit_down_queue_unknown": 4, "suspended": 3},
        "execution_admitted": False,
    }, {
        "status": "completed_saved_stress_exposure",
        "unknown_rows": 1,
        "execution_admitted": False,
    }


def _turnover():
    return {
        "status": "completed_saved_parent_order_attribution",
        "cases": [{"fees_cny": 100.0, "filled_turnover_cny": 100000.0}],
    }


def _protocol():
    return {"candidate_rights_policy": {"id": "rights_replay_waive_all_v1"}}


def test_current_evidence_is_diagnostic_only_and_preserves_unknowns():
    result = evaluate(*_inputs(), holdout_protocol=_protocol())
    assert result["status"] == "blocked"
    assert result["stage"] == "diagnostic_only"
    assert result["execution_admitted"] is False
    assert "holdout_not_completed:waiting_for_new_complete_snapshot" in result["reasons"]
    assert "stale_valuation_days:33" in result["reasons"]
    assert "blocked_execution_attempts:7" in result["reasons"]
    assert "unknown_stress_marks:1" in result["reasons"]
    assert "broker_account_observation_missing_or_unreconciled" in result["reasons"]
    assert "cost_and_turnover_receipt_missing_or_invalid" in result["reasons"]
    assert "rights_replay_waiver_not_account_evidence" in result["reasons"]
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["risk_evidence_completeness"]["passed"] is False
    assert checks["stress_mark_completeness"]["passed"] is False


def test_complete_holdout_still_needs_clean_risk_and_account_evidence():
    holdout, risk, stress = _inputs()
    holdout.update(status="holdout_replay_completed", fresh_holdout_claimed=True)
    risk.update(stale_valuation_days=0, blocked_attempts={})
    stress.update(unknown_rows=0)
    result = evaluate(holdout, risk, stress, turnover=_turnover(), holdout_protocol=_protocol())
    assert result["stage"] == "holdout_complete_evidence_blocked"
    assert result["execution_admitted"] is False
    assert result["reasons"] == ["holdout_strategy_not_validated",
                                  "rights_replay_waiver_not_account_evidence",
                                  "broker_account_observation_missing_or_unreconciled"]


def test_broker_evidence_does_not_turn_gate_into_live_execution():
    holdout, risk, stress = _inputs()
    holdout.update(status="holdout_replay_completed", fresh_holdout_claimed=True, strategy_validated=True)
    risk.update(stale_valuation_days=0, blocked_attempts={})
    stress.update(unknown_rows=0)
    result = evaluate(holdout, risk, stress, {
        "status": "verified_account_observation",
        "cash_positions_reconciled": True,
    }, _turnover(), _protocol())
    assert result["stage"] == "holdout_complete_evidence_blocked"
    assert result["execution_admitted"] is False
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["risk_evidence_completeness"]["passed"] is True
    assert checks["stress_mark_completeness"]["passed"] is True


def test_unclaimed_completed_holdout_cannot_be_misclassified_as_broker_ready():
    holdout, risk, stress = _inputs()
    holdout.update(status="holdout_replay_completed", fresh_holdout_claimed=False, strategy_validated=True)
    risk.update(stale_valuation_days=0, blocked_attempts={})
    stress.update(unknown_rows=0)
    result = evaluate(
        holdout, risk, stress,
        {"status": "verified_account_observation", "cash_positions_reconciled": True},
        _turnover(),
        {"candidate_rights_policy": {"id": "rights_replay_account_bound_v1"}},
    )
    assert result["stage"] == "holdout_complete_evidence_blocked"
    assert "fresh_holdout_not_claimed" in result["reasons"]
    assert result["execution_admitted"] is False
