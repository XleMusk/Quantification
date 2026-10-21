"""Source identity of saved-path diagnostics, using the existing valid fixture."""
from copy import deepcopy

from trading.research.leader_environment_attribution import diagnose_pair
from trading.research.tests.test_leader_environment_attribution import fixture_pair


def sources():
    left, right = fixture_pair()
    policy = {"method_id":"historical_eight_type_native_reported_action_execution_v1",
        "accounting":{"method":"stockdb_reported_action_components_own_raw_reference_diagnostic_v1",
            "knowledge_mode":"posthoc_component_diagnostic",
            "event_view":"D:/synthetic_source/events", "reference_view":"D:/synthetic_source/references",
            "refs":[{"path":"D:/synthetic_source/events/result.json","sha256":"a"*64},
                    {"path":"D:/synthetic_source/references/result.json","sha256":"b"*64}]},
        "old_one_percent_reference_proxy_used":False,
        "risk_mark_validation":"explicit_native_status0_blank_quantity_preserved",
        "candidate_clearance":"exact_D_only_E_guard_before_E_account_mutation",
        "each_case_independent_action_checks":True,"returns_reportable":False,
        "pit_verified":False,"execution_admitted":False}
    for case in (left,right):
        case["historical_execution_policy"] = deepcopy(policy)
    left["source_origin"] = {"mode":"replayed"}
    right["source_origin"] = {"mode":"reused"}
    return left, right


def test_strict_equal_saved_execution_sources_keep_math_and_ignore_only_origin_mode():
    left, right = sources()
    original = deepcopy((left,right))
    saved = diagnose_pair(left,right,require_same_execution_source=True)
    assert saved["status"] == "comparable_posthoc_math_diagnostic", saved
    assert saved["comparison"] is not None and saved["lambda"] == .5
    assert (left,right) == original


def test_strict_changed_pair_hash_rule_or_missing_source_is_not_comparable():
    for change in ("same_path_different_hash","different_pair","different_execution_rule",
                   "unknown_policy","unknown_accounting","unknown_refs"):
        left, right = sources()
        policy = right["historical_execution_policy"]
        if change == "same_path_different_hash":
            policy["accounting"]["refs"][0]["sha256"] = "c"*64
        elif change == "different_pair":
            policy["accounting"]["reference_view"] = "D:/synthetic_source/other_references"
            policy["accounting"]["refs"][1]["path"] = "D:/synthetic_source/other_references/result.json"
        elif change == "different_execution_rule":
            policy["candidate_clearance"] = "different_execution_clearance"
        elif change == "unknown_policy":
            right.pop("historical_execution_policy")
        elif change == "unknown_accounting":
            policy.pop("accounting")
        else:
            policy["accounting"].pop("refs")
        original = deepcopy((left,right))
        saved = diagnose_pair(left,right,require_same_execution_source=True)
        assert saved["status"] == "not_comparable", change
        assert saved["comparison"] is None and saved["lambda"] is None, change
        assert any("execution_source" in reason for reason in saved["reason_codes"]), (change,saved)
        if change in {"same_path_different_hash","different_pair","different_execution_rule"}:
            assert "execution_source_mismatch" in saved["reason_codes"], (change,saved)
        assert (left,right) == original
