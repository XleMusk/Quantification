"""Saved two-case source supplement -> display only; no Engine or market I/O."""
from copy import deepcopy

import pytest

from trading.research import leader_eight_type_history_view as view
from trading.research import leader_environment_exit_ablation as ablation
from trading.research.tests.test_leader_eight_type_history_view import fixture_history, source, save
from trading.research.tests.test_leader_history_ablation_view import saved_ablation, CONTROL


def fixture_supplement(tmp_path):
    indexes, previous, data, _ = fixture_history(tmp_path)
    march_file, march = source(tmp_path / "sources/march", "2026-03", view.CASE_IDS, -.02)
    march_rows = [{**{key: deepcopy(case[key]) for key in ("case_id", "type", "guard", "capacity", "status", "metrics", "residual_positions")},
        "source_result": str(march_file), "order_count": len(case["orders"]), "fill_count": len(case["fills"])} for case in march["cases"]]
    march_index = tmp_path / "sources/march_index.json"
    save(march_index, {"schema": "saved_historical_identity_standalone_month_comparison_v1",
        "start": march["start"], "end": march["end"], "source_days": march["source_days"], "case_rows": march_rows, **view.FLAGS})
    view.build([*indexes, march_index], previous)
    base_file, base = saved_ablation(tmp_path / "case_inputs")
    prototype = deepcopy(next(case for case in base["cases"] if case["case_id"] == CONTROL))
    days = base["source_days"]
    def context(name):
        return {"schema_version": 1, "contract_type": "RunContext", "account_id": name, "mode": "historical_replay"}
    cases = []
    for i, skipped in enumerate(base["cases"]):
        if i >= 29:
            cases.append(deepcopy(skipped))
            continue
        case = deepcopy(prototype)
        identity, guard, cap = skipped["case_id"].split("__")
        case.update(case_id=skipped["case_id"], type=identity, guard=guard, capacity=cap,
            complete_PnL_available=i < 19, metrics={"net_return": -.01 - i / 1000} if i < 19 else None,
            status="completed_conditional_mechanical_replay" if i < 19 else "no_complete_PnL")
        case["entry_readiness"]["case_id"] = case["case_id"]
        case["account"] = {"day": days[-1], "pending_targets": None, "positions": {}, "cash": 10000000,
            "replay_risk_exit_policy": {"context": context(case["case_id"])}}
        if i < 25:
            case["orders"] = [{"id": f"order{i}", "day": days[0], "symbol": "600367.SSE", "side": "buy",
                "qty": 100, "filled": 100, "limit_price": 1000, "fees": 500, "status": "filled"}]
            case["fills"] = [{"order_id": f"order{i}", "day": days[0], "symbol": "600367.SSE", "side": "buy",
                "qty": 100, "price": 1000, "fees": 500}]
        if 19 <= i < 25:
            case.update(daily=case["daily"][:1], decisions=case["decisions"][:1], engine_sessions_committed=1,
                blocking_evidence={"day": days[1], "reason": "held_or_pending_raw_or_action_reference_unknown", "session_not_committed": True})
            case["account"]["day"] = days[0]
        cases.append(case)
    original_cases = deepcopy(cases)
    replay_ids = list(view.ABLATION_CASE_IDS[:2])
    for i in range(2):
        old = original_cases[i]
        old.update(status="no_complete_PnL", complete_PnL_available=False, metrics=None,
            engine_sessions_committed=1, daily=old["daily"][:1], decisions=old["decisions"][:1],
            blocking_evidence={"day": days[1], "reason": "held_or_pending_raw_or_action_reference_unknown",
                "reference_evidence": [{"symbol": "600367.SSE", "status": "unresolved_blank_terms"}], "session_not_committed": True})
        old["account"].update(day=days[0], pending_targets={"600367.SSE": .16})
        new_context = context("new:" + old["case_id"])
        cases[i]["account"]["replay_risk_exit_policy"]["context"] = new_context
        cases[i]["committed_prefix_snapshot"] = deepcopy(old["account"])
        cases[i]["committed_prefix_snapshot"]["replay_risk_exit_policy"]["context"] = new_context
    old_registration = tmp_path / "old_registration.json"
    save(old_registration, {"schema": ablation.REGISTRATION_SCHEMA, "method_id": ablation.METHOD_ID, **view.FLAGS})
    old_ref = view._read(old_registration)[1]
    base.update(cases=original_cases, registration=str(old_registration), registration_sha256=old_ref["sha256"])
    save(base_file, base)
    base_ref = view._read(base_file)[1]
    base_progress_ref = view._read(base_file.parent / "progress.json")[1]
    binding = {"replayed_case_ids": replay_ids,
        "reused_case_ids": [case_id for case_id in view.ABLATION_CASE_IDS if case_id not in replay_ids]}
    registration_file = tmp_path / "supplement_registration.json"
    save(registration_file, {"schema": ablation.SUPPLEMENT_REGISTRATION_SCHEMA,
        "method_id": ablation.SUPPLEMENT_METHOD_ID, "spec": deepcopy(ablation.SUPPLEMENT_SPEC), "binding": binding,
        "original_result": str(base_file), "original_result_sha256": base_ref["sha256"],
        "original_registration": str(old_registration), "original_registration_sha256": old_ref["sha256"],
        "original_progress": base_progress_ref["path"], "original_progress_sha256": base_progress_ref["sha256"], **view.FLAGS})
    origins = {}
    for i, case in enumerate(cases):
        case_id, replayed = case["case_id"], i < 2
        origins[case_id] = {"original_result": str(base_file), "original_result_sha256": base_ref["sha256"],
            "original_case_id": case_id, "replayed_from_original_cash": replayed,
            "reused_original_result": not replayed, "checkpoint_recovery_performed": False}
        if replayed:
            origins[case_id]["committed_prefix_check"] = ablation._committed_prefix_check(original_cases[i], case, days)
    supplement = {**deepcopy(base), "schema": ablation.SUPPLEMENT_RESULT_SCHEMA, "method_id": ablation.SUPPLEMENT_METHOD_ID,
        "ablation_method_id": ablation.METHOD_ID, "supplement_spec": deepcopy(ablation.SUPPLEMENT_SPEC),
        "status": "completed_bounded_source_supplement_with_retained_cases", "cases": cases,
        "original_result": str(base_file), "original_result_sha256": base_ref["sha256"],
        "original_registration": str(old_registration), "original_registration_sha256": old_ref["sha256"],
        "registration": str(registration_file), "registration_sha256": view._read(registration_file)[1]["sha256"],
        "replayed_case_count": 2, "reused_case_count": 46, "source_action_supplement": binding, "case_origins": origins}
    supplement_file = tmp_path / "supplement/result.json"
    save(supplement_file, supplement)
    save(supplement_file.parent / "progress.json", {"status": "completed", "expected_cases": 48,
        "replayed_case_count": 2, "reused_case_count": 46,
        "completed_cases": [{"case_id": case_id} for case_id in view.ABLATION_CASE_IDS]})
    return previous, data, supplement_file, supplement, base_file


