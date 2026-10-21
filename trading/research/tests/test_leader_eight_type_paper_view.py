"""Small saved-publication checks; never read replay accounts or raw providers."""
from datetime import datetime
from functools import partial
import json
from pathlib import Path

import pytest

from trading.research import leader_eight_type_paper_view as m


NOW = datetime.fromisoformat("2026-10-15T23:00:00+08:00")
FLAGS = {"research_only": True, "candidate_panel_only": True, **{key: False for key in m.FALSE_FLAGS}}
ENV_FLAGS = {"candidate_panel_only": True, **{key: False for key in m.COMMON_FALSE}}


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def save(root, name="new", recorded="2026-10-15T21:00:00+08:00", *, late=False):
    folder = root / "outputs/leader_eight_type_paper" / name
    folder.mkdir(parents=True)
    source_day, execution = "2026-10-15", "2026-10-16"
    environment = {"day": source_day, "phase": "broad_weakness_snapshot", "mode": "EXIT_ALL", "reason": "saved_weakness",
        "two_close_confirmation": True, "exact_previous_present": True, "previous_session_required": "2026-10-14",
        "as_of": "2026-10-15T18:00:00+08:00", "latest_inputs_available_at": "2026-10-15T17:00:00+08:00",
        "execution_not_before": execution, "per_type": {}, **ENV_FLAGS}
    for identity in m.TYPES:
        environment["per_type"][identity] = {"id": identity, "mode": "EXIT_ALL", **ENV_FLAGS,
            "rule": {"entry_phases_after_two_close_confirmation": ["broad_strength_snapshot"],
                "additional_candidate_requirements": ["saved requirement " + identity],
                "invalidation_signals": ["saved invalidation " + identity]}}
    record = {"schema": m.SCHEMA, "status": "late_context_only" if late else "unbound_current_close_paper_research",
        "recorded_at": recorded, "publication_at": recorded, "source_close_day": source_day,
        "proposed_execution_day": execution, "execution_day_bound": None if late else execution,
        "next_open_deadline": execution + "T09:30:00+08:00", "publication_time_backfilled": False,
        "execution_day_rolled_forward": False, "same_source_day_evening_required_for_binding": True,
        "budget_is_account_cash": False, "case_count": 32, "environment_policy": environment, **FLAGS,
        "source": {"input_manifest": str(root / "must_not_read_manifest.json"),
            "scope_frozen_at": "2026-10-15T16:00:00+08:00", "input_recorded_at": "2026-10-15T17:00:00+08:00",
            "external_fixed_protocol": {"file": str(root / "must_not_read_protocol.json"),
                "record": {"schema": "leader_eight_type_fixed_comparison_protocol_v1", "case_count": 32,
                    "cases": [{"id": "__".join(key)} for key in m.CASE_KEYS]}}},
        "protocol": {"types": list(m.TYPES), "guards": list(m.GUARDS),
            "capacities": [{"id": m.CAPACITIES[0], "max_symbols": 20}, {"id": m.CAPACITIES[1], "max_symbols": None}],
            "case_count": 32}, "risk_policy_spec": {identity: "saved_structure_" + identity for identity in m.TYPES}, "cases": []}
    for identity, guard, capacity in m.CASE_KEYS:
        identifier = "__".join((identity, guard, capacity))
        unknown = identity == "cross_regime_candidate" and guard == "unguarded"
        decision = "NO_CHANGE" if unknown else "EXIT_ALL" if guard == "two_close_envguard" else "FULL_TARGET"
        weights = None if unknown else {} if decision == "EXIT_ALL" else {"000001.SZSE": .16, "000002.SZSE": .16}
        candidates = [{"symbol": f"{i:06}.SZSE", "score": 100 - i, "source_day": source_day,
                       "unused_full_source_details": "not exported"} for i in (1, 2)]
        rule = environment["per_type"][identity]["rule"]
        record["cases"].append({"case_id": identifier, "type": identity, "guard": guard, "capacity": capacity,
            "decision": decision, "environment_mode": "EXIT_ALL" if guard == "two_close_envguard" else "ALLOW_FIXED_CANDIDATES",
            "reason": "saved reason", "publication_status": "late_context_only" if late else "unbound_current_close_research",
            "original_top5": candidates, "cap_candidates": candidates,
            "target_weights": weights, "target_cash_weight": None if unknown else 1 if decision == "EXIT_ALL" else .68,
            "unresolved_member_count": 2 if unknown else 0,
            "required_evidence": rule["additional_candidate_requirements"], "risk_and_invalidation": rule["invalidation_signals"],
            "structural_invalidation_policy": record["risk_policy_spec"][identity],
            "type_missing_evidence": ["saved descriptive missing"] if identity in m.TYPES[:4] + (m.TYPES[5],) else {"missing_turn": 35},
            "portfolio_intent": {"schema_version": 1, "contract_type": "PortfolioIntent", "unit": "equity_fraction",
                "kind": "NO_CHANGE" if unknown else "FULL_TARGET", "weights": weights or {}, "context": None,
                "decided_at": recorded, "execution_not_before": None if late else execution,
                "source": "research:leader_eight_type_unbound_paper:" + identifier}, **FLAGS})
    write(folder / "paper_plan.json", record)
    return folder, record


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_only_latest_actual_publication_keeps_32_order_intents_missing_union_and_saved_rules(tmp_path, monkeypatch):
    save(tmp_path, "z_old", "2026-10-15T20:00:00+09:00")
    folder, record = save(tmp_path, "a_new", "2026-10-15T19:30:00+08:00")
    before = snapshot(tmp_path)
    original = Path.read_text
    def only_plan(path, *args, **kwargs):
        assert path.name == "paper_plan.json", "View must not open producer, protocol, replay, account or raw files"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", only_plan)
    view = m.leader_eight_type_paper_view(tmp_path, now=NOW)
    assert view["status"] == "saved_paper_plan" and view["execution_window_elapsed"] is False
    assert view["source_file"] == str((folder / "paper_plan.json").resolve())
    assert [row["case_id"] for row in view["rows"]] == ["__".join(key) for key in m.CASE_KEYS]
    assert view["case_count"] == len(view["rows"]) == 32
    full, exit_all = view["rows"][0], view["rows"][2]
    unknown = next(row for row in view["rows"] if row["decision"] == "NO_CHANGE")
    assert full["target_weights"] == {"000001.SZSE": .16, "000002.SZSE": .16}
    assert full["target_cash_weight"] == .68 and full["cap_candidate_count"] == 2
    assert exit_all["target_weights"] == {} and exit_all["target_cash_weight"] == 1
    assert unknown["target_weights"] is unknown["target_cash_weight"] is None
    assert full["missing_evidence"] == ["saved descriptive missing"]
    assert unknown["missing_evidence"] == {"missing_turn": 35}
    assert full["entry_phases"] == record["environment_policy"]["per_type"][full["type"]]["rule"]["entry_phases_after_two_close_confirmation"]
    assert all(set(row) == {"symbol", "score"} for item in view["rows"] for row in item["candidates"])
    assert all(view[key] is False for key in m.FALSE_FLAGS)
    assert snapshot(tmp_path) == before


