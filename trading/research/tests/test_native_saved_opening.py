"""Synthetic saved-file contracts only; no market/model/consumer execution."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import csv
import json
from pathlib import Path

import pytest

from trading.ledger import new_state
from trading.research import native_saved_opening as m
from trading.research.shortline_native_preopen import POLICY_ID, QUALIFIED_POLICY_ID, FLAGS


D, E, PRIOR = "2026-10-16", "2026-10-19", "2026-10-15"
A, B = "600001.SSE", "000002.SZSE"
NOW = datetime.fromisoformat("2026-10-17T10:00:00+08:00")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2).encode() if not isinstance(value, bytes) else value)
    path.write_bytes(data)
    return {"file": str(path.resolve()), "sha256": sha256(data).hexdigest(), "byte_count": len(data)}


def fixture_native(tmp_path, monkeypatch, *, policy=POLICY_ID, decision="NO_CHANGE"):
    """Small realistic native seal fixture, reusable by opening integration checks."""
    from trading.research import opening_pack
    monkeypatch.setattr(opening_pack, "ROOT", tmp_path / "report-root")
    monkeypatch.setattr(opening_pack, "DATA_ROOT", tmp_path / "data-root")
    run = (tmp_path / "native-run").resolve()
    plan_dir = (tmp_path / "next-plan").resolve()
    plan = plan_dir / "plan.json"
    spec = deepcopy(m.policy_spec(policy))
    calendar = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
    calendar_ref = write(tmp_path / "calendar.json", calendar)
    models = [{"kind": k, "sha256": "synthetic-frozen-" + k} for k in m.MODEL_KINDS]
    score_ref = write(tmp_path / "scores-publication.json", {"published_at": D + "T18:02:00+08:00"})
    old = {"schema": "native_shortline_preopen_research_plan_v1", "policy_id": policy,
        "source_day": PRIOR, "execution_day": D, "published_at": D + "T04:17:00+08:00"}
    old_ref = write(tmp_path / "old-plan" / "plan.json", old)
    consumed = {"plan": old_ref, "source_day": PRIOR, "execution_day": D,
                "published_at": old["published_at"]}
    contexts = {k: m._context(run, k) for k in m.MODEL_KINDS}
    binding = {"policy_id": policy, "config": deepcopy(spec["config"]), "calendar": calendar_ref,
        "calendar_payload": calendar, "frozen_model_refs": models, "contexts": contexts,
        "method_id": "synthetic_original_native_method", "run_directory": str(run)}
    cases = {}
    for index, kind in enumerate(m.MODEL_KINDS):
        qty = 100 * (index + 1)
        context = contexts[kind]
        state = new_state(100000)
        state.update(day=D, cash=10000000-qty*1000-4,
            positions={A: [{"qty": qty, "available": E}]}, prices={A: 1000})
        state["orders"] = {"o1": {"id": "o1", "symbol": A, "side": "buy", "qty": qty, "filled": qty,
            "day": D, "status": "filled", "limit_price": 1000, "fees": 4, "reserve": 0}}
        state["fills"] = {"f" + str(n): {"order_id": "o1", "symbol": A, "side": "buy", "qty": qty//2,
            "day": D, "price": 1000, "fees": 2, "available_day": E} for n in (1, 2)}
        bound = {"kind": "FULL_TARGET", "weights": {A: .16}, "context": context,
            "source": f"research:shortline_native_preopen:{old_ref['sha256']}:{kind}"}
        envelope = {"source_day": PRIOR, "execution_day": D, "published_at": old["published_at"], "intent": bound}
        daily = {"date": D, "equity": (state["cash"]+qty*1000)/100, "cash": state["cash"]/100,
            "fees": .04, "source_day": PRIOR, "execution_day": D, "plan": old_ref,
            "original_plan_kind": "FULL_TARGET", "preopen_published_at": old["published_at"],
            "preopen_intent_frontier": {"envelope": envelope}}
        cases[kind] = {"kind": kind, "context": context, "state": state, "daily": [daily]}
    raw_sources = [{"day": PRIOR, "raw_sha256": "synthetic-prior"}, {"day": D, "raw_sha256": "synthetic-D"}]
    journal = {"schema": m.JOURNAL_SCHEMA, "source_day": PRIOR, "execution_day": D,
        "plan": consumed, "binding_sha256": m._hash(binding), "raw_sources_sha256": m._hash(raw_sources),
        "previous_journal_sha256": None, "post_case_sha256": {k: m._hash(v) for k, v in cases.items()},
        "committed_at": D + "T18:01:00+08:00", **FLAGS}
    journal_ref = write(run / "journal" / "session.json", journal)
    head = {"schema": m.HEAD_SCHEMA, "binding": binding, "cases": cases, "consumed_plans": [consumed],
        "journal_files": [journal_ref], "raw_sources": raw_sources, **FLAGS}
    write(run / "head.json", head)
    weights = None if decision == "NO_CHANGE" else {} if decision == "EMPTY" else {B: .16}
    rows = [{"model_kind": k, "decision": "NO_CHANGE" if weights is None else "FULL_TARGET",
        "reason": "synthetic_stage_unknown_NO_CHANGE" if weights is None else "synthetic_fixed_full_target",
        "source_day": D, "execution_day": E, "weights": weights,
        "input_scope": {"unknown_stage_symbols": ["600825.SSE"] if weights is None else []}}
        for k in m.MODEL_KINDS]
    decisions = {"schema": "native_shortline_preopen_research_decisions_v1", "policy_id": policy,
        "source_day": D, "execution_day": E, "models": rows, **FLAGS}
    registration = {"spec": spec, "policy_id": policy, "source_day": D, "execution_day": E,
        "calendar": calendar_ref, "source_publication": score_ref, "frozen_model_refs": models,
        "registered_at": D + "T18:03:00+08:00", **FLAGS}
    payloads = {"registration.json": registration, "decisions.json": decisions,
                "lr_ranked_preview.csv": b"synthetic_only\n", "lgbm_ranked_preview.csv": b"synthetic_only\n"}
    if policy == QUALIFIED_POLICY_ID:
        payloads["qualification_scope.json"] = {"synthetic_only": True}
    files = []
    for name, value in payloads.items():
        ref = write(plan_dir / name, value)
        files.append({"path": name, "sha256": ref["sha256"], "byte_count": ref["byte_count"]})
    record = {"schema": "native_shortline_preopen_research_plan_v1", "status": "published_preopen_research_plan",
        "policy_id": policy, "source_day": D, "execution_day": E, "spec": spec,
        "published_at": D + "T18:04:00+08:00", "next_open_deadline": E + "T09:30:00+08:00",
        "publication_requires_saved_plan": True, "raw_score_target_generation_allowed": False,
        "raw_score_payloads_modified": False, "source_publication": score_ref, "calendar": calendar_ref,
        "files": files, "decisions_file": "decisions.json", "registration_file": "registration.json", **FLAGS}
    if policy == QUALIFIED_POLICY_ID:
        record["qualification_scope_file"] = "qualification_scope.json"
    write(plan, record)
    return {"run": run, "plan": plan, "source": tmp_path / "source", "out": tmp_path / "opening",
            "current": NOW, "head": head, "record": record, "decisions": decisions}


def test_next_no_change_preserves_each_original_book_and_prior_fill_clock(tmp_path, monkeypatch):
    b = fixture_native(tmp_path, monkeypatch)
    m.build_source(b["run"], b["plan"], b["source"], current=NOW)
    intents = m.export_requests(b["source"], E, current=NOW)
    assert list(intents) == ["lr", "lgbm"]
    for kind, intent in intents.items():
        assert intent["kind"] == "NO_CHANGE" and intent["weights"] is None
        assert intent["plan_decision"]["input_scope"]["unknown_stage_symbols"] == ["600825.SSE"]
        assert m.read_request(intent, current=NOW) == intent
        saved = m.load_saved_case(b["source"], kind, current=NOW)
        assert saved["daily"]["date"] == D and saved["state"]["day"] == D
        assert len(saved["state"]["fills"]) == 2
        assert saved["executed_decision"]["source_day"] == PRIOR
        assert saved["executed_decision"]["execution_day"] == D
        assert saved["executed_decision"]["kind"] == "FULL_TARGET"
        assert saved["state"]["positions"][A][0]["available"] == E
        assert not intent["prefill_allowed"] and not intent["execution_admitted"]


@pytest.mark.parametrize("decision,expected", [("FULL", {B: .16}), ("EMPTY", {})])
def test_explicit_next_plan_full_and_empty_are_not_consumed_state_none(tmp_path, monkeypatch, decision, expected):
    b = fixture_native(tmp_path, monkeypatch, decision=decision)
    assert b["head"]["cases"]["lr"]["state"]["pending_targets"] is None
    m.build_source(b["run"], b["plan"], b["source"], current=NOW)
    rows = m.export_requests(b["source"], current=NOW)
    assert all(r["kind"] == "FULL_TARGET" and r["weights"] == expected for r in rows.values())


def test_same_source_no_write_and_next_plan_change_is_visible_without_new_head(tmp_path, monkeypatch):
    b = fixture_native(tmp_path, monkeypatch)
    first = m.build_source(b["run"], b["plan"], b["source"], current=NOW)
    file = b["source"] / "head.json"
    data, stamp = file.read_bytes(), file.stat().st_mtime_ns
    assert m.build_source(b["run"], b["plan"], b["source"], current=NOW) == first
    assert file.read_bytes() == data and file.stat().st_mtime_ns == stamp
    altered = deepcopy(b["record"]); altered["published_at"] = D + "T18:05:00+08:00"
    write(b["plan"], altered)
    assert not m.source_dependencies_current(file)
    with pytest.raises(ValueError, match="stale"):
        m.export_requests(file, current=NOW)
    second = m.build_source(b["run"], b["plan"], b["source"], current=NOW)
    assert second["input_id"] != first["input_id"] and second["refs"]["native_head"] == first["refs"]["native_head"]


def test_unmatched_day_waits_without_source_and_late_or_other_policy_rejects(tmp_path, monkeypatch):
    b = fixture_native(tmp_path, monkeypatch)
    altered = deepcopy(b["record"]); altered["source_day"] = "2026-10-19"; altered["execution_day"] = "2026-10-20"
    # Actual same-policy next plan for another date cannot be applied to this book.
    head = deepcopy(b["head"]); head["consumed_plans"][-1]["execution_day"] = PRIOR
    write(b["run"] / "head.json", head)
    result = m.build_source(b["run"], b["plan"], b["source"], current=NOW)
    assert result["status"].startswith("waiting") and not b["source"].exists()
    write(b["run"] / "head.json", b["head"])
    altered = deepcopy(b["record"]); altered["published_at"] = E + "T09:31:00+08:00"
    write(b["plan"], altered)
    with pytest.raises(ValueError, match="opening"):
        m.build_source(b["run"], b["plan"], b["source"], current=NOW)
    altered = deepcopy(b["record"]); altered["policy_id"] = QUALIFIED_POLICY_ID
    write(b["plan"], altered)
    with pytest.raises(ValueError, match="policy"):
        m.build_source(b["run"], b["plan"], b["source"], current=NOW)


def test_source_seal_drift_rejected_and_opening_keeps_no_change_shares(tmp_path, monkeypatch):
    b = fixture_native(tmp_path, monkeypatch, policy=QUALIFIED_POLICY_ID)
    before = (b["run"] / "head.json").read_bytes()
    m.build_source(b["run"], b["plan"], b["source"], current=NOW)
    _, result = m.build_opening(b["source"], b["out"], current=NOW)
    assert result["preview_count"] == 2 and result["candidate_order_count"] == 0
    for row in result["cases"]:
        with (Path(row["preview"]).parent / "target_holdings.csv").open(newline="") as stream:
            target = next(csv.DictReader(stream))
        assert target["current_qty"] == target["target_qty"] and target["delta_qty"] == "0"
        assert row["strategy_id"].endswith(":" + row["case_id"])
    assert (b["run"] / "head.json").read_bytes() == before
    head = deepcopy(b["head"]); head["cases"]["lr"]["state"]["cash"] -= 1
    write(b["run"] / "head.json", head)
    assert not m.source_dependencies_current(b["source"])
    with pytest.raises(ValueError, match="stale"):
        m.load_saved_case(b["source"], "lr", current=NOW)
