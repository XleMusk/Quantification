from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research.candidate_daily_input_bundle import build_input_bundle


def _nested(path: Path, strategy: str = "candidate_a") -> Path:
    path.write_text(json.dumps({
        "strategy_id": strategy,
        "end_date": "2026-10-23",
        "execution_admitted": False,
        "strategy_validated": False,
        "selected_events": [{"date": "2026-10-23", "symbol": "600000.SSE", "score": 1.0}],
        "state": {"day": "2026-10-23", "targets": {"600000.SSE": 0.2},
                  "prices": {"600000.SSE": 1000}},
    }), encoding="utf-8")
    return path


def _legacy(path: Path) -> Path:
    path.write_text(json.dumps({
        "day": "2026-10-23", "targets": {"000001.SZSE": 0.2},
        "prices": {"000001.SZSE": 2000},
        "candidates": [{"symbol": "000001.SZSE", "score": 1.0}],
    }), encoding="utf-8")
    return path


def test_bundle_builds_alias_safe_manifest_from_two_replay_kinds(tmp_path: Path) -> None:
    nested = _nested(tmp_path / "nested.json")
    legacy = _legacy(tmp_path / "legacy.json")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [
        {"id": "catalog_a", "research_selection": {"research_followup_allowed": True}},
        {"id": "catalog_b", "research_selection": {"research_followup_allowed": True}},
    ]}), encoding="utf-8")
    receipt = build_input_bundle([
        {"candidate_id": "catalog_a", "replay_result": str(nested)},
        {"candidate_id": "catalog_b", "terminal_account": str(legacy),
         "strategy_id": "trend_low_vol_half_momentum_20"},
    ], tmp_path / "bundle", catalog_path=catalog)
    assert receipt["status"] == "COMPLETED_RESEARCH_ONLY_CANDIDATE_INPUT_BUNDLE"
    assert receipt["candidate_count"] == 2
    assert receipt["execution_admitted"] is False
    assert len(receipt["manifest"]["sha256"]) == 64
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    assert [row["candidate_id"] for row in manifest["candidates"]] == ["catalog_a", "catalog_b"]
    assert all(Path(row["target"]).is_file() and Path(row["quotes"]).is_file()
               for row in manifest["candidates"])


def test_bundle_rejects_admitted_replay(tmp_path: Path) -> None:
    replay = _nested(tmp_path / "unsafe.json")
    payload = json.loads(replay.read_text(encoding="utf-8"))
    payload["execution_admitted"] = True
    replay.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unadmitted"):
        build_input_bundle([{"candidate_id": "candidate_a", "replay_result": str(replay)}],
                           tmp_path / "bundle", catalog_path=None)


def test_bundle_rejects_catalog_stop_candidate(tmp_path: Path) -> None:
    replay = _nested(tmp_path / "replay.json")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [
        {"id": "candidate_a", "research_selection": {"research_followup_allowed": False}},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="not enabled"):
        build_input_bundle([{"candidate_id": "candidate_a", "replay_result": str(replay)}],
                           tmp_path / "bundle", catalog_path=catalog)


def test_bundle_allows_explicit_unassessed_separate_gate(tmp_path: Path) -> None:
    replay = _nested(tmp_path / "replay.json")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [
        {"id": "candidate_a", "research_selection": {
            "decision": "UNASSESSED_SEPARATE_GATE",
            "research_followup_allowed": False,
            "paper_allowed": False,
        }},
    ]}), encoding="utf-8")
    receipt = build_input_bundle(
        [{"candidate_id": "candidate_a", "replay_result": str(replay)}],
        tmp_path / "bundle", catalog_path=catalog, gate_mode="separate_gate",
    )
    assert receipt["gate_mode"] == "separate_gate"
    assert receipt["candidates"][0]["catalog_selection"]["paper_allowed"] is False
    assert receipt["candidates"][0]["gate_mode"] == "separate_gate"


def test_bundle_rejects_strategy_alias_that_changes_replay_identity(tmp_path: Path) -> None:
    replay = _nested(tmp_path / "replay.json", strategy="source_strategy")
    with pytest.raises(ValueError, match="does not match replay source"):
        build_input_bundle([{"candidate_id": "candidate_a", "replay_result": str(replay),
                             "strategy_id": "different_strategy"}], tmp_path / "bundle")
