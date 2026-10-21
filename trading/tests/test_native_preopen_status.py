"""Read-only status over tiny saved evidence, with no runner or accounting calls."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading import api, engine, ledger
from trading.contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext, TradingMode
from trading.research import shortline_native_preopen as policy
from trading.research import shortline_native_preopen_forward as forward
from trading.research import shortline_native_preopen_status as view
from trading.research import shortline_native_scoring as scoring


D, E, F = "2026-10-15", "2026-10-16", "2026-10-19"
NOW = datetime.fromisoformat(E + "T20:00:00+08:00")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def hash_record(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def reference(path):
    data = path.read_bytes()
    return {"file": str(path.resolve()), "sha256": sha256(data).hexdigest(), "byte_count": len(data)}


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    root, workspace = tmp_path / "data", tmp_path / "workspace"
    plan_dir = root / "research/shortline_native_preopen" / view.SCOPE
    run = root / "research/shortline_native_preopen_forward" / view.SCOPE
    observation = workspace / "outputs/shortline_native_preopen_forward" / view.SCOPE / "actual_cli_observation.json"
    result = root / "research/shortline_native_preopen_forward_status" / (view.SCOPE + ".json")
    calendar, manifest = tmp_path / "calendar.json", tmp_path / "manifest.json"
    write(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    write(manifest, {"fixture": "saved frozen model metadata, no model load"})
    source = tmp_path / "raw_score_publication.json"
    write(source, {"schema": "native_shortline_raw_score_publication_v1", "published_at": E + "T03:00:00+08:00",
        "target_generation_allowed": False, "execution_admitted": False})
    generation = {"manifest_file": str(manifest), "manifest_sha256": reference(manifest)["sha256"],
        "generation_id": "saved_fixture_generation", "model_available_at": D + "T22:00:00+08:00",
        "feature_contract_id": policy.FEATURE_CONTRACT_ID}
    model_refs = {kind: deepcopy(generation) for kind in ("lr", "lgbm")}

    def save_plan(*, partial=True):
        scope = {"stage_membership_complete": not partial,
            "unknown_stage_symbols": ["301569.SZSE"] if partial else [],
            "source_rows": 13, "valid_scope_rows": 13, "full_market_stage_membership_complete": False}
        models = []
        for kind in ("lr", "lgbm"):
            models.append({"model_kind": kind, "source_day": D, "execution_day": E,
                "decision": "NO_CHANGE" if partial else "FULL_TARGET",
                "reason": "stage_membership_incomplete_whole_day_NO_CHANGE" if partial else "fixed_native_policy",
                "weights": None if partial else {"600001.SSE": .16},
                "target_cash_weight": None if partial else .84,
                "selected_count": 0 if partial else 1, "scope_complete": not partial,
                "input_scope": deepcopy(scope), **policy.FLAGS})
        registration = {"schema": policy.REGISTRATION_SCHEMA, "policy_id": policy.POLICY_ID,
            "source_day": D, "execution_day": E, "registered_at": E + "T03:15:00+08:00",
            "spec": policy.SPEC, "calendar": reference(calendar), "source_publication": reference(source),
            "frozen_model_refs": model_refs, **policy.FLAGS}
        decisions = {"schema": policy.DECISIONS_SCHEMA, "policy_id": policy.POLICY_ID,
            "source_day": D, "execution_day": E, "models": models, **policy.FLAGS}
        write(plan_dir / "registration.json", registration)
        write(plan_dir / "decisions.json", decisions)
        for kind in ("lr", "lgbm"):
            (plan_dir / (kind + "_ranked_preview.csv")).write_text(
                "symbol,paper_candidate_only,rank_pass,candidate_selected\n600001.SSE,False,False,False\n", encoding="utf-8")
        files = []
        for name in ("registration.json", "lr_ranked_preview.csv", "lgbm_ranked_preview.csv", "decisions.json"):
            item = reference(plan_dir / name)
            files.append({"path": name, "sha256": item["sha256"], "byte_count": item["byte_count"]})
        plan = {"schema": policy.SCHEMA, "status": "published_preopen_research_plan",
            "policy_id": policy.POLICY_ID, "source_day": D, "execution_day": E,
            "published_at": E + "T03:30:00+08:00", "next_open_deadline": E + "T09:30:00+08:00",
            "spec": policy.SPEC, "calendar": reference(calendar), "source_publication": reference(source),
            "files": files, **policy.FLAGS}
        write(plan_dir / "plan.json", plan)
        return plan

    def save_waiting():
        write(observation, {"schema": "native_preopen_forward_cli_observation_v1",
            "observed_before_utc": "2026-10-16 02:00:00 UTC", "planned_execution_day": E,
            "provided_source_days": [D], "post_call_filesystem": {"head_exists": False},
            "summary": {"schema": forward.RESULT_SCHEMA, "status": "waiting_for_exact_complete_raw_close",
                "checkpoint": None, "case_count": 0, "metrics": None, "execution_admitted": False}})

    def save_head():
        plan = save_plan(partial=False)
        contexts = {kind: RunContext("native-preopen:" + hash_record(str(run.resolve()))[:20] + ":" + kind,
            TradingMode.REPLAY).to_dict() for kind in ("lr", "lgbm")}
        binding = {"method_id": forward.METHOD_ID, "rule_spec": forward.RULE_SPEC,
            "policy_id": policy.POLICY_ID, "config": policy.SPEC["config"],
            "calendar": reference(calendar), "calendar_payload": json.loads(calendar.read_text()),
            "frozen_model_refs": model_refs, "mode": TradingMode.REPLAY.value,
            "run_directory": str(run.resolve()), "first_source_day": D, "contexts": contexts}
        cases = {}
        for kind in ("lr", "lgbm"):
            context = RunContext.from_dict(contexts[kind])
            intent = PortfolioIntent.full_target({"600001.SSE": .16}, reason="saved fixture target",
                context=context, source="research:saved_native_fixture:" + kind)
            envelope = PreopenPortfolioIntent(D, E, plan["published_at"], intent)
            orders = {"buy-1": {"symbol": "600001.SSE", "side": "buy", "qty": 100,
                "filled": 50, "status": "partial", "reserve": 50_500, "limit_price": 1000, "day": E}}
            fills = {f"fill-{i}": {"order_id": "buy-1", "symbol": "600001.SSE", "side": "buy",
                "qty": 25, "price": 1000, "fees": 300, "day": E, "available_day": F} for i in (1, 2)}
            state = {"cash": 9_949_400, "day": E, "prices": {"600001.SSE": 1100},
                "positions": {"600001.SSE": [{"qty": 25, "available": F}, {"qty": 25, "available": F}]},
                "orders": orders, "fills": fills, "receivables": {}, "pending_targets": None,
                "events": [{"sequence": 3, "day": E, "kind": "order_blocked", "symbol": "600001.SSE",
                    "side": "sell", "qty": 50, "reason": "limit_down_queue_unknown"}],
                "preopen_execution_binding": {"schema": "simulation_preopen_execution_binding_v1",
                    "context": contexts[kind], "config": binding["config"]},
                "last_preopen_intent": {"envelope": envelope.to_dict()}}
            cases[kind] = {"kind": kind, "context": contexts[kind], "state": state,
                "daily": [{"date": E, "cash": 99494., "equity": 100044., "receivables": 0.,
                    "holdings": 1, "new_orders": 1}]}
        consumed = {"plan": reference(plan_dir / "plan.json"), "source_day": D,
            "execution_day": E, "published_at": plan["published_at"]}
        raw = [{"day": D, "raw_sha256": "saved-D"}, {"day": E, "raw_sha256": "saved-E"}]
        journal = {"schema": forward.JOURNAL_SCHEMA, "source_day": D, "execution_day": E,
            "plan": consumed, "binding_sha256": hash_record(binding), "previous_journal_sha256": None,
            "raw_sources_sha256": hash_record(raw), "post_case_sha256": {k: hash_record(v) for k, v in cases.items()},
            "committed_at": E + "T18:40:00+08:00", **forward.FLAGS}
        journal_file = run / "journal" / "saved-session.json"
        write(journal_file, journal)
        head = {"schema": forward.SCHEMA, "registered_at": E + "T18:35:00+08:00", "binding": binding,
            "cases": cases, "consumed_plans": [consumed], "raw_sources": raw,
            "journal_files": [reference(journal_file)], **forward.FLAGS}
        write(run / "head.json", head)
        return head

    def no_call(*args, **kwargs):
        pytest.fail("A status GET must not run, score, initialize or restore accounting objects")
    for module, name in ((forward, "run"), (forward, "new_state"), (forward, "advance"),
            (engine, "advance"), (ledger, "new_state"), (ledger, "Ledger"),
            (policy, "build_preopen_plan"), (scoring, "score_saved_generation")):
        monkeypatch.setattr(module, name, no_call)
    monkeypatch.setattr(api.settings, "DATA_ROOT", root)
    monkeypatch.setattr(api.settings, "ROOT", workspace)
    original = view.native_preopen_status
    monkeypatch.setattr(view, "native_preopen_status", lambda data_root, workspace: original(data_root, workspace, now=NOW))
    save_plan()
    save_waiting()
    return SimpleNamespace(root=tmp_path, data=root, workspace=workspace, run=run,
        observation=observation, result=result, plan=plan_dir / "plan.json", source=source,
        save_plan=save_plan, save_head=save_head)


def test_waiting_no_head_is_zero_accounts_but_unknown_money_and_return(evidence):
    before = snapshot(evidence.root)
    result = api.native_preopen_status()
    assert result["status"] == "waiting_for_execution_source"
    assert result["account_count"] == 0 and result["accounts"] == []
    assert result["cash_cents"] is result["equity_cents"] is result["return_metrics"] is None
    assert [m["decision"] for m in result["models"]] == ["NO_CHANGE", "NO_CHANGE"]
    assert all(m["weights"] is None and m["selected_count"] == 0 for m in result["models"])
    assert result["source_day"] == D and result["execution_day"] == E
    assert result["plan_published_at"] == E + "T03:30:00+08:00"
    assert result["observation"]["observed_at"] == E + "T10:00:00+08:00"
    assert result["observation"]["missing_source_days"] == [E]
    assert result["read_at"] == NOW.isoformat()
    assert result["runner_called"] is result["ledger_mutated"] is result["orders_generated"] is False
    assert not evidence.run.exists()
    assert snapshot(evidence.root) == before
    assert "/api/trading/research/native-preopen" in {route.path for route in api.router.routes}


def test_saved_head_overrides_old_waiting_and_preserves_exact_partial_accounting(evidence):
    evidence.save_head()
    before = snapshot(evidence.root)
    result = api.native_preopen_status()
    assert result["status"] == "active_research_books", result.get("error")
    assert result["account_count"] == 2 and result["observation"]["stale_for_current_head"] is True
    assert result["cash_cents"] is result["equity_cents"] is result["return_metrics"] is None
    for account in result["accounts"]:
        assert account["cash_cents"] == 9_949_400 and account["cash_available_cents"] is None
        assert account["equity_cents"] == 10_004_400 and account["metrics"] is None
        assert account["positions"]["600001.SSE"]["quantity"] == 50
        assert account["positions"]["600001.SSE"]["available"] == 0
        assert account["financial_summary_basis"] == "last_original_Engine_daily_not_recomputed_accounting"
        assert account["order_count"] == 1 and account["fill_count"] == 2
        assert account["orders"]["buy-1"]["status"] == "partial"
        assert [f["qty"] for f in account["fills"].values()] == [25, 25]
        assert account["blocked_order_count"] == 1
        assert account["blocked_orders"][0]["reason"] == "limit_down_queue_unknown"
    assert snapshot(evidence.root) == before


def test_corrupt_head_never_falls_back_to_saved_success_or_initial_cash(evidence):
    evidence.save_head()
    write(evidence.result, {"schema": forward.RESULT_SCHEMA, "status": "advanced_isolated_native_research_books",
        "run_directory": str(evidence.run), "read_at": E + "T18:50:00+08:00",
        "checkpoint": str(evidence.run / "head.json"), "metrics": None, **forward.FLAGS})
    head = json.loads((evidence.run / "head.json").read_text())
    head["cases"]["lr"]["state"]["cash"] += 1
    write(evidence.run / "head.json", head)
    before = snapshot(evidence.root)
    result = api.native_preopen_status()
    assert result["status"] == "unknown_saved_evidence"
    assert result["accounts"] == [] and result["account_count"] is None
    assert result["cash_cents"] is result["equity_cents"] is result["return_metrics"] is None
    assert result["fallback_to_older_success"] is False and result["error"]
    assert snapshot(evidence.root) == before


def test_new_command_failure_remains_visible_above_older_success(evidence):
    evidence.save_head()
    write(evidence.run / "attempts/old-success.json", {"schema": forward.RESULT_SCHEMA,
        "status": "advanced_isolated_native_research_books", "run_directory": str(evidence.run),
        "read_at": E + "T18:50:00+08:00", "checkpoint": str(evidence.run / "head.json"),
        "metrics": None, **forward.FLAGS})
    write(evidence.result, {"schema": view.FAILURE_SCHEMA, "status": "failed_command_progress_unknown",
        "run_directory": str(evidence.run), "recorded_at": E + "T19:00:00+08:00",
        "execution_progress_unknown": True, "error": "preserved command failure", **forward.FLAGS})
    before = snapshot(evidence.root)
    result = api.native_preopen_status()
    assert result["status"] == "blocked_or_failed_research_books", result.get("error")
    assert result["account_count"] == 2
    assert result["observation"]["status"] == "failed_command_progress_unknown"
    assert result["observation"]["error"] == "preserved command failure"
    assert result["observation"]["execution_progress_unknown"] is True
    assert result["return_metrics"] is None and result["fallback_to_older_success"] is False
    assert snapshot(evidence.root) == before


@pytest.mark.parametrize("missing", ["plan", "source"])
def test_missing_saved_evidence_is_unknown_not_zero_and_never_repaired(evidence, missing):
    getattr(evidence, missing).unlink()
    before = snapshot(evidence.root)
    result = api.native_preopen_status()
    assert result["status"] == ("unavailable" if missing == "plan" else "unknown_saved_evidence")
    assert result["account_count"] is None and result["accounts"] == []
    assert result["cash_cents"] is result["equity_cents"] is result["return_metrics"] is None
    assert result["runner_called"] is result["account_created"] is False
    assert snapshot(evidence.root) == before


@pytest.mark.parametrize("corrupt", [False, True])
def test_actual_newest_plan_is_selected_by_clock_and_bad_latest_is_not_hidden(evidence, corrupt):
    later = evidence.plan.parent.parent / "000_directory_name_is_not_the_clock"
    later.mkdir()
    for file in evidence.plan.parent.iterdir():
        (later / file.name).write_bytes(file.read_bytes())
    plan = json.loads((later / "plan.json").read_text())
    plan["published_at"] = E + "T03:45:00+08:00"
    write(later / "plan.json", plan)
    if corrupt:
        (later / "decisions.json").write_text("{}", encoding="utf8")
    before = snapshot(evidence.root)
    result = api.native_preopen_status()
    if corrupt:
        assert result["status"] == "unknown_saved_evidence"
        assert result["account_count"] is None and result["accounts"] == []
        assert result["fallback_to_older_success"] is False
    else:
        assert result["plan_published_at"] == E + "T03:45:00+08:00"
        assert result["source_refs"]["plan"]["file"] == str((later / "plan.json").resolve())
    assert snapshot(evidence.root) == before
