"""Bind each candidate strategy to a fixed research configuration.

The registry is a small executable contract between the candidate catalog and
future replays.  It does not run a strategy, rank mixed samples or open an
execution channel.  A candidate may be research-replayable while still being
ineligible for paper promotion; the two gates are kept separate here.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_candidate_strategy_spec_registry_v1"
VERIFIED = "VERIFIED_CANDIDATE_SPEC_REGISTRY"
BLOCKED = "CANDIDATE_SPEC_REGISTRY_BLOCKED"
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")
DEFAULT_SPECS = Path("docs/architecture_v3/candidate_strategy_specs_20261024.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_candidate_spec_registry.json")

REQUIRED_SPEC_FIELDS = (
    "id", "family", "comparability_group", "data_snapshot", "primary_window",
    "portfolio", "cost_profile", "acceptance_gate", "promotion_rule",
)
REQUIRED_PORTFOLIO_FIELDS = (
    "initial_cash_cny", "holdings", "retain_rank", "gross_limit", "single_limit",
    "lot_size_shares",
)
REQUIRED_COST_FIELDS = (
    "commission", "minimum_commission_cny", "sell_tax", "transfer_fee",
    "slippage_bps", "participation",
)
REQUIRED_GATE_FIELDS = (
    "execution_admitted", "strategy_validated", "pit_required_before_promotion",
    "fresh_holdout_required", "source_hashes_required", "shared_engine_ledger_required",
    "replay_allowed", "paper_allowed",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _required(mapping: Mapping[str, Any], fields: tuple[str, ...], prefix: str, issues: list[str]) -> None:
    for field in fields:
        if field not in mapping:
            issues.append(f"{prefix}.{field}")


def validate_registry(catalog: Mapping[str, Any], specs: Mapping[str, Any]) -> dict[str, Any]:
    """Validate catalog/spec identity and fixed execution boundaries."""
    issues: list[str] = []
    if catalog.get("execution_admitted") is not False:
        issues.append("catalog.execution_admitted")
    if specs.get("schema") != SCHEMA:
        issues.append("specs.schema")
    if specs.get("execution_admitted") is not False:
        issues.append("specs.execution_admitted")
    if specs.get("promotion_allowed") is not False:
        issues.append("specs.promotion_allowed")
    catalog_entries = catalog.get("entries")
    spec_entries = specs.get("entries")
    if not isinstance(catalog_entries, list) or not catalog_entries:
        issues.append("catalog.entries")
        catalog_entries = []
    if not isinstance(spec_entries, list) or not spec_entries:
        issues.append("specs.entries")
        spec_entries = []
    catalog_by_id = {row.get("id"): row for row in catalog_entries if isinstance(row, dict)}
    spec_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: list[str] = []
    for row in spec_entries:
        identifier = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier:
            issues.append("spec.id")
            continue
        if identifier in spec_by_id:
            duplicate_ids.append(identifier)
            issues.append(f"spec.duplicate_id.{identifier}")
        spec_by_id[identifier] = row
    for identifier in sorted(set(catalog_by_id) - set(spec_by_id)):
        issues.append(f"missing_spec.{identifier}")
    for identifier in sorted(set(spec_by_id) - set(catalog_by_id)):
        issues.append(f"orphan_spec.{identifier}")
    replay_allowed = 0
    paper_allowed = 0
    groups: Counter[str] = Counter()
    for identifier, spec in spec_by_id.items():
        prefix = f"spec.{identifier}"
        _required(spec, REQUIRED_SPEC_FIELDS, prefix, issues)
        catalog_row = catalog_by_id.get(identifier, {})
        if spec.get("family") != catalog_row.get("family"):
            issues.append(f"{prefix}.family_mismatch")
        if spec.get("comparability_group") != catalog_row.get("comparability_group"):
            issues.append(f"{prefix}.comparability_group_mismatch")
        group = spec.get("comparability_group")
        if isinstance(group, str) and group:
            groups[group] += 1
        snapshot = spec.get("data_snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str) or snapshot.get("source_bound_by_catalog") is not True:
            issues.append(f"{prefix}.data_snapshot")
        window = spec.get("primary_window")
        if not isinstance(window, dict):
            issues.append(f"{prefix}.primary_window")
        else:
            for key in ("kind", "start", "end"):
                if not isinstance(window.get(key), str) or not window[key]:
                    issues.append(f"{prefix}.primary_window.{key}")
            if type(window.get("sessions_expected")) is not int or window["sessions_expected"] <= 0:
                issues.append(f"{prefix}.primary_window.sessions_expected")
        portfolio = spec.get("portfolio")
        if not isinstance(portfolio, dict):
            issues.append(f"{prefix}.portfolio")
        else:
            _required(portfolio, REQUIRED_PORTFOLIO_FIELDS, f"{prefix}.portfolio", issues)
            for key in ("initial_cash_cny", "gross_limit", "single_limit"):
                if not _finite(portfolio.get(key)) or portfolio[key] <= 0:
                    issues.append(f"{prefix}.portfolio.{key}")
            for key in ("holdings", "retain_rank", "lot_size_shares"):
                if type(portfolio.get(key)) is not int or portfolio[key] <= 0:
                    issues.append(f"{prefix}.portfolio.{key}")
            if _finite(portfolio.get("single_limit")) and _finite(portfolio.get("gross_limit")) and portfolio["single_limit"] > portfolio["gross_limit"]:
                issues.append(f"{prefix}.portfolio.single_limit_gt_gross")
        costs = spec.get("cost_profile")
        if not isinstance(costs, dict):
            issues.append(f"{prefix}.cost_profile")
        else:
            _required(costs, REQUIRED_COST_FIELDS, f"{prefix}.cost_profile", issues)
            for key in REQUIRED_COST_FIELDS:
                if not _finite(costs.get(key)) or costs[key] < 0:
                    issues.append(f"{prefix}.cost_profile.{key}")
        gate = spec.get("acceptance_gate")
        if not isinstance(gate, dict):
            issues.append(f"{prefix}.acceptance_gate")
        else:
            _required(gate, REQUIRED_GATE_FIELDS, f"{prefix}.acceptance_gate", issues)
            for key in REQUIRED_GATE_FIELDS:
                if not isinstance(gate.get(key), bool):
                    issues.append(f"{prefix}.acceptance_gate.{key}")
            if gate.get("execution_admitted") is not False or gate.get("strategy_validated") is not False:
                issues.append(f"{prefix}.acceptance_gate.open")
            if gate.get("replay_allowed") is True:
                replay_allowed += 1
            if gate.get("paper_allowed") is True:
                paper_allowed += 1
            if gate.get("replay_allowed") is False and not isinstance(gate.get("stop_reason"), str):
                issues.append(f"{prefix}.acceptance_gate.stop_reason")
        promotion = spec.get("promotion_rule")
        if not isinstance(promotion, dict) or promotion.get("promotion_allowed") is not False:
            issues.append(f"{prefix}.promotion_rule.closed")
        if spec.get("execution_admitted") is not False or spec.get("live_channel_allowed") is not False:
            issues.append(f"{prefix}.live_channel")
    return {
        "schema": SCHEMA,
        "status": VERIFIED if not issues else BLOCKED,
        "catalog_entries": len(catalog_entries),
        "spec_entries": len(spec_entries),
        "unique_spec_ids": len(spec_by_id),
        "duplicate_ids": duplicate_ids,
        "comparability_groups": dict(sorted(groups.items())),
        "replay_allowed_count": replay_allowed,
        "paper_allowed_count": paper_allowed,
        "issues": issues,
        "execution_admitted": False,
        "promotion_allowed": False,
        "client_interacted": False,
        "orders_created": False,
        "limitations": [
            "This registry binds fixed research inputs and gates; it does not run a strategy or establish PIT validity.",
            "Diagnostics, paper shadow paths and promotion remain separate; blocked candidates retain their stop reason.",
            "No login, prefill, submit, cancel, transfer or paid data action is attempted.",
        ],
    }


def run(catalog_path: Path = DEFAULT_CATALOG, specs_path: Path = DEFAULT_SPECS,
        output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    catalog_path, specs_path, output_path = catalog_path.resolve(), specs_path.resolve(), output_path.resolve()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    specs = json.loads(specs_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(specs, dict):
        raise ValueError("Candidate catalog and specs must be JSON objects")
    receipt = validate_registry(catalog, specs)
    receipt.update({
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "catalog_path": str(catalog_path), "specs_path": str(specs_path),
        "source_hashes": {"catalog_sha256": _sha256(catalog_path), "specs_sha256": _sha256(specs_path)},
        "receipt_path": str(output_path),
    })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--specs", type=Path, default=DEFAULT_SPECS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    receipt = run(args.catalog, args.specs, args.output)
    print(json.dumps({"status": receipt["status"], "catalog_entries": receipt["catalog_entries"],
                      "spec_entries": receipt["spec_entries"], "issues": receipt["issues"],
                      "execution_admitted": receipt["execution_admitted"]}, ensure_ascii=False))
    return 0 if receipt["status"] == VERIFIED else 1


__all__ = ["SCHEMA", "VERIFIED", "BLOCKED", "validate_registry", "run", "main"]

