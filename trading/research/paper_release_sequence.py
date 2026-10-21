"""Create a paper-only sell-first manual release sequence for selected plans."""

import argparse
import csv
import json
from pathlib import Path

from ..routing import route_order


SCHEMA = "paper_manual_release_sequence_v1"


def _read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path, rows):
    fields = list(rows[0]) if rows else ["release_sequence"]
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _key(row):
    return row.get("strategy_id", ""), row.get("symbol", ""), row.get("side", ""), row.get("qty", "")


def build_plan(plan_path):
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    if plan.get("schema") != "offline_selected_strategy_plan_v1" or plan.get("execution_admitted") is not False:
        raise ValueError(f"Only a non-admitted offline strategy plan is supported: {plan_path}")
    root = plan_path.parent
    orders = _read_csv(root / "selected_orders.csv")
    preflight = _read_csv(root / "selected_preflight.csv")
    release = _read_csv(root / "paper_release_decision.csv")
    preflight_by_key = {_key(row): row for row in preflight}
    release_by_key = {_key(row): row for row in release if row.get("row_type") == "order"}
    rows = []
    for source_sequence, order in enumerate(orders, start=1):
        check = preflight_by_key.get(_key(order), {})
        decision = release_by_key.get(_key(order), {})
        status = check.get("preflight_status", "missing")
        side = order.get("side", "")
        route = route_order(order.get("symbol", ""))
        if status != "accepted":
            phase = 3
            release_phase = "blocked_preflight_rejected"
            release_gate = "repair_shared_ledger_preflight_before_any_release"
            required_observation = "repair_shared_ledger_preflight"
        elif route.status != "route_available":
            phase = 3
            release_phase = "blocked_route_unavailable"
            release_gate = "keep_research_only_or_add_an_authorized_supported_route"
            required_observation = "authorized_supported_route"
        elif side == "sell":
            phase = 1
            release_phase = "sell_review_before_buy"
            release_gate = "observed_account_positions_and_sellable_T1"
            required_observation = "account_position;sellable_quantity;T1_status;same_session_quote"
        else:
            phase = 2
            release_phase = "buy_after_sellable_refresh"
            release_gate = "observed_cash_after_sell_review_and_sellable_T1"
            required_observation = "account_cash_after_sell_review;sellable_quantity;T1_status;same_session_quote"
        blocker_reason = decision.get("reason") or route.reason or check.get("preflight_reason") or "paper_release_requires_manual_observation"
        rows.append({
            "source_sequence": source_sequence,
            "strategy_id": order.get("strategy_id", ""),
            "symbol": order.get("symbol", ""),
            "side": side,
            "qty": order.get("qty", ""),
            "price_rmb": order.get("price_rmb", ""),
            "price_basis": "frozen_opening_pack_reference_not_current_rmb",
            "preflight_status": status,
            "paper_release_decision": decision.get("paper_release_decision", "blocked_missing_live_inputs"),
            "route_id": route.route_id,
            "route_channel": route.channel,
            "route_mode": route.mode,
            "route_status": route.status if status == "accepted" else "not_routable_preflight_rejected",
            "route_reason": route.reason,
            "release_phase": release_phase,
            "release_gate": release_gate,
            "required_observation": required_observation,
            "release_blocker_reason": blocker_reason,
            "account_observed": decision.get("account_observed", "false"),
            "quotes_observed": decision.get("quotes_observed", "false"),
            "target_reconciliation_status": decision.get("target_reconciliation_status", "unknown"),
            "release_status": "manual_only_blocked",
            "execution_admitted": "false",
        })
    phase_order = {"sell_review_before_buy": 1, "buy_after_sellable_refresh": 2,
                   "blocked_preflight_rejected": 3, "blocked_route_unavailable": 3}
    rows.sort(key=lambda row: (phase_order.get(row["release_phase"], 4),
                               int(row["source_sequence"])))
    for release_sequence, row in enumerate(rows, start=1):
        row["release_sequence"] = release_sequence
    rows.sort(key=lambda row: int(row["release_sequence"]))
    sell_sequences = [int(row["release_sequence"]) for row in rows
                      if row["release_phase"] == "sell_review_before_buy"]
    buy_sequences = [int(row["release_sequence"]) for row in rows
                     if row["release_phase"] == "buy_after_sellable_refresh"]
    ordering_verified = not sell_sequences or not buy_sequences or max(sell_sequences) < min(buy_sequences)
    if not ordering_verified:
        raise ValueError(f"Sell-before-buy ordering invariant failed: {plan_path}")
    counts = {
        "sell_review_count": sum(row["release_phase"] == "sell_review_before_buy" for row in rows),
        "buy_after_sell_review_count": sum(row["release_phase"] == "buy_after_sellable_refresh" for row in rows),
        "blocked_preflight_count": sum(row["release_phase"] == "blocked_preflight_rejected" for row in rows),
        "blocked_route_count": sum(row["release_phase"] == "blocked_route_unavailable" for row in rows),
        "route_available_count": sum(row["route_status"] == "route_available" for row in rows),
        "route_unavailable_count": sum(row["route_status"] != "route_available" for row in rows),
        "observation_required_count": sum(bool(row.get("required_observation")) for row in rows),
    }
    payload = {
        "schema": SCHEMA,
        "status": "paper_manual_release_sequence_only",
        "strategy_id": plan.get("strategy_id"),
        "execution_day": plan.get("execution_day"),
        "source_plan": str(plan_path),
        "ordering_policy": "sell_review_then_cash_and_T1_refresh_then_buy_release",
        "ordering_verified": True,
        "automatic_strategy_selection": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "execution_admitted": False,
        "counts": counts,
        "rows": rows,
        "not_attempted": [
            "No order form was written and no THS client was opened.",
            "The sequence is an operator ordering constraint, not an approval to submit.",
            "Frozen reference prices are not current RMB quotes.",
        ],
    }
    json_path = root / "paper_manual_release_sequence_v1.json"
    csv_path = root / "paper_manual_release_sequence_v1.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(csv_path, rows)
    return json_path, csv_path, payload


def build(matrix_path):
    matrix_path = Path(matrix_path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8-sig"))
    results = []
    for row in matrix.get("rows", []):
        json_path, csv_path, payload = build_plan(row["plan"])
        results.append({
            "strategy_id": payload["strategy_id"],
            "plan": payload["source_plan"],
            "sequence_json": str(json_path),
            "sequence_csv": str(csv_path),
            "ordering_verified": payload["ordering_verified"],
            **payload["counts"],
            "execution_admitted": False,
        })
    index = {
        "schema": "paper_manual_release_sequence_index_v1",
        "status": "paper_manual_release_sequences_indexed",
        "execution_day": matrix.get("execution_day"),
        "candidate_count": len(results),
        "automatic_strategy_selection": False,
        "execution_admitted": False,
        "rows": results,
    }
    index_path = matrix_path.parent / "paper_manual_release_sequence_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index_path, index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True)
    args = parser.parse_args(argv)
    index_path, index = build(args.matrix)
    print(json.dumps({
        "index": str(index_path),
        "candidate_count": index["candidate_count"],
        "execution_admitted": index["execution_admitted"],
    }, ensure_ascii=False))
    return 0
