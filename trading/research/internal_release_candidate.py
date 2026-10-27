"""Build a versioned internal research-only release manifest.

The manifest packages the executable paper path and its evidence references.
It is an engineering release artifact, not a live-account approval and never
opens a broker/THS client or mutates a Ledger.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable


SCHEMA = "quantification_internal_release_candidate_v1"
STATUS = "RESEARCH_ONLY_INTERNAL_RELEASE_CANDIDATE"

REQUIRED_FILES = (
    "docs/CODE_STYLE.md",
    "docs/architecture_v3/execution_wave_plan_20261025.json",
    "docs/architecture_v3/work_packages_v3_binding.json",
    "trading/research/next_close_d0_d1.py",
    "trading/research/post_close_d0_d1.py",
    "trading/research/stockdb_fixed_daily.py",
    "trading/research/shortline_candidate_handoff.py",
    "trading/research/f01_financial_factor.py",
    "trading/research/shortline_baostock_raw_snapshot.py",
    "trading/research/shortline_adoption_gate.py",
    "trading/research/shortline_panel_replay.py",
    "trading/research/stockdb_source_coverage.py",
    "trading/research/factor_replay_comparison.py",
    "trading/research/research_adoption_rollup.py",
    "trading/research/candidate_library_binding.py",
    "trading/research/workbench_manual_resubmit.py",
    "trading/research/paper_preview_readiness_view.py",
    "trading/research/selected_plan_view.py",
    "trading/research/candidate_evidence_view.py",
    "trading/research/current_research_evidence_view.py",
    "trading/research/operator_release_controls_view.py",
    "trading/research/ths_prefill_readback_view.py",
    "trading/research/operator_lifecycle_view.py",
    "trading/research/operator_session_handoff_view.py",
    "trading/research/operator_blotter_view.py",
    "trading/research/operator_execution_attribution_view.py",
    "trading/research/operator_execution_evidence_view.py",
    "trading/research/operator_portfolio_views.py",
    "trading/research/operator_basket_schedule_view.py",
    "trading/research/operator_paper_selection_view.py",
    "trading/research/operator_manual_statements_view.py",
    "trading/research/ths_native_observation_view.py",
    "trading/research/opening_window_status_view.py",
    "trading/research/selected_plan_view.py",
    "trading/research/daily_candidate_paper_batch.py",
    "trading/research/candidate_daily_paper_run.py",
    "trading/research/runtime_recipe.py",
    "webapp/backend/app/job_lifecycle_view.py",
    "run_committee.py",
)

EVIDENCE_FILES = (
    "docs/architecture_v3/goal_continuity.json",
    "docs/architecture_v3/current_handoff_quantification.md",
    "docs/architecture_v3/candidate_strategy_catalog_20261024.json",
    "outputs/pipeline_receipts/20261025_f2_alternate_financials_bridge_v2.json",
    "outputs/pipeline_receipts/20261025_ths_target_readback_synthetic_v1.json",
    "outputs/research_reports/20261026_shortline_candidate_handoff_v1.md",
    "outputs/shortline_native_candidate_handoff/20261026_source1015_ipoqualified_v1/receipt.json",
    "outputs/pipeline_receipts/20261026_shortline_candidate_panel_replay_blocked_v1.json",
    "outputs/research_reports/20261026_shortline_candidate_panel_replay_blocked_v1.md",
    "trading/research/tests/test_f01_financial_factor.py",
    "trading/research/tests/test_f2_vertical_slice.py",
    "trading/research/tests/test_shortline_baostock_raw_snapshot.py",
    "outputs/pipeline_receipts/20261026_shortline_baostock_candidate_replay_v1.json",
    "outputs/research_reports/20261026_shortline_baostock_candidate_replay_v1.md",
    "outputs/pipeline_receipts/20261026_shortline_adoption_gate_v1.json",
    "outputs/research_reports/20261026_shortline_adoption_gate_v1.md",
    "outputs/pipeline_receipts/20261026_shortline_baostock_long_path_replay_v1.json",
    "outputs/research_reports/20261026_shortline_baostock_long_path_replay_v1.md",
    "outputs/pipeline_receipts/20261026_shortline_adoption_gate_v2.json",
    "outputs/research_reports/20261026_shortline_adoption_gate_v2.md",
    "outputs/pipeline_receipts/20261026_shortline_stockdb_long_path_replay_v1.json",
    "outputs/research_reports/20261026_shortline_stockdb_long_path_replay_v1.md",
    "outputs/pipeline_receipts/20261026_shortline_adoption_gate_v3.json",
    "outputs/research_reports/20261026_shortline_adoption_gate_v3.md",
    "outputs/pipeline_receipts/20261026_stockdb_close_coverage_v1.json",
    "outputs/research_reports/20261026_stockdb_close_coverage_v1.md",
    "outputs/pipeline_receipts/20261026_post_close_d0_d1_v1/post_close_receipt.json",
    "outputs/pipeline_receipts/20261026_mature_factor_horizon_comparison_v1.json",
    "outputs/research_reports/20261026_mature_factor_horizon_comparison_v1.md",
    "outputs/pipeline_receipts/20261026_research_adoption_rollup_v1.json",
    "outputs/research_reports/20261026_research_adoption_rollup_v1.md",
    "outputs/pipeline_receipts/20261026_workbench_manual_resubmit_v1.json",
    "outputs/research_reports/20261026_workbench_manual_resubmit_v1.md",
    "trading/research/tests/test_shortline_adoption_gate.py",
    "trading/tests/test_stockdb_source_coverage.py",
    "trading/tests/test_factor_replay_comparison.py",
    "trading/research/tests/test_research_adoption_rollup.py",
    "trading/research/tests/test_workbench_manual_resubmit.py",
    "outputs/pipeline_receipts/20261027_research_adoption_rollup_v12.json",
    "outputs/research_reports/20261027_research_adoption_rollup_v12.md",
    "outputs/pipeline_receipts/20261027_research_adoption_rollup_v13.json",
    "outputs/research_reports/20261027_research_adoption_rollup_v13.md",
    "outputs/pipeline_receipts/20261027_paper_pipeline_lineage_v5.json",
    "outputs/research_reports/20261027_paper_pipeline_lineage_v5.md",
    "outputs/pipeline_receipts/20261027_candidate_library_binding_v9.json",
    "outputs/research_reports/20261027_candidate_library_binding_v9.md",
    "outputs/pipeline_receipts/20261027_full_panel_common_window_cost_pressure_v1.json",
    "outputs/research_reports/20261027_full_panel_common_window_cost_pressure_v1.md",
    "outputs/pipeline_receipts/20261027_operator_evidence_usecase_v1.json",
    "outputs/pipeline_receipts/20261027_operator_decision_queue_usecase_v1.json",
    "trading/research/tests/test_operator_evidence_view.py",
    "trading/research/tests/test_operator_decision_queue_view.py",
    "trading/research/tests/test_paper_preview_readiness_view.py",
    "outputs/pipeline_receipts/20261027_paper_preview_readiness_usecase_v1.json",
    "outputs/research_reports/20261027_paper_preview_readiness_usecase_v1.md",
    "trading/research/tests/test_selected_plan_view.py",
    "outputs/pipeline_receipts/20261027_selected_plan_usecase_v1.json",
    "outputs/research_reports/20261027_selected_plan_usecase_v1.md",
    "trading/research/tests/test_operator_manual_statements_view.py",
    "trading/tests/test_api_manual_statements.py",
    "outputs/pipeline_receipts/20261027_operator_manual_statements_usecase_v1.json",
    "outputs/research_reports/20261027_operator_manual_statements_usecase_v1.md",
    "trading/research/tests/test_ths_native_observation_view.py",
    "trading/research/tests/test_opening_window_status_view.py",
    "outputs/pipeline_receipts/20261027_opening_window_status_usecase_v1.json",
    "outputs/research_reports/20261027_opening_window_status_usecase_v1.md",
    "outputs/pipeline_receipts/20261027_ths_native_observation_usecase_v1.json",
    "outputs/research_reports/20261027_ths_native_observation_usecase_v1.md",
    "trading/research/tests/test_candidate_evidence_view.py",
    "outputs/pipeline_receipts/20261027_candidate_evidence_usecase_v1.json",
    "outputs/research_reports/20261027_candidate_evidence_usecase_v1.md",
    "trading/research/tests/test_current_research_evidence_view.py",
    "outputs/pipeline_receipts/20261027_current_research_evidence_usecase_v1.json",
    "outputs/research_reports/20261027_current_research_evidence_usecase_v1.md",
    "trading/research/tests/test_operator_release_controls_view.py",
    "outputs/pipeline_receipts/20261027_operator_release_controls_usecase_v1.json",
    "outputs/research_reports/20261027_operator_release_controls_usecase_v1.md",
    "trading/research/tests/test_ths_prefill_readback_view.py",
    "outputs/pipeline_receipts/20261027_ths_prefill_readback_usecase_v1.json",
    "trading/research/tests/test_operator_lifecycle_view.py",
    "outputs/pipeline_receipts/20261027_operator_lifecycle_usecase_v1.json",
    "outputs/research_reports/20261027_operator_lifecycle_usecase_v1.md",
    "trading/research/tests/test_operator_session_handoff_view.py",
    "outputs/pipeline_receipts/20261027_operator_session_handoff_usecase_v1.json",
    "outputs/research_reports/20261027_operator_session_handoff_usecase_v1.md",
    "trading/research/tests/test_operator_blotter_view.py",
    "outputs/pipeline_receipts/20261027_operator_blotter_usecase_v1.json",
    "outputs/research_reports/20261027_operator_blotter_usecase_v1.md",
    "trading/research/tests/test_operator_execution_attribution_view.py",
    "outputs/pipeline_receipts/20261027_operator_execution_attribution_usecase_v1.json",
    "outputs/research_reports/20261027_operator_execution_attribution_usecase_v1.md",
    "trading/research/tests/test_operator_execution_evidence_view.py",
    "outputs/pipeline_receipts/20261027_operator_execution_evidence_usecase_v1.json",
    "outputs/research_reports/20261027_operator_execution_evidence_usecase_v1.md",
    "trading/research/tests/test_operator_portfolio_views.py",
    "outputs/pipeline_receipts/20261027_operator_portfolio_views_usecase_v1.json",
    "outputs/research_reports/20261027_operator_portfolio_views_usecase_v1.md",
    "trading/research/tests/test_operator_basket_schedule_view.py",
    "outputs/pipeline_receipts/20261027_operator_basket_schedule_usecase_v1.json",
    "outputs/research_reports/20261027_operator_basket_schedule_usecase_v1.md",
    "trading/research/tests/test_operator_paper_selection_view.py",
    "outputs/pipeline_receipts/20261027_operator_paper_selection_usecase_v1.json",
    "outputs/research_reports/20261027_operator_paper_selection_usecase_v1.md",
    "outputs/research_reports/20261027_ths_prefill_readback_usecase_v1.md",
    "trading/research/tests/test_selected_plan_view.py",
    "outputs/pipeline_receipts/20261027_selected_plan_view_usecase_v1.json",
    "outputs/research_reports/20261027_selected_plan_view_usecase_v1.md",
    "D:/QuantificationData/trading/research/financials/20261027_f2_issuer_2023q1_official_cninfo/source_snapshot.json",
    "outputs/pipeline_receipts/20261027_f2_official_source_gate_v1.json",
    "outputs/research_reports/20261027_f2_official_source_gate_v1.md",
    "trading/research/tests/test_daily_candidate_paper_batch.py",
    "trading/research/tests/test_candidate_daily_paper_run.py",
    "outputs/pipeline_receipts/20261027_candidate_stage_trace_v1.json",
    "outputs/research_reports/20261027_candidate_stage_trace_v1.md",
    "trading/research/tests/test_post_close_d0_d1.py",
    "outputs/pipeline_receipts/20261027_post_close_pipeline_stage_trace_v1.json",
    "outputs/research_reports/20261027_post_close_pipeline_stage_trace_v1.md",
    "trading/research/tests/test_runtime_recipe.py",
    "outputs/pipeline_receipts/20261027_runtime_recipe_v4.json",
    "outputs/research_reports/20261027_runtime_recipe_v4.md",
    "webapp/backend/tests/test_job_lifecycle_view.py",
    "outputs/pipeline_receipts/20261027_job_lifecycle_view_usecase_v1.json",
    "outputs/research_reports/20261027_job_lifecycle_view_usecase_v1.md",
)

PAPER_EVIDENCE_FILES = (
    "D:/QuantificationData/trading/research/releases/20261025_v1/release_manifest.json",
    "D:/QuantificationData/trading/research/candidate_shadow/alpha158_lgbm_20261025_v1/paper_batch/batch_receipt.json",
    "D:/QuantificationData/trading/research/candidate_shadow/value_size_activity_2023_20261025_v1/paper_batch/joint_paper_receipt.json",
    "D:/Quantification/outputs/pipeline_receipts/20261026_leader_eight_type_turnover_joint_paper_v1/joint_paper_receipt.json",
    "D:/Quantification/outputs/pipeline_receipts/20261026_public_seat_joint_paper_v2/joint_paper_receipt.json",
    "D:/Quantification/outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v2/contract_receipt.json",
    "D:/Quantification/outputs/research_reports/20261026_fixed_candidate_d0_d1_v2.md",
    "D:/Quantification/outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v3/contract_receipt.json",
    "D:/Quantification/outputs/research_reports/20261026_fixed_candidate_d0_d1_v3.md",
    "D:/Quantification/outputs/pipeline_receipts/20261026_workbench_lifecycle_job_id_v2.json",
    "D:/Quantification/outputs/research_reports/20261026_workbench_lifecycle_job_id_v2.md",
    "D:/Quantification/outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v5/contract_receipt.json",
    "D:/Quantification/outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v5_summary.json",
    "D:/Quantification/outputs/pipeline_receipts/20261027_paper_pipeline_lineage_v5.json",
    "D:/Quantification/outputs/pipeline_receipts/20261027_candidate_library_binding_v9.json",
    "D:/Quantification/outputs/pipeline_receipts/20261027_research_adoption_rollup_v13.json",
    "D:/Quantification/outputs/pipeline_receipts/20261027_operator_evidence_usecase_v1.json",
    "D:/Quantification/outputs/pipeline_receipts/20261027_operator_decision_queue_usecase_v1.json",
)

RUNTIME_RECIPE_FILES = (
    "outputs/pipeline_receipts/20261027_runtime_recipe_v4.json",
    "outputs/research_reports/20261027_runtime_recipe_v4.md",
)


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _refs(root: Path, paths: Iterable[str]) -> tuple[list[dict[str, Any]], list[str]]:
    refs, missing = [], []
    for relative in paths:
        path = (root / relative).resolve()
        if not path.is_file():
            missing.append(relative)
            continue
        refs.append({"path": str(path), "relative_path": relative,
                     "sha256": _sha256(path), "bytes": path.stat().st_size})
    return refs, missing


def _git(root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        return {"commit": commit, "worktree_clean": not bool(status.strip())}
    except (OSError, subprocess.CalledProcessError) as error:
        return {"commit": None, "worktree_clean": None, "error": str(error)}


def build_release(root: str | Path, output_dir: str | Path, *, version: str,
                  paper_evidence_files: Iterable[str] | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    output = Path(output_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"Project root does not exist: {root}")
    if not version or any(char.isspace() for char in version):
        raise ValueError("version must be a non-empty identifier without whitespace")
    required, missing_required = _refs(root, REQUIRED_FILES)
    evidence, missing_evidence = _refs(root, EVIDENCE_FILES)
    runtime_recipe, missing_runtime_recipe = _refs(root, RUNTIME_RECIPE_FILES)
    paper_evidence, missing_paper_evidence = _refs(
        root, PAPER_EVIDENCE_FILES if paper_evidence_files is None else paper_evidence_files)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "release_manifest.json"
    payload = {
        "schema": SCHEMA,
        "status": STATUS,
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "research_only_paper_pipeline",
        "execution_admitted": False,
        "real_login": False,
        "prefill_submit_cancel_transfer": False,
        "paid_data": False,
        "live_execution_ready": False,
        "required_code": required,
        "evidence": evidence,
        "paper_evidence": paper_evidence,
        "runtime_recipe": runtime_recipe,
        "missing_required_code": missing_required,
        "missing_evidence": missing_evidence,
        "missing_paper_evidence": missing_paper_evidence,
        "missing_runtime_recipe": missing_runtime_recipe,
        "account_readiness": {
            "research_paper_account": "BOUND_RESEARCH_ONLY",
            "current_broker_quote": "MISSING",
            "four_table_account_export": "MISSING",
            "t_plus_1_sellable_export": "MISSING",
            "ths_readonly_observation": "REFERENCE_ONLY_NOT_OBSERVED",
            "execution_admitted": False,
            "live_execution_ready": False,
        },
        "git": _git(root),
        "validation_commands": [
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_next_close_d0_d1.py trading/research/tests/test_post_close_d0_d1.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_paper_pipeline_lineage.py trading/research/tests/test_candidate_library_binding.py trading/research/tests/test_research_adoption_rollup.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_evidence_view.py trading/research/tests/test_operator_decision_queue_view.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_paper_preview_readiness_view.py trading/tests/test_api_operator_package.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_selected_plan_view.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_manual_statements_view.py trading/tests/test_api_manual_statements.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_ths_native_observation_view.py trading/tests/test_api_operator_package.py -k 'ths_native_observation or native_observation'",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_opening_window_status_view.py trading/tests/test_architecture.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_lifecycle_view.py trading/tests/test_api_operator_package.py -k lifecycle",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_session_handoff_view.py trading/tests/test_api_operator_package.py -k session_handoff",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_blotter_view.py trading/tests/test_api_operator_package.py -k blotter",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_execution_attribution_view.py trading/tests/test_api_operator_package.py -k execution_attribution",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_execution_evidence_view.py trading/tests/test_api_operator_package.py -k execution_evidence",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_portfolio_views.py trading/tests/test_api_operator_package.py -k 'portfolio_controls or portfolio_risk or portfolio_views'",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_basket_schedule_view.py trading/tests/test_api_operator_package.py -k 'basket_schedule or order_desk_consumers'",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_paper_selection_view.py trading/tests/test_api_operator_package.py -k paper_selection",
            "D:/Quantification/env/python.exe -m pytest -q webapp/backend/tests/test_job_lifecycle_view.py webapp/backend/tests/test_webapp.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_candidate_evidence_view.py trading/research/tests/test_research_evidence_catalog.py trading/research/tests/test_native_research_evidence_catalog.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_current_research_evidence_view.py trading/tests/test_api_operator_package.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_operator_release_controls_view.py trading/tests/test_api_operator_package.py -k release_controls",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_ths_prefill_readback_view.py trading/tests/test_ths_target_readback.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_daily_candidate_paper_batch.py trading/research/tests/test_candidate_daily_paper_run.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_post_close_d0_d1.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_runtime_recipe.py",
            "D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_f2_historical_evidence.py",
            "D:/Quantification/env/python.exe run_committee.py post-close-d0-d1 --help",
        ],
        "operating_entrypoints": {
            "fixed_daily": "run_committee.py stockdb-fixed-daily",
            "next_close": "run_committee.py next-close-d0-d1",
            "post_close": "run_committee.py post-close-d0-d1",
            "ths_readback": "run_committee.py ths-target-readback",
        },
        "release_boundary": [
            "This package is for local research/paper operation only.",
            "A missing F2 PIT fact, current BrokerQuote, sellable T+1 export or THS observation keeps live_execution_ready=false.",
            "No unknown external order is retried or resent by this package.",
        ],
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload["manifest_path"] = str(manifest_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    payload = build_release(args.root, args.output_dir, version=args.version)
    print(json.dumps({"status": payload["status"], "version": payload["version"],
                      "execution_admitted": payload["execution_admitted"],
                      "live_execution_ready": payload["live_execution_ready"],
                      "missing_required_code": payload["missing_required_code"],
                      "manifest": payload["manifest_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
