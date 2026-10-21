"""Two saved native policies remain separate through the read-only API."""
from copy import deepcopy
import csv
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading import api, engine, ledger
from trading.research import shortline_frozen_model as frozen
from trading.research import shortline_native_preopen as policy
from trading.research import shortline_native_preopen_forward as forward
from trading.research import shortline_native_preopen_status as status
from trading.research import shortline_native_scoring as scoring
from trading.research import shortline_native_training as training
from trading.research.tests import test_ipo_listing_supplement as ipo_fixture
from trading.tests.test_native_preopen_status import (evidence as legacy_evidence,
    reference, snapshot, write)


D, E = "2026-10-15", "2026-10-16"
NOW = datetime.fromisoformat(E + "T20:00:00+08:00")
RAW_UNKNOWN = "301569.SZSE"
ORIGINAL_STATUS = status.native_preopen_status


def _paths(root, workspace, plan, *, qualified=False):
    name = "qualified" if qualified else "baseline"
    return {"plan_file": plan, "run_dir": root / (name + "_run"),
        "observation_file": workspace / (name + "_observation.json"),
        "result_file": workspace / (name + "_result.json")}


def _install_read_sentinels(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("A comparison GET cannot start accounting, build a plan, score or fit")
    for module, name in ((forward, "run"), (forward, "new_state"), (forward, "advance"),
            (forward, "Ledger"), (engine, "advance"), (ledger, "Ledger"), (ledger, "new_state"),
            (policy, "build_preopen_plan"), (policy, "bind_preopen_plan"),
            (scoring, "score_saved_generation"), (scoring, "score_day"), (scoring, "load_generation"),
            (training, "train_and_score"), (frozen, "save_generation")):
        monkeypatch.setattr(module, name, fail)


def _api(monkeypatch, root, workspace, paths):
    read_comparison = status.native_preopen_comparison
    monkeypatch.setattr(api.settings, "DATA_ROOT", root)
    monkeypatch.setattr(api.settings, "ROOT", workspace)
    monkeypatch.setattr(status, "native_preopen_comparison", lambda data_root, work:
        read_comparison(data_root, work, policy_paths=paths, now=NOW))


@pytest.fixture
def comparison_evidence(tmp_path, monkeypatch):
    root, workspace = tmp_path / "data", tmp_path / "workspace"
    root.mkdir(); workspace.mkdir()
    ipo_dir = tmp_path / "ipo"
    ipo_dir.mkdir()
    # Real saved PDF/receipt/bundle loading, not a replacement qualification loader.
    listing = ipo_fixture.bundle.__wrapped__(ipo_dir)
    model = tmp_path / "frozen_manifest.json"
    write(model, {"feature_contract_id": policy.FEATURE_CONTRACT_ID,
        "feature_semantics": {"scope": "synthetic native observations"}, "no_model_fitted": True})
    features = tmp_path / "latest_features.csv"
    features.write_text("saved source identity; inference is not run by this fixture\n", encoding="utf8")
    score_root = tmp_path / "saved_scores"
    score_root.mkdir()
    scope = {"stage_membership_complete": False, "unknown_stage_symbols": [RAW_UNKNOWN],
        "source_rows": 35, "ordinary_rows": 35, "valid_scope_rows": 35,
        "full_market_stage_membership_complete": False, "stage123_event_count": 34}
    source = {"day": D, "available_at": D + "T18:00:00+08:00",
        "feature_contract_id": policy.FEATURE_CONTRACT_ID, "feature_semantics": {"scope": "synthetic native observations"},
        "source_scope": scope, "stage_membership_complete": False,
        "unknown_stage_symbols": [RAW_UNKNOWN], "known_stage_partial_research_only": True,
        "source_file": str(features), "source_sha256": reference(features)["sha256"]}
    batches = []
    stages = [1] * 30 + [2] * 2 + [3] * 2
    for kind in ("lr", "lgbm"):
        score_file, proof_file = score_root / (kind + ".csv"), score_root / (kind + ".json")
        rows = []
        for i, stage in enumerate(stages, 1):
            symbol = f"600{i:03d}.SSE"
            rows.append({"event_id": f"{symbol}|{D}|s{stage}", "symbol": symbol, "date": D,
                "stage": stage, "model_hit5_probability": .1 + (i if kind == "lr" else 35 - i) / 1000,
                "paper_candidate_only": False})
        with score_file.open("w", encoding="utf8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=policy.CSV_COLUMNS)
            writer.writeheader(); writer.writerows(rows)
        proof = {"schema": "shortline_frozen_single_date_score_v1", "day": D, "model_kind": kind,
            "status": "partial_known_stage_frozen_single_date_probabilities_only",
            "scored_at": D + "T22:00:00+08:00", "score_available_at": D + "T22:00:00+08:00",
            "model_available_at": D + "T21:00:00+08:00", "model_refit": False,
            "score_rows": 34, "stage_counts": {"1": 30, "2": 2, "3": 2},
            "stage_membership_complete": False, "known_stage_partial_research_only": True,
            "unknown_stage_symbols": [RAW_UNKNOWN], "batch_complete_for_target": False,
            "score_only_not_candidate_batch": True, "target_generation_allowed": False,
            "feature_source": deepcopy(source), "feature_contract_id": policy.FEATURE_CONTRACT_ID,
            "feature_semantics": {"scope": "synthetic native observations"}, "generation_id": "saved_generation",
            "manifest_file": str(model), "manifest_sha256": reference(model)["sha256"], **scoring.FLAGS}
        write(proof_file, proof)
        batches.append({"model_kind": kind, "day": D, "rows": 34, "score_file": str(score_file),
            "provenance_file": str(proof_file), "stage_counts": proof["stage_counts"],
            "model_refit": False, "paper_candidate_flags_true": 0, "target_generation_allowed": False})
    batch = score_root / "result.json"
    write(batch, {"schema": "native_shortline_frozen_score_batch_v1", "status": "completed_frozen_probabilities_only",
        "started_at": D + "T21:00:00+08:00", "completed_at": D + "T22:00:00+08:00",
        "score_batches": batches, "source_scope": scope, "model_refit": False,
        "target_generation_allowed": False, **scoring.FLAGS})
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            actual = datetime.fromisoformat(E + "T03:00:00+08:00")
            return actual.astimezone(tz) if tz is not None else actual
    monkeypatch.setattr(scoring, "datetime", Clock)
    publication = tmp_path / "raw_publication.json"
    scoring.publish_saved_score_batch(batch, publication)
    baseline, qualified = root / "baseline_plan", root / "qualified_plan"
    monkeypatch.setattr(policy, "_now", lambda: datetime.fromisoformat(E + "T03:30:00+08:00"))
    policy.build_preopen_plan(publication, listing.calendar, baseline)
    monkeypatch.setattr(policy, "_now", lambda: datetime.fromisoformat(E + "T04:17:00+08:00"))
    policy.build_preopen_plan(publication, listing.calendar, qualified,
        ipo_evidence=listing.path, research_master_run=listing.master)
    policy.load_preopen_plan(qualified)  # Actual canonical validation before read-only sentinels.
    paths = {policy.POLICY_ID: _paths(root, workspace, baseline / "plan.json"),
        policy.QUALIFIED_POLICY_ID: _paths(root, workspace, qualified / "plan.json", qualified=True)}
    write(paths[policy.POLICY_ID]["observation_file"], {
        "schema": "native_preopen_forward_cli_observation_v1", "observed_before_utc": "2026-10-16 02:00:00 UTC",
        "provided_source_days": [D], "planned_execution_day": E,
        "post_call_filesystem": {"head_exists": False},
        "summary": {"schema": forward.RESULT_SCHEMA, "status": "waiting_for_exact_complete_raw_close",
            "checkpoint": None, "case_count": 0, "metrics": None, "execution_admitted": False}})
    loader = policy.load_preopen_plan
    calls = []
    def observed_loader(path):
        calls.append(Path(path).resolve())
        return loader(path)
    monkeypatch.setattr(status, "load_preopen_plan", observed_loader)
    _install_read_sentinels(monkeypatch)
    _api(monkeypatch, root, workspace, paths)
    return SimpleNamespace(root=tmp_path, paths=paths, baseline=baseline, qualified=qualified,
        calls=calls, scope=scope)


def _views(response):
    assert response["policy_count"] == 2
    assert [item["policy_id"] for item in response["policies"]] == [policy.POLICY_ID, policy.QUALIFIED_POLICY_ID]
    return [item["view"] for item in response["policies"]]


def test_canonical_five_payload_qualified_plan_is_separate_from_same_publication_baseline(comparison_evidence):
    e = comparison_evidence
    before = snapshot(e.root)
    baseline, qualified = _views(api.native_preopen_comparison())
    assert baseline["status"] == "waiting_for_execution_source"
    assert all(m["decision"] == "NO_CHANGE" and m["weights"] is None for m in baseline["models"])
    assert qualified["status"] == "saved_preopen_plan_only", qualified.get("error")
    assert qualified["observation"] is None  # It has not run; never borrow v1's waiting call.
    assert baseline["plan_published_at"] == E + "T03:30:00+08:00"
    assert qualified["plan_published_at"] == E + "T04:17:00+08:00"
    assert baseline["observation"]["observed_at"] == E + "T10:00:00+08:00"
    assert baseline["read_at"] == qualified["read_at"] == NOW.isoformat()
    for model in qualified["models"]:
        assert model["decision"] == "FULL_TARGET" and len(model["weights"]) == 3
        assert list(model["weights"].values()) == [.16, .16, .16]
        assert model["target_cash_weight"] == .52
    scope = qualified["stage_scope"]
    assert scope["raw_unknown_stage_symbols"] == scope["excluded_no_limit_symbols"] == [RAW_UNKNOWN]
    assert scope["remaining_unknown_stage_symbols"] == scope["scored_no_limit_conflicts"] == []
    assert scope["qualified_stage_membership_complete"] is True
    for branch in (baseline, qualified):
        assert branch["account_count"] == 0 and branch["accounts"] == []
        assert branch["cash_cents"] is branch["equity_cents"] is branch["return_metrics"] is None
        assert branch["runner_called"] is branch["model_scored"] is branch["orders_generated"] is False
    assert (e.qualified / "plan.json").resolve() in e.calls
    assert snapshot(e.root) == before


@pytest.mark.parametrize("fault", ["missing", "canonical_scope_tamper", "wrong_run_result"])
def test_qualified_fault_does_not_fallback_borrow_or_remove_valid_baseline(comparison_evidence, fault):
    e = comparison_evidence
    if fault == "missing":
        (e.qualified / "plan.json").unlink()
    elif fault == "canonical_scope_tamper":
        scope_file, plan_file = e.qualified / "qualification_scope.json", e.qualified / "plan.json"
        projection = json.loads(scope_file.read_text(encoding="utf8"))
        projection["excluded_no_limit_symbols"] = []
        write(scope_file, projection)
        plan = json.loads(plan_file.read_text(encoding="utf8"))
        updated = reference(scope_file)
        binding = next(row for row in plan["files"] if row["path"] == "qualification_scope.json")
        binding.update(sha256=updated["sha256"], byte_count=updated["byte_count"])
        write(plan_file, plan)
    else:
        write(e.paths[policy.QUALIFIED_POLICY_ID]["result_file"], {"schema": forward.RESULT_SCHEMA,
            "status": "waiting_for_exact_complete_raw_close", "read_at": E + "T19:00:00+08:00",
            "run_directory": str(e.paths[policy.POLICY_ID]["run_dir"]), "checkpoint": None,
            "case_count": 0, "metrics": None, **forward.FLAGS})
    before = snapshot(e.root)
    baseline, qualified = _views(api.native_preopen_comparison())
    assert baseline["status"] == "waiting_for_execution_source"
    assert len(baseline["models"]) == 2 and baseline["observation"]["missing_source_days"] == [E]
    assert qualified["status"] == ("unavailable" if fault == "missing" else "unknown_saved_evidence")
    assert qualified["accounts"] == [] and qualified["cash_cents"] is qualified["return_metrics"] is None
    assert qualified["fallback_to_older_success"] is False
    assert snapshot(e.root) == before


def test_legacy_head_keeps_saved_accounting_when_qualified_evidence_is_missing(legacy_evidence, monkeypatch):
    e = legacy_evidence
    e.save_head()
    # The old fixture pins a single-status clock; the new comparison owns its
    # own explicit read clock and calls the actual per-policy reader.
    monkeypatch.setattr(status, "native_preopen_status", ORIGINAL_STATUS)
    paths = {policy.POLICY_ID: {"plan_file": e.plan, "run_dir": e.run,
        "observation_file": e.observation, "result_file": e.result},
        policy.QUALIFIED_POLICY_ID: {"plan_file": e.root / "missing/plan.json", "run_dir": e.root / "different_run",
            "observation_file": e.root / "missing_observation.json", "result_file": e.root / "missing_result.json"}}
    _api(monkeypatch, e.data, e.workspace, paths)
    before = snapshot(e.root)
    baseline, qualified = _views(api.native_preopen_comparison())
    assert baseline["status"] == "active_research_books" and baseline["account_count"] == 2
    assert qualified["status"] == "unavailable" and qualified["accounts"] == []
    for account in baseline["accounts"]:
        assert account["cash_cents"] == 9_949_400 and account["equity_cents"] == 10_004_400
        assert account["order_count"] == 1 and account["fill_count"] == 2
        assert account["orders"]["buy-1"]["status"] == "partial"
        assert account["blocked_order_count"] == 1
    assert snapshot(e.root) == before