def test_supplement_replaces_same48_and_preserves_96_monthly_refs_and_per_case_old_failures(tmp_path):
    previous, data, file, supplement, base_file = fixture_supplement(tmp_path)
    old_index = view._read(previous / "index.json")[0]
    output = previous.parent / "supplemented_current"
    result = view.build_supplement(file, previous, output)
    assert result["case_count"] == 96 + 48
    index = view._read(output / "index.json")[0]
    assert len(index["windows"]) == 4
    assert [[row["detail_ref"] for row in window["case_rows"]] for window in index["windows"][:3]] == [
        [row["detail_ref"] for row in window["case_rows"]] for window in old_index["windows"]]
    view.publish(output, data)
    window_id = "ablation48_2026-04-10_2026-09-21"
    new = view.read_history(data, window_id, view.ABLATION_CASE_IDS[0])
    summary = new["detail"]["summary"]
    provenance = new["detail"]["source_supplement"]
    assert summary["source_supplement"] == provenance
    assert summary["source_result"] == str(file) and provenance["original_result"] == str(base_file)
    assert provenance["replayed"] is True and provenance["prefix_preserved"] is True
    assert provenance["new_action_evidence_applied"] is True and provenance["original_engine_sessions_committed"] == 1
    assert provenance["original_status"] == "no_complete_PnL" and provenance["original_blocking_evidence"]["reference_evidence"][0]["status"] == "unresolved_blank_terms"
    window = new["windows"][-1]
    assert len(window["case_rows"]) == 48 and window["source_supplement_counts"] == {"replayed": 2, "reused": 46}
    assert sum(row["metrics"] is not None for row in window["case_rows"]) == 19
    retained = view.read_history(data, window_id, view.ABLATION_CASE_IDS[20])["detail"]
    assert retained["summary"]["source_result"] == str(base_file)
    assert retained["source_supplement"]["replayed"] is False and retained["source_supplement"]["prefix_preserved"] is None
    assert retained["source_supplement"]["new_action_evidence_applied"] is False
    assert retained["source_supplement"]["original_blocking_evidence"] and retained["summary"]["metrics"] is None
    monthly = view.read_history(data, "2026-03", view.CASE_IDS[0])["detail"]
    assert "source_supplement" not in monthly and "source_supplement" not in monthly["summary"]


def test_supplement_projection_rejects_forged_prefix_or_reused_origin_before_output(tmp_path):
    previous, _, file, supplement, _ = fixture_supplement(tmp_path)
    for mutation in ("prefix", "origin"):
        bad = deepcopy(supplement)
        if mutation == "prefix":
            bad["case_origins"][view.ABLATION_CASE_IDS[0]]["committed_prefix_check"]["complete_account_snapshot_compared"] = False
        else:
            bad["case_origins"][view.ABLATION_CASE_IDS[20]]["reused_original_result"] = False
        save(file, bad)
        out = previous.parent / ("bad_" + mutation)
        with pytest.raises(ValueError, match="prefix|origin"):
            view.build_supplement(file, previous, out)
        assert not out.exists()
