"""Fixed component opt-in through native plans and the real Engine/Ledger.

All prices/evidence are synthetic. Equal E references intentionally allow the
cash component exercise; this is not an ex-dividend reference proof or return.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.contracts import RunContext
from trading.ledger import Ledger
from trading.research import shortline_native_preopen_forward as forward
from trading.research.research_corporate_action_view import STATE_KEY
from trading.research.shortline_panel_replay import _config
from trading.research.tests import test_shortline_native_preopen_forward as saved
from trading.research.tests import test_research_corporate_action_view as actions
from trading.research.tests import test_native_preopen_ipo_append as append


D, E, F = saved.D, saved.E, saved.F


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def review(root, monkeypatch, *, partial=False, late=False, suffix=""):
    root.mkdir()
    pieces, acquisition = [], []
    for kind in ("lr", "lgbm"):
        with monkeypatch.context() as patch:
            patch.setattr(actions, "S", saved.TARGET[kind])
            patch.setattr(actions, "DAYS", [E, E, F, F, F])
            file = actions.source(root / kind, bonus=None if partial and kind == "lr" else "0",
                pay=F, pdf_suffix=suffix)
        item, receipt = read(file), read(file.parent / "retrieval_receipt.json")
        pieces.extend(item["events"])
        for source in receipt["sources"]:
            source["file"] = str(Path(kind) / source["file"])
            acquisition.append(source)
    item["events"] = pieces
    item["scope"]["symbols"] = [saved.TARGET[kind] for kind in ("lr", "lgbm")]
    if late:
        item["reviewed_at"] = E + "T10:00:00+08:00"
        for event in pieces:
            event["local_primary_evidence_available_at"] = E + "T09:45:00+08:00"
            for source in event["official_sources"]: source["retrieved_at"] = E + "T09:45:00+08:00"
        for source in acquisition:
            source.update(started_at=E + "T09:45:00+08:00", retrieved_at=E + "T09:45:00+08:00")
    write(root / "retrieval_receipt.json", {"sources": acquisition,
        "execution_admitted": False, "selection_enabled": False, "ledger_mutated": False})
    write(root / "result.json", item)
    return root / "result.json"


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Fixed action integration may not contact a provider")
    monkeypatch.setattr("requests.Session", forbidden)
    e = saved.evidence.__wrapped__(tmp_path, monkeypatch)
    e.action_review = review(tmp_path / "fixed_action_review", monkeypatch)
    e.action_run = tmp_path / "fixed_action_book"
    return e


def execute(e, *, plans=None, days=(D, E, F), action_review=None, mode="dated_asof", directory=None):
    return forward.run(e.plans if plans is None else plans, [e.closes[day] for day in days], e.ipo, e.master,
        e.action_run if directory is None else directory,
        action_review=e.action_review if action_review is None else action_review, action_knowledge_mode=mode)


def test_fixed_review_two_contexts_records_then_pays_cash_and_restores_without_rewriting_journal(evidence):
    e = evidence
    default_before = deepcopy(forward.RULE_SPEC)
    first = execute(e, plans=e.plans[:1], days=(D, E))
    assert first["method_id"] == forward.ACTION_METHOD_ID and first["metrics"] is None
    head_file = e.action_run / "head.json"
    initial = read(head_file)
    assert initial["binding"]["rule_spec"]["action_reference_override_supported"] is False
    assert initial["binding"]["fixed_action_components"]["action_evidence_append_supported"] is False
    old_journal = Path(initial["journal_files"][0]["file"])
    journal_before = old_journal.read_bytes(), old_journal.stat().st_mtime_ns
    for kind, case in initial["cases"].items():
        state = case["state"]
        assert state["cash"] == 8_397_884
        assert state["entitlements"][saved.TARGET[kind] + ":" + F] == 1600
        assert state[STATE_KEY]["replay_start_day"] == E
        assert state[STATE_KEY]["context"] == case["context"]
        assert case["daily"][-1]["action_component_binding"] == state[STATE_KEY]
    assert initial["cases"]["lr"]["state"][STATE_KEY]["context"] != initial["cases"]["lgbm"]["state"][STATE_KEY]["context"]
    resumed = execute(e)
    assert resumed["new_order_count"] == 0 and resumed["newly_committed_case_sessions"] == 2
    final = read(head_file)
    for kind, case in final["cases"].items():
        state = case["state"]
        # 1600 shares * 0.10 CNY * (1 - 0.20 reserve) = 128 CNY.
        assert state["cash"] == 8_397_884 + 12800
        assert case["daily"][-1]["original_plan_kind"] == "NO_CHANGE"
        assert Ledger(state, _config(100000, 5), RunContext.from_dict(case["context"])).quantity(saved.TARGET[kind]) == 1600
        result_case = next(row for row in resumed["cases"] if row["model_kind"] == kind)
        assert result_case["action_component_binding"] == state[STATE_KEY]
        assert result_case["entitlements"] == state["entitlements"] and result_case["receivables"] == state["receivables"]
    assert (old_journal.read_bytes(), old_journal.stat().st_mtime_ns) == journal_before
    assert read(old_journal)["action_component_bindings"] == {
        kind: case["state"][STATE_KEY] for kind, case in initial["cases"].items()}
    before = head_file.read_bytes()
    repeated = execute(e)
    assert repeated["new_order_count"] == 0 and head_file.read_bytes() == before
    assert forward.RULE_SPEC == default_before
    assert forward.forward_rule_spec(forward.POLICY_ID) == default_before


@pytest.mark.parametrize("change", ["disable", "review", "mode", "future_clock"])
def test_bound_component_account_rejects_disable_replacement_mode_or_future_review(evidence, monkeypatch, change):
    e = evidence
    execute(e, plans=e.plans[:1], days=(D, E))
    head_file = e.action_run / "head.json"
    before = head_file.read_bytes()
    with pytest.raises(ValueError):
        if change == "disable":
            forward.run(e.plans, [e.closes[day] for day in (D, E, F)], e.ipo, e.master, e.action_run)
        elif change == "review":
            another = review(e.root / "different_review", monkeypatch, suffix="different original bytes")
            execute(e, action_review=another)
        elif change == "mode":
            execute(e, mode="posthoc_component_diagnostic")
        else:
            item = read(e.action_review)
            item["reviewed_at"] = "2027-01-01T10:00:00+08:00"
            write(e.action_review, item)
            execute(e)
    assert head_file.read_bytes() == before


@pytest.mark.parametrize("fault", ["partial", "late_open"])
def test_incomplete_positive_record_and_after_open_knowledge_stop_real_trials_before_commit(evidence, monkeypatch, fault):
    e = evidence
    item = review(e.root / ("review_" + fault), monkeypatch, partial=fault == "partial", late=fault == "late_open")
    result = execute(e, plans=e.plans[:1], days=(D, E), action_review=item)
    assert result["status"] == "failed_engine_session_no_commit" and result["new_order_count"] == 0
    assert result["both_model_trials_discarded"] is True and result["metrics"] is None
    assert not (e.action_run / "head.json").exists()
    assert ("Unresolved" if fault == "partial" else "unavailable before preopen") in result["error"]


def test_action_component_optin_does_not_override_exact_raw_reference(evidence):
    e = evidence
    other = saved.strict_close(e.root / "changed_reference", E, e.calendar, e.sdk,
        reference_override={saved.TARGET["lr"]: 10.01})
    result = forward.run(e.plans[:1], [e.closes[D], other], e.ipo, e.master, e.action_run,
        action_review=e.action_review, action_knowledge_mode="dated_asof")
    assert result["status"] == "blocked_required_raw_reference_or_IPO_evidence"
    assert "corporate_action_or_reference_change_unresolved_no_cash_imputation" in result["blocking_evidence"]["lr"]["issues"][0]["reasons"]
    assert not (e.action_run / "head.json").exists()


def test_default_account_retains_original_binding_and_cannot_acquire_actions_in_place(evidence):
    e = evidence
    old_run = e.root / "unchanged_default_book"
    result = forward.run(e.plans[:1], [e.closes[D], e.closes[E]], e.ipo, e.master, old_run)
    assert result["method_id"] == forward.METHOD_ID and result["metrics"] is None
    old_head = old_run / "head.json"
    before, stored = old_head.read_bytes(), read(old_head)
    assert "fixed_action_components" not in stored["binding"]
    assert stored["binding"]["rule_spec"] == forward.RULE_SPEC
    assert all(STATE_KEY not in case["state"] and case["state"]["cash"] == 8_397_884
        for case in stored["cases"].values())
    with pytest.raises(ValueError, match="binding changed"):
        execute(e, directory=old_run)
    assert old_head.read_bytes() == before


def test_two_kind_load_cannot_mix_legally_changed_review_between_independent_context_reads(evidence, monkeypatch):
    e = evidence
    loader = forward.load_research_corporate_action_view
    seen = []
    def replace_after_first(*args, **kwargs):
        loaded = loader(*args, **kwargs)
        seen.append(loaded.binding)
        if len(seen) == 1:
            item = read(e.action_review)
            item["reviewed_at"] = "2026-05-29T18:00:00+08:00"
            write(e.action_review, item)
        return loaded
    monkeypatch.setattr(forward, "load_research_corporate_action_view", replace_after_first)
    with pytest.raises(ValueError, match="same fixed action review"):
        forward.load_action_views(e.action_review, "dated_asof", e.action_run, _config(100000, 5), e.clock["value"])
    assert len(seen) == 2 and seen[0]["review_result_sha256"] != seen[1]["review_result_sha256"]
    assert not e.action_run.exists()


def test_pair_cli_external_result_and_failure_source_protection(evidence, capsys):
    e = evidence
    with pytest.raises(ValueError, match="supplied together"):
        forward.run(e.plans, [e.closes[D]], e.ipo, e.master, e.action_run, action_review=e.action_review)
    with pytest.raises(ValueError, match="supplied together"):
        forward.run(e.plans, [e.closes[D]], e.ipo, e.master, e.action_run, action_knowledge_mode="dated_asof")
    args = ["--plan", str(e.plans[0]), "--raw-close", str(e.closes[D]), "--ipo-proof", str(e.ipo),
        "--research-master-run", str(e.master), "--run", str(e.action_run),
        "--action-review", str(e.action_review), "--action-knowledge-mode", "dated_asof"]
    export = e.root / "observations" / "waiting.json"
    assert forward.main(args + ["--result-json", str(export)]) == 0
    result = read(export)
    assert result["status"] == "waiting_for_exact_complete_raw_close" and result["method_id"] == forward.ACTION_METHOD_ID
    assert not e.action_run.exists() and json.loads(capsys.readouterr().out)["case_count"] == 0
    protected = e.action_review.parent / "lr" / "forbidden.json"
    with pytest.raises(ValueError, match="outside"):
        forward.main(args + ["--result-json", str(protected)])
    assert not protected.exists()
    item = read(e.action_review)
    item["reviewed_at"] = "2027-01-01T10:00:00+08:00"
    write(e.action_review, item)
    failed = e.root / "observations" / "failed.json"
    with pytest.raises(ValueError, match="not actually"):
        forward.main(args + ["--result-json", str(failed)])
    record = read(failed)
    assert record["execution_progress_unknown"] is True and record["method_id"] == forward.ACTION_METHOD_ID
    assert record["action_knowledge_mode"] == "dated_asof"


def test_IPO_append_combination_uses_distinct_method_with_fixed_action_review(tmp_path, monkeypatch):
    e = append.append_evidence.__wrapped__(tmp_path, monkeypatch)
    item = review(tmp_path / "fixed_component_review", monkeypatch)
    result = forward.run(e.qualified_plans, [e.closes[day] for day in (D, E, F)], e.anchor, e.master,
        e.qualified_run, ipo_append_only=True, action_review=item, action_knowledge_mode="dated_asof")
    assert result["method_id"] == forward.IPO_APPEND_ACTION_METHOD_ID and result["ipo_append_only"] is True
    assert result["metrics"] is None and result["newly_committed_case_sessions"] == 4
    head = read(e.qualified_run / "head.json")
    assert head["binding"]["ipo_append_only"] is True
    assert head["binding"]["fixed_action_components"]["action_evidence_append_supported"] is False
    assert all(case["state"]["cash"] == 8_397_884 + 12800 for case in head["cases"].values())
