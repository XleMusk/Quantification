"""Fixed-mode ablation through the original case/Engine on tiny saved fixtures."""
from copy import deepcopy

import pytest

from trading.research import leader_eight_type_panel_replay as original
from trading.research import leader_environment_exit_ablation as ablation
from trading.research.tests import test_leader_eight_type_panel_replay as fixtures


CONTROL = "two_close_entry_filter_only"


def test_control_only_replaces_current_exit_all_and_never_mutates_or_consults_future_states():
    kinds = list(original.TYPE_IDS)
    env = {"2026-04-01": {"phase": "strong", "source_complete": True,
        "per_type": {kind: {"mode": "ALLOW_FIXED_CANDIDATES", "reason": "two_closes_confirmed"} for kind in kinds}},
        "2026-04-02": {"phase": "weak", "source_complete": True,
        "per_type": {kind: {"mode": "EXIT_ALL" if i < 4 else "NO_CHANGE",
            "reason": "weak_exit" if i < 4 else "original_unknown"} for i, kind in enumerate(kinds)}}}
    before = deepcopy(env)
    control = ablation.controlled_environment(env)
    assert env == before and control["2026-04-01"] == before["2026-04-01"]
    for i, kind in enumerate(kinds):
        old, changed = before["2026-04-02"]["per_type"][kind], control["2026-04-02"]["per_type"][kind]
        if i < 4:
            assert changed["mode"] == "NO_CHANGE" and changed["original_mode"] == "EXIT_ALL"
            assert changed["reason"] == old["reason"] and changed["original_reason"] == old["reason"]
            assert changed["control_reason"]
        else:
            assert changed == old
    extended = deepcopy(env)
    extended["2026-04-03"] = {"phase": "strong", "future_performance": 999,
        "per_type": {kind: {"mode": "ALLOW_FIXED_CANDIDATES", "reason": "later_context"} for kind in kinds}}
    later = ablation.controlled_environment(extended)
    assert {day: later[day] for day in env} == control


def tiny_case(days, panels, environment, *, guard):
    paths, _ = original._rank_path(panels, {fixtures.A, fixtures.B}, days[0], days[3])
    return original._case(fixtures.raw(days), days, fixtures.SESSIONS, fixtures.PREVIOUS, panels,
        paths["relay_continuation_leader"]["uncapped"], environment, {fixtures.A, fixtures.B},
        fixtures.qualification(), {"day": "2026-10-09", "closes": {fixtures.A: 10., fixtures.B: 10.}},
        days[0], days[3], "relay_continuation_leader", "uncapped", guard,
        "synthetic-environment-ablation:" + guard)


def test_original_engine_keeps_structure_and_five_session_exits_after_environment_exit_is_suppressed():
    at = fixtures.SESSIONS.index(fixtures.DAYS[0])
    days = fixtures.SESSIONS[at:at + 10]
    panels = fixtures.panels(days)
    for i, day in enumerate(days):
        if i >= 2:
            panels[day]["environment"].update(breadth=-.5, median_return_pct=-1.)
        panels[day]["risk_features"] = {fixtures.A: {"date": day,
            "listing_feature_method_id": "leader_listing_window_features_asof_decision_v1",
            "feature_decision_as_of": day + "T15:05:00+08:00", "listing_qualification_status": "supported",
            "limit_up": True, "stage": 2, "stage_reference_known": True, "board": "main"}}
        panels[day]["risk_feature_scope_complete"] = True
    environment = original._environment(panels, fixtures.SESSIONS,
        max(panel["available_at"] for panel in panels.values()))
    guarded = tiny_case(days, panels, environment, guard="two_close_envguard")
    assert guarded["decisions"][2]["status"] == "EXPLICIT_EXIT_ALL"
    assert next(fill for fill in guarded["fills"] if fill["side"] == "buy")["day"] == days[2]
    assert next(fill for fill in guarded["fills"] if fill["side"] == "sell")["day"] == days[3]
    control_env = ablation.controlled_environment(environment)
    expiry = tiny_case(days, panels, control_env, guard=CONTROL)
    assert expiry["decisions"][2]["status"] == "NO_CHANGE"
    assert next(fill for fill in expiry["fills"] if fill["side"] == "buy")["day"] == days[2]
    assert next(fill for fill in expiry["fills"] if fill["side"] == "sell")["day"] == days[7]
    structural_panels = deepcopy(panels)
    structural_panels[days[3]]["risk_features"][fixtures.A].update(limit_up=False, stage=0)
    structural = tiny_case(days, structural_panels, control_env, guard=CONTROL)
    assert next(fill for fill in structural["fills"] if fill["side"] == "sell")["day"] == days[4]
    invalid = next(event for row in structural["daily"] for event in row["events"]
        if event["kind"] == "leader_type_risk_assessed" and event["status"] == "invalid")
    assert invalid["day"] == days[3] and invalid["entry"]["fill_day"] == days[2]
    assert structural["config"] == guarded["config"] == expiry["config"]
    assert all(case["blocking_evidence"] is None for case in (guarded, expiry, structural))


