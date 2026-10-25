from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research.internal_release_candidate import build_release


def test_internal_release_manifest_binds_code_and_keeps_live_gate_closed(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CODE_STYLE.md").write_text("style", encoding="utf-8")
    (tmp_path / "run_committee.py").write_text("entry", encoding="utf-8")
    payload = build_release(tmp_path, tmp_path / "release", version="vtest",
                            paper_evidence_files=())
    manifest = json.loads((tmp_path / "release" / "release_manifest.json").read_text())
    assert payload["status"] == "RESEARCH_ONLY_INTERNAL_RELEASE_CANDIDATE"
    assert manifest["execution_admitted"] is False
    assert manifest["live_execution_ready"] is False
    assert manifest["required_code"]
    assert "trading/research/next_close_d0_d1.py" in manifest["missing_required_code"]
    assert manifest["account_readiness"]["research_paper_account"] == "BOUND_RESEARCH_ONLY"
    assert manifest["account_readiness"]["four_table_account_export"] == "MISSING"
    assert manifest["paper_evidence"] == []


def test_internal_release_rejects_invalid_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version"):
        build_release(tmp_path, tmp_path / "release", version="bad version")
