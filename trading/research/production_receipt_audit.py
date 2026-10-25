"""Audit saved producer/split receipts without rerunning their routes.

The audit classifies an existing receipt as historical, local and read-only.
It never starts a producer, opens a client, advances an account or requests a
new market source.
"""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA = "quantification_production_receipt_audit_v1"
READ_ONLY_FLAGS = {
    "execution_admitted": False,
    "publication_generated": False,
    "engine_advanced": False,
    "original_accounts_advanced": False,
    "model_refit": False,
    "selection_enabled": False,
    "strategy_validated": False,
    "candidate_panel_only": True,
}


def _read(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Receipt must be a JSON object: {path}")
    return value


def _flag_check(value: dict[str, Any], flags: dict[str, bool]) -> list[str]:
    return [key for key, expected in flags.items() if value.get(key) is not expected]


def audit_receipts(alpha_readback: str | Path, route_compatibility: str | Path | None = None,
                   *, observed_at: str | None = None) -> dict[str, Any]:
    alpha_path = Path(alpha_readback).resolve()
    alpha = _read(alpha_path)
    issues = _flag_check(alpha, READ_ONLY_FLAGS)
    if alpha.get("actual_status") != "completed_alpha158_source_context":
        issues.append("alpha_receipt_status")
    if type(alpha.get("score_rows")) is not int or alpha["score_rows"] <= 0:
        issues.append("alpha_score_rows")
    if not isinstance(alpha.get("source_days"), list) or not alpha["source_days"]:
        issues.append("alpha_source_days")
    if not isinstance(alpha.get("score_dates"), list) or not alpha["score_dates"]:
        issues.append("alpha_score_dates")
    route = None
    route_path = None
    if route_compatibility is not None:
        route_path = Path(route_compatibility).resolve()
        route = _read(route_path)
        for key, expected in {"execution_admitted": False, "orders_generated": False,
                              "ths_payload_generated": False, "client_interacted": False}.items():
            if route.get(key) is not expected:
                issues.append(f"route_{key}")
        if route.get("publication_status") != "close_research_target_publication_only":
            issues.append("route_publication_status")
        if not isinstance(route.get("cases"), list) or not route["cases"]:
            issues.append("route_cases")
    stamp = observed_at or datetime.now(timezone.utc).isoformat()
    source_observed = alpha.get("observed_at") or alpha.get("completed_at")
    temporal_class = "HISTORICAL_RECEIPT"
    if isinstance(source_observed, str):
        try:
            datetime.fromisoformat(source_observed.replace("Z", "+00:00"))
        except ValueError:
            issues.append("alpha_observed_at")
    else:
        issues.append("alpha_observed_at")
    return {
        "schema": SCHEMA,
        "status": "VERIFIED_LOCAL_READONLY_RECEIPT" if not issues else "RECEIPT_REVIEW_BLOCKED",
        "observed_at": stamp,
        "temporal_class": temporal_class,
        "alpha_readback": str(alpha_path),
        "route_compatibility": str(route_path) if route_path else None,
        "source_day": alpha.get("source_day"),
        "publication_source_day": alpha.get("publication_source_day"),
        "score_rows": alpha.get("score_rows"),
        "provider_dates": alpha.get("provider_dates"),
        "route_cases": len(route.get("cases", [])) if route else 0,
        "route_symbols": sorted(set(route.get("unique_symbols", []))) if route else [],
        "issues": issues,
        "execution_admitted": False,
        "producer_repeated": False,
        "account_advanced": False,
        "network_accessed": False,
        "client_interacted": False,
        "strategy_validated": False,
        "limitations": [
            "This is a read-only audit of saved receipts; it does not rerun a producer or split route.",
            "The receipt is historical and cannot prove today's source, account, quote or THS state.",
            "A route-compatible symbol does not prove account, permission or same-session quote evidence.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alpha-readback", required=True)
    parser.add_argument("--route-compatibility")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = audit_receipts(args.alpha_readback, args.route_compatibility)
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "out": str(output), "issues": result["issues"]}, ensure_ascii=False))
    return 0 if result["status"] == "VERIFIED_LOCAL_READONLY_RECEIPT" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "audit_receipts", "main"]
