"""Single known action-source failure binding; no loader, Engine or market I/O."""
from copy import deepcopy

import pytest

from trading.research import leader_trend_eligible_scope as m


def fixture_binding(tmp_path):
    old_event, old_reference = tmp_path / "original_event", tmp_path / "original_reference"
    event, reference = tmp_path / "amended_event", tmp_path / "amended_reference"
    original = {"parent_manifest": str(tmp_path / "manifest.json"), "parent_manifest_sha256": "a" * 64,
        "start": "2026-03-02", "end": "2026-03-31", "source_days": ["2026-03-02", "2026-04-01", "2026-04-09"],
        "scope_members": ["001309.SZSE", "600000.SSE"], "spec": deepcopy(m.SPEC),
        "config": m._config(100000, 5).to_dict(),
        "action_event_view": {"directory": str(old_event)},
        "action_reference_view": {"directory": str(old_reference)}}
    cases = []
    for guard in m.GUARDS:
        for cap, _ in m.CAPS:
            identifier = f"trend_anchor_eligible__{guard}__{cap}"
            blocked = identifier == m.SUPPLEMENT_CASE_ID
            cases.append({"case_id": identifier, "type": m.TYPE_ID, "guard": guard, "capacity": cap,
                "complete_PnL_available": not blocked, "metrics": None if blocked else {"net_return": -.08},
                "engine_sessions_committed": 22 if blocked else 28,
                "residual_positions": ["001309.SZSE"] if blocked else [],
                "blocking_evidence": {"day": "2026-04-01",
                    "reason": "held_or_pending_raw_or_action_reference_unknown", "session_not_committed": True,
                    "raw_issues": [], "reference_evidence": [{"symbol": "001309.SZSE", "status": "unresolved_blank_terms"}]}
                    if blocked else None})
    result = {key: deepcopy(original[key]) for key in ("start", "end", "source_days", "scope_members", "spec", "config")}
    result.update(schema=m.RESULT_SCHEMA, method_id=m.METHOD_ID, case_count=4, cases=cases,
        parent_fixed_protocol=deepcopy(m.PROTOCOL), source={"manifest": original["parent_manifest"],
            "manifest_sha256": original["parent_manifest_sha256"]})
    amendment = {"parent_event_view": str(old_event), "parent_reference_view": str(old_reference),
        "events": [["001309.SZSE", "2026-04-01"]], "raw_prices_rescanned": False, "other_events_changed": False}
    events = {"status": "research_action_events_projected", "execution_admitted": False,
        "reviewed_cash_amendment": deepcopy(amendment)}
    references = {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
        "event_view": str(event), "reviewed_cash_amendment": deepcopy(amendment)}
    return original, result, events, references, event, reference


def bind(parts):
    return m._supplement_binding(*parts[:4], event_view=parts[4], reference_view=parts[5])


def test_exact_blocked_case_binding_retains_three_complete_original_case_references(tmp_path):
    parts = fixture_binding(tmp_path)
    before = deepcopy(parts[:4])
    saved = bind(parts)
    expected = [case["case_id"] for case in parts[1]["cases"] if case["case_id"] != m.SUPPLEMENT_CASE_ID]
    assert saved["original_case_id"] == "trend_anchor_eligible__unguarded__uncapped"
    assert saved["event"] == ["001309.SZSE", "2026-04-01"]
    assert saved["original_committed_sessions"] == 22 and saved["original_metrics"] is None
    assert saved["original_residual_positions"] == ["001309.SZSE"]
    assert saved["retained_complete_case_ids"] == expected and len(expected) == 3
    assert saved["replay_from_original_cash"] is True
    assert saved["checkpoint_recovery_performed"] is False and saved["other_three_cases_replayed"] is False
    assert parts[:4] == before
    assert all(case["metrics"] == {"net_return": -.08} for case in parts[1]["cases"]
        if case["case_id"] in expected)


def test_completed_arm_extra_event_parent_and_cost_changes_cannot_select_a_new_replay(tmp_path):
    original = fixture_binding(tmp_path)
    for mutation in ("completed_arm", "extra_event", "parent", "cost"):
        parts = deepcopy(original)
        if mutation == "completed_arm":
            selected = next(case for case in parts[1]["cases"] if case["case_id"] == m.SUPPLEMENT_CASE_ID)
            selected.update(complete_PnL_available=True, metrics={"net_return": .9}, blocking_evidence=None)
        elif mutation == "extra_event":
            for metadata in parts[2:4]:
                metadata["reviewed_cash_amendment"]["events"].append(["600000.SSE", "2026-04-01"])
        elif mutation == "parent":
            for metadata in parts[2:4]:
                metadata["reviewed_cash_amendment"]["parent_event_view"] = str(tmp_path / "another_parent")
        else:
            parts[1]["config"]["commission"] = .01
        with pytest.raises(ValueError):
            bind(parts)
