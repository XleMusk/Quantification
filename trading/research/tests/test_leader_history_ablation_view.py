"""Fixed saved-view extension only; no strategies, market inputs or accounts run."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import leader_eight_type_history_view as view
from trading.research import leader_environment_exit_ablation as ablation
from trading.research.shortline_panel_replay import _config
from trading.research.tests.test_leader_eight_type_history_view import fixture_history, SELECTED, save


CONTROL = "relay_continuation_leader__two_close_entry_filter_only__uncapped"


def saved_ablation(tmp_path):
    days = ["2026-04-10", "2026-09-21", "2026-09-22"]
    config = _config(100000, 5).to_dict()
    environment = {day: {"phase": "broad_strength_snapshot" if i == 0 else "broad_weakness_snapshot",
        "mode": "ALLOW_FIXED_CANDIDATES" if i == 0 else "EXIT_ALL", "reason": "saved_weakness",
        "two_close_confirmation": True, "exact_previous_present": True,
        "per_type": {kind: {"mode": "ALLOW_FIXED_CANDIDATES" if i == 0 else "EXIT_ALL",
            "reason": "saved_type_reason"} for kind in view.TYPE_IDS}} for i, day in enumerate(days)}
    control = ablation.controlled_environment(environment)
    cases = []
    for case_id in view.ABLATION_CASE_IDS:
        kind, guard, cap = case_id.split("__")
        possible = case_id == CONTROL
        entry = {"case_id": case_id, "potential_entry_days": int(possible),
            "decision_day_count": 2, "nonempty_candidate_days": int(possible),
            "membership_unknown_days": 0 if possible else 2,
            "first_potential_entry_day": days[0] if possible else None,
            "failure_reason_counts": {} if possible else {"type_membership_unknown_in_fixed_panel": 2},
            "common_unresolved_symbols": []}
        case = {"case_id": case_id, "type": kind, "guard": guard, "capacity": cap,
            "status": "no_complete_PnL" if possible else "skipped_no_possible_entry_from_saved_panel",
            "config": deepcopy(config), "metrics": None, "residual_positions": [] if possible else None,
            "account": {"synthetic_saved_account": True} if possible else None,
            "daily": [], "decisions": [], "orders": [], "fills": [], "blocking_evidence": None,
            "engine_sessions_committed": 3 if possible else 0, "entry_readiness": entry}
        if possible:
            case["daily"] = [{"date": day, "cash": 100000., "equity": 100000., "holdings": 0,
                "fees": 0., "events": []} for day in days]
            case["decisions"] = [{"day": day, "execution_day": days[i + 1] if i < 2 else None,
                "status": "FULL_TARGET" if i == 0 else "NO_CHANGE",
                "environment_mode": "ALLOW_FIXED_CANDIDATES" if i == 0 else "NO_CHANGE",
                "original_top5": [], "cap_candidates": [], "cap_dropped_rows": 0,
                "reasons": [] if i == 0 else ["environment_unknown_or_unconfirmed"]}
                for i, day in enumerate(days)]
        cases.append(case)
    record = {"schema": ablation.RESULT_SCHEMA, "method_id": ablation.METHOD_ID,
        "status": "checked_fixed_48_cases_with_skipped_entries", "spec": deepcopy(ablation.SPEC),
        "parent_fixed_protocol": deepcopy(view.PROTOCOL), "config": config, "case_count": 48,
        "cases": cases, "start": days[0], "end": days[1], "source_days": days,
        "environment_decisions": environment, "control_environment_decisions": control,
        "execution_optimization": deepcopy(ablation.READINESS_OPTIMIZATION), **view.FLAGS}
    file = tmp_path / "ablation" / "result.json"
    save(file, record)
    save(file.parent / "progress.json", {"status": "completed", "expected_cases": 48,
        "completed_cases": [{"case_id": case_id} for case_id in view.ABLATION_CASE_IDS]})
    return file, record


def test_append_fixed48_reuses_legacy_display_and_preserves_skipped_null_and_effective_control(tmp_path, monkeypatch):
    indexes, previous, data, _ = fixture_history(tmp_path)
    view.build(indexes, previous)
    old = view.read_history(data, "2026-01", SELECTED)
    assert old["detail"]["summary"]["metrics"] == {"net_return": .0123}
    result_file, _ = saved_ablation(tmp_path)
    original_read = view._read

    def no_old_research_sources(path):
        assert not Path(path).resolve().is_relative_to((tmp_path / "sources").resolve())
        return original_read(path)

    monkeypatch.setattr(view, "_read", no_old_research_sources)
    output = previous.parent / "synthetic_months_plus_ablation"
    built = view.build_ablation(result_file, previous, output)
    assert built["case_count"] == 64 + 48
    published = view.publish(output, data)
    window_id = "ablation48_2026-04-10_2026-09-21"
    assert published["case_count"] == 112 and published["window_ids"][-1] == window_id
    retained = view.read_history(data, "2026-01", SELECTED)
    assert retained["detail"] == old["detail"]  # original display clock, facts and nulls unchanged
    assert view.read_history(data, "2026-01", CONTROL)["status"] == "invalid_selection"
    skipped = view.read_history(data, window_id, view.ABLATION_CASE_IDS[0])
    assert skipped["detail"]["days"] == [] and skipped["detail"]["skipped"] is True
    summary = skipped["detail"]["summary"]
    assert summary["residual_positions"] is None and summary["metrics"] is None
    assert summary["engine_sessions_committed"] == 0 and summary["entry_readiness"]["potential_entry_days"] == 0
    window = skipped["windows"][-1]
    assert window["guards"] == list(view.ABLATION_GUARDS) and window["case_count"] == 48
    assert window["simulated_case_count"] == 1 and window["skipped_case_count"] == 47
    actual = view.read_history(data, window_id, CONTROL)
    weak_day = actual["detail"]["days"][1]
    assert weak_day["environment"]["effective_mode"] == "NO_CHANGE"
    assert weak_day["environment"]["original_type_mode"] == "EXIT_ALL"
    assert weak_day["environment"]["explicit_exit_suppressed"] is True
    assert weak_day["environment"]["type_policy"]["mode"] == "NO_CHANGE"
    assert weak_day["decision"]["reasons"] == ["environment_unknown_or_unconfirmed"]
    assert actual["engine_advanced"] is False and actual["profits_recomputed"] is False


def test_new_projection_refuses_pending_or_fake_skipped_account_and_keeps_pointer(tmp_path):
    indexes, previous, data, _ = fixture_history(tmp_path)
    view.build(indexes, previous)
    view.publish(previous, data)
    pointer = previous.parent / "current.json"
    original_pointer = pointer.read_bytes()
    result_file, record = saved_ablation(tmp_path)
    progress = result_file.parent / "progress.json"
    complete = {"status": "completed", "expected_cases": 48,
        "completed_cases": [{"case_id": case_id} for case_id in view.ABLATION_CASE_IDS]}
    save(progress, {**complete, "status": "running"})
    out = previous.parent / "not_complete"
    with pytest.raises(ValueError, match="has not completed"):
        view.build_ablation(result_file, previous, out)
    assert not out.exists() and pointer.read_bytes() == original_pointer
    save(progress, complete)
    record["cases"][0]["account"] = {"cash": 100000}
    save(result_file, record)
    with pytest.raises(ValueError, match="no account"):
        view.build_ablation(result_file, previous, out)
    assert not out.exists() and pointer.read_bytes() == original_pointer
