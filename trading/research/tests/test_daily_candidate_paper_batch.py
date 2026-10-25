from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research.daily_candidate_paper_batch import build_batch


def _write_candidate(root: Path, strategy: str, code: str) -> tuple[str, str]:
    target = {
        "schema": "stockdb_candidate_shadow_target_v1", "strategy_id": strategy,
        "decision_day": "2026-10-23", "research_only": True, "execution_admitted": False,
        "weights": {f"{code}.SSE": 0.2},
        "signal_frame": {"signals": [{"security": {"code": code, "exchange": "SSE"},
                                       "diagnostics": {"rank": 1, "score": 1.0}}]},
    }
    quotes = {
        "schema": "ths_raw_quote_observation_v1", "source": "synthetic_stockdb_close",
        "observed_at": "2026-10-23T15:00:00+08:00", "price_basis": "raw_rmb",
        "execution_admitted": False, "client_interacted": False,
        "prices": {f"{code}.SSE": {"price_rmb": 10.0, "source": "synthetic", "observed_date": "2026-10-23"}},
    }
    tp, qp = root / f"{strategy}_target.json", root / f"{strategy}_quotes.json"
    tp.write_text(json.dumps(target), encoding="utf-8")
    qp.write_text(json.dumps(quotes), encoding="utf-8")
    return str(tp), str(qp)


def test_batch_projects_each_candidate_with_closed_execution_boundary(tmp_path: Path) -> None:
    first = _write_candidate(tmp_path, "candidate_a", "600004")
    second = _write_candidate(tmp_path, "candidate_b", "600005")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [
        {"id": "catalog_candidate_a", "research_selection": {"research_followup_allowed": True}},
        {"id": "catalog_candidate_b", "research_selection": {"research_followup_allowed": True}},
    ]}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"candidates": [
        {"candidate_id": "catalog_candidate_a", "strategy_id": "candidate_a", "target": first[0], "quotes": first[1]},
        {"candidate_id": "catalog_candidate_b", "strategy_id": "candidate_b", "target": second[0], "quotes": second[1]},
    ]}), encoding="utf-8")
    result = build_batch([
        {"candidate_id": "catalog_candidate_a", "strategy_id": "candidate_a", "target": first[0], "quotes": first[1]},
        {"candidate_id": "catalog_candidate_b", "strategy_id": "candidate_b", "target": second[0], "quotes": second[1]},
    ], tmp_path / "batch", initial_cash=1000000.0,
        execution_day="2026-10-24", settlement_day="2026-10-26", catalog_path=catalog, manifest_path=manifest)
    assert result["status"] == "COMPLETED_RESEARCH_ONLY_CANDIDATE_BATCH"
    assert result["candidate_count"] == 2
    assert all(row["target_count"] == 1 and row["paper_order_count"] == 1 for row in result["candidates"])
    assert result["execution_admitted"] is False
    assert result["broker_interacted"] is False
    assert all(row["catalog_selection"]["research_followup_allowed"] is True for row in result["candidates"])
    assert result["manifest"]["path"] == str(manifest.resolve())
    assert result["manifest"]["candidate_count"] == 2
    assert len(result["manifest"]["sha256"]) == 64
    assert (tmp_path / "batch" / "batch_receipt.json").is_file()


def test_batch_rejects_manifest_candidate_mismatch(tmp_path: Path) -> None:
    target, quotes = _write_candidate(tmp_path, "candidate_a", "600004")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"candidates": [{"candidate_id": "other", "target": target, "quotes": quotes}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="Manifest candidate set"):
        build_batch([
            {"candidate_id": "candidate_a", "target": target, "quotes": quotes},
        ], tmp_path / "batch", initial_cash=1000000.0,
            execution_day="2026-10-24", settlement_day="2026-10-26", manifest_path=manifest)


def test_batch_rejects_duplicate_candidate_ids(tmp_path: Path) -> None:
    target, quotes = _write_candidate(tmp_path, "candidate_a", "600004")
    with pytest.raises(ValueError, match="identifiers must be unique"):
        build_batch([
            {"candidate_id": "candidate_a", "target": target, "quotes": quotes},
            {"candidate_id": "candidate_a", "target": target, "quotes": quotes},
        ], tmp_path / "batch", initial_cash=1000000.0,
            execution_day="2026-10-24", settlement_day="2026-10-26")


def test_batch_rejects_catalog_stop_candidate(tmp_path: Path) -> None:
    target, quotes = _write_candidate(tmp_path, "candidate_a", "600004")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [
        {"id": "catalog_candidate_a", "research_selection": {"research_followup_allowed": False}},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="not enabled"):
        build_batch([
            {"candidate_id": "catalog_candidate_a", "strategy_id": "candidate_a", "target": target, "quotes": quotes},
        ], tmp_path / "batch", initial_cash=1000000.0,
            execution_day="2026-10-24", settlement_day="2026-10-26", catalog_path=catalog)


def test_batch_projects_explicit_historical_shadow_stop_candidate(tmp_path: Path) -> None:
    target, quotes = _write_candidate(tmp_path, "candidate_a", "600004")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [
        {"id": "catalog_candidate_a", "research_selection": {
            "decision": "STOP_RESEARCH_ONLY",
            "research_followup_allowed": False,
            "paper_allowed": False,
        }},
    ]}), encoding="utf-8")
    result = build_batch([
        {"candidate_id": "catalog_candidate_a", "strategy_id": "candidate_a",
         "target": target, "quotes": quotes},
    ], tmp_path / "batch", initial_cash=1000000.0,
        execution_day="2026-10-24", settlement_day="2026-10-26",
        catalog_path=catalog, gate_mode="historical_shadow")
    assert result["gate_mode"] == "historical_shadow"
    assert result["candidates"][0]["target_count"] == 1
    assert result["candidates"][0]["paper_order_count"] == 1
    assert result["execution_admitted"] is False


def test_batch_rejects_future_quote_day(tmp_path: Path) -> None:
    target, quotes = _write_candidate(tmp_path, "candidate_a", "600004")
    quote = json.loads(Path(quotes).read_text(encoding="utf-8"))
    quote["observed_at"] = "2026-10-24T15:00:00+08:00"
    quote["prices"]["600004.SSE"]["observed_date"] = "2026-10-24"
    Path(quotes).write_text(json.dumps(quote), encoding="utf-8")
    with pytest.raises(ValueError, match="not on its decision day"):
        build_batch([
            {"candidate_id": "candidate_a", "target": target, "quotes": quotes},
        ], tmp_path / "batch", initial_cash=1000000.0,
            execution_day="2026-10-25", settlement_day="2026-10-26")
