"""Scoped-action stage wiring and durable reuse, with no provider/Engine calls."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import recent_action_refresh as refresh
from trading.research import system_daily_refresh as m
from trading.research.tests.test_system_daily_refresh import D, saved_system, write


def test_ready_action_stage_passes_exact_bundle_to_existing_lowvol_consumer(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    bundle = tmp_path / "action_evidence" / "bundle.json"
    write(bundle, {"synthetic_ready_bundle": True})
    stages, lowvol = [], []

    def ready(head, day, package, operation, **options):
        stages.append((day, package, options))
        return {"status": "ready", "bundle": str(bundle), "network_accessed": False}, str(bundle)

    def consume(head, day, package, operation, **options):
        lowvol.append(options)
        return {"status": "saved_exact_source_plan_reused"}, deepcopy(head["lowvol"])

    monkeypatch.setattr(m, "_recent_actions", ready)
    monkeypatch.setattr(m, "_lowvol", consume)
    result = m.run(saved.directory, D, saved.package[D], action_refresh={"reuse_roots": []})
    assert result["status"] == "completed_saved_research_refresh"
    assert result["action_refresh"]["status"] == "ready"
    assert len(stages) == len(lowvol) == 1 and lowvol[0]["recent_action_extension"] == str(bundle)
    assert stages[0][2]["source_proof"]["day"] == D
    assert result["network_accessed"] is False and saved.openings == [D]


def test_pending_review_blocks_only_lowvol_and_still_refreshes_other_saved_routes(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    pending = {"status": "pending_review", "bundle": None, "network_accessed": False,
        "pending_review": [{"symbol": "600000.SSE", "reason": "official_reference_missing"}]}
    monkeypatch.setattr(m, "_recent_actions", lambda *args, **kwargs: (pending, None))
    monkeypatch.setattr(m, "_lowvol", lambda *args, **kwargs: pytest.fail("Pending review cannot enter lowvol consumer"))
    result = m.run(saved.directory, D, saved.package[D], action_refresh={"reuse_roots": []})
    assert result["status"] == "partial_research_routes"
    assert result["action_refresh"]["pending_review"] == pending["pending_review"]
    assert result["lowvol"]["status"] == "blocked_by_scoped_action_refresh"
    assert result["lowvol"]["consumer_called"] is False and saved.openings == [D]
    assert result["core_producers"] == "saved_exact_source_day_reused_without_reexecution"
    assert result["native_production"]["status"] == "saved_exact_source_day_reused_without_rescoring"
    assert all(result["native_forward_" + key]["status"] == "exact_execution_day_already_committed"
        for key in ("v1", "qualified"))


def test_omitting_action_refresh_keeps_old_path_and_never_calls_producer(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_recent_actions", lambda *args, **kwargs: pytest.fail("No explicit refresh was requested"))
    monkeypatch.setattr(refresh, "run", lambda *args, **kwargs: pytest.fail("No implicit provider or action producer"))
    result = m.run(saved.directory, D, saved.package[D])
    assert result["status"] == "completed_saved_research_refresh" and saved.openings == [D]
    assert "action_refresh" not in result


def test_action_helper_reuses_completed_stage_and_never_restarts_unknown_progress(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    publication = tmp_path / "saved_plan" / "publication.json"
    write(publication, {"synthetic_saved_plan": True})
    saved.head["lowvol"]["publications"] = [str(publication)]
    bundle = tmp_path / "action_evidence" / "bundle.json"
    write(bundle, {"synthetic_ready_bundle": True})
    calls = []

    def produce(day, package, calendar, out, **options):
        calls.append(Path(out))
        request = Path(out) / "request.json"
        manifest = Path(out) / "source" / "manifest.json"
        write(request, {"synthetic_request": True})
        write(manifest, {"synthetic_exact_scope": True})
        value = {"status": "ready", "bundle": str(bundle), "request_file": str(request),
            "preparation": {"manifest_ref": {"path": str(manifest), "sha256": m._ref(manifest)["sha256"]},
                "scope_symbols": ["600000.SSE"]}, "source": {"network_accessed": False}}
        write(Path(out) / "result.json", value)
        return value

    def checked(head, day, package, result_file, source_proof):
        return m._read(result_file), {"refs": [{"path": str(bundle), "sha256": m._ref(bundle)["sha256"]}]}

    monkeypatch.setattr(refresh, "run", produce)
    monkeypatch.setattr(m, "_checked_action_result", checked)
    operation = saved.directory / "operations" / "synthetic_completed"
    options = {"reuse_roots": []}
    proof = {"day": D, "synthetic_complete_close": True}
    first = m._recent_actions(saved.head, D, saved.package[D], operation, options=options, source_proof=proof)
    second = m._recent_actions(saved.head, D, saved.package[D], operation, options=options, source_proof=proof)
    assert first == second and first[0]["status"] == "ready" and len(calls) == 1

    def interrupted(*args, **kwargs):
        calls.append("interrupted_once")
        raise KeyboardInterrupt("synthetic interruption after the durable start")

    monkeypatch.setattr(refresh, "run", interrupted)
    unknown_operation = saved.directory / "operations" / "synthetic_unknown"
    with pytest.raises(KeyboardInterrupt):
        m._recent_actions(saved.head, D, saved.package[D], unknown_operation, options=options, source_proof=proof)
    unresolved, extension = m._recent_actions(saved.head, D, saved.package[D], unknown_operation,
        options=options, source_proof=proof)
    assert "unknown" in unresolved["status"] and extension is None
    assert unresolved["producer_called"] is False and calls == [calls[0], "interrupted_once"]


def test_new_review_reuses_failed_attempt_provider_without_retrying_same_request(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    publication = tmp_path / "saved_plan" / "publication.json"
    write(publication, {"synthetic_saved_plan": True})
    saved.head["lowvol"]["publications"] = [str(publication)]
    bundle = tmp_path / "action_evidence" / "bundle.json"
    write(bundle, {"synthetic_ready_bundle": True})
    operation = saved.directory / "operations" / "new_review_after_failed_producer"
    proof = {"day": D, "synthetic_complete_close": True}
    calls = []

    def produce(day, package, calendar, out, **options):
        out = Path(out)
        provider = out / "source" / "provider"
        manifest = out / "source" / "manifest.json"
        write(manifest, {"provider_root": str(provider)})
        calls.append({"out": out, "provider": provider, "options": options})
        if len(calls) == 1:
            write(provider / "collection_runs" / "failed.json", {"status": "failed",
                "failed_query": {"symbol": "600000.SSE", "task": "actions_2026"}})
            raise RuntimeError("synthetic synchronous provider failure")
        request = out / "request.json"
        write(request, {"synthetic_new_review_request": True})
        value = {"status": "ready", "bundle": str(bundle), "request_file": str(request),
            "preparation": {"manifest_ref": {"path": str(manifest), "sha256": m._ref(manifest)["sha256"]},
                "scope_symbols": ["600000.SSE"]}, "source": {"network_accessed": False}}
        write(out / "result.json", value)
        return value

    def checked(head, day, package, result_file, source_proof):
        return m._read(result_file), {"refs": [{"path": str(bundle), "sha256": m._ref(bundle)["sha256"]}]}

    monkeypatch.setattr(refresh, "run", produce)
    monkeypatch.setattr(m, "_checked_action_result", checked)
    first = m._recent_actions(saved.head, D, saved.package[D], operation,
        options={"reuse_roots": []}, source_proof=proof)
    assert first[0]["status"] == "failed_action_refresh_no_automatic_retry"
    assert first[0]["execution_progress_unknown"] is False and first[1] is None
    repeated = m._recent_actions(saved.head, D, saved.package[D], operation,
        options={"reuse_roots": []}, source_proof=proof)
    assert repeated == first and len(calls) == 1
    failure_file = calls[0]["provider"] / "collection_runs" / "failed.json"
    prior_failure_bytes = failure_file.read_bytes()
    review = tmp_path / "new_review" / "review.json"
    write(review, {"synthetic_new_official_review": True})
    amended = m._recent_actions(saved.head, D, saved.package[D], operation,
        options={"reuse_roots": [], "reviews": [review]}, source_proof=proof)
    assert amended[0]["status"] == "ready" and amended[1] == str(bundle) and len(calls) == 2
    assert str(calls[0]["provider"].resolve()) in calls[1]["options"]["reuse_roots"]
    assert calls[1]["options"]["reviews"] == [review]
    assert calls[0]["out"] != calls[1]["out"]
    assert failure_file.read_bytes() == prior_failure_bytes
