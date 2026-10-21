"""Rehearse offline paper-order lifecycle transitions through the shared Ledger."""

import argparse
import csv
import json
from decimal import Decimal
from pathlib import Path

from ..config import StrategyConfig
from ..ledger import Ledger, new_state
from .exchange_calendar import next_exchange_session


SCHEMA = "paper_lifecycle_rehearsal_v1"


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _price_cents(row):
    if row.get("price_cents"):
        return int(row["price_cents"])
    return int(Decimal(str(row["price_rmb"])) * 100)


def _money(cents):
    return round(cents / 100, 2)


def _ledger_summary(book, symbol, day, next_session):
    return {
        "cash_rmb": _money(book.state["cash"]),
        "cash_available_rmb": _money(book.cash_available()),
        "symbol": symbol,
        "quantity": book.quantity(symbol),
        "available_today": book.available(symbol, day),
        "available_next_exchange_session": book.available(symbol, next_session),
        "order_count": len(book.state["orders"]),
        "fill_count": len(book.state["fills"]),
        "event_count": len(book.state["events"]),
    }


def _base_scenario(name, description):
    return {
        "scenario_id": name,
        "description": description,
        "status": "not_run",
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "manual_action": None,
        "reason": None,
    }


def _run_partial_cancel(config, row, day, next_session, next_settlement_session):
    result = _base_scenario(
        "partial_fill_then_cancel",
        "Submit an accepted buy, apply one legal partial fill, then confirm cancellation of the remainder.",
    )
    if not row or int(row["qty"]) < 200:
        result.update(status="unavailable", reason="no_accepted_buy_with_at_least_200_shares")
        return result
    symbol, qty, price = row["symbol"], int(row["qty"]), _price_cents(row)
    book = Ledger(new_state(config.initial_cash), config)
    order_id = "rehearsal:partial_fill_then_cancel"
    order = book.submit(order_id, day, symbol, "buy", qty, price)
    if order["status"] != "submitted":
        result.update(status="failed", reason=f"submit_{order.get('reason') or order['status']}")
        return result
    partial_qty = 100
    book.fill(
        "rehearsal:partial_fill",
        order_id,
        partial_qty,
        price,
        day,
        next_session,
    )
    book.request_cancel(order_id, day)
    book.confirm_cancel(order_id, day)
    observed = book.state["orders"][order_id]
    result.update(
        status="passed" if observed["status"] == "cancelled" and observed["filled"] == partial_qty else "failed",
        reason=None if observed["status"] == "cancelled" else "cancel_transition_incomplete",
        symbol=symbol,
        requested_qty=qty,
        filled_qty=observed["filled"],
        remaining_qty=qty - observed["filled"],
        final_order_status=observed["status"],
        available_day=next_session,
        settlement_day=next_settlement_session,
        ledger=_ledger_summary(book, symbol, day, next_session),
        events=book.state["events"],
    )
    return result


def _run_t1_block(config, row, day, next_session, next_settlement_session):
    result = _base_scenario(
        "t1_sell_blocked",
        "Fill a buy with next-session availability and attempt to sell it on the same session.",
    )
    if not row:
        result.update(status="unavailable", reason="no_accepted_buy_order")
        return result
    symbol, price = row["symbol"], _price_cents(row)
    qty = 100 if int(row["qty"]) >= 100 else int(row["qty"])
    book = Ledger(new_state(config.initial_cash), config)
    buy_id = "rehearsal:t1_buy"
    buy = book.submit(buy_id, day, symbol, "buy", qty, price)
    if buy["status"] != "submitted":
        result.update(status="failed", reason=f"buy_submit_{buy.get('reason') or buy['status']}")
        return result
    book.fill(
        "rehearsal:t1_buy_fill",
        buy_id,
        qty,
        price,
        day,
        next_session,
    )
    sell_id = "rehearsal:t1_same_day_sell"
    sell = book.submit(sell_id, day, symbol, "sell", qty, price)
    sellable_before_next_session = book.available(symbol, next_session)
    next_sell_id = "rehearsal:t1_next_session_sell"
    next_sell = book.submit(next_sell_id, next_session, symbol, "sell", qty, price)
    next_fill_ok = False
    if next_sell["status"] == "submitted":
        book.fill(
            "rehearsal:t1_next_session_sell_fill",
            next_sell_id,
            qty,
            price,
            next_session,
            next_settlement_session,
        )
        next_fill_ok = book.state["orders"][next_sell_id]["status"] == "filled"
    passed = (
        sell["status"] == "rejected"
        and sell.get("reason") == "insufficient_sellable_T1"
        and sellable_before_next_session == qty
        and next_fill_ok
    )
    result.update(
        status="passed" if passed else "failed",
        reason=None if passed else "t1_transition_or_next_session_fill_incomplete",
        symbol=symbol,
        requested_qty=qty,
        same_day_sell_status=sell["status"],
        same_day_sell_reason=sell.get("reason"),
        sellable_before_next_session=sellable_before_next_session,
        next_session_sell_status=next_sell["status"],
        next_session_sell_reason=next_sell.get("reason"),
        next_session_sell_final_status=book.state["orders"][next_sell_id]["status"],
        next_session_sell_settlement_day=next_settlement_session,
        available_day=next_session,
        ledger=_ledger_summary(book, symbol, day, next_session),
        events=book.state["events"],
    )
    return result


