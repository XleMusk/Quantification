"""Build a reproducible local workbench whole-flow acceptance receipt.

The acceptance deliberately separates what the local engineering path proves
from what still requires a market or account channel.  It consumes saved
lifecycle and browser receipts; it never starts a job, retries a task, or
touches a broker/THS account.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..settings import ROOT


SCHEMA = "quantification_workbench_whole_flow_acceptance_v1"
DEFAULT_LIFECYCLE = ROOT / "outputs/pipeline_receipts/20261026_workbench_job_lifecycle_v1.json"
DEFAULT_BROWSER = ROOT / "outputs/pipeline_receipts/20261026_workbench_job_lifecycle_browser_v1.json"
DEFAULT_OUTPUT = ROOT / "outputs/pipeline_receipts/20261026_workbench_whole_flow_acceptance_v1.json"
DEFAULT_REPORT = ROOT / "outputs/research_reports/20261026_workbench_whole_flow_acceptance_v1.md"


def _read(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def build_acceptance(
    lifecycle: Mapping[str, Any],
    browser: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify one engineering-only whole-flow slice without side effects."""

    if lifecycle.get("schema") != "quantification_workbench_job_lifecycle_receipt_v1":
        raise ValueError("unexpected lifecycle receipt schema")
    if browser.get("schema") != "quantification_workbench_job_lifecycle_browser_receipt_v1":
        raise ValueError("unexpected browser receipt schema")

    lifecycle_stages = list(lifecycle.get("api", {}).get("stages") or [])
    observed_stages = [str(item.get("stage")) for item in browser.get("flow", [])]
    observed_contract = [
        {"submit": "submitted", "refresh_completed": "result"}.get(stage, stage)
        for stage in observed_stages
    ]
    required = ["submitted", "compute", "result", "download"]
    stage_contract_ok = lifecycle_stages == required and all(stage in observed_contract for stage in required)
    execution_closed = (
        lifecycle.get("api", {}).get("execution_admitted") is False
        and browser.get("execution_admitted") is False
        and browser.get("live_trading") is False
        and browser.get("broker_or_ths_interacted") is False
    )
    engineering_verified = bool(stage_contract_ok and execution_closed)
    return {
        "schema": SCHEMA,
        "status": "ENGINEERING_VERIFIED_MARKET_SCOPE_BLOCKED" if engineering_verified else "ACCEPTANCE_INCOMPLETE",
        "created_at": datetime.now().astimezone().isoformat(),
        "engineering_scope": {
            "status": "VERIFIED_LOCAL_RESEARCH_ONLY" if engineering_verified else "INCOMPLETE",
            "lifecycle_stage_contract": lifecycle_stages,
            "browser_observed_stages": observed_stages,
            "browser_observed_contract": observed_contract,
            "stage_contract_ok": stage_contract_ok,
            "execution_admitted": False,
            "automatic_recovery": False,
            "source_receipts": [
                "outputs/pipeline_receipts/20261026_workbench_job_lifecycle_v1.json",
                "outputs/pipeline_receipts/20261026_workbench_job_lifecycle_browser_v1.json",
            ],
        },
        "market_account_scope": {
            "status": "BLOCKED_EXTERNAL_EVIDENCE",
            "live_trading": False,
            "broker_observed": False,
            "ths_observed": False,
            "current_market_data": False,
            "reason": "Local seeded research UI evidence does not prove current market, account, broker or THS behavior.",
        },
        "next_action": "Keep engineering path available; obtain separately authorized market/account evidence before claiming channel acceptance.",
        "execution_admitted": False,
        "limitations": [
            "The receipt consumes saved local lifecycle/browser evidence and does not run or retry a task.",
            "Download links prove result availability in the local workbench, not broker or account delivery.",
        ],
    }


def render_report(receipt: Mapping[str, Any]) -> str:
    engineering = receipt["engineering_scope"]
    market = receipt["market_account_scope"]
    return "\n".join(
        [
            "# Workbench whole-flow acceptance",
            "",
            f"Engineering scope: `{engineering['status']}`; market/account scope: `{market['status']}`.",
            "",
            "The local slice covers submit, compute, result refresh and download links with execution closed and manual recovery only.",
            "",
            "Market/account acceptance remains separate: no current quote, broker, personal account or THS observation is claimed.",
            "",
        ]
    )


def write_outputs(
    lifecycle_path: str | Path = DEFAULT_LIFECYCLE,
    browser_path: str | Path = DEFAULT_BROWSER,
    output_path: str | Path = DEFAULT_OUTPUT,
    report_path: str | Path = DEFAULT_REPORT,
) -> tuple[Path, Path]:
    receipt = build_acceptance(_read(lifecycle_path), _read(browser_path))
    output = Path(output_path).expanduser().resolve()
    report = Path(report_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(render_report(receipt), encoding="utf-8")
    return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lifecycle", type=Path, default=DEFAULT_LIFECYCLE)
    parser.add_argument("--browser", type=Path, default=DEFAULT_BROWSER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    output, report = write_outputs(args.lifecycle, args.browser, args.output, args.report)
    print(json.dumps({"output": str(output), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_acceptance", "main", "render_report", "write_outputs"]
