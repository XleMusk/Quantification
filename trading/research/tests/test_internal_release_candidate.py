from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research.internal_release_candidate import build_release
from trading.research.internal_release_candidate import PAPER_EVIDENCE_FILES


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


def test_internal_release_default_paper_evidence_includes_current_joint_d0_d1() -> None:
    assert (
        "D:/Quantification/outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v2/contract_receipt.json"
        in PAPER_EVIDENCE_FILES
    )
    assert (
        "D:/Quantification/outputs/research_reports/20261026_fixed_candidate_d0_d1_v2.md"
        in PAPER_EVIDENCE_FILES
    )


def test_internal_release_binds_current_runtime_recipe() -> None:
    from trading.research.internal_release_candidate import RUNTIME_RECIPE_FILES

    assert "outputs/pipeline_receipts/20261027_runtime_recipe_v4.json" in RUNTIME_RECIPE_FILES
    assert "outputs/research_reports/20261027_runtime_recipe_v4.md" in RUNTIME_RECIPE_FILES


def test_internal_release_binds_current_browser_job_observation() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES

    assert "outputs/pipeline_receipts/20261027_workbench_u02_browser_observation_v1.json" in EVIDENCE_FILES
    assert "outputs/research_reports/20261027_workbench_u02_browser_observation_v1.md" in EVIDENCE_FILES


def test_internal_release_binds_direct_candidate_manifest_binding() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES

    assert "outputs/pipeline_receipts/20261027_post_close_direct_manifest_binding_v1.json" in EVIDENCE_FILES
    assert "outputs/research_reports/20261027_post_close_direct_manifest_binding_v1.md" in EVIDENCE_FILES


def test_internal_release_binds_close_delivery_entrypoint() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES, REQUIRED_FILES

    assert "trading/research/close_delivery.py" in REQUIRED_FILES
    assert "trading/research/tests/test_close_delivery.py" in EVIDENCE_FILES
    assert "outputs/pipeline_receipts/20261027_close_delivery_v2/close_delivery_receipt.json" in EVIDENCE_FILES


def test_internal_release_binds_full_panel_strategy_count_evidence() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES, REQUIRED_FILES

    assert "trading/research/common_window_strategy_comparison.py" in REQUIRED_FILES
    assert "trading/tests/test_common_window_strategy_comparison.py" in EVIDENCE_FILES
    assert "outputs/pipeline_receipts/20261027_full_panel_common_window_strategy_comparison_v1.json" in EVIDENCE_FILES


def test_internal_release_binds_account_risk_application_view() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES, REQUIRED_FILES

    assert "trading/research/operator_account_risk_view.py" in REQUIRED_FILES
    assert "trading/research/tests/test_operator_account_risk_view.py" in EVIDENCE_FILES
    assert "outputs/pipeline_receipts/20261027_operator_account_risk_view_usecase_v1.json" in EVIDENCE_FILES


def test_internal_release_binds_broker_observation_application_view() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES, REQUIRED_FILES

    assert "trading/research/operator_broker_observation_view.py" in REQUIRED_FILES
    assert "trading/research/tests/test_operator_broker_observation_view.py" in EVIDENCE_FILES
    assert "outputs/pipeline_receipts/20261027_operator_broker_observation_view_usecase_v1.json" in EVIDENCE_FILES


def test_internal_release_binds_instrument_master_application_view() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES, REQUIRED_FILES

    assert "trading/research/operator_instrument_master_view.py" in REQUIRED_FILES
    assert "trading/research/tests/test_operator_instrument_master_view.py" in EVIDENCE_FILES
    assert "outputs/pipeline_receipts/20261027_operator_instrument_master_view_usecase_v1.json" in EVIDENCE_FILES


def test_internal_release_binds_workbench_boundary_view_consumer() -> None:
    from trading.research.internal_release_candidate import EVIDENCE_FILES, REQUIRED_FILES

    assert "webapp/frontend/src/components/OfflineOperatorBoard.tsx" in REQUIRED_FILES
    assert "webapp/frontend/src/tradingTypes.ts" in REQUIRED_FILES
    assert "outputs/pipeline_receipts/20261027_workbench_boundary_views_consumer_v1.json" in EVIDENCE_FILES
    assert "outputs/pipeline_receipts/20261027_workbench_boundary_views_browser_observation_v1.json" in EVIDENCE_FILES
