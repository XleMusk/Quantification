from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research import candidate_daily_paper_run as pipeline

run_pipeline = pipeline.run_pipeline


def test_pipeline_binds_input_bundle_to_shared_paper_batch(tmp_path: Path) -> None:
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "candidate_a", "end_date": "2026-10-23",
        "execution_admitted": False, "strategy_validated": False,
        "selected_events": [{"date": "2026-10-23", "symbol": "600000.SSE", "score": 1.0}],
        "state": {"day": "2026-10-23", "targets": {"600000.SSE": 0.2}, "prices": {"600000.SSE": 1000}},
    }), encoding="utf-8")
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"candidates": [{"candidate_id": "candidate_a", "replay_result": str(replay)}]}), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [{"id": "candidate_a", "research_selection": {"research_followup_allowed": True}}]}), encoding="utf-8")
    receipt = run_pipeline(sources, tmp_path / "run", catalog_path=catalog,
                           initial_cash=100000.0, execution_day="2026-10-24", settlement_day="2026-10-26")
    assert receipt["status"] == "COMPLETED_RESEARCH_ONLY_CANDIDATE_DAILY_PIPELINE"
    assert receipt["candidate_count"] == 1
    assert receipt["execution_admitted"] is False
    batch = json.loads(Path(receipt["paper_batch"]["receipt_path"]).read_text(encoding="utf-8"))
    assert batch["candidate_count"] == 1
    assert batch["candidates"][0]["target_count"] == 1
    assert batch["candidates"][0]["paper_order_count"] == 1
    assert receipt["stage_trace"]["stages"] == [
        "Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position"
    ]
    assert receipt["stage_trace"]["fixed"] is None
    assert receipt["stage_trace"]["broker_adapter_attempted"] is False
    package = json.loads(Path(batch["candidates"][0]["package_result"]["path"]).read_text(encoding="utf-8"))
    assert package["cost_model"]["id"] == "cn_a_share_standard_ledger_costs_v1"
    assert package["benchmark_binding"]["status"] == "NOT_APPLICABLE_TO_PAPER_TARGET_PROJECTION"


def test_pipeline_projects_fixed_leg_through_same_paper_package(tmp_path: Path) -> None:
    fixed = tmp_path / "fixed_account.json"
    fixed.write_text(json.dumps({
        "day": "2026-10-23", "targets": {"600000.SSE": 0.2},
        "prices": {"600000.SSE": 1000}, "candidates": [{"symbol": "600000.SSE", "score": 1.0}],
    }), encoding="utf-8")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "candidate_a", "end_date": "2026-10-23",
        "execution_admitted": False, "strategy_validated": False,
        "selected_events": [{"date": "2026-10-23", "symbol": "000001.SZSE", "score": 1.0}],
        "state": {"day": "2026-10-23", "targets": {"000001.SZSE": 0.2}, "prices": {"000001.SZSE": 1000}},
    }), encoding="utf-8")
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"candidates": [{"candidate_id": "candidate_a", "replay_result": str(replay)}]}), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [{"id": "candidate_a", "research_selection": {"research_followup_allowed": True}}]}), encoding="utf-8")
    receipt = run_pipeline(sources, tmp_path / "run", catalog_path=catalog,
                           initial_cash=100000.0, execution_day="2026-10-24", settlement_day="2026-10-26",
                           fixed_terminal_account=fixed, fixed_strategy_id="low_volatility_control")
    assert receipt["fixed_strategy"]["target_count"] == 1
    assert receipt["fixed_strategy"]["paper_order_count"] == 1
    assert [row["stage"] for row in receipt["stage_trace"]["fixed"]] == [
        "Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position"
    ]
    assert receipt["stage_trace"]["fixed"][4]["status"] == "NOT_ATTEMPTED_RESEARCH_ONLY"
    assert receipt["execution_admitted"] is False
    assert Path(receipt["fixed_strategy"]["target"]["path"]).is_file()
    assert len(receipt["fixed_strategy"]["target"]["sha256"]) == 64
    assert Path(receipt["fixed_strategy"]["package_result"]).is_file()


