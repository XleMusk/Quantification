import json

from trading.research.strategy_robustness_matrix import build_matrix, classify_evidence


def test_fixed_rule_distinguishes_missing_and_mixed_evidence():
    assert classify_evidence({"sessions": 10, "net_return": .1}, None, None) == "BLOCKED_COVERAGE"
    assert classify_evidence({"sessions": 10, "net_return": .1}, {"sessions": 2, "net_return": -.1, "holdout_untouched": True, "future_scores_used": False}, None) == "MIXED_EVIDENCE_RESEARCH_ONLY"
    assert classify_evidence({"sessions": 10, "net_return": -.1}, {"sessions": 2, "net_return": -.1, "holdout_untouched": True, "future_scores_used": False}, None) == "STOP_RESEARCH_ONLY"


def test_saved_matrix_has_fixed_decisions_and_closed_gates():
    def load(name):
        return json.loads(open("outputs/pipeline_receipts/" + name, encoding="utf-8").read())
    result = build_matrix(load("20261024_long_window_strategy_comparison_v1.json"), load("20261024_common_untouched_forward_comparison_v2.json"), load("20261024_p05_capacity_cost_comparison_v1.json"))
    assert result["status"] == "VERIFIED_RESEARCH_ROBUSTNESS_MATRIX"
    assert result["decision_counts"] == {"STOP_RESEARCH_ONLY": 2, "MIXED_EVIDENCE_RESEARCH_ONLY": 2, "BLOCKED_COVERAGE": 1}
    assert all(row["promotion_allowed"] is False for row in result["strategies"])
    assert result["execution_admitted"] is False


def test_forward_mapping_keeps_half_momentum_lineage():
    def load(name):
        return json.loads(open("outputs/pipeline_receipts/" + name, encoding="utf-8").read())
    result = build_matrix(load("20261024_long_window_strategy_comparison_v1.json"), load("20261024_common_untouched_forward_comparison_v2.json"), load("20261024_p05_capacity_cost_comparison_v1.json"))
    row = next(x for x in result["strategies"] if x["strategy_id"] == "low_vol_half_momentum_20")
    assert row["forward_strategy_id"] == "trend_low_vol_half_momentum_20"
    assert row["untouched_forward"]["holdout_untouched"] is True
