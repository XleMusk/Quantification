"""Scan implementation gaps against public quant-system capability patterns."""

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .. import settings
from .experiment import write_json


PUBLIC_REFERENCES = {
    "order_lifecycle": "https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/trading-and-orders",
    "broker_reconciliation": "https://www.quantconnect.com/docs/v1/live-trading/live-reconciliation",
    "engine_layers": "https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine",
    "time_frontier": "https://www.quantconnect.com/docs/v1/key-concepts/understanding-time",
    "quant_stackexchange": "https://quant.stackexchange.com/help/on-topic",
    "wilmott": "https://wilmott.com/forum/index.php",
    "elite_trader": "https://www.elitetrader.com/",
}


def _latest_structured_package():
    root = settings.DATA_ROOT / "ths" / "observation_bundle" / "structured"
    candidates = sorted(root.glob("*/structured_observation_package_v1.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            manifest = payload.get("source_manifest")
            if isinstance(manifest, dict):
                from ..ths.structured_observation import verify_source_manifest

                checked = verify_source_manifest(manifest)
                payload["source_manifest_status"] = checked["status"]
                payload["source_manifest_issues"] = checked["issues"]
            else:
                payload.setdefault("source_manifest_status", "not_recorded")
                payload.setdefault("source_manifest_issues", [])
            return path, payload
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return None, {}


def _latest_auto_cycle():
    root = settings.ROOT / "outputs" / "auto_research_cycles"
    candidates = sorted(root.glob("*/run.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            return path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return None, {}


def _latest_idea_frontier():
    root = settings.ROOT / "outputs" / "auto_research_frontiers"
    candidates = sorted(root.glob("*/run.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            return path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return None, {}


def _latest_research_scope():
    root = settings.ROOT / "outputs" / "research_scopes"
    candidates = sorted(root.glob("*/run.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") == "research_scope_v1":
                return path, payload
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return None, {}


def _latest_factor_current_score():
    root = settings.ROOT / "outputs" / "factor_current_scores"
    candidates = sorted(root.glob("*/run.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") == "completed_factor_candidate_current_score":
                return path, payload
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return None, {}


def _latest_function_matrix():
    """Load the latest institutional capability matrix from a paper desk.

    The matrix is intentionally evidence-backed: it is a view of the existing
    paper/read-only consumer, not a claim that broker or live-feed capabilities
    are complete.  Standalone trader-desk snapshots and opening-session packs
    are both first-class consumers; otherwise a fresh offline desk snapshot
    can be silently hidden by an older opening-session directory.
    """
    roots = (
        settings.DATA_ROOT / "research" / "opening_session_runs",
        settings.ROOT / "outputs" / "trader_desk_snapshots",
    )
    candidates = []
    for root in roots:
        candidates.extend(root.glob("*/offline_desk*/function_matrix.csv"))
        candidates.extend(root.glob("*/function_matrix.csv"))
    candidates = sorted(set(candidates),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            if rows:
                return path, rows
        except (OSError, UnicodeError, csv.Error):
            continue
    return None, []


def _current_bundle_preview():
    """Read the current standard THS bundle without importing or mutating it."""
    root = (settings.DATA_ROOT / "ths" / "observation_bundle").resolve()
    paths = {
        "account": root / "account_observation.json",
        "orders": root / "orders.csv",
        "trades": root / "trades.csv",
        "cashflows": root / "cashflows.csv",
    }
    profile = None
    profile_path = root / "profile.json"
    if profile_path.is_file():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            if not isinstance(profile, dict):
                profile = None
        except (OSError, UnicodeError, json.JSONDecodeError):
            profile = None
    try:
        from ..ths.observation_preview import preview_bundle

        preview = preview_bundle(
            paths["account"], paths["orders"], paths["trades"],
            paths["cashflows"], profile=profile,
        )
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        preview = {
            "status": "validation_error",
            "error": f"{type(error).__name__}: {error}",
        }
    return root, preview


def _latest_gui_prefill_profile():
    """Find the newest external profile that might describe GUI prefill controls."""
    root = (settings.DATA_ROOT / "ths").resolve()
    candidates = sorted(root.glob("*profile*.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return path, payload
    return None, {}


def _file_contains(relative_path, text):
    path = settings.ROOT / relative_path
    try:
        return text in path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def scan():
    package_path, package = _latest_structured_package()
    cycle_path, cycle = _latest_auto_cycle()
    frontier_path, frontier = _latest_idea_frontier()
    research_scope_path, research_scope = _latest_research_scope()
    factor_score_path, factor_score = _latest_factor_current_score()
    function_matrix_path, function_matrix = _latest_function_matrix()
    current_bundle_root, current_bundle = _current_bundle_preview()
    gui_profile_path, gui_profile = _latest_gui_prefill_profile()
    from ..ths.adapter import gui_prefill_profile_status
    gui_profile_status = gui_prefill_profile_status(gui_profile)
    gui_missing_fields = gui_profile_status["missing_fields"]
    gui_prefill_ready = gui_profile_status["status"] == "ready"
    complete_native = bool(
        package.get("status") == "complete_structured_observation_package"
        and package.get("ready_for_ledger_reconciliation") is True
        and package.get("source_scope") == "persistent_user_export"
        and package.get("source_manifest_status") == "verified"
        and all(bool(package.get(key)) for key in (
            "account_observed", "orders_observed", "trades_observed", "cashflows_observed",
        ))
    )
    cycle_complete = any(item.get("stage") == "attribution" and item.get("status") == "reused"
                         for item in cycle.get("stages", []))
    checks = [
        {
            "id": "unified_order_ledger",
            "area": "execution",
            "priority": "P0",
            "capability": "One order/fill/account path with explicit lifecycle and same-ledger import",
            "status": "implemented" if _file_contains("trading/ths/ledger_import.py", "def preview_plan") else "missing",
            "evidence": ["trading/ths/ledger_import.py", "trading/ths/journal.py"],
            "reference": PUBLIC_REFERENCES["order_lifecycle"],
        },
        {
            "id": "structured_ths_four_table_input",
            "area": "execution",
            "priority": "P0",
            "capability": "Persistent account, order, trade and cumulative cashflow evidence",
            "status": "implemented" if (
                complete_native
                and current_bundle.get("status") == "bundle_ready_for_reconciliation"
            ) else "partial_paper_only",
            "evidence": [str(package_path) if package_path else "UNKNOWN",
                         "trading/ths/structured_observation.py",
                         "trading/ths/observation_preview.py"],
            "detail": (
                "The native receipt adapter and reconciliation rules exist; the current standard THS bundle "
                "is checked directly so an older isolated package cannot mask blank or stale user exports."
            ),
            "current_bundle_root": str(current_bundle_root),
            "current_bundle_status": current_bundle.get("status"),
            "current_bundle_error": current_bundle.get("error"),
            "source_scope": package.get("source_scope"),
            "source_manifest_status": package.get("source_manifest_status", "not_recorded"),
            "source_manifest_issues": package.get("source_manifest_issues") or [],
            "reference": PUBLIC_REFERENCES["broker_reconciliation"],
        },
        {
            "id": "native_receipt_normalization",
            "area": "execution",
            "priority": "P0",
            "capability": "Normalize native structured THS receipts into the standard four-table journal contract",
            "status": "implemented_readonly_adapter" if _file_contains(
                "trading/ths/structured_observation.py",
                "def build_observations_from_native_index",
            ) else "missing",
            "evidence": ["trading/ths/structured_observation.py",
                         "trading/ths/cli.py#preview-native-observation-bundle",
                         "trading/tests/test_ths_bundle_export.py"],
            "detail": "The adapter rejects screenshot/OCR metadata and requires explicit account/order/trade/cumulative-cash fields; it does not submit orders.",
            "reference": PUBLIC_REFERENCES["broker_reconciliation"],
        },
        {
            "id": "ths_gui_prefill_mapping",
            "area": "execution",
            "priority": "P0",
            "capability": "Verified THS GUI symbol/price/quantity mapping for bounded prefill and readback",
            "status": "implemented_verified_local" if gui_prefill_ready else "partial_unverified_profile",
            "evidence": [str(gui_profile_path) if gui_profile_path else "UNKNOWN",
                         "trading/ths/adapter.py#validate_gui_prefill_profile",
                         "trading/ths/profile.example.json"],
            "profile_schema": gui_profile.get("schema"),
            "profile_status": gui_profile_status["status"],
            "verified_local": gui_profile_status["verified_local"],
            "missing_fields": gui_missing_fields,
            "detail": (
                "The current external profile is an observation-only mapping or lacks exact GUI controls; "
                "it cannot drive prefill. A verified local GUI profile is required and selectors are not inferred."
                if not gui_prefill_ready else
                "Verified local GUI controls are available; automatic submission remains disabled."
            ),
            "reference": PUBLIC_REFERENCES["order_lifecycle"],
        },
        {
            "id": "account_risk_and_t1",
            "area": "risk",
            "priority": "P0",
            "capability": "Cash/frozen funds, exposure concentration, T+1 sellability and admission blockers",
            "status": "implemented_paper_only" if _file_contains("trading/ths/account_risk.py", "def build_account_risk_view") else "missing",
            "evidence": ["trading/ths/account_risk.py", "trading/api.py#/operator-package/account-risk"],
            "detail": "Requires a persistent native account export before it can describe the real account.",
            "reference": PUBLIC_REFERENCES["engine_layers"],
        },
        {
            "id": "account_order_risk_gate",
            "area": "risk",
            "priority": "P0",
            "capability": "Projected cash, concentration, gross exposure and sellability checks for one proposed order batch",
            "status": "implemented_readonly_paper" if _file_contains("trading/ths/account_risk.py", "def evaluate_order_risk") else "missing",
            "evidence": ["trading/ths/account_risk.py", "trading/ths/cli.py#preview-account-order-risk", "trading/api.py#/operator-package/account-order-risk"],
            "detail": "The gate is read-only and paper-only; it does not authorize submission or replace the same-Ledger reconciliation preview.",
            "reference": PUBLIC_REFERENCES["order_lifecycle"],
        },
        {
            "id": "restart_and_reconciliation",
            "area": "reconciliation",
            "priority": "P0",
            "capability": "Restart-safe open-order/fill/balance reconciliation without duplicate application",
            "status": "implemented_paper_only" if _file_contains("trading/ths/ledger_import.py", "def reconcile_session") else "missing",
            "evidence": ["trading/ths/ledger_import.py", "trading/ths/structured_observation.py"],
            "detail": "Same-ledger paper path is implemented; broker-native persistent evidence is still absent.",
            "reference": PUBLIC_REFERENCES["broker_reconciliation"],
        },
        {
            "id": "event_time_frontier",
            "area": "research",
            "priority": "P1",
            "capability": "Explicit event-time frontier and lookahead-safe live/replay ordering",
            "status": ("implemented_replay_paper_no_live_feed"
                       if _file_contains("trading/event_frontier.py", "def validate_signal_frontier")
                       and _file_contains("trading/engine.py", "validate_signal_frontier")
                       else "partial_replay_only"),
            "evidence": ["trading/event_frontier.py", "trading/engine.py", "trading/research/experiment.py"],
            "detail": "Replay and paper target timing is receipt-checked; a continuous event-time live feed is not connected.",
            "reference": PUBLIC_REFERENCES["time_frontier"],
        },
        {
            "id": "automatic_idea_discovery",
            "area": "research",
            "priority": "P1",
            "capability": "Discover fixed candidate ideas, replay them through the shared Ledger, and retain negative evidence",
            "status": "implemented_research_only" if cycle_complete or frontier_path else "partial",
            "evidence": ["trading/research/auto_idea_cycle.py", "trading/research/idea_frontier.py",
                         str(cycle_path) if cycle_path else "UNKNOWN",
                         str(frontier_path) if frontier_path else "UNKNOWN"],
            "detail": "Fixed candidates are discovered by input snapshot, missing rules can be replayed one at a time, and failed evidence is retained; no return-based promotion is performed.",
            "reference": PUBLIC_REFERENCES["engine_layers"],
        },
        {
            "id": "pit_financials",
            "area": "research",
            "priority": "P1",
            "capability": "Point-in-time financial inputs with disclosure-date eligibility",
            "status": "blocked_source_partial",
            "evidence": ["D:/QuantificationData/trading/research/financials/", "D:/QuantificationData/trading/research/"],
            "detail": "BaoStock is blacklisted; existing frozen research remains diagnostic and does not fabricate missing rows.",
            "reference": PUBLIC_REFERENCES["engine_layers"],
        },
        {
            "id": "security_master_and_scope",
            "area": "research",
            "priority": "P1",
            "capability": "Dated research scope with permanent security identity and explicit admission boundary",
            "status": ("implemented_research_scope_no_pit" if research_scope_path
                       and research_scope.get("execution_admitted") is False
                       and research_scope.get("pit_qualified") is False else "missing"),
            "evidence": [str(research_scope_path) if research_scope_path else "UNKNOWN",
                         "trading/research/instrument_master.py",
                         "trading/research/bulk_feed.py"],
            "detail": ("The scope receipt now constrains replay membership and preserves unknown/missing identities; "
                       "PIT status history, issuer lineage and independent exchange reconciliation remain unavailable."),
            "reference": PUBLIC_REFERENCES["time_frontier"],
        },
        {
            "id": "frozen_factor_current_score",
            "area": "research",
            "priority": "P1",
            "capability": "Frozen factor model current-session inference for paper target construction",
            "status": "implemented_research_only_no_live_feed" if factor_score_path else "missing",
            "evidence": [str(factor_score_path) if factor_score_path else "UNKNOWN",
                         "trading/research/factor_current_score.py"],
            "detail": ("Current ranking is reconstructed from frozen training rows with no new labels; "
                       "it is not a broker quote, account sizing input or execution admission."),
            "reference": PUBLIC_REFERENCES["engine_layers"],
        },
        {
            "id": "production_monitoring",
            "area": "operations",
            "priority": "P2",
            "capability": "Continuous broker heartbeat, stale-data alarms and operator handoff",
            "status": "partial_read_only_monitoring",
            "evidence": [
                "trading/api.py#/ths-observation-health",
                "trading/ths/cli.py#observation-health",
                "D:/QuantificationData/trading/ths/health/observation_health_20261012.json",
                "trading/ths/README.md",
            ],
            "detail": "Saved-observation freshness, structured-evidence gaps and operator handoff are now exposed through the read-only API/CLI; continuous live broker transport and heartbeat remain intentionally disabled.",
            "reference": PUBLIC_REFERENCES["broker_reconciliation"],
        },
    ]
    matrix_gaps = [
        {
            "function_id": row.get("function_id", ""),
            "priority": row.get("priority", ""),
            "local_state": row.get("local_state", ""),
            "next_gap": row.get("next_gap", ""),
            "next_implementation": row.get("next_implementation", ""),
            "delivery_stage": row.get("delivery_stage", ""),
            "workstream": row.get("workstream", ""),
        }
        for row in function_matrix
        if row.get("next_gap") or row.get("next_implementation")
    ]
    if research_scope_path:
        for row in matrix_gaps:
            if row["function_id"] == "security_master_and_scope":
                row["local_state"] = "research_scope_consumer_ready"
                row["next_gap"] = "PIT status history, issuer lineage and independent exchange reconciliation"
                row["next_implementation"] = "add reviewed PIT evidence without changing account admission"
    if factor_score_path:
        for row in matrix_gaps:
            if row["function_id"] == "frozen_factor_current_score":
                row["local_state"] = "current_research_score_ready"
                row["next_gap"] = "new labels, current live data and independent validation before promotion"
                row["next_implementation"] = "keep score separate from quotes, account sizing and submission"
    matrix_gaps.sort(key=lambda row: (
        {"P0": 0, "P1": 1, "P2": 2}.get(row["priority"], 9),
        row["function_id"],
    ))
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    queued = {}
    for item in checks:
        if (item["status"].startswith("partial")
                or item["status"].startswith("blocked")
                or item["status"].startswith("missing")):
            queued[item["id"]] = {
                "id": item["id"],
                "priority": item["priority"],
                "source_order": 0,
            }
    for row in matrix_gaps:
        queued.setdefault(row["function_id"], {
            "id": row["function_id"],
            "priority": row["priority"],
            "source_order": 1,
        })
    ordered_queue = sorted(
        queued.values(),
        key=lambda item: (
            priority_rank.get(item["priority"], 9),
            item["source_order"],
            item["id"],
        ),
    )
    priority_queue = [item["id"] for item in ordered_queue]
    priority_queue_by_priority = {
        priority: [item["id"] for item in ordered_queue if item["priority"] == priority]
        for priority in ("P0", "P1", "P2")
    }
    return {
        "schema": "quant_capability_gap_scan_v1",
        "status": "completed_capability_gap_scan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "public_references": PUBLIC_REFERENCES,
            "latest_structured_package": str(package_path) if package_path else None,
            "latest_auto_idea_cycle": str(cycle_path) if cycle_path else None,
            "latest_fixed_idea_frontier": str(frontier_path) if frontier_path else None,
            "latest_research_scope": str(research_scope_path) if research_scope_path else None,
            "latest_factor_current_score": str(factor_score_path) if factor_score_path else None,
            "current_ths_bundle_root": str(current_bundle_root),
            "current_ths_bundle_status": current_bundle.get("status"),
            "latest_gui_prefill_profile": str(gui_profile_path) if gui_profile_path else None,
            "latest_institutional_function_matrix": (
                str(function_matrix_path) if function_matrix_path else None
            ),
        },
        "checks": checks,
        "institutional_function_matrix": {
            "schema": "institutional_function_matrix_v1",
            "status": "paper_matrix_available" if function_matrix_path else "missing",
            "artifact": str(function_matrix_path) if function_matrix_path else None,
            "function_count": len(function_matrix),
            "gap_count": len(matrix_gaps),
            "gaps_by_priority": {
                priority: sum(1 for row in matrix_gaps if row["priority"] == priority)
                for priority in ("P0", "P1", "P2")
            },
            "gaps": matrix_gaps,
            "execution_admitted": False,
        },
        "priority_queue": priority_queue,
        "priority_queue_by_priority": priority_queue_by_priority,
        "execution_admitted": False,
        "real_order_submission": False,
    }


def run():
    identifier = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "capability_gap_scans" / identifier
    out.mkdir(parents=True, exist_ok=False)
    result = {"id": identifier, **scan()}
    write_json(out / "run.json", result)
    matrix = result["institutional_function_matrix"]
    matrix_lines = [
        f"- {row['priority']} {row['function_id']}: {row['local_state']} -> {row['next_gap']}"
        for row in matrix["gaps"]
    ]
    (out / "REPORT.md").write_text(
        "\n".join([
            "# Quant Capability Gap Scan", "", "Public reference comparison; no execution admission.", "",
            *[f"- {item['priority']} {item['id']}: {item['status']}" for item in result["checks"]],
            "", "## Institutional function matrix", "",
            *matrix_lines,
            "", "Priority queue: " + ", ".join(result["priority_queue"]), "",
        ]), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return out, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
