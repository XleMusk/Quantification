"""Materialize paper-only recovery actions for lifecycle exceptions."""

import argparse
import csv
import json
from pathlib import Path


SCHEMA = "paper_recovery_handoff_v1"


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_csv(path, rows):
    fields = list(rows[0]) if rows else ["scenario_id"]
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _recovery_action(scenario_id):
    actions = {
        "partial_fill_then_cancel": (
            "paper_outcome_only_no_real_cancel",
            "Keep the paper partial-fill/cancel result; do not cancel or retry a real order.",
            "false",
            "account_orders_trades",
        ),
        "t1_sell_blocked": (
            "defer_until_explicit_sellable_refresh",
            "Keep the sell blocked until a following-session account observation confirms sellable quantity.",
            "false",
            "account_positions_and_sellable_T1",
        ),
        "unknown_submission_reconcile_required": (
            "stop_and_reconcile_before_retry",
            "Stop all retry/release actions; import current orders and trades and reconcile the original client state first.",
            "false",
            "account_orders_trades",
        ),
        "prefill_readback_mismatch_stop": (
            "stop_partial_form_and_regenerate",
            "Stop after the mismatch; clear or manually inspect the partial form, then regenerate only from fresh account and quote evidence.",
            "false",
            "account_and_same_session_quotes",
        ),
    }
    return actions.get(scenario_id, (
        "manual_review_required",
        "Do not retry automatically; inspect the recorded evidence and obtain an explicit operator decision.",
        "false",
        "operator_review",
    ))


def _required_evidence(required_observation):
    evidence = {
        "account_positions_and_sellable_T1": [
            "account", "cashflows", "positions", "sellable_quantity", "t1_status",
        ],
        "account_orders_trades": [
            "account", "orders", "trades", "cashflows",
        ],
        "account_and_same_session_quotes": [
            "account", "same_session_quotes", "prefill_readback",
        ],
        "operator_review": ["operator_decision"],
        "none": [],
    }
    return evidence.get(required_observation, [required_observation] if required_observation else [])


def _reconciliation_template(rows):
    checks = []
    for row in rows:
        required = row["required_observation"]
        required_evidence = _required_evidence(required)
        checks.append({
            "scenario_id": row["scenario_id"],
            "required_observation": required,
            "required_evidence": required_evidence,
            "pending_evidence": required_evidence,
            "status": "not_required" if required == "none" else "pending_observation",
            "account_observed": False,
            "orders_observed": False,
            "trades_observed": False,
            "cashflows_observed": False,
            "positions_observed": False,
            "sellable_quantity_observed": False,
            "t1_status_observed": False,
            "same_session_quotes_observed": False,
            "prefill_readback_observed": False,
            "retry_permitted": False,
            "execution_admitted": False,
        })
    return {
        "schema": "broker_state_reconciliation_receipt_v1",
        "status": "unobserved_paper_reconciliation_template",
        "checks": checks,
        "account_observed": False,
        "orders_observed": False,
        "trades_observed": False,
        "retry_permitted": False,
        "execution_admitted": False,
        "not_attempted": [
            "No broker account, order or trade state was inferred from the paper lifecycle.",
            "No retry, release, cancellation or submission was attempted.",
        ],
    }


def build_plan(plan_path):
    plan_path = Path(plan_path)
    plan = _read_json(plan_path)
    if plan.get("schema") != "offline_selected_strategy_plan_v1" or plan.get("execution_admitted") is not False:
        raise ValueError(f"Only a non-admitted offline strategy plan is supported: {plan_path}")
    lifecycle_path = plan_path.parent / "paper_lifecycle_rehearsal_v1.json"
    lifecycle = _read_json(lifecycle_path)
    rows = []
    for scenario in lifecycle.get("scenarios", []):
        action, instruction, retry_permitted, required_observation = _recovery_action(scenario.get("scenario_id", ""))
        rows.append({
            "scenario_id": scenario.get("scenario_id", ""),
            "lifecycle_status": scenario.get("status", ""),
            "lifecycle_reason": scenario.get("reason", ""),
            "recovery_action": action,
            "operator_instruction": instruction,
            "retry_permitted": retry_permitted,
            "required_observation": required_observation,
            "execution_admitted": "false",
        })
    unresolved = [row for row in rows if row["lifecycle_status"] != "passed"]
    reconciliation_template = _reconciliation_template(rows)
    payload = {
        "schema": SCHEMA,
        "status": "paper_recovery_handoff_manual_only",
        "strategy_id": plan.get("strategy_id"),
        "execution_day": plan.get("execution_day"),
        "source_plan": str(plan_path),
        "source_lifecycle": str(lifecycle_path),
        "scenario_count": len(rows),
        "unresolved_scenario_count": len(unresolved),
        "retry_permitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "execution_admitted": False,
        "reconciliation_template": reconciliation_template,
        "rows": rows,
        "not_attempted": [
            "No client order was cancelled, retried or resumed.",
            "No broker acknowledgement, order or trade was inferred from paper rehearsal.",
        ],
    }
    json_path = plan_path.parent / "paper_recovery_handoff_v1.json"
    csv_path = plan_path.parent / "paper_recovery_handoff_v1.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(csv_path, rows)
    return json_path, csv_path, payload


def build(matrix_path):
    matrix_path = Path(matrix_path)
    matrix = _read_json(matrix_path)
    results = []
    for row in matrix.get("rows", []):
        json_path, csv_path, payload = build_plan(row["plan"])
        results.append({
            "strategy_id": payload["strategy_id"],
            "handoff_json": str(json_path),
            "handoff_csv": str(csv_path),
            "scenario_count": payload["scenario_count"],
            "unresolved_scenario_count": payload["unresolved_scenario_count"],
            "retry_permitted": payload["retry_permitted"],
            "execution_admitted": payload["execution_admitted"],
        })
    index = {
        "schema": "paper_recovery_handoff_index_v1",
        "status": "paper_recovery_handoffs_indexed",
        "execution_day": matrix.get("execution_day"),
        "candidate_count": len(results),
        "retry_permitted": False,
        "execution_admitted": False,
        "rows": results,
    }
    index_path = matrix_path.parent / "paper_recovery_handoff_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index_path, index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--matrix", help="Recovery matrix containing selected paper plans")
    group.add_argument("--plan", help="One non-admitted selected strategy plan")
    args = parser.parse_args(argv)
    if args.plan:
        json_path, csv_path, payload = build_plan(args.plan)
        print(json.dumps({
            "json": str(json_path),
            "csv": str(csv_path),
            "scenario_count": payload["scenario_count"],
            "unresolved_scenario_count": payload["unresolved_scenario_count"],
            "retry_permitted": payload["retry_permitted"],
            "execution_admitted": payload["execution_admitted"],
        }, ensure_ascii=False))
        return 0
    index_path, index = build(args.matrix)
    print(json.dumps({
        "index": str(index_path),
        "candidate_count": index["candidate_count"],
        "retry_permitted": index["retry_permitted"],
        "execution_admitted": index["execution_admitted"],
    }, ensure_ascii=False))
    return 0
