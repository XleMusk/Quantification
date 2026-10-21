"""Saved four-case projections only; no raw, feature calculation or Engine."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import leader_eight_type_history_view as view
from trading.research import leader_known_identity_scope as scope
from trading.research import leader_known_identity_continue as continuation
from trading.research.shortline_panel_replay import _config
from trading.research.tests.test_leader_eight_type_history_view import fixture_history, save, source


def saved_continuation(tmp_path, month):
    folder = tmp_path / month
    _, full = source(folder / "original", month, scope.CASE_IDS, -.2)
    inputs_path = folder / "inputs" / "manifest.json"
    inputs = {"raw_file": str(folder / "never_read" / "raw.parquet"), "source_days": full["source_days"]}
    save(inputs_path, inputs)
    inputs_ref = view._read(inputs_path)[1]
    reg_path = folder / "origin_registration" / "registration.json"
    save(reg_path, {"schema": scope.REGISTRATION_SCHEMA, "method_id": scope.METHOD_ID,
        "policy": deepcopy(scope.SPEC), "case_ids": list(scope.CASE_IDS),
        "parent_fixed_protocol": deepcopy(view.PROTOCOL), "inputs": inputs_ref["path"],
        "inputs_sha256": inputs_ref["sha256"], **view.FLAGS})
    reg_ref = view._read(reg_path)[1]
    old_pair = {"event_view": "saved_old_events", "reference_view": "saved_old_references"}
    for index, case in enumerate(full["cases"]):
        case.update(config=_config(100000, 5).to_dict(), engine_sessions_committed=2,
            complete_PnL_available=True, metrics={"net_return": [-.11, -.2, .05, .02][index],
                "max_drawdown": -.3}, historical_execution_policy={"accounting": deepcopy(old_pair)})
    parent = {**deepcopy(full), "schema": scope.SCHEMA, "method_id": scope.METHOD_ID,
        "registration": reg_ref["path"], "registration_sha256": reg_ref["sha256"],
        "source": {"manifest": inputs_ref["path"], "manifest_sha256": inputs_ref["sha256"],
            "input_record": deepcopy(inputs)}, "scope_members": ["000001.SZSE"],
        "case_count": 4, "entry_information_policy": deepcopy(scope.SPEC),
        "parent_fixed_protocol": deepcopy(view.PROTOCOL), "raw_top5_paths": {}, "capacity_paths": {}}
    selected = [scope.CASE_IDS[1], scope.CASE_IDS[3]]
    for case in parent["cases"]:
        if case["case_id"] in selected:
            case.update(engine_sessions_committed=1, daily=case["daily"][:1], decisions=case["decisions"][:1],
                metrics=None, complete_PnL_available=False,
                blocking_evidence={"day": full["source_days"][1], "reason": "synthetic_missing_terms"})
    parent_path = folder / "parent" / "result.json"
    save(parent_path, parent)
    parent_ref = view._read(parent_path)[1]
    pair = {}
    for key in ("event", "reference"):
        path = folder / ("new_" + key) / "result.json"
        save(path, {"synthetic": True, "execution_admitted": False})
        ref = view._read(path)[1]
        pair[key] = {"directory": str(path.parent.resolve()), "result": ref["path"], "sha256": ref["sha256"]}
    reused = [case_id for case_id in scope.CASE_IDS if case_id not in selected]
    binding = {"continued_case_ids": selected, "reused_case_ids": reused}
    continuation_reg = folder / "continuation_registration" / "registration.json"
    save(continuation_reg, {"schema": continuation.REGISTRATION_SCHEMA, "method_id": continuation.METHOD_ID,
        "spec": deepcopy(continuation.SPEC), "parent_result": parent_ref, "origin_registration": reg_ref,
        "action_pair": pair, "binding": binding, **view.FLAGS})
    continuation_ref = view._read(continuation_reg)[1]
    result = {**deepcopy(parent), "schema": continuation.SCHEMA, "method_id": continuation.METHOD_ID,
        "spec": deepcopy(continuation.SPEC), "registration": continuation_ref["path"],
        "registration_sha256": continuation_ref["sha256"], "origin_registration": reg_ref,
        "parent_result": parent_ref, "action_pair": pair, "source_continuation": binding,
        "case_origins": {}, "continued_case_count": 2, "reused_case_count": 2, "new_cases_executed": 0,
        "cash_reset": False, "old_committed_sessions_replayed": False, "old_accounts_modified": False,
        "independent_holdout_claimed": False, "all_cases_complete_PnL": False}
    for index, old in enumerate(parent["cases"]):
        case_id = old["case_id"]
        continued = case_id in selected
        case = deepcopy(full["cases"][index]) if continued else deepcopy(old)
        if continued:
            case.update(old_committed_sessions_replayed=False, cash_reset=False,
                original_context_preserved=True, committed_prefix_preserved=True,
                historical_execution_policy={"accounting": {
                    "event_view": pair["event"]["directory"], "reference_view": pair["reference"]["directory"]}})
        if index == 3:
            case.update(complete_PnL_available=False, metrics=None,
                blocking_evidence={"day": full["source_days"][-1], "reason": "synthetic_later_block"})
        result["cases"][index] = case
        result["case_origins"][case_id] = {"parent_result": parent_ref, "continued_from_checkpoint": continued,
            "reused_original_case": not continued, "old_committed_sessions_replayed": False,
            "prior_blocking_evidence": deepcopy(old["blocking_evidence"]),
            "reused_sessions": old["engine_sessions_committed"],
            "newly_completed_sessions": case["engine_sessions_committed"] - old["engine_sessions_committed"]}
    path = folder / "result" / "result.json"
    save(path, result)
    save(path.parent / "progress.json", {"schema": continuation.SCHEMA, "status": "completed",
        "result": str(path.resolve()), "continued_case_ids": selected,
        "completed_cases": [{"case_id": case_id, **result["case_origins"][case_id]} for case_id in scope.CASE_IDS]})
    return path, result


def test_saved_two_period_scope4_appends_preserves_old_details_and_reads_only_projection(tmp_path, monkeypatch):
    indexes, previous, data, _ = fixture_history(tmp_path)
    view.build(indexes, previous)
    old_bytes = {file.relative_to(previous): file.read_bytes() for file in (previous / "windows").rglob("*.json")}
    paths = [saved_continuation(tmp_path, month)[0] for month in ("2025-01", "2026-04")]
    output = previous.parent / "with_known_scope"
    built = view.build_known_scope_continuations(paths, previous, output)
    assert built["case_count"] == 64 + 8
    assert all((output / relative).read_bytes() == old for relative, old in old_bytes.items())
    view.publish(output, data)
    read = view._read
    def only_projection(path):
        assert Path(path).resolve() == data / "research/leader_eight_type_history_views/current.json" or output in Path(path).resolve().parents
        return read(path)
    monkeypatch.setattr(view, "_read", only_projection)
    window_id = "known_positive_crossing_2025-01-05_2025-01-05"
    actual = view.read_history(data, window_id, scope.CASE_IDS[0])
    assert actual["status"] == "saved_history_projection" and len(actual["windows"]) == 4
    window = next(window for window in actual["windows"] if window["id"] == window_id)
    assert window["case_count"] == 4 and window["types"] == [scope.TYPE_ID]
    assert window["source_family"] == view.KNOWN_SCOPE_FAMILY and window["entry_information_policy"] == scope.SPEC
    assert [row["metrics"]["net_return"] if row["metrics"] else None for row in window["case_rows"]] == [-.11, -.2, .05, None]
    assert actual["detail"]["days"][0]["environment"]["effective_mode"] == "EXIT_ALL"
    assert actual["detail"]["days"][0]["environment"]["effective_reason"] == "unguarded_original_rule_environment_observation_only"
    assert actual["detail"]["source_continuation"]["reused_original_case"] is True
    continued = view.read_history(data, window_id, scope.CASE_IDS[1])
    assert continued["detail"]["source_continuation"]["newly_completed_sessions"] == 1
    assert continued["detail"]["source_continuation"]["cash_reset"] is False
    assert view.read_history(data, window_id, view.CASE_IDS[0])["status"] == "invalid_selection"
    assert actual["engine_advanced"] is False and actual["profits_recomputed"] is False


def test_changed_continuation_prefix_rejected_before_projection_created(tmp_path):
    indexes, previous, _, _ = fixture_history(tmp_path)
    view.build(indexes, previous)
    path, result = saved_continuation(tmp_path, "2025-01")
    result["cases"][1]["daily"][0]["cash"] = 1
    save(path, result)
    output = previous.parent / "bad_scope"
    with pytest.raises(ValueError, match="committed prefix"):
        view.build_known_scope_continuations([path], previous, output)
    assert not output.exists()