def _static_reconciliation_scenarios():
    unknown = _base_scenario(
        "unknown_submission_reconcile_required",
        "Represent an order acknowledgement that is absent or ambiguous after interruption.",
    )
    unknown.update(
        status="manual_reconcile_required",
        reason="no broker acknowledgement or fill receipt is available in offline mode",
        manual_action="reconcile_broker_order_and_fill_state_before_retry_or_cancel",
    )
    mismatch = _base_scenario(
        "prefill_readback_mismatch_stop",
        "Stop release when prefill readback cannot be compared with an observed account and quote.",
    )
    mismatch.update(
        status="blocked_by_unobserved_inputs",
        reason="offline reference payload has no current quote, account or client readback",
        manual_action="stop_and_review_readback_before_any_submission",
    )
    return [unknown, mismatch]


def _write_selection_index(plan_path):
    selection_root = Path(plan_path).parent.parent
    entries = []
    for result_path in sorted(selection_root.glob("*/paper_lifecycle_rehearsal_v1.json")):
        try:
            result = _read_json(result_path)
        except (OSError, json.JSONDecodeError):
            continue
        scenarios = result.get("scenarios") or []
        entries.append({
            "strategy_id": result.get("strategy_id"),
            "artifact": str(result_path),
            "execution_day": result.get("execution_day"),
            "status": result.get("status"),
            "passed_count": sum(row.get("status") == "passed" for row in scenarios),
            "manual_action_count": sum(row.get("status") == "manual_reconcile_required" for row in scenarios),
            "blocked_count": sum(row.get("status") == "blocked_by_unobserved_inputs" for row in scenarios),
            "execution_admitted": bool(result.get("execution_admitted")),
            "account_observed": bool(result.get("account_observed")),
            "quotes_observed": bool(result.get("quotes_observed")),
            "client_interacted": bool(result.get("client_interacted")),
        })
    index = {
        "schema": "paper_lifecycle_rehearsal_index_v1",
        "status": "offline_paper_lifecycle_index",
        "execution_day": entries[0].get("execution_day") if entries else None,
        "candidate_count": len(entries),
        "automatic_strategy_selection": False,
        "execution_admitted": False,
        "entries": entries,
    }
    path = selection_root / "paper_lifecycle_rehearsal_index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build(plan_path, out_dir=None):
    plan_path = Path(plan_path)
    plan = _read_json(plan_path)
    config_values = plan.get("strategy_metadata", {}).get("execution_config") or {}
    config = StrategyConfig(**config_values).validate()
    day = plan["execution_day"]
    next_day, calendar_evidence = next_exchange_session(day)
    if not next_day:
        raise ValueError(
            "Pinned exchange calendar cannot resolve the next paper session: "
            f"{calendar_evidence.get('status')}"
        )
    next_settlement_day, settlement_evidence = next_exchange_session(next_day)
    if not next_settlement_day:
        raise ValueError(
            "Pinned exchange calendar cannot resolve the following paper session: "
            f"{settlement_evidence.get('status')}"
        )
    artifacts = plan.get("selected_artifacts", {})
    orders_path = Path(artifacts.get("orders", plan_path.parent / "selected_orders.csv"))
    preflight_path = Path(artifacts.get("preflight", plan_path.parent / "selected_preflight.csv"))
    orders = _read_csv(orders_path)
    preflight = {
        (row.get("symbol"), row.get("side"), row.get("qty")): row
        for row in _read_csv(preflight_path)
        if row.get("preflight_status") == "accepted"
    }
    accepted = [
        row for row in orders
        if (row.get("symbol"), row.get("side"), row.get("qty")) in preflight
    ]
    accepted_buys = [row for row in accepted if row.get("side") == "buy"]
    partial_candidate = next((row for row in accepted_buys if int(row["qty"]) >= 200), None)
    t1_candidate = accepted_buys[0] if accepted_buys else None
    scenarios = [
        _run_partial_cancel(
            config, partial_candidate, day, next_day, next_settlement_day
        ),
        _run_t1_block(
            config, t1_candidate, day, next_day, next_settlement_day
        ),
        *_static_reconciliation_scenarios(),
    ]
    out = Path(out_dir) if out_dir else plan_path.parent
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "status": "offline_paper_lifecycle_rehearsal",
        "created_from": str(plan_path),
        "strategy_id": plan.get("strategy_id"),
        "execution_day": day,
        "next_exchange_session": next_day,
        "next_settlement_session": next_settlement_day,
        "settlement_day_basis": "pinned_exchange_calendar",
        "calendar_evidence": calendar_evidence,
        "settlement_calendar_evidence": settlement_evidence,
        "accepted_order_count": len(accepted),
        "accepted_buy_count": len(accepted_buys),
        "scenarios": scenarios,
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "submission_attempted": False,
        "not_attempted": [
            "No current market quote was requested or inferred.",
            "No account, broker, or Tonghuashun client state was read or modified.",
            "No order was submitted, cancelled, or reconciled against a real broker.",
        ],
    }
    json_path = out / "paper_lifecycle_rehearsal_v1.json"
    csv_path = out / "paper_lifecycle_rehearsal_v1.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "scenario_id", "status", "reason", "symbol", "requested_qty", "filled_qty",
        "remaining_qty", "final_order_status", "same_day_sell_status", "same_day_sell_reason",
        "manual_action", "execution_admitted", "account_observed", "quotes_observed",
        "client_interacted",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for scenario in scenarios:
            writer.writerow({key: scenario.get(key, "") for key in fields})
    index_path = _write_selection_index(plan_path)
    payload["selection_index"] = str(index_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return json_path, csv_path, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    json_path, csv_path, payload = build(args.plan, args.out)
    print(json.dumps({
        "json": str(json_path),
        "csv": str(csv_path),
        "selection_index": payload.get("selection_index"),
        "scenario_statuses": {row["scenario_id"]: row["status"] for row in payload["scenarios"]},
        "execution_admitted": payload["execution_admitted"],
    }, ensure_ascii=False))
    return 0
