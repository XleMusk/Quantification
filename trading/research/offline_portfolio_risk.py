"""Build an offline candidate portfolio risk and cash-usage comparison."""

import argparse
import csv
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path


SCHEMA = "offline_portfolio_risk_summary_v1"


def _decimal(value, default="0"):
    try:
        return Decimal(str(value or default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _money(value):
    return f"{Decimal(value).quantize(Decimal('0.01')):.2f}"


def _ratio(value):
    return f"{Decimal(value):.6f}"


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _preflight_metrics(path):
    rows = _read_csv(path) if Path(path).exists() else []
    accepted = [row for row in rows if row.get("preflight_status") == "accepted"]
    largest = max((_decimal(row.get("estimated_notional_rmb")) for row in accepted), default=Decimal("0"))
    return len(rows), len(accepted), largest


def _candidate_row(matrix_row):
    plan_path = Path(matrix_row["plan"])
    plan = _read_json(plan_path)
    sizing = plan.get("paper_account_sizing") or {}
    initial_cash = _decimal(sizing.get("initial_cash_rmb"))
    buy_gross = _decimal(sizing.get("accepted_buy_gross_rmb"))
    sell_gross = _decimal(sizing.get("accepted_sell_gross_rmb"))
    fees = _decimal(sizing.get("fees_rmb"))
    cash_after_buys = _decimal(sizing.get("cash_after_buys_only_rmb"))
    projected_cash = _decimal(sizing.get("projected_cash_after_paper_orders_rmb"))
    preflight_path = plan_path.parent / "selected_preflight.csv"
    order_count, accepted_count, largest_order = _preflight_metrics(preflight_path)
    rejected_count = max(0, order_count - accepted_count)
    buy_utilization = buy_gross / initial_cash if initial_cash else Decimal("0")
    cash_remaining_ratio = cash_after_buys / initial_cash if initial_cash else Decimal("0")
    total_order_ratio = (buy_gross + sell_gross) / initial_cash if initial_cash else Decimal("0")
    largest_order_ratio = largest_order / initial_cash if initial_cash else Decimal("0")

    flags = ["account_not_observed", "quotes_not_observed", "execution_disabled"]
    if rejected_count:
        flags.append("paper_preflight_rejections_present")
    if buy_utilization >= Decimal("0.50"):
        flags.append("buy_cash_usage_high_review_capacity")
    if sell_gross:
        flags.append("sellable_T1_position_check_required")
    if largest_order_ratio >= Decimal("0.20"):
        flags.append("single_order_size_at_or_above_20pct_cash")

    if rejected_count:
        next_action = "resolve paper preflight rejections and verify holdings before any manual release"
    elif buy_utilization >= Decimal("0.50"):
        next_action = "verify observed cash capacity and stage the buy basket before manual release"
    elif sell_gross:
        next_action = "verify observed sellable T+1 positions before manual release"
    else:
        next_action = "refresh observed account and quote inputs before manual release"

    return {
        "strategy_id": str(matrix_row.get("strategy_id", "")),
        "plan": str(plan_path),
        "execution_day": str(plan.get("execution_day", "")),
        "initial_cash_rmb": _money(initial_cash),
        "order_count": order_count,
        "paper_preflight_accepted": accepted_count,
        "paper_preflight_rejected": rejected_count,
        "paper_accepted_buy_gross_rmb": _money(buy_gross),
        "paper_accepted_sell_gross_rmb": _money(sell_gross),
        "paper_fees_rmb": _money(fees),
        "paper_cash_after_buys_only_rmb": _money(cash_after_buys),
        "paper_projected_cash_after_orders_rmb": _money(projected_cash),
        "buy_cash_utilization": _ratio(buy_utilization),
        "cash_remaining_after_buys_ratio": _ratio(cash_remaining_ratio),
        "gross_order_ratio": _ratio(total_order_ratio),
        "largest_accepted_order_rmb": _money(largest_order),
        "largest_order_cash_ratio": _ratio(largest_order_ratio),
        "review_state": "offline_reference_only_manual_review_required",
        "risk_flags": ";".join(flags),
        "next_required_action": next_action,
        "account_observed": "false",
        "quotes_observed": "false",
        "execution_admitted": "false",
    }


def build(matrix, out_dir=None):
    matrix_path = Path(matrix)
    matrix_payload = _read_json(matrix_path)
    rows = [_candidate_row(row) for row in matrix_payload.get("rows", [])]
    rows.sort(key=lambda row: Decimal(row["buy_cash_utilization"]), reverse=True)
    out = Path(out_dir) if out_dir else matrix_path.parent
    out.mkdir(parents=True, exist_ok=True)
    initial_cash_values = {_decimal(row["initial_cash_rmb"]) for row in rows}
    shared_initial_cash = next(iter(initial_cash_values), Decimal("0"))
    shared_buy_gross = sum((_decimal(row["paper_accepted_buy_gross_rmb"]) for row in rows), Decimal("0"))
    shared_sell_gross = sum((_decimal(row["paper_accepted_sell_gross_rmb"]) for row in rows), Decimal("0"))
    shared_fees = sum((_decimal(row["paper_fees_rmb"]) for row in rows), Decimal("0"))
    shared_cash_after_buys = shared_initial_cash - shared_buy_gross - shared_fees
    shared_account_scenario = {
        "scenario_id": "all_candidates_on_one_shared_account",
        "candidate_count": len(rows),
        "configured_initial_cash_rmb": _money(shared_initial_cash),
        "aggregate_buy_gross_rmb": _money(shared_buy_gross),
        "aggregate_sell_gross_rmb": _money(shared_sell_gross),
        "aggregate_fees_rmb": _money(shared_fees),
        "cash_after_all_candidate_buys_rmb": _money(shared_cash_after_buys),
        "capacity_status": "rejected_shared_account_conflict" if shared_cash_after_buys < 0 else "review_required",
        "reason": "candidate strategies are alternatives; their paper orders must not be merged into the sole account ledger",
        "account_observed": False,
        "execution_admitted": False,
    }
    json_path = out / "offline_portfolio_risk_summary_v1.json"
    csv_path = out / "offline_portfolio_risk_summary_v1.csv"
    payload = {
        "schema": SCHEMA,
        "status": "offline_reference_risk_comparison",
        "source_matrix": str(matrix_path),
        "execution_day": matrix_payload.get("execution_day"),
        "candidate_count": len(rows),
        "sort_basis": "buy_cash_utilization_descending_for_review_only",
        "automatic_strategy_selection": False,
        "shared_account_scenario": shared_account_scenario,
        "account_observed": False,
        "quotes_observed": False,
        "execution_admitted": False,
        "rows": rows,
        "not_attempted": [
            "No live cash, positions, sellable T+1 quantity or quotes were read.",
            "No risk row authorizes prefill, order release, submission or cancellation.",
            "Paper prices and configured initial cash are not current account values.",
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = list(rows[0]) if rows else ["strategy_id"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    json_path, csv_path, payload = build(args.matrix, args.out)
    print(json.dumps({
        "json": str(json_path),
        "csv": str(csv_path),
        "candidate_count": payload["candidate_count"],
        "execution_admitted": payload["execution_admitted"],
    }, ensure_ascii=False))
    return 0
