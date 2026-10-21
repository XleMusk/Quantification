"""Publication and shared Engine behavior with synthetic saved-source adapters.

The source and already-tested candidate producer are adapted; projection,
portfolio preparation, typed intents, publication revalidation, the shared
runner, new exit factories, Engine and Ledger remain real.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_dated_catchup_paper as paper
from trading.research import leader_eight_type_paper as original
from trading.research import leader_eight_type_forward_run as shared
from trading.research import leader_dated_catchup_forward as forward
from trading.research.tests.test_leader_eight_type_paper import inputs, CALENDAR


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sources(tmp_path, monkeypatch):
    days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
        "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    calendar = tmp_path / "calendar.json"
    save(calendar, CALENDAR)
    clock = {"now": datetime.fromisoformat("2026-10-17T12:00:00+08:00")}
    monkeypatch.setattr(paper, "_now", lambda: clock["now"])
    monkeypatch.setattr(shared, "_now", lambda: clock["now"])
    from trading import engine
    monkeypatch.setattr(engine, "_published_now", lambda: clock["now"])
    loaded, panels, expected = {}, {}, {}
    symbols = inputs(count=25)["scope_members"]
    all_panels = inputs(days, count=25)["panel_by_day"]
    for day in days:
        all_panels[day]["risk_features"] = {s: {"low": 9.9, "high": 10.1, "close": 10.,
            "sma20": 9., "sma60": 8., "amount_rank_pct": .999} for s in symbols}
        all_panels[day]["risk_feature_scope_complete"] = True
    for index, day in enumerate(days):
        value = inputs(days[:index + 1], count=25)
        value["panel_by_day"] = deepcopy({d: all_panels[d] for d in days[:index + 1]})
        value["context_anchor"] = {"day": "2026-10-09", "closes": {s: 10. for s in symbols}}
        record = {"created_at": day + "T17:00:00+08:00", "calendar": str(calendar),
            "calendar_sha256": paper.digest(calendar), "scope_members": symbols,
            "source_days": days[:index + 1],
            "source_proofs": [{"day": d, "source": "synthetic:" + d} for d in days[:index + 1]],
            "panels": list(deepcopy(value["panel_by_day"]).values()),
            "preview_sources": [{"day": d, "synthetic": True} for d in days[:index + 1]]}
        path = tmp_path / (day + "_base.json")
        save(path, record)
        provenance = {"panel": str(path), "panel_sha256": paper.digest(path), "input_record": record,
            "source_proofs": deepcopy(record["source_proofs"]), "context_anchor": {"synthetic": "fixed"},
            "external_fixed_protocol": {"record": deepcopy(original.PROTOCOL)}}
        value["provenance"] = provenance
        value["binding"] = {"input_manifest": str(path), "input_manifest_sha256": paper.digest(path),
            "input_recorded_at": record["created_at"], "external_fixed_protocol": provenance["external_fixed_protocol"]}
        loaded[str(path)] = value
        panels[day] = path
    be = {"schema": paper.candidate.REG_SCHEMA, "method_id": paper.candidate.METHOD_ID,
        "registered_at": "2026-10-17T10:00:00+08:00", "first_decision_day": "2026-10-19",
        "first_execution_day": "2026-10-20", "rules": deepcopy(paper.candidate.RULES),
        "base_panel": paper._ref(panels["2026-10-16"]),
        "base_binding": {"scope_members": symbols, "calendar_sha256": paper.digest(calendar)},
        "candidate_only": True, "target_generation_allowed": False,
        "paper_publication_implemented": False, "forward_consumer_implemented": False,
        **paper.candidate.FLAGS}
    be_file = tmp_path / "candidate_registration.json"
    save(be_file, be)
    registration_dir = tmp_path / "paper_registration"
    paper.register(be_file, registration_dir)
    reg = registration_dir / "registration.json"
    candidate_files = {}
    for day in days[5:]:
        own = loaded[str(panels[day])]
        execution = days[days.index(day) + 1] if day != days[-1] else "2026-10-26"
        value = {"schema": paper.candidate.INPUT_SCHEMA, "method_id": paper.candidate.METHOD_ID,
            "day": day, "execution_day": execution, "created_at": day + "T17:05:00+08:00",
            "calculation_started_at": day + "T17:00:00+08:00", "status": "completed_prospective_candidate_input",
            "registration": paper._ref(be_file), "source": deepcopy(own["binding"]),
            "catalog_reference": {"file": "synthetic_catalog", "sha256": "fixed"},
            "candidates": [{"symbol": s, "source_day": day, "available_at": day + "T17:05:00+08:00",
                "amount_cny": 10000000. - i, "score": 10000000. - i,
                "evidence": {"original_rule": True}, "trade_ready": False} for i, s in enumerate(symbols[:5])],
            "unresolved_symbols": []}
        expected[day] = value
        path = tmp_path / (day + "_candidate.json")
        save(path, value)
        candidate_files[day] = path
    monkeypatch.setattr(paper, "load_inputs", lambda path, *_: loaded[str(Path(path).resolve())])
    monkeypatch.setattr(shared, "load_inputs", lambda path, *_: loaded[str(Path(path).resolve())])
    monkeypatch.setattr(original, "_source_bindings", lambda own, _: deepcopy(own["binding"]))
    monkeypatch.setattr(paper.candidate, "prepare_loaded_input", lambda own, *args, **kwargs:
        deepcopy(expected[own["source_days"][-1]]))
    def persist(day):
        save(candidate_files[day], expected[day])
    def publish(day, path=None, candidates=None):
        clock["now"] = datetime.fromisoformat(day + "T18:00:00+08:00")
        prefix = candidates if candidates is not None else [candidate_files[d] for d in days[5:days.index(day) + 1]]
        folder = path or tmp_path / ("publication_" + day)
        return paper.publish(prefix, panels[day], None, None, reg, folder), folder / "paper_plan.json"
    return {"root": tmp_path, "days": days, "loaded": loaded, "panels": panels, "expected": expected,
        "candidate_files": candidate_files, "be": be_file, "reg": reg, "clock": clock,
        "publish": publish, "persist": persist, "symbols": symbols}


def case(result, guard="unguarded", cap="uncapped"):
    return next(c for c in result["cases"] if c["case_id"] == f"{paper.TYPE_ID}__{guard}__{cap}")


def test_register_is_lightweight_preserves_BE_and_refuses_actual_clock_crossing(sources, monkeypatch):
    e = sources
    before = e["be"].read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("Registration must not reload historical feeds")
    monkeypatch.setattr(paper, "load_inputs", forbidden)
    paper.register(e["be"], e["root"] / "second_registration")
    ticks = iter(datetime.fromisoformat(x) for x in ["2026-10-19T14:59:59+08:00", "2026-10-19T15:00:00+08:00"])
    monkeypatch.setattr(paper, "_now", lambda: next(ticks))
    with pytest.raises(ValueError, match="before the original first"):
        paper.register(e["be"], e["root"] / "late_registration")
    assert not (e["root"] / "late_registration").exists() and e["be"].read_bytes() == before


def test_four_cases_no_successor_or_account_and_own_day_authority(sources, monkeypatch):
    e = sources; own = e["loaded"][str(e["panels"]["2026-10-19"])]; before = deepcopy(own["panel_by_day"])
    from trading import engine, ledger
    def forbidden(*args, **kwargs): raise AssertionError("Publishing cannot execute/create an account")
    monkeypatch.setattr(engine, "advance", forbidden); monkeypatch.setattr(ledger, "new_state", forbidden)
    result, file = e["publish"]("2026-10-19")
    assert [c["case_id"] for c in result["cases"]] == list(paper.CASE_IDS) and result["case_count"] == 4
    assert result["execution_day_bound"] == "2026-10-20" and "2026-10-20" not in own["source_days"]
    assert set(case(result)["target_weights"].values()) == {.16}
    assert all(c["portfolio_intent"]["context"] is None for c in result["cases"])
    value = paper.load_publication(file, None, None, e["clock"]["now"], {})
    assert value["loaded"] is own and set(value["authoritative_panels"]) == {"2026-10-19"}
    assert value["loaded"]["panel_by_day"] == before and len(value["intents"]) == 4
    assert case(result)["candidate_reference_and_setup"][0]["setup_low_cny"] == 9.9
    assert result["protocol"]["execution_lifecycle_changed"] is True


@pytest.mark.parametrize("stamp", ["2026-10-19T15:04:59+08:00", "2026-10-20T08:00:00+08:00"])
def test_publication_never_early_or_late_and_never_rolls_execution(sources, stamp):
    e = sources; e["clock"]["now"] = datetime.fromisoformat(stamp)
    # Direct finalize isolates the publication deadline from an intentionally later candidate fixture.
    loaded = e["loaded"][str(e["panels"]["2026-10-19"])]; projected = paper._project(
        [{"record": e["expected"]["2026-10-19"], "loaded": loaded}], loaded, read(e["reg"]))
    _, execution, env, rows = original._prepare(projected, datetime.fromisoformat("2026-10-19T18:00:00+08:00"))
    rows = [r for r in rows if r["case_id"] in paper.CASE_IDS]
    with pytest.raises(ValueError, match="own D at/after 15:05"):
        paper._finalize("2026-10-19", execution, env, rows, loaded["binding"], e["clock"]["now"],
            e["clock"]["now"], paper._ref(e["reg"]), [])


@pytest.mark.parametrize("unknown,empty", [(True, False), (False, True)])
def test_unknown_or_known_empty_remains_NO_CHANGE_not_cash(sources, unknown, empty):
    e = sources; value = e["expected"]["2026-10-19"]
    if unknown: value["unresolved_symbols"] = [e["symbols"][-1]]
    if empty: value["candidates"] = []
    e["persist"]("2026-10-19")
    result, _ = e["publish"]("2026-10-19")
    assert all(r["decision"] == "NO_CHANGE" and r["target_weights"] is None for r in result["cases"])


def test_environment_EXIT_ALL_precedes_candidate_unknown(sources):
    e = sources; day = "2026-10-19"
    e["expected"][day]["unresolved_symbols"] = list(e["symbols"]); e["persist"](day)
    loaded = e["loaded"][str(e["panels"][day])]
    loaded["panel_by_day"][day]["environment"].update(breadth=-.5, median_return_pct=-1.)
    # The source adapter represents the corresponding immutable source publication.
    loaded["provenance"]["input_record"]["panels"][-1] = deepcopy(loaded["panel_by_day"][day])
    result, _ = e["publish"](day)
    assert case(result)["decision"] == "NO_CHANGE"
    guarded = case(result, guard="two_close_envguard")
    assert guarded["decision"] == "EXIT_ALL" and guarded["target_weights"] == {}
    assert guarded["entry_unknown_checks_bypassed_for_independent_exit"] is True


def test_pre_firstD_candidates_do_not_poison_cap_and_new_days_keep_original_top5(sources):
    e = sources; day = "2026-10-19"
    first, _ = e["publish"](day)
    assert case(first, cap="causal_first_seen_cap20")["capacity_members_before"] == []
    for i, d in enumerate(e["days"][5:]):
        for n, row in enumerate(e["expected"][d]["candidates"]): row["symbol"] = e["symbols"][i * 5 + n]
        e["persist"](d)
    result, _ = e["publish"]("2026-10-23")
    bounded = case(result, cap="causal_first_seen_cap20")
    assert bounded["decision"] == "NO_CHANGE" and bounded["cap_dropped_rows"] == 5
    assert len(bounded["capacity_members_before"]) == 20
    assert len(case(result)["target_weights"]) == 5 and bounded["refill_performed"] is False


def test_missing_duplicate_candidate_days_and_tampered_old_facts_rejected(sources):
    e = sources; first = e["candidate_files"]["2026-10-19"]
    with pytest.raises(ValueError, match="without gaps or duplicates"):
        e["publish"]("2026-10-20", e["root"] / "gap", candidates=[e["candidate_files"]["2026-10-20"]])
    with pytest.raises(ValueError, match="without gaps or duplicates"):
        e["publish"]("2026-10-19", e["root"] / "duplicate", candidates=[first, first])
    edited = read(first); edited["candidates"][0]["evidence"] = {"tampered": True}; save(first, edited)
    with pytest.raises(ValueError, match="original facts changed"):
        e["publish"]("2026-10-19", e["root"] / "tampered")


def test_actual_paper_adapter_shared_runner_Engine_Ledger_queue_consume_and_resume(sources):
    e = sources; _, pub19 = e["publish"]("2026-10-19"); _, pub20 = e["publish"]("2026-10-20")
    directory = e["root"] / "research_books"
    result = forward.run([pub19], e["panels"]["2026-10-20"], None, None, directory,
        registration=e["reg"], through="2026-10-19")
    assert result["schema"] == paper.RESULT_SCHEMA
    assert result["newly_committed_case_sessions"] == 4
    head = read(directory / "head.json")
    assert list(head["cases"]) == list(paper.CASE_IDS) and head["schema"] == paper.CHECKPOINT_SCHEMA
    assert all(c["error"] is None and not c["state"]["fills"] and c["state"]["pending_published_close_intent"]["execution_not_before"] == "2026-10-20"
        for c in head["cases"].values())
    consumed = forward.run([pub19, pub20], e["panels"]["2026-10-20"], None, None, directory,
        registration=e["reg"], through="2026-10-20")
    assert consumed["newly_committed_case_sessions"] == 4 and consumed["research_accounts_created"] == 0
    head = read(directory / "head.json")
    assert all(c["error"] is None and c["state"]["day"] == "2026-10-20" and c["state"]["fills"]
        and len(c["daily"]) == len(c["journal_files"]) == 2 for c in head["cases"].values())
    assert all(c["context"]["account_id"].startswith(paper.ACCOUNT_PREFIX) for c in head["cases"].values())
    assert all(c["state"]["replay_risk_exit_policy"]["policy_id"] == paper.RISK_POLICY_ID + ":" + paper.TYPE_ID
        for c in head["cases"].values())
    before = (directory / "head.json").read_bytes()
    again = forward.run([pub19, pub20], e["panels"]["2026-10-20"], None, None, directory,
        registration=e["reg"], through="2026-10-20")
    assert again["newly_committed_case_sessions"] == 0 and (directory / "head.json").read_bytes() == before
