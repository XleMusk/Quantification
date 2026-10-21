"""Small saved identity/secondary layers; no panels, source refresh or Engine."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import leader_eight_type_history_view as view
from trading.research import leader_eight_type_historical_native as native
from trading.research import leader_eight_type_secondary_supplement as secondary
from trading.research import leader_historical_identity_inputs as identity
from trading.research.leader_historical_identity_rules import SPEC as IDENTITY_SPEC
from trading.research.tests.test_leader_eight_type_history_view import (
    SELECTED, fixture_history, save, source,
)
from trading.research.tests.test_leader_history_ablation_view import saved_ablation
from trading.research.tests.test_leader_history_native_supplement_view import (
    REPLAYED, supplement_fixture,
)


def six_windows(tmp_path):
    indexes, monthly, data, _ = fixture_history(tmp_path)
    march_path, march = source(tmp_path / "sources" / "march", "2026-03", view.CASE_IDS, .031)
    march_rows = [{**{key: deepcopy(case[key]) for key in (
        "case_id", "type", "guard", "capacity", "status", "metrics", "residual_positions")},
        "order_count": len(case["orders"]), "fill_count": len(case["fills"]),
        "source_result": str(march_path), "origin": "standalone"} for case in march["cases"]]
    march_index = tmp_path / "sources" / "march_index.json"
    save(march_index, {"schema": "saved_historical_identity_standalone_month_comparison_v1",
        "start": march["start"], "end": march["end"], "source_days": march["source_days"],
        "case_rows": march_rows, **view.FLAGS})
    view.build([*indexes, march_index], monthly)
    ablation_path, _ = saved_ablation(tmp_path)
    mixed = monthly.parent / "six_ablation"
    view.build_ablation(ablation_path, monthly, mixed)
    annual_path, parent, parent_path = supplement_fixture(tmp_path)
    annual, _ = view._read(annual_path)

    # Supply only the parent's small saved input descriptor, never raw/panels.
    inputs_path = tmp_path / "native_inputs" / "manifest.json"
    manifest = {"schema": native.INPUT_SCHEMA, "method_id": native.METHOD_ID,
        "fixed_protocol": deepcopy(view.PROTOCOL),
        **{key: deepcopy(annual[key]) for key in ("start", "end", "source_days", "scope_members")},
        "through": annual["source_days"][-1], "created_at": "2026-10-17T00:00:00+08:00",
        "raw_sha256": "1" * 64, "context_raw_sha256": "2" * 64,
        "calendar_sha256": "3" * 64, "panels_file": str(tmp_path / "never_read" / "panels.json"),
        **native.FLAGS}
    save(inputs_path, manifest)
    inputs_ref = view._read(inputs_path)[1]
    parent["source"] = {"manifest": str(inputs_path.resolve()), "manifest_sha256": inputs_ref["sha256"],
        "input_record": deepcopy(manifest)}
    parent.setdefault("raw_top5_paths", {})
    parent.setdefault("capacity_paths", {})
    registration_path = Path(parent["registration"]["path"])
    registration, _ = view._read(registration_path)
    registration.update(inputs=inputs_ref, protocol=deepcopy(view.PROTOCOL))
    save(registration_path, registration)
    parent["registration"] = view._read(registration_path)[1]
    save(parent_path, parent)
    progress_path = parent_path.parent / "progress.json"
    progress, _ = view._read(progress_path)
    progress.update(registration=parent["registration"], result=view._read(parent_path)[1])
    save(progress_path, progress)

    with_annual, previous = monthly.parent / "six_annual", monthly.parent / "six_native"
    view.build_annual(annual_path, mixed, with_annual)
    assert view.build_native_supplement(parent_path, with_annual, previous)["case_count"] == 208
    view.publish(previous, data)
    index, _ = view._read(previous / "index.json")
    assert len(index["windows"]) == 6
    return SimpleNamespace(previous=previous, data=data, annual=annual, annual_path=annual_path,
        parent=parent, parent_path=parent_path, parent_progress=progress_path,
        parent_registration=registration_path, inputs=inputs_path, manifest=manifest, index=index)


def saved_identity_layer(tmp_path, fixture):
    base_path = tmp_path / "identity_base" / "manifest.json"
    save(base_path, fixture.manifest)
    base_ref = view._read(base_path)[1]
    baseline = deepcopy(fixture.annual)
    baseline["source"] = {"manifest": base_ref["path"], "manifest_sha256": base_ref["sha256"],
        "input_record": deepcopy(fixture.manifest)}
    baseline_path = tmp_path / "identity_baseline" / "result.json"
    save(baseline_path, baseline)
    baseline_ref = view._read(baseline_path)[1]
    registration_path = tmp_path / "identity_registration" / "registration.json"
    registration = {"schema": identity.REGISTRATION_SCHEMA, "method_id": identity.METHOD_ID,
        "registered_at": "2026-10-17T00:10:00+08:00", "rules": deepcopy(IDENTITY_SPEC),
        "changed_types": list(identity.TYPES), "fixed_protocol": deepcopy(view.PROTOCOL),
        "case_execution": identity.REUSED_EIGHT_EXECUTION, "baseline_input_relation": "exact_same_parent",
        "base_manifest": base_ref["path"], "base_manifest_sha256": base_ref["sha256"],
        "baseline_result": baseline_ref["path"], "baseline_result_sha256": baseline_ref["sha256"],
        "baseline_reuse_evidence": None, "baseline_reuse_evidence_sha256": None, **identity.FLAGS}
    save(registration_path, registration)
    registration_ref = view._read(registration_path)[1]
    child = deepcopy(fixture.manifest)
    child["identity_research"] = {"method_id": identity.METHOD_ID,
        "changed_types": list(identity.TYPES), "registration": registration_ref["path"],
        "registration_sha256": registration_ref["sha256"], "parent_manifest": base_ref["path"],
        "parent_manifest_sha256": base_ref["sha256"], "historical_publication_backdated": False,
        "old_six_type_observations_modified": False, "actual_derived_at": "2026-10-17T00:20:00+08:00"}
    child_path = tmp_path / "identity_child" / "manifest.json"
    save(child_path, child)
    child_ref = view._read(child_path)[1]
    cases = [deepcopy(case) for case in baseline["cases"] if case["type"] in identity.TYPES]
    next(case for case in cases if case["case_id"] == SELECTED).update(
        complete_PnL_available=True, metrics={"net_return": .127, "max_drawdown": -.083},
        status="completed_conditional_mechanical_replay", residual_positions=[])
    result = {"schema": "leader_historical_identity_eight_case_shared_replay_v1",
        "method_id": identity.METHOD_ID, "created_at": "2026-10-17T00:30:00+08:00",
        **{key: deepcopy(baseline[key]) for key in ("start", "end", "source_days", "environment_decisions")},
        "source": {"manifest": child_ref["path"], "manifest_sha256": child_ref["sha256"],
            "input_record": child}, "parent_fixed_protocol": deepcopy(view.PROTOCOL),
        "executed_type_ids": list(identity.TYPES), "case_count": 8, "cases": cases,
        "case_selection_by_performance": False, "shared_case_function": "leader_eight_type_panel_replay._case",
        "unchanged_24_cases_reused_from": {"result": baseline_ref["path"],
            "sha256": baseline_ref["sha256"], "input_reuse_evidence": None}, **identity.FLAGS}
    path = tmp_path / "identity_result" / "result.json"
    save(path, result)
    return path, result, baseline_path, baseline


def saved_secondary_layer(tmp_path, fixture):
    selected = [case_id for case_id in view.CASE_IDS if case_id not in REPLAYED][:2]
    pairs = {}
    event_folder = tmp_path / "secondary_events"
    for key, folder in (("action_event_view", event_folder),
                        ("action_reference_view", tmp_path / "secondary_references")):
        path = folder / "result.json"
        save(path, {"status": "research_action_events_projected" if key == "action_event_view"
            else "research_reference_presence_diagnostic", "execution_admitted": False,
            "event_view": str(event_folder.resolve())})
        reference = view._read(path)[1]
        pairs[key] = {"directory": str(folder.resolve()), "result": reference["path"], "sha256": reference["sha256"]}
    parent_ref = view._read(fixture.parent_path)[1]
    binding = {"replayed_case_ids": selected,
        "reused_case_ids": [case_id for case_id in view.CASE_IDS if case_id not in selected],
        "new_pair_scope": "replayed_case_ids_only"}
    registration = {"schema": secondary.METHOD + "_registration", "method_id": secondary.METHOD,
        "spec": deepcopy(secondary.SPEC), "protocol": deepcopy(view.PROTOCOL),
        "config": deepcopy(fixture.parent["config"]), "case_ids": list(view.CASE_IDS),
        "inputs": view._read(fixture.inputs)[1], "parent": {"result": parent_ref,
            "progress": view._read(fixture.parent_progress)[1],
            "registration": view._read(fixture.parent_registration)[1]},
        "binding": binding, **pairs, "issuer_confirmed": False, "PIT_verified": False, **secondary.FLAGS}
    registration_path = tmp_path / "secondary_registration" / "registration.json"
    save(registration_path, registration)
    registration_ref = view._read(registration_path)[1]
    result = deepcopy(fixture.parent)
    for case in result["cases"]:
        replayed = case["case_id"] in selected
        own_pair = ({key: value["directory"] for key, value in pairs.items()} if replayed else
            deepcopy(case["source_origin"]["action_pair"]))
        layer = {"mode": "replayed" if replayed else "reused", "parent_result": parent_ref,
            "parent_case_id": case["case_id"], "action_pair": own_pair,
            "replayed_from_original_cash": replayed, "checkpoint_recovery_performed": False}
        if replayed:
            layer.update(issuer_confirmed=False, PIT_verified=False,
                committed_prefix_check={"status": "exact_original_committed_prefix_preserved",
                    "complete_account_snapshot_compared": True})
            case["historical_execution_policy"]["accounting"] = {
                "event_view": own_pair["action_event_view"], "reference_view": own_pair["action_reference_view"]}
            if case["case_id"] == selected[0]:
                case["metrics"] = {"net_return": -.017, "max_drawdown": -.072}
        case["source_supplement_origin"] = layer
    result.update(schema=secondary.METHOD + "_result", method_id=secondary.METHOD,
        spec=deepcopy(secondary.SPEC), registration=registration_ref, parent_result=parent_ref,
        source_secondary_supplement=binding, replayed_case_count=2, reused_case_count=30,
        new_action_pair=pairs, new_action_pair_scope="replayed_case_ids_only",
        historical_execution_policy_scope="replayed_case_ids_only", issuer_confirmed=False, PIT_verified=False,
        late_evidence_not_historical_PIT=True, original_results_modified=False,
        case_origins={case["case_id"]: deepcopy(case["source_supplement_origin"]) for case in result["cases"]})
    path = tmp_path / "secondary_result" / "result.json"
    save(path, result)
    progress = {"method_id": secondary.METHOD, "status": "completed", "registration": registration_ref,
        "result": view._read(path)[1], "active_case": None, "case_ids": selected,
        "completed_cases": [{"case_id": case_id} for case_id in selected]}
    save(path.parent / "progress.json", progress)
    return path, result, progress, selected


def test_identity_eight_plus_exact_parent_twenty_four_appends_and_keeps_all_208_details(tmp_path, monkeypatch):
    fixture = six_windows(tmp_path)
    path, source_result, parent_path, parent = saved_identity_layer(tmp_path, fixture)
    output = fixture.previous.parent / "with_identity"
    old_details = {file.relative_to(fixture.previous): file.read_bytes()
        for file in (fixture.previous / "windows").rglob("*.json")}
    assert len(old_details) == 208
    original_read = view._read

    def no_market_inputs(file):
        resolved = Path(file).resolve()
        assert not resolved.is_relative_to((tmp_path / "sources").resolve())
        assert not resolved.is_relative_to(fixture.annual_path.parent.resolve())
        assert resolved.suffix == ".json" and resolved.name != "panels.json"
        return original_read(file)

    with monkeypatch.context() as guarded:
        guarded.setattr(view, "_read", no_market_inputs)
        built = view.build_identity_overlay(path, fixture.previous, output)
    assert built["case_count"] == 240
    assert all((output / relative).read_bytes() == content for relative, content in old_details.items())
    assert all((fixture.previous / relative).read_bytes() == content for relative, content in old_details.items())
    index, _ = view._read(output / "index.json")
    window = next(row for row in index["windows"] if row["id"] == "identity_overlay_2025")
    assert len(index["windows"]) == 7 and window.get("source_family") is None
    assert window["identity_execution_counts"] == {"executed": 8, "reused": 24}
    assert window["independent_account"] is False and window["equity_series_concatenated"] is False
    assert [row["case_id"] for row in window["case_rows"]] == list(view.CASE_IDS)
    new_cases = {case["case_id"]: case for case in source_result["cases"]}
    old_cases = {case["case_id"]: case for case in parent["cases"]}
    for row in window["case_rows"]:
        executed = row["case_id"] in new_cases
        case = (new_cases if executed else old_cases)[row["case_id"]]
        assert row["metrics"] == case["metrics"]
        assert row["source_result"] == str((path if executed else parent_path).resolve())
        assert row["identity_overlay"]["executed"] is executed
        assert row["identity_overlay"]["reused"] is not executed
        assert row["identity_overlay"]["new_independent_all32_claimed"] is False
    assert sum(row["metrics"] is None for row in window["case_rows"] if row["type"] in identity.TYPES) == 7

    bad = deepcopy(source_result)
    bad["unchanged_24_cases_reused_from"]["sha256"] = "0" * 64
    bad_path, rejected = tmp_path / "bad_identity" / "result.json", fixture.previous.parent / "rejected_identity"
    save(bad_path, bad)
    with pytest.raises(ValueError):
        view.build_identity_overlay(bad_path, fixture.previous, rejected)
    assert not rejected.exists()
    view.publish(output, fixture.data)

    def projection_only(file):
        assert Path(file).resolve().is_relative_to(fixture.data.resolve())
        return original_read(file)

    with monkeypatch.context() as guarded:
        guarded.setattr(view, "_read", projection_only)
        readback = view.read_history(fixture.data, "identity_overlay_2025", SELECTED)
        assert readback["status"] == "saved_history_projection"
        assert readback["detail"]["summary"]["metrics"] == {"net_return": .127, "max_drawdown": -.083}
        assert readback["detail"]["identity_overlay"]["executed"] is True
        assert view.read_history(fixture.data, "2026-01", SELECTED)["detail"]["summary"]["metrics"] == {"net_return": .0123}
    assert all(readback[key] is False for key in ("engine_advanced", "ledger_mutated", "profits_recomputed",
        "provider_contacted", "execution_admitted", "returns_reportable", "pit_verified"))


def test_secondary_two_completed_attempts_show_latest_pair_and_retain_old_origin_and_208_details(tmp_path, monkeypatch):
    fixture = six_windows(tmp_path)
    path, result, progress, selected = saved_secondary_layer(tmp_path, fixture)
    assert len(progress["completed_cases"]) == 2 and result["case_count"] == 32
    output = fixture.previous.parent / "with_secondary"
    old_details = {file.relative_to(fixture.previous): file.read_bytes()
        for file in (fixture.previous / "windows").rglob("*.json")}
    original_read = view._read

    def no_market_inputs(file):
        resolved = Path(file).resolve()
        assert not resolved.is_relative_to((tmp_path / "sources").resolve())
        assert not resolved.is_relative_to(fixture.annual_path.parent.resolve())
        assert resolved.suffix == ".json" and resolved.name != "panels.json"
        return original_read(file)

    with monkeypatch.context() as guarded:
        guarded.setattr(view, "_read", no_market_inputs)
        built = view.build_secondary_supplement(path, fixture.previous, output)
    assert built["case_count"] == 240 and len(old_details) == 208
    assert all((output / relative).read_bytes() == content for relative, content in old_details.items())
    index, _ = view._read(output / "index.json")
    window = next(row for row in index["windows"] if row["id"] == "native_secondary_supplement_2025")
    assert len(index["windows"]) == 7 and window.get("source_family") is None
    assert window["source_supplement_counts"] == {"replayed": 2, "reused": 30}
    assert window["independent_account"] is False and window["equity_series_concatenated"] is False
    assert window["issuer_confirmed"] is False and window["PIT_verified"] is False
    parent_cases = {case["case_id"]: case for case in fixture.parent["cases"]}
    current_cases = {case["case_id"]: case for case in result["cases"]}
    for row in window["case_rows"]:
        old = parent_cases[row["case_id"]]
        replayed = row["case_id"] in selected
        assert row["source_supplement"]["mode"] == ("replayed" if replayed else "reused")
        assert row["source_origin_history"] == old["source_origin"]
        assert row["own_action_accounting_pair"] == row["source_supplement"]["action_pair"]
        assert row["source_result"] == str((path if replayed else fixture.parent_path).resolve())
        assert row["metrics"] == current_cases[row["case_id"]]["metrics"]
        if not replayed:
            assert row["metrics"] == old["metrics"]
    rows = {row["case_id"]: row for row in window["case_rows"]}
    assert rows[selected[0]]["own_action_accounting_pair"] != rows[selected[0]]["source_origin_history"]["action_pair"]
    assert rows[REPLAYED[0]]["source_supplement"]["mode"] == "reused"
    assert rows[REPLAYED[0]]["source_origin_history"]["mode"] == "replayed"

    bad = deepcopy(result)
    replayed_case = next(case for case in bad["cases"] if case["case_id"] == selected[0])
    replayed_case["source_supplement_origin"]["committed_prefix_check"]["complete_account_snapshot_compared"] = False
    bad["case_origins"][selected[0]] = deepcopy(replayed_case["source_supplement_origin"])
    bad_path, rejected = tmp_path / "bad_secondary" / "result.json", fixture.previous.parent / "rejected_secondary"
    save(bad_path, bad)
    bad_progress = deepcopy(progress)
    bad_progress["result"] = view._read(bad_path)[1]
    save(bad_path.parent / "progress.json", bad_progress)
    with pytest.raises(ValueError):
        view.build_secondary_supplement(bad_path, fixture.previous, rejected)
    assert not rejected.exists()
    view.publish(output, fixture.data)

    def projection_only(file):
        assert Path(file).resolve().is_relative_to(fixture.data.resolve())
        return original_read(file)

    with monkeypatch.context() as guarded:
        guarded.setattr(view, "_read", projection_only)
        readback = view.read_history(fixture.data, "native_secondary_supplement_2025", selected[0])
        assert readback["status"] == "saved_history_projection"
        assert readback["detail"]["summary"]["metrics"] == {"net_return": -.017, "max_drawdown": -.072}
        assert readback["detail"]["source_origin_history"] == parent_cases[selected[0]]["source_origin"]
        assert readback["detail"]["source_supplement"]["mode"] == "replayed"
        assert view.read_history(fixture.data, "native_action_supplement_2025", REPLAYED[0])["detail"]["summary"]["metrics"] == {"net_return": -.123, "max_drawdown": -.2}
    assert all(readback[key] is False for key in ("engine_advanced", "ledger_mutated", "profits_recomputed",
        "provider_contacted", "execution_admitted", "returns_reportable", "pit_verified"))
