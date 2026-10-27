"""Bind the candidate catalog to the latest research decisions.

This module joins already saved catalog, common-window panel, and adoption
receipts. It does not rerun a strategy, rank mixed snapshots, or promote a
candidate. Separate gates remain separate records when they have no exact
common-window strategy id.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_candidate_library_binding_v1"
VERIFIED = "VERIFIED_RESEARCH_ONLY_CANDIDATE_LIBRARY"
BLOCKED = "CANDIDATE_LIBRARY_BINDING_BLOCKED"
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")
DEFAULT_ROLLUP = Path("outputs/pipeline_receipts/20261027_research_adoption_rollup_v10.json")
DEFAULT_PANEL = Path("outputs/pipeline_receipts/20261027_full_panel_common_window_strategy_comparison_v1.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261027_candidate_library_binding_v1.json")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"binding input must be a JSON object: {path}")
    return value


def _ref(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def _require_boundary(name: str, value: Mapping[str, Any]) -> None:
    if value.get("execution_admitted") is not False:
        raise ValueError(f"{name}.execution_admitted must be false")


def _panel_index(panel: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    strategies = panel.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("full panel receipt requires strategies")
    index: dict[str, list[dict[str, Any]]] = {}
    for row in strategies:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            raise ValueError("full panel strategy rows require ids")
        spec_id = row.get("candidate_spec_id")
        if not isinstance(spec_id, str) or not spec_id:
            raise ValueError(f"full panel strategy {row['id']} lacks candidate_spec_id")
        index.setdefault(spec_id, []).append(dict(row))
    return index


def _decision_index(rollup: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = rollup.get("candidate_decisions")
    if not isinstance(rows, list):
        raise ValueError("adoption rollup requires candidate_decisions")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("adoption decision rows must be objects")
        identifier = row.get("strategy_id")
        if identifier is None:
            continue
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("adoption decision strategy_id must be a string or null")
        if identifier in result:
            raise ValueError(f"duplicate adoption decision: {identifier}")
        result[identifier] = dict(row)
    return result


def _separate_gate_for(identifier: str, gates: Mapping[str, Any]) -> tuple[str | None, Any]:
    direct = identifier if identifier in gates else None
    aliases = {
        "stockdb_value_size_activity_2023": "value_size_activity_2023",
    }
    key = direct or aliases.get(identifier)
    return key, deepcopy(gates.get(key)) if key else None


def bind_library(
    catalog_path: str | Path = DEFAULT_CATALOG,
    rollup_path: str | Path = DEFAULT_ROLLUP,
    panel_path: str | Path = DEFAULT_PANEL,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    """Join three saved receipts into one reproducible candidate library."""

    paths = {name: Path(value).expanduser().resolve() for name, value in {
        "catalog": catalog_path, "adoption_rollup": rollup_path, "full_panel": panel_path,
    }.items()}
    for name, path in paths.items():
        if not path.is_file():
            raise ValueError(f"{name} input is missing: {path}")
    catalog = _read(paths["catalog"])
    rollup = _read(paths["adoption_rollup"])
    panel = _read(paths["full_panel"])
    _require_boundary("catalog", catalog)
    _require_boundary("adoption_rollup", rollup)
    _require_boundary("full_panel", panel)
    if rollup.get("status") != "COMPLETED_RESEARCH_ONLY_ADOPTION_ROLLUP":
        raise ValueError("adoption rollup is not a completed research-only rollup")
    if panel.get("status") != "COMPLETED_RESEARCH_ONLY_FULL_PANEL_COMMON_WINDOW":
        raise ValueError("full panel receipt is not a completed research-only comparison")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("catalog requires entries")
    panel_by_spec = _panel_index(panel)
    decisions = _decision_index(rollup)
    gates = rollup.get("separate_gates")
    if not isinstance(gates, Mapping):
        raise ValueError("adoption rollup requires separate_gates")
    bound: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            raise ValueError("catalog entries require ids")
        identifier = str(entry["id"])
        if identifier in seen:
            raise ValueError(f"duplicate catalog entry: {identifier}")
        seen.add(identifier)
        panel_rows = panel_by_spec.get(identifier, [])
        matched_decisions = [decisions[row["id"]] for row in panel_rows if row["id"] in decisions]
        gate_key, gate = _separate_gate_for(identifier, gates)
        if panel_rows:
            panel_binding = {
                "kind": "candidate_spec_id",
                "catalog_id": identifier,
                "panel_strategy_ids": [row["id"] for row in panel_rows],
                "shared_spec_variant": len(panel_rows) > 1,
            }
        else:
            panel_binding = None
        if not matched_decisions and gate is None:
            action = (entry.get("research_selection") or {}).get("decision", "UNASSESSED")
            binding_status = "CATALOG_ONLY_NO_CURRENT_ROLLUP_BINDING"
        elif matched_decisions:
            action = matched_decisions[0].get("research_action")
            binding_status = "COMMON_PANEL_DECISION_BOUND"
        else:
            action = "UNASSESSED_SEPARATE_GATE"
            binding_status = "SEPARATE_GATE_BOUND"
        bound.append({
            "id": identifier,
            "family": entry.get("family"),
            "state": entry.get("state"),
            "comparability_group": entry.get("comparability_group"),
            "catalog_metrics": deepcopy(entry.get("metrics")),
            "catalog_evidence": deepcopy(entry.get("evidence") or []),
            "research_selection": deepcopy(entry.get("research_selection")),
            "binding_status": binding_status,
            "research_action": action,
            "panel_binding": panel_binding,
            "panel_strategies": deepcopy(panel_rows),
            "adoption_decisions": deepcopy(matched_decisions),
            "separate_gate": {"key": gate_key, "value": gate} if gate_key else None,
        })
    output = Path(output_path).expanduser().resolve()
    result = {
        "schema": SCHEMA,
        "status": VERIFIED,
        "execution_admitted": False,
        "strategy_validated": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "sources": {name: _ref(path) for name, path in paths.items()},
        "common_window": deepcopy(panel.get("common_window")),
        "comparison_contract": deepcopy(panel.get("comparison_contract")),
        "candidate_count": len(bound),
        "bound_candidates": bound,
        "adoption_summary": deepcopy(rollup.get("candidate_summary")),
        "separate_gate_keys": sorted(str(key) for key in gates),
        "limitations": [
            "This library joins saved evidence; it does not rerun or rank strategies.",
            "Separate gates remain separate and do not become common-window comparisons.",
            "Research-only evidence is not strategy validation, paper permission, or live execution.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["output"] = str(output)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--rollup", type=Path, default=DEFAULT_ROLLUP)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = bind_library(args.catalog, args.rollup, args.panel, args.output)
    print(json.dumps({
        "status": result["status"], "candidate_count": result["candidate_count"],
        "execution_admitted": False, "output": result["output"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