def test_fixed_study_binds_all_48_and_refuses_changed_window_fees_or_arm_set():
    manifest = {"created_at": "2026-10-16T18:00:00+08:00", "start": "2026-04-01", "end": "2026-06-30",
        "through": "2026-07-08", "identity_research": {"registered_original_overlay": True},
        "source_days": ["2026-04-01", "2026-06-30", "2026-07-08"],
        "fixed_protocol": deepcopy(original.PROTOCOL), "scope_members": [fixtures.A, fixtures.B]}
    plan = {"schema": "leader_environment_exit_ablation_study_plan_v1",
        "method_id": "fixed_eight_type_environment_exit_ablation_shared_engine_v1",
        "specified_at": "2026-10-16T17:00:00+08:00", "start": manifest["start"], "end": manifest["end"],
        "through": manifest["through"], "types": list(original.TYPE_IDS),
        "guards": [*original.GUARDS, CONTROL], "capacities": [name for name, _ in original.CAPS],
        "case_count": 48, "config": original._config(100000, 5).to_dict(), "parameters_selected_by_return": False}
    before = deepcopy((manifest, plan))
    assert isinstance(ablation._registration_binding(manifest, plan), dict)
    assert (manifest, plan) == before
    for mutation in ("dates", "fees", "arms", "count", "future_registration"):
        bad = deepcopy(plan)
        if mutation == "dates":
            bad["end"] = "2026-06-29"
        elif mutation == "fees":
            bad["config"]["commission"] = .01
        elif mutation == "arms":
            bad["guards"].remove(CONTROL)
        elif mutation == "count":
            bad["case_count"] = 32
        else:
            bad["specified_at"] = "2026-10-16T19:00:00+08:00"
        with pytest.raises(ValueError):
            ablation._registration_binding(manifest, bad)


