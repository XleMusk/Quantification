from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research import post_close_d0_d1 as orchestrator


CLOSE = "2026-10-26"


def _inputs(tmp_path: Path):
    source = tmp_path / "same_day_sources.json"
    source.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    fixed_dir = tmp_path / "fixed_daily"
    fixed_dir.mkdir()
    package = tmp_path / "complete_close_package"
    package.mkdir()
    (package / "plan.json").write_text(json.dumps({"day": CLOSE}), encoding="utf-8")
    (package / "receipt.json").write_text(json.dumps({"day": CLOSE}), encoding="utf-8")
    stockdb = tmp_path / "stockdb"
    stockdb.mkdir()
    return source, fixed_dir, package, stockdb


def test_post_close_advances_fixed_daily_before_d0_d1(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    calls: list[str] = []

    def fixed_run(*args, **kwargs):
        calls.append("fixed")
        assert args[0] == fixed_dir
        assert args[1] == CLOSE
        assert kwargs["packages"] == [package]
        return {"status": "continued_research_only", "last_completed_day": CLOSE,
                "execution_admitted": False}

    def d0_run(*args, **kwargs):
        calls.append("d0")
        assert kwargs["fixed_run_dir"] == fixed_dir
        assert kwargs["joint_capacity_policy"] == "budgeted"
        return {"status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
                "execution_admitted": False}

    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", fixed_run)
    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract", d0_run)
    receipt = orchestrator.run_pipeline(
        source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0, stockdb_root=stockdb)
    assert calls == ["fixed", "d0"]
    assert receipt["status"] == orchestrator.STATUS
    assert receipt["execution_admitted"] is False
    assert receipt["persistent_fixed_daily_account_scope"] == "persistent_registered_account"
    assert receipt["target_preview_projection_scope"]["fixed"] == "TARGET_PREVIEW"
    assert receipt["target_preview_projection_scope"]["candidate"] == "TARGET_PREVIEW"
    assert receipt["target_preview_projection_scope"]["continuous_account_consumer"] is False
    assert [stage["stage"] for stage in receipt["stage_trace"]] == [
        "inputs", "fixed_daily", "candidate_inputs", "d0_d1"
    ]
    assert [stage["stage"] for stage in receipt["pipeline_stage_trace"]] == [
        "Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position"
    ]
    assert receipt["pipeline_stage_trace"][4]["status"] == "NOT_ATTEMPTED_RESEARCH_ONLY"
    assert receipt["pipeline_stage_trace"][5]["broker_fill"] is False
    assert receipt["pipeline_stage_trace"][6]["persistent_ledger_mutated"] is False
    assert (tmp_path / "run" / "post_close_receipt.json").is_file()
    assert receipt["timing"]["elapsed_seconds"] >= 0
    assert receipt["timing"]["finished_at"].endswith("+00:00")


def test_post_close_resolves_execution_and_settlement_from_calendar(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({
        "schema": "cn_a_share_exchange_calendar_v1", "calendar_id": "fixture_2026",
        "year": 2026, "closed_ranges": [], "open_sessions": [],
    }), encoding="utf-8")

    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "continued_research_only", "last_completed_day": CLOSE,
        "execution_admitted": False,
    })
    observed: dict[str, str] = {}

    def d0_run(*args, **kwargs):
        observed["execution_day"] = kwargs["execution_day"]
        observed["settlement_day"] = kwargs["settlement_day"]
        return {"status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
                "execution_admitted": False}

    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract", d0_run)
    receipt = orchestrator.run_pipeline(
        source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        close_date=CLOSE, calendar_path=calendar,
        initial_cash=100000.0, stockdb_root=stockdb)

    assert receipt["status"] == orchestrator.STATUS
    assert observed == {"execution_day": "2026-10-27", "settlement_day": "2026-10-28"}
    assert receipt["session_resolution"]["mode"] == "pinned_calendar"


def test_post_close_waits_for_complete_fixed_source_without_d0_projection(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "waiting_for_complete_sources", "last_completed_day": "2026-10-23",
        "missing_days": [CLOSE], "execution_admitted": False,
    })
    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract",
                        lambda *args, **kwargs: pytest.fail("D0/D1 must wait for fixed sources"))
    receipt = orchestrator.run_pipeline(
        source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0, stockdb_root=stockdb)
    assert receipt["status"] == orchestrator.WAITING_STATUS
    assert receipt["missing_days"] == [CLOSE]
    assert receipt["execution_admitted"] is False
    assert receipt["stage_trace"][-1]["stage"] == "fixed_daily"
    assert receipt["pipeline_stage_trace"][0]["status"] == "BOUND_CLOSE_SOURCES"
    assert receipt["pipeline_stage_trace"][2]["status"] == "NOT_CREATED"


def test_post_close_propagates_d0_block_and_snapshot_coverage(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "continued_research_only", "last_completed_day": CLOSE,
        "execution_admitted": False,
    })
    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract", lambda *args, **kwargs: {
        "status": orchestrator.next_close_d0_d1.BLOCKED_STATUS,
        "reason": "Missing same-day StockDB rows; no price was carried forward",
        "execution_admitted": False,
        "snapshot_coverage": {"status": "STALE_SNAPSHOT", "requested_close_date": CLOSE},
    })
    receipt = orchestrator.run_pipeline(
        source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0, stockdb_root=stockdb)
    assert receipt["status"] == orchestrator.BLOCKED_D0_STATUS
    assert receipt["reason"] == "Missing same-day StockDB rows; no price was carried forward"
    assert receipt["snapshot_coverage"]["status"] == "STALE_SNAPSHOT"
    assert receipt["execution_admitted"] is False
    assert receipt["pipeline_stage_trace"][2]["status"] == "BLOCKED"


