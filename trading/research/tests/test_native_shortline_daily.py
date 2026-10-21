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
    calls, behavior = [], {"scorable": True, "eligible": 3, "score_error": False}
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
        artifact(Path(out) / "checkpoint.json", {"day": day})
        artifact(Path(out) / "result.json", {"day": day,
            "score_input_published": behavior["scorable"],
            "eligible_stage123_rows": behavior["eligible"]})
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
