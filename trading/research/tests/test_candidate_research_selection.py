import json

from trading.research.candidate_research_selection import build_selection_plan


def _load(path):
    return json.loads(open(path, encoding="utf-8").read())


def test_saved_candidate_library_gets_fixed_research_queue():
    result = build_selection_plan(
        _load("outputs/pipeline_receipts/20261024_strategy_robustness_matrix_v1.json"),
        _load("docs/architecture_v3/candidate_strategy_catalog_20261024.json"),
    )
    assert result["status"] == "VERIFIED_CANDIDATE_RESEARCH_SELECTION"
    assert result["research_followup_queue"] == [
        "stockdb_low_vol_half_momentum_20", "factor_candidate_family_balanced_ridge"
    ]
    assert result["coverage_blocked"] == ["stockdb_momentum_12_1_20"]
    assert set(result["stopped"]) == {"stockdb_low_volatility_252_20", "alpha158_lgbm_candidate_panel_v1"}
    assert all(not row["paper_allowed"] and not row["promotion_allowed"] for row in result["rows"])


def test_unassessed_candidate_keeps_separate_gate():
    result = build_selection_plan(
        _load("outputs/pipeline_receipts/20261024_strategy_robustness_matrix_v1.json"),
        _load("docs/architecture_v3/candidate_strategy_catalog_20261024.json"),
    )
    row = next(x for x in result["rows"] if x["catalog_id"] == "shortline_native_lr_lgbm")
    assert row["research_action"] == "UNASSESSED_SEPARATE_GATE"
    assert row["research_followup_allowed"] is False


def test_synthetic_policy_keeps_mixed_as_research_only():
    result = build_selection_plan(
        {"strategies": [{"strategy_id": "fixed_low_volatility_252_20", "decision": "MIXED_EVIDENCE_RESEARCH_ONLY"}]},
        {"entries": [{"id": value, "state": "X"} for value in {
            "stockdb_low_volatility_252_20", "stockdb_low_vol_half_momentum_20",
            "stockdb_momentum_12_1_20", "factor_candidate_family_balanced_ridge",
            "alpha158_lgbm_candidate_panel_v1"}]},
    )
    assert result["research_followup_queue"] == ["stockdb_low_volatility_252_20"]
    assert result["selection_enabled"] is False