def test_post_close_builds_candidate_targets_from_replay_manifest(tmp_path: Path, monkeypatch) -> None:
    _, fixed_dir, package, stockdb = _inputs(tmp_path)
    replay = tmp_path / "candidate_replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "candidate_close", "end_date": CLOSE,
        "execution_admitted": False, "strategy_validated": False,
        "selected_events": [{"date": CLOSE, "symbol": "000001.SZSE", "score": 1.0}],
        "state": {"day": CLOSE, "targets": {"000001.SZSE": 0.2}, "prices": {"000001.SZSE": 1000}},
    }), encoding="utf-8")
    replay_manifest = tmp_path / "replays.json"
    replay_manifest.write_text(json.dumps({"candidates": [{
        "candidate_id": "candidate_close", "replay_result": str(replay),
    }]}), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [{"id": "candidate_close",
        "research_selection": {"research_followup_allowed": True}}]}), encoding="utf-8")
    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "continued_research_only", "last_completed_day": CLOSE,
        "execution_admitted": False,
    })
    observed: dict[str, Path] = {}

    def d0_run(source, *args, **kwargs):
        observed["source"] = Path(source)
        return {"status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
                "execution_admitted": False}

    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract", d0_run)
    receipt = orchestrator.run_pipeline(
        None, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        candidate_replay_manifest=replay_manifest, catalog_path=catalog,
        close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0, stockdb_root=stockdb)
    assert receipt["status"] == orchestrator.STATUS
    assert observed["source"] == tmp_path / "run" / "candidate_inputs" / "manifest.json"
    assert receipt["candidate_input_bundle"]["manifest"]["candidate_count"] == 1


def test_post_close_replay_manifest_requires_registered_catalog(tmp_path: Path) -> None:
    _, fixed_dir, package, stockdb = _inputs(tmp_path)
    replay_manifest = tmp_path / "replays.json"
    replay_manifest.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="registered candidate catalog"):
        orchestrator.run_pipeline(
            None, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
            candidate_replay_manifest=replay_manifest, close_date=CLOSE,
            execution_day="2026-10-27", settlement_day="2026-10-28",
            initial_cash=100000.0, stockdb_root=stockdb)


def test_post_close_filters_stop_replay_before_target_bundle(tmp_path: Path, monkeypatch) -> None:
    _, fixed_dir, package, stockdb = _inputs(tmp_path)
    replay_paths = []
    for candidate_id in ("keep", "stop"):
        replay = tmp_path / f"{candidate_id}.json"
        replay.write_text(json.dumps({
            "strategy_id": candidate_id, "end_date": CLOSE,
            "execution_admitted": False, "strategy_validated": False,
            "selected_events": [{"date": CLOSE, "symbol": "000001.SZSE", "score": 1.0}],
            "state": {"day": CLOSE, "targets": {"000001.SZSE": 0.2}, "prices": {"000001.SZSE": 1000}},
        }), encoding="utf-8")
        replay_paths.append(replay)
    replay_manifest = tmp_path / "replays.json"
    replay_manifest.write_text(json.dumps({"candidates": [
        {"candidate_id": "keep", "decision_id": "keep", "replay_result": str(replay_paths[0])},
        {"candidate_id": "stop", "decision_id": "stop", "replay_result": str(replay_paths[1])},
    ]}), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [
        {"id": "keep", "research_selection": {"research_followup_allowed": True}},
        {"id": "stop", "research_selection": {"research_followup_allowed": True}},
    ]}), encoding="utf-8")
    decision = tmp_path / "decisions.json"
    decision.write_text(json.dumps({"strategies": [
        {"id": "keep", "research_decision": "CONTINUE_RESEARCH_ONLY"},
        {"id": "stop", "research_decision": "STOP_RESEARCH_ONLY"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "continued_research_only", "last_completed_day": CLOSE,
        "execution_admitted": False,
    })
    observed: dict[str, list[str]] = {}

    def d0_run(source, *args, **kwargs):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        observed["candidates"] = [row["candidate_id"] for row in payload["candidates"]]
        return {"status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
                "execution_admitted": False}

    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract", d0_run)
    receipt = orchestrator.run_pipeline(
        None, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        candidate_replay_manifest=replay_manifest, catalog_path=catalog,
        research_decision_receipt=decision, close_date=CLOSE,
        execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0, stockdb_root=stockdb)
    assert observed["candidates"] == ["keep"]
    assert (tmp_path / "run" / "candidate_inputs" / "keep").is_dir()
    assert not (tmp_path / "run" / "candidate_inputs" / "stop").exists()
    assert receipt["research_decision_selection"]["excluded"][0]["candidate_id"] == "stop"


def test_post_close_preserves_d0_failure_receipt(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "continued_research_only", "last_completed_day": CLOSE,
        "execution_admitted": False,
    })
    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("synthetic D0 failure")))
    with pytest.raises(ValueError, match="synthetic D0 failure"):
        orchestrator.run_pipeline(
            source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
            close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
            initial_cash=100000.0, stockdb_root=stockdb)
    failure = json.loads((tmp_path / "run" / "post_close_receipt.json").read_text(encoding="utf-8"))
    assert failure["status"] == orchestrator.FAILED_STATUS
    assert failure["execution_admitted"] is False