def test_registered_readiness_skips_without_accounts_and_reuses_loaded_routes(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from trading.research import leader_panel_readiness as readiness

    days = fixtures.DAYS
    panels = fixtures.panels(days, unknown=True)
    loaded = {"panel_by_day": panels, "raw": fixtures.raw(days), "source_days": days,
        "calendar_payload": fixtures.CALENDAR, "scope_members": [fixtures.A, fixtures.B],
        "qualification": fixtures.qualification(),
        "context_anchor": {"day": "2026-10-09", "closes": {fixtures.A: 10., fixtures.B: 10.}},
        "provenance": {"synthetic": True}}
    manifest = {"created_at": "2026-10-16T18:00:00+08:00", "start": days[0], "end": days[1],
        "through": days[-1], "identity_research": {"synthetic": True}, "source_days": days,
        "scope_members": loaded["scope_members"], "fixed_protocol": deepcopy(original.PROTOCOL),
        **{key: "synthetic_unchanged_pin" for key in ("raw_file", "raw_sha256", "panels_file", "panels_sha256",
            "context_raw_file", "context_raw_sha256", "calendar", "calendar_sha256")}}
    plan = {"schema": ablation.STUDY_PLAN_SCHEMA, "method_id": ablation.METHOD_ID,
        "specified_at": "2026-10-16T17:00:00+08:00", "start": manifest["start"], "end": manifest["end"],
        "through": manifest["through"], "types": list(original.TYPE_IDS), "guards": list(ablation.GUARDS),
        "capacities": [name for name, _ in original.CAPS], "case_count": 48,
        "config": original._config(100000, 5).to_dict(), "parameters_selected_by_return": False}
    inputs, study = tmp_path / "inputs.json", tmp_path / "study.json"
    inputs.write_text(json.dumps(manifest), encoding="utf-8")
    study.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(ablation, "load_native_inputs", lambda path: (loaded, manifest))
    monkeypatch.setattr(ablation, "_view_pin", lambda path: {"directory": str(path), "synthetic": True})
    registered = ablation.register(inputs, study, tmp_path / "registration",
        action_event_view="synthetic_events", action_reference_view="synthetic_references", check_entry_readiness=True)
    registration_file = tmp_path / "registration" / "registration.json"
    assert registered["execution_optimization"] == ablation.READINESS_OPTIMIZATION
    assert json.loads(registration_file.read_text(encoding="utf-8"))["execution_optimization"] == ablation.READINESS_OPTIMIZATION
    legacy = dict(registered)
    legacy.pop("execution_optimization")
    ablation._require_readiness_mode(legacy, False)
    for saved, flag in ((legacy, True), (registered, False),
            ({**registered, "execution_optimization": {"id": "unregistered_other_version"}}, True)):
        with pytest.raises(ValueError):
            ablation._require_readiness_mode(saved, flag)
    monkeypatch.setattr(ablation, "_load_registration", lambda path: (registered, loaded, manifest))

    def forbidden(*args, **kwargs):
        raise AssertionError("A no-entry case must not construct a factory, account or rerank loaded routes")

    monkeypatch.setattr(readiness, "load_inputs", forbidden)
    monkeypatch.setattr(readiness, "_rank_path", forbidden)
    monkeypatch.setattr(readiness, "_environment", forbidden)
    monkeypatch.setattr(ablation, "_execution_inputs", forbidden)
    monkeypatch.setattr(ablation, "_case", forbidden)
    skipped = ablation.run(registration_file, tmp_path / "all_skipped", check_entry_readiness=True)
    assert skipped["status"] == "checked_fixed_48_cases_with_unavailable_entries"
    assert skipped["checked_case_count"] == skipped["skipped_case_count"] == 48
    assert skipped["simulated_case_count"] == 0 and skipped["historical_execution_policy"] is None
    assert skipped["all_cases_complete_PnL"] is False
    for case in skipped["cases"]:
        assert case["status"] == "skipped_no_possible_entry_from_saved_panel"
        assert case["account"] is None and case["metrics"] is None and case["residual_positions"] is None
        assert case["engine_sessions_committed"] == 0
        folder = tmp_path / "all_skipped" / case["case_id"]
        assert json.loads((folder / "account.json").read_text(encoding="utf-8")) is None
        assert all(json.loads((folder / (key + ".json")).read_text(encoding="utf-8")) == []
            for key in ("decisions", "daily", "orders", "fills"))

    # Only the one now-known type reaches the original runner; its costs/ranks stay exact.
    identity = "relay_continuation_leader"
    for panel in panels.values():
        next(item for item in panel["types"] if item["id"] == identity).update(status="matched", unresolved_symbols=[])
    execution_calls, case_calls = [], []

    def prepare_execution(*args):
        execution_calls.append(True)
        return "synthetic_context_raw", SimpleNamespace(provenance={"synthetic": True})

    def original_case(*args, **kwargs):
        kind, cap, guard = args[12:15]
        case_calls.append((kind, cap, guard))
        assert kind == identity and args[7] == set(loaded["scope_members"])
        return {"case_id": f"{kind}__{guard}__{cap}", "type": kind, "guard": guard, "capacity": cap,
            "status": "synthetic_original_runner_result", "config": deepcopy(plan["config"]),
            "decisions": [], "daily": [], "orders": [], "fills": [], "account": {"synthetic": True},
            "metrics": None, "engine_sessions_committed": 0, "blocking_evidence": None,
            "residual_positions": [], "complete_PnL_available": False}

    monkeypatch.setattr(ablation, "_execution_inputs", prepare_execution)
    monkeypatch.setattr(ablation, "_case", original_case)
    mixed = ablation.run(registration_file, tmp_path / "some_possible", check_entry_readiness=True)
    assert len(execution_calls) == 1 and len(case_calls) == 6
    assert mixed["checked_case_count"] == 48 and mixed["simulated_case_count"] == 6 and mixed["skipped_case_count"] == 42
    assert mixed["status"] == "checked_fixed_48_cases_with_skipped_entries"
    progress = json.loads((tmp_path / "some_possible" / "progress.json").read_text(encoding="utf-8"))
    assert progress["checked_case_count"] == 48 and progress["simulated_case_count"] == 6 and progress["skipped_case_count"] == 42