def test_pipeline_resolves_fixed_daily_run_dir(tmp_path: Path, monkeypatch) -> None:
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "candidate_a", "end_date": "2026-10-13",
        "execution_admitted": False, "strategy_validated": False,
        "selected_events": [{"date": "2026-10-13", "symbol": "000001.SZSE", "score": 1.0}],
        "state": {"day": "2026-10-13", "targets": {"000001.SZSE": 0.2}, "prices": {"000001.SZSE": 1000}},
    }), encoding="utf-8")
    source_manifest = tmp_path / "sources.json"
    source_manifest.write_text(json.dumps({"candidates": [
        {"candidate_id": "candidate_a", "replay_result": str(replay)},
    ]}), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [{"id": "candidate_a",
        "research_selection": {"research_followup_allowed": True}}]}), encoding="utf-8")
    fixed = tmp_path / "fixed_account.json"
    fixed.write_text(json.dumps({
        "strategy_id": "fixed_close", "end_date": "2026-10-13",
        "execution_admitted": False, "strategy_validated": False,
        "targets": {"600000.SSE": 0.2}, "prices": {"600000.SSE": 1000},
        "candidates": [{"date": "2026-10-13", "symbol": "600000.SSE", "score": 1.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(pipeline.stockdb_fixed_daily, "resolve_terminal_account",
                        lambda _run_dir: (fixed, "fixed_close"))
    receipt = pipeline.run_pipeline(
        source_manifest, tmp_path / "pipeline", catalog_path=catalog,
        initial_cash=100000.0, execution_day="2026-10-14", settlement_day="2026-10-15",
        fixed_run_dir=tmp_path / "fixed_daily")
    assert receipt["fixed_strategy"]["strategy_id"] == "fixed_close"
    assert receipt["execution_admitted"] is False


def test_pipeline_projects_explicit_separate_gate_candidate(tmp_path: Path) -> None:
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "candidate_separate_gate", "end_date": "2026-10-23",
        "execution_admitted": False, "strategy_validated": False,
        "selected_events": [{"date": "2026-10-23", "symbol": "600000.SSE", "score": 1.0}],
        "state": {"day": "2026-10-23", "targets": {"600000.SSE": 0.2}, "prices": {"600000.SSE": 1000}},
    }), encoding="utf-8")
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"candidates": [{"candidate_id": "candidate_separate_gate",
                                                    "replay_result": str(replay)}]}), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [{"id": "candidate_separate_gate",
        "research_selection": {"decision": "UNASSESSED_SEPARATE_GATE",
                                 "research_followup_allowed": False,
                                 "paper_allowed": False}}]}), encoding="utf-8")
    receipt = run_pipeline(sources, tmp_path / "run", catalog_path=catalog,
                           initial_cash=100000.0, execution_day="2026-10-24",
                           settlement_day="2026-10-26", gate_mode="separate_gate")
    assert receipt["gate_mode"] == "separate_gate"
    batch = json.loads(Path(receipt["paper_batch"]["receipt_path"]).read_text(encoding="utf-8"))
    assert batch["gate_mode"] == "separate_gate"
    assert batch["candidates"][0]["catalog_selection"]["paper_allowed"] is False
    assert receipt["execution_admitted"] is False


def test_pipeline_reuses_same_identity_and_rejects_changed_inputs(tmp_path: Path) -> None:
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "candidate_idempotent", "end_date": "2026-10-23",
        "execution_admitted": False, "strategy_validated": False,
        "selected_events": [{"date": "2026-10-23", "symbol": "600000.SSE", "score": 1.0}],
        "state": {"day": "2026-10-23", "targets": {"600000.SSE": 0.2}, "prices": {"600000.SSE": 1000}},
    }), encoding="utf-8")
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"candidates": [{"candidate_id": "candidate_idempotent",
                                                    "replay_result": str(replay)}]}), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [{"id": "candidate_idempotent",
        "research_selection": {"research_followup_allowed": True}}]}), encoding="utf-8")
    output = tmp_path / "run"
    first = run_pipeline(sources, output, catalog_path=catalog, initial_cash=100000.0,
                         execution_day="2026-10-24", settlement_day="2026-10-26")
    second = run_pipeline(sources, output, catalog_path=catalog, initial_cash=100000,
                          execution_day="2026-10-24", settlement_day="2026-10-26")
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert second["reused_existing_run"] is True
    assert Path(second["paper_batch"]["receipt_path"]).is_file()
    sources.write_text(json.dumps({"candidates": [{"candidate_id": "candidate_idempotent",
                                                    "replay_result": str(replay), "strategy_id": "changed"}]}),
                       encoding="utf-8")
    with pytest.raises(ValueError, match="different inputs"):
        run_pipeline(sources, output, catalog_path=catalog, initial_cash=100000.0,
                     execution_day="2026-10-24", settlement_day="2026-10-26")
