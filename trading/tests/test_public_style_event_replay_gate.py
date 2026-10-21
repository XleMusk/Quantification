import json
from pathlib import Path


GATE = Path(
    r"D:\Quantification\outputs\trader_style_research\20261014_public_accounts\PUBLIC_STYLE_EVENT_REPLAY_GATE_20261014.json"
)


def test_public_style_gate_separates_identity_from_replay_admission():
    gate = json.loads(GATE.read_text(encoding="utf-8"))

    assert gate["schema"] == "public_style_event_replay_gate_v1"
    assert gate["status"] == "blocked_missing_native_post_and_trade_semantics"
    checks = {row["id"]: row["result"] for row in gate["gate_checks"]}
    assert checks["identity_binding"] == "pass"
    assert checks["post_grain"] == "fail"
    assert checks["decision_fields"] == "fail"
    assert checks["execution_fields"] == "fail"
    assert checks["fixed_rule_spec"] == "fail"

    identity = gate["identity_resolution"]
    assert identity["status"] == "exact_ths_profile_confirmed"
    assert identity["user_id"] == "639669238"
    assert identity["circle_id"] == "305529"
    assert identity["follower_count_observed"] >= 500_000

    # Public identity evidence must never silently become an order or paper target.
    assert gate["execution_admitted"] is False
    assert gate["paper_target_admitted"] is False
    assert "automatic signal copying" in gate["does_not_support"]

