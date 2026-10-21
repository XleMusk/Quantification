"""Known-after no-limit facts qualify membership without replacing raw scores.

The saved-score fixture substitutes fixed tiny probabilities, never real model
fit/score or market acquisition. IPO source loading is substituted here because
its real saved-PDF validation has its own tests; this file exercises the new
policy, knowledge clocks and publication binding.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.contracts import IntentKind, RunContext, TradingMode
from trading.research import shortline_native_preopen as policy
from trading.research.ipo_qualification import IpoQualification
from trading.research.tests.test_shortline_native_preopen import saved


D, E = "2026-10-15", "2026-10-16"
IPO = "301569.SZSE"


def _write(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


@pytest.fixture
def qualified(saved, monkeypatch):
    evidence, master = saved.root / "ipo.json", saved.root / "master.json"
    _write(evidence, {"fixture": "explicit saved known-after IPO facts, not an official source claim"})
    _write(master, {"fixture": "retrieval-time provider master, not PIT"})
    records = {IPO: {"ipo_date": D, "board": "chinext", "available_at": D + "T19:32:06+08:00",
        "source_basis": "synthetic official-fact exercise", "rule_effective_from": "2026-07-06"}}
    verification = {"evidence_file": str(evidence), "research_master": {"run": str(master)},
        "execution_admitted": False, "pit_verified": False}

    def loader(path, calendar, *, master_run=None):
        assert Path(path) == evidence and Path(calendar) == saved.calendar
        assert master_run is not None and Path(master_run) == master
        return IpoQualification(deepcopy(records), (D, E, "2026-10-19"), deepcopy(verification))

    monkeypatch.setattr(policy, "load_ipo_qualification", loader)
    return evidence, master, records, verification


def test_new_policy_excludes_only_proven_no_limit_unknown_and_preserves_old_plan_and_scores(saved, qualified):
    evidence, master, _, _ = qualified
    publication = saved.make("partial", partial=True)
    source_bytes = {p: p.read_bytes() for p in publication.parent.iterdir() if p.is_file()}
    old = saved.root / "old_plan"
    policy.build_preopen_plan(publication, saved.calendar, old)
    old_bytes = {p: p.read_bytes() for p in old.iterdir()}
    out = saved.root / "qualified_plan"
    plan = policy.build_preopen_plan(publication, saved.calendar, out,
        ipo_evidence=evidence, research_master_run=master)
    loaded = policy.load_preopen_plan(out)
    projection = json.loads((out / "qualification_scope.json").read_text(encoding="utf-8"))
    assert policy.policy_spec(policy.POLICY_ID) == policy.SPEC
    assert plan["policy_id"] == policy.QUALIFIED_POLICY_ID
    assert plan["spec"] == policy.policy_spec(policy.QUALIFIED_POLICY_ID)
    assert projection["raw_unknown_stage_symbols"] == projection["excluded_no_limit_symbols"] == [IPO]
    assert projection["remaining_unknown_stage_symbols"] == projection["scored_no_limit_conflicts"] == []
    assert projection["qualified_stage_membership_complete"] is True
    assert projection["raw_score_event_count_per_model"] == {"lr": 12, "lgbm": 12}
    assert projection["same_scored_unique_symbol_count"] == 12
    assert projection["decision_as_of"] == loaded["registration"]["registered_at"]
    for kind, target in (("lr", "600010.SSE"), ("lgbm", "600001.SSE")):
        decision = loaded["decisions"][kind]
        assert decision["input_scope"]["stage_membership_complete"] is False
        assert decision["input_scope"]["unknown_stage_symbols"] == [IPO]
        assert decision["scope_complete"] is True and decision["input_rows"] == 12
        assert decision["weights"] == {target: .16}
        bound = policy.bind_preopen_plan(out, kind, RunContext("qualified_" + kind, TradingMode.REPLAY))
        assert bound.intent.kind == IntentKind.FULL_TARGET and dict(bound.intent.weights) == {target: .16}
        assert policy.load_preopen_plan(old)["decisions"][kind]["weights"] is None
    assert {p: p.read_bytes() for p in source_bytes} == source_bytes
    assert {p: p.read_bytes() for p in old_bytes} == old_bytes


def test_later_fact_availability_does_not_rewrite_registered_unknown_on_reload(saved, qualified, monkeypatch):
    evidence, master, records, _ = qualified
    records[IPO]["available_at"] = E + "T04:00:00+08:00"
    publication = saved.make("late_fact", partial=True)
    early, later = saved.root / "before_fact", saved.root / "after_fact"
    policy.build_preopen_plan(publication, saved.calendar, early,
        ipo_evidence=evidence, research_master_run=master)
    monkeypatch.setattr(policy, "_now", lambda: datetime.fromisoformat(E + "T04:30:00+08:00"))
    original = policy.load_preopen_plan(early)
    assert all(item["weights"] is None for item in original["decisions"].values())
    assert original["record"]["qualified_input_scope"]["remaining_unknown_stage_symbols"] == [IPO]
    policy.build_preopen_plan(publication, saved.calendar, later,
        ipo_evidence=evidence, research_master_run=master)
    assert all(item["weights"] for item in policy.load_preopen_plan(later)["decisions"].values())
    assert original["registration"]["registered_at"] == E + "T03:30:00+08:00"


def test_scored_no_limit_conflict_stops_whole_variant_without_deleting_or_reranking_rows():
    scored = "301570.SZSE"
    frame = pd.DataFrame([{"event_id": f"{scored}|{D}|s1", "symbol": scored, "date": D,
        "stage": 1, "model_hit5_probability": .99, "paper_candidate_only": False}])
    saved_scores = {"source_day": D, "scope": {"stage_membership_complete": False,
        "unknown_stage_symbols": [IPO]}, "scope_complete": False,
        "frames": {"lr": frame.copy(), "lgbm": frame.copy()}}
    before = deepcopy(saved_scores)
    records = {symbol: {"ipo_date": D, "board": "chinext", "available_at": D + "T19:00:00+08:00",
        "source_basis": "synthetic dated fact", "rule_effective_from": "2026-07-06"} for symbol in (IPO, scored)}
    qualification = IpoQualification(records, (D, E), {"fixture": True})
    projection = policy.qualify_saved_scope(saved_scores, qualification, decision_as_of=E + "T03:30:00+08:00")
    assert projection["excluded_no_limit_symbols"] == [IPO]
    assert projection["scored_no_limit_conflicts"] == [scored]
    assert projection["qualified_stage_membership_complete"] is False
    assert projection["score_rows_removed"] is False and projection["stage_values_imputed"] is False
    for kind in policy.MODEL_KINDS:
        pd.testing.assert_frame_equal(saved_scores["frames"][kind], before["frames"][kind])
    assert saved_scores["scope"] == before["scope"]


def test_projection_requires_knowledge_before_actual_decision_not_announcement_date():
    empty = pd.DataFrame(columns=["symbol"])
    saved_scores = {"source_day": D, "scope": {"stage_membership_complete": False,
        "unknown_stage_symbols": [IPO]}, "scope_complete": False,
        "frames": {"lr": empty, "lgbm": empty}}
    record = {IPO: {"ipo_date": D, "board": "chinext", "available_at": D + "T19:32:06+08:00",
        "source_basis": "synthetic fact", "rule_effective_from": "2026-07-06"}}
    qualification = IpoQualification(record, (D, E), {})
    before = policy.qualify_saved_scope(saved_scores, qualification, decision_as_of=D + "T15:05:00+08:00")
    after = policy.qualify_saved_scope(saved_scores, qualification, decision_as_of=E + "T03:30:00+08:00")
    assert before["qualified_stage_membership_complete"] is False
    assert before["remaining_unknown_stage_symbols"] == [IPO]
    assert after["excluded_no_limit_symbols"] == [IPO]


def test_bound_scope_and_qualification_evidence_cannot_be_replaced_after_publication(saved, qualified):
    evidence, master, _, verification = qualified
    publication = saved.make("binding", partial=True)
    out = saved.root / "bound_plan"
    policy.build_preopen_plan(publication, saved.calendar, out,
        ipo_evidence=evidence, research_master_run=master)
    verification["different_source"] = True
    with pytest.raises(ValueError, match="pinned facts"):
        policy.load_preopen_plan(out)
    verification.pop("different_source")
    original = master.read_bytes()
    master.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="Qualification inputs changed"):
        policy.load_preopen_plan(out)
    master.write_bytes(original)
    plan_file = out / "plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    scope_file = out / "qualification_scope.json"
    scope = json.loads(scope_file.read_text(encoding="utf-8"))
    scope["decision_as_of"] = D + "T15:05:00+08:00"
    _write(scope_file, scope)
    # Even a caller updating the file hash cannot turn a different cutoff into
    # facts known at registration; the loader recomputes from the pinned proof.
    data = scope_file.read_bytes()
    binding = next(item for item in plan["files"] if item["path"] == scope_file.name)
    binding.update(sha256=policy._sha(data), byte_count=len(data))
    _write(plan_file, plan)
    with pytest.raises(ValueError, match="actual clock"):
        policy.load_preopen_plan(out)


def test_master_alone_cannot_silently_change_original_policy(saved, qualified):
    _, master, _, _ = qualified
    publication = saved.make("master_only", partial=True)
    out = saved.root / "invalid"
    with pytest.raises(ValueError, match="explicit IPO scope variant"):
        policy.build_preopen_plan(publication, saved.calendar, out, research_master_run=master)
    assert not out.exists()
