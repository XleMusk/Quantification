"""Small producer-orchestration fixtures; no model fit or market replay.

Existing feature/score/plan contract loaders are tested in their own modules.
Here their explicit substitutes isolate stage ordering, reuse and no-retry.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import native_shortline_daily as daily


DAY = "2026-10-16"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    package = tmp_path / "close"
    package.mkdir()
    daily._atomic(package / "plan.json", {"day": DAY})
    calls, behavior = [], {"scorable": True, "eligible": 3, "score_error": False, "feature_error": False}
    monkeypatch.setattr(daily, "_inputs", lambda *args: {"day": DAY, "synthetic_input": "fixed"})
    class Source:
        def __init__(self, *args, **kwargs):
            calls.append("source")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(daily, "NativeHistoryChainSource", Source)
    def artifact(path, record):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        daily._atomic(path, record)
    def features(source, checkpoint, day, out):
        calls.append("features")
        if behavior["feature_error"]:
            raise ValueError("synthetic known feature failure")
        artifact(Path(out) / "checkpoint.json", {"day": day})
        artifact(Path(out) / "result.json", {"day": day,
            "score_input_published": behavior["scorable"],
            "eligible_stage123_rows": behavior["eligible"],
            "computed_at": daily._now(), "available_at": daily._now(),
            "source_proof": {"source_available_at": daily._now(), "synthetic_source_day": day}})
    monkeypatch.setattr(daily.incremental, "advance_day", features)
    def score(model, panel, out):
        calls.append("score")
        if behavior["score_error"]:
            raise ValueError("synthetic known scoring failure")
        artifact(Path(out) / "result.json", {"panel_result": str(panel)})
        artifact(Path(out) / "publication.json", {"real_receipt_fixture": True})
    monkeypatch.setattr(daily.scoring, "score_saved_generation", score)
    def plan(publication, calendar, out, **options):
        name = "plan_qualified" if options else "plan_v1"
        calls.append(name)
        artifact(Path(out) / "plan.json", {"policy": name})
    monkeypatch.setattr(daily.preopen, "build_preopen_plan", plan)
    def validate_features(path, *args):
        result = daily._read(path)
        checkpoint = Path(path).parent / "checkpoint.json"
        return {"result": str(path), "checkpoint": str(checkpoint),
            "score_input_published": result["score_input_published"],
            "eligible_stage123_rows": result["eligible_stage123_rows"],
            "feature_sha256": "synthetic-feature-hash",
            "refs": [daily._ref(path), daily._ref(checkpoint)]}
    monkeypatch.setattr(daily, "_validate_features", validate_features)
    monkeypatch.setattr(daily, "_validate_score", lambda path, publication, *args:
        {"result": str(path), "publication": str(publication), "published_at": "synthetic_actual_fixture",
         "refs": [daily._ref(path), daily._ref(publication)]})
    monkeypatch.setattr(daily, "_validate_plan", lambda path, policy, *args:
        {"plan": str(path), "policy_id": policy, "source_day": DAY,
         "execution_day": "2026-10-19", "refs": [daily._ref(path)]})
    out = tmp_path / "operation"
    options = {"base_view": tmp_path / "base", "extension_views": [],
        "full_closes": [package], "calendar": tmp_path / "calendar" / "calendar.json",
        "model": tmp_path / "models", "ipo_evidence": tmp_path / "ipo" / "bundle.json",
        "master_run": tmp_path / "master" / "run.json", "out": out}
    return SimpleNamespace(root=tmp_path, options=options, calls=calls,
        behavior=behavior, checkpoint=tmp_path / "old_state" / "checkpoint.json", artifact=artifact)


def invoke(e, **options):
    return daily.run(DAY, e.checkpoint, **(e.options | options))


def test_exact_stage_order_success_reuse_and_changed_saved_artifact_rejected(setup):
    e = setup
    result = invoke(e)
    assert result["status"] == "completed_saved_native_daily_features_scores_two_policies"
    assert e.calls == ["source", "features", "score", "plan_v1", "plan_qualified"]
    assert len(result["stages"]) == 4 and set(result["plans"]) == {"plan_v1", "plan_qualified"}
    assert all(value["status"] == "completed" for value in result["stages"].values())
    before = (e.options["out"] / "receipt.json").read_bytes()
    again = invoke(e)
    assert again["input_id"] == result["input_id"] and e.calls == ["source", "features", "score", "plan_v1", "plan_qualified"]
    assert (e.options["out"] / "receipt.json").read_bytes() == before
    e.artifact(result["plans"]["plan_v1"], {"changed_saved_plan": True})
    with pytest.raises(ValueError, match="saved daily producer input or artifact changed"):
        invoke(e)
    for field in ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit",
            "network_accessed", "engine_advanced", "ledger_mutated", "orders_generated"):
        assert result[field] is False


def test_failed_same_input_keeps_completed_features_but_never_retries_score(setup):
    e = setup
    e.behavior["score_error"] = True
    result = invoke(e)
    assert result["status"] == "stopped_failed_or_unresolved_step_no_retry"
    assert result["stages"]["features"]["status"] == "completed"
    assert result["stages"]["frozen_scores"]["status"] == "failed"
    assert e.calls == ["source", "features", "score"]
    # A recovered environment is not permission to repeat an already failed
    # same-input producer; an explicit new reviewed input is required.
    e.behavior["score_error"] = False
    repeated = invoke(e)
    assert repeated["status"] == "stopped_failed_or_unresolved_step_no_retry"
    assert "failed_same_input_no_retry" in repeated["reason"]
    assert e.calls == ["source", "features", "score"]
    assert not (e.options["out"] / "plan_v1").exists()


@pytest.mark.parametrize("scorable,eligible,reason", [
    (False, 3, "operational_history_unknown"), (True, 0, "no_known_eligible_stage123_events")])
def test_state_without_score_input_never_manufactures_NO_CHANGE_or_empty_target(setup, scorable, eligible, reason):
    e = setup
    e.behavior.update(scorable=scorable, eligible=eligible)
    result = invoke(e)
    assert result["status"] == "saved_state_without_scorable_input_no_plan"
    assert result["reason"] == reason and e.calls == ["source", "features"]
    assert result["no_change_manufactured"] is result["empty_target_manufactured"] is False
    assert Path(result["latest_checkpoint"]).exists() and "plans" not in result
    assert not (e.options["out"] / "scores").exists()


def test_complete_artifact_after_interrupted_started_stage_recovers_without_producer(setup):
    e = setup
    out = e.options["out"]
    out.mkdir()
    artifact = out / "saved.json"
    e.artifact(artifact, {"closed_before_interruption": True})
    receipt = {"stages": {"fixture_stage": {"status": "started"}}}
    called = []
    result = daily._stage(out, receipt, "fixture_stage", artifact,
        lambda: pytest.fail("No producer replay during durable artifact recovery"),
        lambda: called.append("validate") or {"refs": [daily._ref(artifact)]})
    assert called == ["validate"] and result["refs"] == [daily._ref(artifact)]
    assert receipt["stages"]["fixture_stage"]["status"] == "completed"
    assert receipt["stages"]["fixture_stage"]["producer_repeated"] is False
    missing = {"stages": {"missing_stage": {"status": "started"}}}
    with pytest.raises(daily._Stopped, match="unresolved_same_input_no_retry"):
        daily._stage(out, missing, "missing_stage", out / "absent.json",
            lambda: pytest.fail("Unknown progress cannot authorize a retry"), lambda: {})
    corrupt = {"stages": {"corrupt_stage": {"status": "started"}}}
    def invalid():
        raise ValueError("synthetic saved artifact corruption")
    with pytest.raises(daily._Stopped, match="saved artifact corruption"):
        daily._stage(out, corrupt, "corrupt_stage", artifact,
            lambda: pytest.fail("Never re-produce corrupt existing work"), invalid)
    assert corrupt["stages"]["corrupt_stage"]["status"] == "failed"
    with pytest.raises(daily._Stopped, match="failed_same_input_no_retry"):
        daily._stage(out, corrupt, "corrupt_stage", artifact,
            lambda: pytest.fail("No producer retry"), lambda: pytest.fail("No repeated failed validation"))


def test_explicit_existing_day_artifacts_are_adopted_without_any_producer(setup):
    e = setup
    prior = e.root / "existing_saved"
    feature_path = prior / "features" / "result.json"
    e.artifact(feature_path, {"score_input_published": True, "eligible_stage123_rows": 4})
    e.artifact(feature_path.parent / "checkpoint.json", {"day": DAY})
    score = prior / "scores" / "result.json"
    publication = prior / "scores" / "publication.json"
    e.artifact(score, {"saved_score": True})
    e.artifact(publication, {"saved_actual_publication": True})
    plans = {name: prior / name / "plan.json" for name in ("plan_v1", "plan_qualified")}
    for name, path in plans.items():
        e.artifact(path, {"policy": name})
    result = invoke(e, reuse={"features_result": feature_path,
        "score_result": score, "score_publication": publication, **plans})
    assert result["status"] == "completed_saved_native_daily_features_scores_two_policies"
    assert e.calls == []
    assert all(stage["adopted_existing"] and not stage["producer_called"]
        for stage in result["stages"].values())


def test_profile_appends_policies_and_raw_prefix_without_running_forward(setup, monkeypatch):
    e = setup
    checkpoint = e.root / "profile_state" / "checkpoint.json"
    e.artifact(checkpoint, {"day": "2026-10-15"})
    templates = {kind: {"argv": ["python", "run_committee.py", "shortline-native-preopen-forward",
        "--plan", "old_" + kind, "--raw-close", "old_close", "--through", "2026-10-15",
        "--result-json", "old_result"], "plan_prefix": ["old_" + kind],
        "raw_close_prefix": ["old_close"], "run_dir": "fixed_" + kind}
        for kind in ("v1", "qualified")}
    templates["qualified"]["argv"].append("--ipo-append-only")
    profile = {"schema": "native_shortline_daily_profile_v1", "method_id": daily.METHOD_ID,
        "source_day": "2026-10-15", "checkpoint": str(checkpoint), "checkpoint_ref": daily._ref(checkpoint),
        "base_view": "base", "extension_views": [], "full_closes": ["old_close"],
        "calendar": "calendar", "model": "model", "ipo_evidence": "ipo", "master_run": "master",
        "forward_templates": templates, "missing_plan_days": [], "chain_has_missing_plan": False,
        **daily.FLAGS}
    next_checkpoint = e.root / "new_state" / "checkpoint.json"
    e.artifact(next_checkpoint, {"day": DAY})
    result = {"status": "completed_saved_native_daily_features_scores_two_policies",
        "latest_checkpoint": str(next_checkpoint), "operation": "saved_operation",
        "plans": {"plan_v1": "new_v1", "plan_qualified": "new_qualified"},
        "score_publication": "new_real_publication"}
    monkeypatch.setattr(daily, "run", lambda *args, **kwargs: deepcopy(result))
    original = deepcopy(profile)
    value = daily.run_profile(profile, DAY, e.options["full_closes"][0], e.options["out"])
    updated = value["next_profile"]
    assert profile == original and updated["source_day"] == DAY
    for kind, template in updated["forward_templates"].items():
        assert template["plan_prefix"] == ["old_" + kind, "new_" + kind]
        assert len(template["raw_close_prefix"]) == 2
        assert daily._values(template["argv"], "--plan") == template["plan_prefix"]
        assert daily._one(template["argv"], "--through") == DAY
        assert daily._one(template["argv"], "--result-json") == "old_result"
        assert template["run_dir"] == "fixed_" + kind
    assert "--ipo-append-only" in updated["forward_templates"]["qualified"]["argv"]


def source_only_setup(e, monkeypatch, clock="2026-10-19T10:01:00+08:00"):
    e.artifact(e.options["calendar"], {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    monkeypatch.setattr(daily, "_now", lambda: clock)


def test_source_only_preserves_actual_clocks_and_gap_without_any_score_or_plan_and_reuses_exact_success(setup, monkeypatch):
    e = setup
    source_only_setup(e, monkeypatch)
    result = invoke(e, source_only=True)
    assert result["status"] == daily.SOURCE_ONLY_STATUS
    assert result["source_only"] is True and result["inputs"]["source_only"] is True
    assert result["missing_plan_day"] == DAY
    assert result["publication_gap"]["missed_execution_day"] == "2026-10-19"
    assert result["publication_gap"]["publication_deadline"] == "2026-10-19T09:30:00+08:00"
    assert result["reason"] == "original_next_open_publication_window_elapsed"
    assert result["source_proof"]["source_available_at"] == result["feature_computed_at"] == result["feature_available_at"] == daily._now()
    assert result["signals_published"] is result["score_publication_created"] is result["plan_publication_created"] is False
    assert e.calls == ["source", "features"] and set(result["stages"]) == {"features"}
    assert "plans" not in result and "score_publication" not in result
    assert not (e.options["out"] / "scores").exists()
    before = (e.options["out"] / "receipt.json").read_bytes()
    assert invoke(e, source_only=True)["input_id"] == result["input_id"]
    assert (e.options["out"] / "receipt.json").read_bytes() == before
    assert e.calls == ["source", "features"]
    with pytest.raises(ValueError, match="cannot replace its original"):
        invoke(e)


def test_default_input_hash_remains_original_and_cannot_be_changed_to_source_only(setup, monkeypatch):
    e = setup
    source_only_setup(e, monkeypatch)
    result = invoke(e)
    original_inputs = {"day": DAY, "synthetic_input": "fixed"}
    expected = daily.sha256(daily.json.dumps(original_inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert result["inputs"] == original_inputs and result["input_id"] == expected
    assert "source_only" not in result
    with pytest.raises(ValueError, match="cannot replace its original"):
        invoke(e, source_only=True)
    assert e.calls == ["source", "features", "score", "plan_v1", "plan_qualified"]


@pytest.mark.parametrize("clock", ["2026-10-16T16:00:00+08:00", "2026-10-17T12:00:00+08:00", "2026-10-19T09:29:59+08:00"])
def test_source_only_cannot_skip_a_still_available_original_publication_window(setup, monkeypatch, clock):
    e = setup
    source_only_setup(e, monkeypatch, clock)
    with pytest.raises(ValueError, match="window to have elapsed"):
        invoke(e, source_only=True)
    assert not e.calls and not e.options["out"].exists()


def test_source_only_requires_exact_calendar_and_accepts_only_features_reuse(setup, monkeypatch):
    e = setup
    source_only_setup(e, monkeypatch)
    for reuse in ({"score_result": "score", "score_publication": "publication"}, {"plan_v1": "plan"}):
        with pytest.raises(ValueError, match="reuse only"):
            invoke(e, source_only=True, reuse=reuse)
    e.artifact(e.options["calendar"], {"schema": "invalid"})
    with pytest.raises(ValueError, match="pinned full-year"):
        invoke(e, source_only=True)
    assert not e.calls


def test_source_only_feature_failure_never_retries_or_falls_through_to_scoring(setup, monkeypatch):
    e = setup
    source_only_setup(e, monkeypatch)
    e.behavior["feature_error"] = True
    result = invoke(e, source_only=True)
    assert result["status"] == "stopped_failed_or_unresolved_step_no_retry"
    e.behavior["feature_error"] = False
    again = invoke(e, source_only=True)
    assert again["status"] == result["status"] and "failed_same_input_no_retry" in again["reason"]
    assert e.calls == ["source", "features"] and "latest_checkpoint" not in again


def test_source_only_reuses_complete_features_and_retains_input_directory_protection(setup, monkeypatch):
    e = setup
    source_only_setup(e, monkeypatch)
    feature = e.root / "saved_features" / "result.json"
    e.artifact(feature, {"score_input_published": True, "eligible_stage123_rows": 4,
        "computed_at": daily._now(), "available_at": daily._now(), "source_proof": {"actual_source": True}})
    e.artifact(feature.parent / "checkpoint.json", {"day": DAY})
    with pytest.raises(ValueError, match="separate from all saved inputs"):
        invoke(e, source_only=True, out=feature.parent / "nested_operation", reuse={"features_result": feature})
    result = invoke(e, source_only=True, reuse={"features_result": feature})
    assert result["status"] == daily.SOURCE_ONLY_STATUS and not e.calls
    assert result["stages"]["features"]["adopted_existing"] is True


def test_source_only_profile_keeps_original_publications_and_forward_prefix_then_later_producer_retains_gap(setup, monkeypatch):
    e = setup
    checkpoint = e.root / "profile_state" / "checkpoint.json"
    e.artifact(checkpoint, {"day": "2026-10-15"})
    templates = {key: {"argv": ["shortline-native-preopen-forward", "--plan", "original_" + key,
        "--raw-close", "old_close", "--through", "2026-10-15", "--result-json", "original_result"],
        "plan_prefix": ["original_" + key], "raw_close_prefix": ["old_close"], "run_dir": "original_" + key}
        for key in ("v1", "qualified")}
    profile = {"schema": "native_shortline_daily_profile_v1", "method_id": daily.METHOD_ID,
        "source_day": "2026-10-15", "checkpoint": str(checkpoint), "checkpoint_ref": daily._ref(checkpoint),
        "base_view": "base", "extension_views": [], "full_closes": ["old_close"], "calendar": "calendar",
        "model": "model", "ipo_evidence": "ipo", "master_run": "master", "forward_templates": templates,
        "latest_plans": {"v1": "original_v1", "qualified": "original_qualified"}, "score_publication": "original_score",
        "missing_plan_days": [], "chain_has_missing_plan": False, **daily.FLAGS}
    next_checkpoint = e.root / "new_state" / "checkpoint.json"
    e.artifact(next_checkpoint, {"day": DAY})
    result = {"status": daily.SOURCE_ONLY_STATUS, "latest_checkpoint": str(next_checkpoint), "operation": "saved_gap_operation"}
    calls = []
    monkeypatch.setattr(daily, "run", lambda *a, **k: calls.append(k) or deepcopy(result))
    original = deepcopy(profile)
    updated = daily.run_profile(profile, DAY, e.options["full_closes"][0], e.options["out"], source_only=True)["next_profile"]
    assert profile == original and calls[0]["source_only"] is True
    assert updated["source_day"] == DAY and len(updated["full_closes"]) == 2
    assert updated["missing_plan_days"] == [DAY] and updated["chain_has_missing_plan"] is True
    for name in ("latest_plans", "score_publication", "forward_templates"):
        assert updated[name] == original[name]
    result.update(status="completed_saved_native_daily_features_scores_two_policies",
        plans={"plan_v1": "real_next_v1", "plan_qualified": "real_next_qualified"}, score_publication="real_next_score")
    later = daily.run_profile(updated, "2026-10-19", e.root / "next_close", e.root / "next_operation")["next_profile"]
    assert "source_only" not in calls[1]
    assert later["missing_plan_days"] == [DAY] and later["chain_has_missing_plan"] is True
    assert later["latest_plans"]["v1"] == "real_next_v1"
    assert str(e.options["full_closes"][0].resolve()) not in later["forward_templates"]["v1"]["raw_close_prefix"]


def recovery_profile(e):
    e.artifact(e.checkpoint, {"day": "2026-10-15", "original_parent": True})
    return {"schema": "native_shortline_daily_profile_v1", "method_id": daily.METHOD_ID,
        "source_day": "2026-10-15", "checkpoint": str(e.checkpoint), "checkpoint_ref": daily._ref(e.checkpoint),
        "base_view": str(e.options["base_view"]), "extension_views": [], "full_closes": [],
        "calendar": str(e.options["calendar"]), "model": str(e.options["model"]),
        "ipo_evidence": str(e.options["ipo_evidence"]), "master_run": str(e.options["master_run"]),
        "missing_plan_days": [], "chain_has_missing_plan": False,
        "latest_plans": {"v1": "old_v1", "qualified": "old_qualified"}, "score_publication": "old_score",
        "forward_templates": {key: {"argv": ["shortline-native-preopen-forward", "--plan", "old_" + key,
            "--raw-close", "old_close", "--through", "2026-10-15"], "plan_prefix": ["old_" + key],
            "raw_close_prefix": ["old_close"]} for key in ("v1", "qualified")}, **daily.FLAGS}


@pytest.mark.parametrize("source_only", [True, False])
def test_saved_profile_recovery_adopts_complete_exact_child_without_any_producer_or_file_change(setup, monkeypatch, source_only):
    e = setup
    source_only_setup(e, monkeypatch)
    profile = recovery_profile(e)
    child_result = invoke(e, source_only=source_only)
    expected = daily._continued_profile(profile, DAY, e.options["full_closes"][0], child_result)
    snapshot = {path: path.read_bytes() for path in e.options["out"].rglob("*") if path.is_file()}
    calls = list(e.calls)
    def forbidden(*args, **kwargs):
        pytest.fail("Saved profile recovery is strictly read-only")
    for name in ("run", "run_profile", "_stage", "NativeHistoryChainSource"):
        monkeypatch.setattr(daily, name, forbidden)
    monkeypatch.setattr(daily.incremental, "advance_day", forbidden)
    monkeypatch.setattr(daily.scoring, "score_saved_generation", forbidden)
    monkeypatch.setattr(daily.preopen, "build_preopen_plan", forbidden)
    recovered = daily.recover_saved_profile(profile, DAY, e.options["full_closes"][0], e.options["out"], source_only=source_only)
    assert recovered == {"result": child_result, "next_profile": expected}
    assert e.calls == calls and all(path.read_bytes() == data for path, data in snapshot.items())


@pytest.mark.parametrize("damage", ["partial", "pending_stage", "missing_artifact", "changed_artifact", "parent_changed", "wrong_mode", "gap_changed"])
def test_saved_profile_recovery_rejects_partial_changed_or_wrong_bound_child_without_retry(setup, monkeypatch, damage):
    e = setup
    source_only_setup(e, monkeypatch)
    profile = recovery_profile(e)
    invoke(e, source_only=True)
    receipt_file = e.options["out"] / "receipt.json"
    receipt = daily._read(receipt_file)
    if damage == "partial":
        receipt["status"] = "stopped_failed_or_unresolved_step_no_retry"
    elif damage == "pending_stage":
        receipt["stages"]["features"]["status"] = "started"
        receipt["stages"]["features"]["execution_progress_unknown"] = True
    elif damage == "missing_artifact":
        (e.options["out"] / "features/result.json").unlink()
    elif damage == "changed_artifact":
        checkpoint = e.options["out"] / "features/checkpoint.json"
        checkpoint.write_bytes(checkpoint.read_bytes() + b" ")
    elif damage == "parent_changed":
        e.checkpoint.write_bytes(e.checkpoint.read_bytes() + b" ")
    elif damage == "gap_changed":
        receipt["publication_gap"]["missed_execution_day"] = "2026-10-20"
    daily._atomic(receipt_file, receipt)
    original = receipt_file.read_bytes()
    calls = list(e.calls)
    for name in ("run", "run_profile", "_stage"):
        monkeypatch.setattr(daily, name, lambda *a, **k: pytest.fail("Recovery cannot retry"))
    with pytest.raises((ValueError, OSError)):
        daily.recover_saved_profile(profile, DAY, e.options["full_closes"][0], e.options["out"], source_only=damage != "wrong_mode")
    assert receipt_file.read_bytes() == original and e.calls == calls


def test_saved_profile_recovery_validates_both_exact_publication_plan_contracts(setup, monkeypatch):
    e = setup
    profile = recovery_profile(e)
    invoke(e)
    plan = e.options["out"] / "plan_qualified/plan.json"
    plan.write_bytes(plan.read_bytes() + b" ")
    calls = list(e.calls)
    monkeypatch.setattr(daily, "run", lambda *a, **k: pytest.fail("No producer retry"))
    with pytest.raises(ValueError, match="artifact changed"):
        daily.recover_saved_profile(profile, DAY, e.options["full_closes"][0], e.options["out"], source_only=False)
    assert e.calls == calls


def test_saved_profile_recovery_adopts_original_default_no_scorable_plan_success(setup, monkeypatch):
    e = setup
    profile = recovery_profile(e)
    e.behavior["scorable"] = False
    child_result = invoke(e)
    assert child_result["status"] == "saved_state_without_scorable_input_no_plan"
    calls = list(e.calls)
    for name in ("run", "run_profile", "_stage"):
        monkeypatch.setattr(daily, name, lambda *a, **k: pytest.fail("Saved success cannot re-produce"))
    recovered = daily.recover_saved_profile(profile, DAY, e.options["full_closes"][0], e.options["out"], source_only=False)
    assert recovered["result"] == child_result
    assert recovered["next_profile"]["source_day"] == DAY
    assert recovered["next_profile"]["missing_plan_days"] == [DAY]
    assert recovered["next_profile"]["chain_has_missing_plan"] is True
    assert recovered["next_profile"]["forward_templates"] == profile["forward_templates"]
    assert e.calls == calls
