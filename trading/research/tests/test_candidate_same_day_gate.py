from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research.candidate_same_day_gate import BLOCKED, READY, evaluate_manifest, write_outputs


def _manifest(tmp_path: Path, *, day: str | None) -> Path:
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"strategy_id": "candidate", "decision_day": day,
                                  "research_only": True, "execution_admitted": False}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"candidates": [{"candidate_id": "candidate", "target": str(target)}]}), encoding="utf-8")
    return manifest


def test_stale_candidate_is_explicitly_blocked(tmp_path: Path) -> None:
    receipt = evaluate_manifest(_manifest(tmp_path, day="2026-10-23"), "2026-10-26")
    assert receipt["status"] == BLOCKED
    assert receipt["candidates"][0]["reason"] == "decision_day_mismatch"
    assert receipt["execution_admitted"] is False


def test_same_day_candidate_is_ready_without_creating_a_target(tmp_path: Path) -> None:
    receipt = evaluate_manifest(_manifest(tmp_path, day="2026-10-26"), "2026-10-26")
    assert receipt["status"] == READY
    assert receipt["candidates"][0]["status"] == READY
    assert receipt["paper_allowed"] is False


def test_legacy_manifest_uses_declared_source_basis_day(tmp_path: Path) -> None:
    manifest = tmp_path / "legacy.json"
    manifest.write_text(json.dumps({
        "source_basis": {"decision_day": "2026-10-23"},
        "candidates": [{"candidate_id": "legacy", "terminal_account": "account.json"}],
    }), encoding="utf-8")
    receipt = evaluate_manifest(manifest, "2026-10-26")
    assert receipt["status"] == BLOCKED
    assert receipt["candidates"][0]["decision_day"] == "2026-10-23"


def test_cli_writes_blocked_receipt_and_report(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, day="2026-10-23")
    output = tmp_path / "receipt.json"
    report = tmp_path / "report.md"
    receipt = write_outputs(manifest, "2026-10-26", output, report)
    assert receipt["status"] == BLOCKED
    assert output.is_file() and report.is_file()