def test_future_read_does_not_roll_or_make_original_timely_publication_current(tmp_path):
    _, record = save(tmp_path)
    view = m.leader_eight_type_paper_view(tmp_path, now="2026-10-16T09:30:00+08:00")
    assert view["status"] == "saved_paper_plan" and view["execution_window_elapsed"] is True
    assert view["recorded_at"] == record["recorded_at"]


def test_explicit_publication_never_substitutes_or_reads_a_newer_file(tmp_path, monkeypatch):
    old, record = save(tmp_path, "original", "2026-10-15T19:00:00+08:00")
    newer, _ = save(tmp_path, "newer", "2026-10-15T21:00:00+08:00")
    (newer / "paper_plan.json").write_text("{broken unrelated later file", encoding="utf-8")
    expected = (old / "paper_plan.json").resolve()
    original_read = Path.read_text
    def exact_only(path, *args, **kwargs):
        assert path.resolve() == expected, "An explicit publication must not read any substitute"
        return original_read(path, *args, **kwargs)
    before = snapshot(tmp_path)
    monkeypatch.setattr(Path, "read_text", exact_only)
    view = m.read_saved_plan(expected, now=NOW)
    assert view["source_file"] == str(expected)
    assert view["recorded_at"] == record["recorded_at"]
    assert snapshot(tmp_path) == before
    assert view["proposed_execution_day"] == view["execution_day_bound"] == "2026-10-16"


