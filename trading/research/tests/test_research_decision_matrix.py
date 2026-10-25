import json

from trading.research.research_decision_matrix import build_matrix, classify_case


def test_complete_positive_case_continues_research_only():
    row = classify_case({"signal": "x", "mode": "x", "status": "completed",
                         "completed_sessions": 10, "net_return": .05,
                         "max_drawdown": -.1, "orders": 2, "fills": 3}, expected_sessions=10)
    assert row["complete_window"] is True
    assert row["enterable"] is True
    assert row["decision"] == "CONTINUE_RESEARCH_ONLY"
    assert row["promotion_allowed"] is False


def test_incomplete_case_has_no_return_and_is_blocked():
    row = classify_case({"signal": "x", "status": "failed", "completed_sessions": 9,
                         "net_return": .5, "orders": 2, "fills": 3,
                         "error": "source_missing"}, expected_sessions=10)
    assert row["post_event_opportunity"] is None
    assert row["decision"] == "BLOCKED_INCOMPLETE_WINDOW"


def test_saved_public_seat_matrix_preserves_gates():
    source = json.loads(open("outputs/pipeline_receipts/20261024_public_seat_replay_audit_v2.json", encoding="utf-8").read())
    result = build_matrix(source)
    assert result["status"] == "VERIFIED_RESEARCH_DECISION_MATRIX"
    assert result["decision_counts"]["CONTINUE_RESEARCH_ONLY"] == 2
    assert result["decision_counts"]["STOP_RESEARCH_ONLY"] == 3
    assert result["decision_counts"]["BLOCKED_INCOMPLETE_WINDOW"] == 4
    assert result["promotion_gates"]["promotion_allowed"] is False