def test_after_midnight_original_late_context_remains_unbound_before_next_open(tmp_path):
    save(tmp_path, recorded="2026-10-16T08:00:00+08:00", late=True)
    view = m.leader_eight_type_paper_view(tmp_path, now="2026-10-16T08:30:00+08:00")
    assert view["status"] == "late_context_only" and view["execution_day_bound"] is None
    assert view["proposed_execution_day"] == "2026-10-16" and view["execution_window_elapsed"] is False


@pytest.mark.parametrize("fault", ["source_flag", "row_flag", "future_clock", "case_order", "nochange_cash",
    "context", "share_weight", "missing_saved_entry_rule"])
def test_bad_latest_never_falls_back_to_older_plan(tmp_path, fault):
    save(tmp_path, "old", "2026-10-15T19:00:00+08:00")
    folder, record = save(tmp_path)
    if fault == "source_flag": record["source"]["external_fixed_protocol"]["record"]["selection_enabled"] = True
    elif fault == "row_flag": record["cases"][-1]["orders_generated"] = True
    elif fault == "future_clock": record["recorded_at"] = "2026-10-16T01:00:00+08:00"
    elif fault == "case_order": record["cases"][0], record["cases"][1] = record["cases"][1], record["cases"][0]
    elif fault == "nochange_cash": next(row for row in record["cases"] if row["decision"] == "NO_CHANGE")["target_cash_weight"] = 0
    elif fault == "context": record["cases"][0]["portfolio_intent"]["context"] = {"account_id": "forbidden"}
    elif fault == "share_weight": record["cases"][0]["target_weights"]["000001.SZSE"] = 100
    elif fault == "missing_saved_entry_rule": record["environment_policy"]["per_type"][m.TYPES[0]]["rule"].pop("entry_phases_after_two_close_confirmation")
    write(folder / "paper_plan.json", record)
    with pytest.raises(ValueError):
        m.leader_eight_type_paper_view(tmp_path, now=NOW)


def test_absent_unrelated_and_api_are_get_only_without_creating_or_running(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from trading import api
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    monkeypatch.setattr(m, "leader_eight_type_paper_view", partial(m.leader_eight_type_paper_view, now=NOW))
    assert api.latest_leader_eight_type_paper()["status"] == "unavailable"
    assert not list(tmp_path.iterdir())
    old = tmp_path / "outputs/leader_eight_type_paper/unrelated/paper_plan.json"
    old.parent.mkdir(parents=True)
    write(old, {"schema": "unrelated_old_research"})
    assert api.latest_leader_eight_type_paper()["rows"] == []
    folder, record = save(tmp_path)
    record["execution_admitted"] = True
    write(folder / "paper_plan.json", record)
    before = snapshot(tmp_path)
    with pytest.raises(HTTPException) as error:
        api.latest_leader_eight_type_paper()
    assert error.value.status_code == 409 and snapshot(tmp_path) == before
    assert any(route.path == "/api/trading/research/leader-eight-type-paper" and route.methods == {"GET"}
               for route in api.router.routes)
