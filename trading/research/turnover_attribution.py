"""Read saved parent orders and explain mechanical turnover, not economic alpha."""
import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import json
import math
from pathlib import Path
import shutil
import uuid

import pandas as pd

from .. import replay_reports, settings
from ..config import StrategyConfig
from ..rules import cents, execution_rule, notional_cents
from .experiment import write_json
from .loss_attribution import asset_pnl, order_costs


LIMITS = [
    "Mechanical entry/exit/reweight labels are not economic causes, rank replacements or fixed-horizon expiries; missing strategy reasons remain UNKNOWN",
    "Legacy accounts lack per-attempt opening equity and desired quantities; their ideal/legal/actual quantity gaps are UNKNOWN, not reconstructed from terminal close",
    "The saved-order reader projects only share quantities, not cash or hypothetical fills. A separately requested gap observation uses the original Engine/Ledger in isolation",
    "Parent-order cumulative fees are counted once, never once per partial-fill callback",
    "Requested-minus-filled quantities include rejected/cancelled orders; they do not measure an executable opportunity or its PnL",
    "16,666.67CNY at the default fee schedule is a commission crossover, NOT an optimal trade threshold",
    "Inspected historical research only; existing source, PIT, staleness and prospective-validation limitations remain. NO_TRADE",
    "Daily invested value is saved equity minus cash and unpaid receivables; all-session and nonempty-session means are separate",
    "Target realization values use each saved execution attempt's mark, NOT daily sector holdings, execution-open guarantees or counterfactual PnL",
    "Legacy rounded_target and rounding_gap fields mean engine-selected target and signed budget-minus-target; open-lot allocation may round up, so a negative gap is not a floor error",
    "Target-budget realization covers positive-weight targets; explicit zero-budget slots are counted separately, not assumed filled or included as selected holdings",
    "Industry concentration describes target weights only on exactly matched historical decision dates; no forward/backfill or historical PIT certificate",
    "Close-lot transitions compare saved estimates with attempt marks: exact-ratio and decimal/binary floors are arithmetic diagnostics, not executable alternative quantities or PnL",
]


def daily_exposure(marks):
    frame = pd.DataFrame(marks).copy()
    if frame.empty or not {"date", "equity", "cash", "receivables", "holdings", "stale_symbols"} <= set(frame):
        raise ValueError("Saved daily cash, receivables and holdings are required")
    for key in ("equity", "cash", "receivables", "holdings"):
        frame[key] = pd.to_numeric(frame[key], errors="raise")
        if not frame[key].map(math.isfinite).all():
            raise ValueError("Nonfinite saved exposure amount")
    if (frame.equity.le(0).any() or frame[["cash", "receivables", "holdings"]].lt(0).any().any()
            or frame.holdings.mod(1).ne(0).any()
            or frame.date.tolist() != sorted(set(frame.date))):
        raise ValueError("Invalid saved exposure amounts or dates")
    invested = frame.equity.map(cents) - frame.cash.map(cents) - frame.receivables.map(cents)
    if invested.lt(0).any() or (invested.ne(0) != frame.holdings.gt(0)).any():
        raise ValueError("Saved invested value and nonempty holdings disagree")
    frame["invested_value_cny"] = invested / 100
    frame["invested_weight"] = invested / frame.equity.map(cents)
    frame["cash_weight"] = frame.cash / frame.equity
    frame["unpaid_receivable_weight"] = frame.receivables / frame.equity
    frame["stale_valuation"] = frame.stale_symbols.map(bool)
    active = frame.loc[frame.holdings.gt(0)]
    summary = {"sessions": len(frame), "nonempty_sessions": len(active),
        "empty_holding_days": int(frame.holdings.eq(0).sum()),
        "mean_invested_weight": float(frame.invested_weight.mean()),
        "mean_nonempty_invested_weight": float(active.invested_weight.mean()) if len(active) else None,
        "maximum_invested_weight": float(frame.invested_weight.max()),
        "mean_holdings": float(frame.holdings.mean()), "maximum_holdings": int(frame.holdings.max()),
        "stale_valuation_days": int(frame.stale_valuation.sum())}
    return frame.drop(columns="stale_symbols"), summary


def target_realization(account, traces, config, *, industries=None, profiles=None):
    targets = [e for e in account["events"] if e["kind"] == "targets_generated"]
    lookup = {(e["day"], e["symbol"]): e for e in traces}
    if len(lookup) != len(traces) or len({e["signal_date"] for e in targets}) != len(targets):
        raise ValueError("Duplicate saved targets or execution attempts")
    industries = pd.DataFrame(columns=["date", "symbol", "industry"]) if industries is None else industries
    if industries.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate dated industry identity")
    sector = {(e["date"], e["symbol"]): e["industry"] for e in industries.to_dict("records")}
    members, anchors = [], []
    for event in targets:
        day, execution_day, weights = event["signal_date"], event["execution_not_before"], event["targets"]
        if (event["day"] != day or execution_day <= day or len(weights) > config.holdings
                or any(not math.isfinite(w) or w < 0 or w > config.single_limit for w in weights.values())
                or sum(weights.values()) > config.gross_limit + 1e-9):
            raise ValueError("Saved target timing or constraints changed")
        selected = []
        for symbol, weight in sorted(weights.items()):
            if weight == 0:
                continue
            trace = lookup.get((execution_day, symbol))
            row = {"signal_date": day, "execution_date": execution_day, "symbol": symbol,
                   "target_weight": weight, "industry": sector.get((day, symbol)) or None,
                   "trace_status": "missing_saved_attempt", "mechanical_reason": None,
                   "has_execution_open": None, "target_budget_cny": None, "minimum_buy_value_cny": None,
                   "below_minimum_target_budget": None, "rounded_target_value_cny": None,
                   "quantity_rule_only_target_value_cny": None, "minimum_rule_gap_cny": None,
                   "post_quantity_rule_gap_cny": None,
                   "selected_held_after_value_cny": None, "rounding_gap_cny": None,
                   "execution_quantity_gap_cny": None, "total_target_gap_cny": None,
                   "open_equity_cny": None, "engine_target_quantity": None, "held_after": None}
            if trace:
                row.update(mechanical_reason=trace["mechanical_reason"], has_execution_open=trace["has_execution_open"],
                           engine_target_quantity=trace["engine_target_quantity"], held_after=trace["held_after"])
                if not math.isclose(trace["target_weight"], weight, abs_tol=1e-10):
                    row["trace_status"] = "execution_target_override"
                elif not trace["engine_mark"]:
                    row["trace_status"] = "saved_mark_unknown"
                else:
                    rule = execution_rule(symbol, execution_day, profiles)
                    if (trace["price_unit"] != rule.price_unit or trace["minimum_buy_quantity"] != rule.quantity.minimum
                            or trace["quantity_step"] != rule.quantity.step
                            or any(type(trace[k]) is not int or trace[k] < 0 for k in
                                   ("engine_mark", "open_equity_cents", "engine_target_quantity", "held_before", "held_after"))
                            or not trace["open_equity_cents"]
                            or trace["remaining_quantity_gap"] != trace["engine_target_quantity"] - trace["held_after"]):
                        raise ValueError("Saved execution quantities, mark or instrument rule changed")
                    budget = float(Decimal(trace["open_equity_cents"]) * Decimal(str(weight)) / 100)
                    minimum = notional_cents(trace["engine_mark"], rule.quantity.minimum, rule.price_unit) / 100
                    rounded = notional_cents(trace["engine_mark"], trace["engine_target_quantity"], rule.price_unit) / 100
                    after = notional_cents(trace["engine_mark"], trace["held_after"], rule.price_unit) / 100
                    delta = trace["engine_target_quantity"] - trace["held_before"]
                    legal_delta = rule.quantity.rounded(abs(delta), "buy" if delta > 0 else "sell", trace["held_before"])
                    legal_quantity = trace["held_before"] + (legal_delta if delta > 0 else -legal_delta)
                    legal_value = notional_cents(trace["engine_mark"], legal_quantity, rule.price_unit) / 100
                    row.update(trace_status="saved_attempt_mark", target_budget_cny=budget,
                               minimum_buy_value_cny=minimum, below_minimum_target_budget=minimum > budget,
                               open_equity_cny=trace["open_equity_cents"] / 100,
                               rounded_target_value_cny=rounded, selected_held_after_value_cny=after,
                               quantity_rule_only_target_value_cny=legal_value, minimum_rule_gap_cny=rounded - legal_value,
                               post_quantity_rule_gap_cny=legal_value - after,
                               rounding_gap_cny=budget - rounded, execution_quantity_gap_cny=rounded - after,
                               total_target_gap_cny=budget - after)
            selected.append(row)
        members.extend(selected)
        gross = sum(weights.values())
        known = [r for r in selected if r["industry"] is not None]
        grouped = defaultdict(float)
        for row in known:
            grouped[row["industry"]] += row["target_weight"]
        full = bool(selected) and all(r["trace_status"] == "saved_attempt_mark" for r in selected)
        industry_full = bool(selected) and len(known) == len(selected)
        anchor = {"signal_date": day, "execution_date": execution_day, "targets": len(selected),
                  "supplied_target_slots": len(weights), "explicit_zero_budget_slots": len(weights) - len(selected),
                  "gross_target_weight": gross, "target_trace_complete": full,
                  "valued_target_count": sum(r["trace_status"] == "saved_attempt_mark" for r in selected),
                  "below_minimum_no_entry_records": sum(r["mechanical_reason"] == "below_minimum_no_entry" for r in selected),
                  "industry_target_weight_coverage": sum(r["target_weight"] for r in known) / gross if gross else None,
                  "industry_complete": industry_full,
                  "largest_known_industry": max(grouped, key=grouped.get) if grouped else None,
                  "largest_known_industry_target_fraction": max(grouped.values()) / gross if grouped else None,
                  "below_minimum_budget_fraction": None, "rounded_target_weight": None,
                  "quantity_rule_only_target_weight": None, "minimum_rule_gap_weight": None,
                  "post_quantity_rule_gap_weight": None,
                  "selected_held_after_weight": None, "rounding_gap_weight": None,
                  "execution_quantity_gap_weight": None, "total_target_gap_weight": None}
        if full:
            equities = {r["open_equity_cny"] for r in selected}
            if len(equities) != 1:
                raise ValueError("Saved target attempt does not share one opening equity")
            equity = equities.pop()
            anchor["below_minimum_budget_fraction"] = sum(r["target_budget_cny"] for r in selected
                if r["below_minimum_target_budget"]) / (equity * gross)
            for name, field in (("rounded_target", "rounded_target_value_cny"),
                                ("quantity_rule_only_target", "quantity_rule_only_target_value_cny"),
                                ("minimum_rule_gap", "minimum_rule_gap_cny"),
                                ("post_quantity_rule_gap", "post_quantity_rule_gap_cny"),
                                ("selected_held_after", "selected_held_after_value_cny"),
                                ("rounding_gap", "rounding_gap_cny"),
                                ("execution_quantity_gap", "execution_quantity_gap_cny"),
                                ("total_target_gap", "total_target_gap_cny")):
                anchor[name + "_weight"] = sum(r[field] for r in selected) / equity
        anchors.append(anchor)
    dates = pd.DataFrame(anchors)
    complete = dates.loc[dates.target_trace_complete] if len(dates) else dates
    industry_complete = dates.loc[dates.industry_complete] if len(dates) else dates
    summary = {"target_anchors": len(dates), "fully_observed_execution_anchors": len(complete),
               "target_records": len(members),
               "explicit_zero_budget_slots": int(dates.explicit_zero_budget_slots.sum()) if len(dates) else 0,
               "mean_gross_target_weight": float(dates.gross_target_weight.mean()) if len(dates) else None,
               "below_minimum_no_entry_records": sum(r["mechanical_reason"] == "below_minimum_no_entry" for r in members),
               "below_minimum_budget_records": sum(r["below_minimum_target_budget"] is True for r in members),
               "industry_complete_anchors": len(industry_complete),
               "maximum_industry_fraction_on_complete_anchors": float(industry_complete.largest_known_industry_target_fraction.max())
                   if len(industry_complete) else None}
    for name in ("below_minimum_budget_fraction", "rounded_target_weight", "quantity_rule_only_target_weight",
                 "minimum_rule_gap_weight", "post_quantity_rule_gap_weight", "selected_held_after_weight",
                 "rounding_gap_weight", "execution_quantity_gap_weight", "total_target_gap_weight"):
        summary["mean_" + name] = float(complete[name].mean()) if len(complete) else None
    return pd.DataFrame(members), dates, summary


def action_map(record, identifier):
    inputs = record.get("inputs")
    if inputs is None:
        inputs = record.get("protocol", {}).get("stock_inputs", {})
    if not inputs.get("event_view") and record.get("source_run"):
        original = json.loads(Path(record["source_run"]).read_text(encoding="utf-8"))
        inputs = original.get("bulk_inputs", {})
    if not inputs.get("event_view") and record.get("research_family") == "alpha158_lightgbm":
        original = json.loads((settings.ROOT / "outputs/factor_mining" / identifier / "run.json").read_text(encoding="utf-8"))
        inputs = original.get("bulk_inputs", {})
    if not inputs.get("event_view"):
        return {}
    actions = pd.read_parquet(Path(inputs["event_view"]) / "canonical_actions.parquet").fillna("")
    if actions.duplicated(["symbol", "dividOperateDate"]).any():
        raise ValueError("Duplicate saved corporate-action identities")
    return {(r["symbol"], r["dividOperateDate"]): r for r in actions.to_dict("records")}


def credited_shares(event, actions):
    if "allocated_shares" in event:
        quantity = event["allocated_shares"]
        if type(quantity) is not int or quantity < 0:
            raise ValueError("Invalid saved actual share allocation")
        return quantity
    action = actions.get((event["symbol"], event["day"]))
    if action is None:
        raise ValueError("Legacy corporate-action share quantity is unknown without its pinned source")
    # Mirror only the recorded share credit, never round a missing holder allocation.
    bonus = sum((Decimal(str(action[field] or "0")) for field in
                 ("dividStocksPs", "dividReserveToStockPs")), Decimal(0))
    quantity = event["entitlement"] * bonus
    if not quantity.is_finite() or quantity < 0 or quantity != int(quantity):
        raise ValueError("Legacy fractional share allocation needs its saved actual credit")
    return int(quantity)


def project_orders(account, actions, *, decision_holdings=None, daily_holdings=None):
    if account.get("broker_binding"):
        raise PermissionError("Turnover attribution consumes unbound historical research accounts only")
    if any(value is not None and value for value in (decision_holdings, daily_holdings)):
        raise ValueError("Saved holding observations require empty destinations")
    quantities, seen_orders, seen_fills = defaultdict(int), set(), set()
    target, rows, traces, blocks = None, [], {}, []
    events = account["events"]
    if [e["sequence"] for e in events] != list(range(1, len(events) + 1)):
        raise ValueError("Saved event order is incomplete or duplicated")
    for event in events:
        kind = event["kind"]
        if kind == "targets_generated":
            target = event
            if decision_holdings is not None:
                key = event["signal_date"]
                if key in decision_holdings:
                    raise ValueError("Duplicate saved decision holding date")
                decision_holdings[key] = {s: q for s, q in quantities.items() if q}
        elif kind == "target_execution_diagnostic":
            key = event["day"], event["symbol"]
            if key in traces or event.get("schema_version") != 1:
                raise ValueError("Duplicate or unsupported execution-gap diagnostic")
            traces[key] = event
        elif kind in {"order_blocked", "rebalance_deferred"}:
            blocks.append({"date": event["day"], "symbol": event["symbol"],
                           "side": event.get("side"), "quantity": event.get("qty"),
                           "reason": event.get("reason"), "event_sequence": event["sequence"]})
        elif kind == "corporate_action":
            quantities[event["symbol"]] += credited_shares(event, actions)
        elif kind == "successor_security_transfer":
            source = event.get("source_symbol")
            successor = event.get("target_symbol")
            source_quantity = event.get("source_quantity")
            allocated_quantity = event.get("allocated_quantity")
            if (not isinstance(source, str) or not isinstance(successor, str) or source == successor
                    or not isinstance(source_quantity, int) or source_quantity <= 0
                    or not isinstance(allocated_quantity, int) or allocated_quantity <= 0):
                raise ValueError("Invalid saved successor-security transfer")
            if quantities[source] != source_quantity:
                raise ValueError("Successor transfer disagrees with saved source quantity")
            quantities[source] -= source_quantity
            quantities[successor] += allocated_quantity
        elif event.get("execution_id"):
            execution_id = event["execution_id"]
            fill = account["fills"][execution_id]
            if execution_id in seen_fills or any(event[k] != fill[k] for k in ("symbol", "qty", "price", "day", "order_id")):
                raise ValueError("Saved fill event does not match its unique ledger execution")
            seen_fills.add(execution_id)
            quantities[fill["symbol"]] += fill["qty"] * (1 if fill["side"] == "buy" else -1)
            if quantities[fill["symbol"]] < 0:
                raise ValueError("Saved share projection implies an unexplained opening position")
        elif kind in {"submitted", "rejected"} and event.get("order_id"):
            order = account["orders"][event["order_id"]]
            if order["id"] in seen_orders:
                raise ValueError("Duplicate saved parent-order creation")
            seen_orders.add(order["id"])
            current = quantities[order["symbol"]]
            matched = target is not None and target.get("execution_not_before") == order["day"]
            weight = target["targets"].get(order["symbol"], 0) if matched else None
            reason = ("new_entry" if order["side"] == "buy" and not current else
                      "target_removed_exit" if order["side"] == "sell" and weight == 0 else
                      "full_exit_cause_unknown" if order["side"] == "sell" and order["qty"] >= current else
                      "internal_reweight")
            rows.append({"order_id": order["id"], "date": order["day"], "symbol": order["symbol"],
                "side": order["side"], "held_before": current, "requested_quantity": order["qty"],
                "filled_quantity": order["filled"], "unfilled_quantity": order["qty"] - order["filled"],
                "status": order["status"], "rejection_reason": order.get("reason"),
                "mechanical_reason": reason, "business_exit_reason": None,
                "signal_date": target["signal_date"] if matched else None,
                "matched_target_weight": weight, "reason_basis": "saved_fill_action_quantities_and_target_membership"})
        if daily_holdings is not None:
            daily_holdings[event["day"]] = {s: q for s, q in quantities.items() if q}
    if seen_orders != set(account["orders"]) or seen_fills != set(account["fills"]):
        raise ValueError("Saved events do not cover every parent order and fill")
    terminal = {s: sum(lot["qty"] for lot in lots) for s, lots in account["positions"].items()}
    if any(quantities[s] != terminal.get(s, 0) for s in set(quantities) | set(terminal)):
        raise ValueError("Share projection does not reconcile terminal ledger quantities")
    for row in rows:
        trace = traces.get((row["date"], row["symbol"]))
        if trace and row["order_id"] in trace["order_ids"]:
            if row["held_before"] != trace["held_before"]:
                raise ValueError("Execution diagnostic disagrees with saved pre-order holdings")
            row.update(mechanical_reason=trace["mechanical_reason"], reason_basis="saved_engine_execution_diagnostic")
    frame = pd.DataFrame(rows, columns=["order_id", "date", "symbol", "side", "held_before",
        "requested_quantity", "filled_quantity", "unfilled_quantity", "status", "rejection_reason",
        "mechanical_reason", "business_exit_reason", "signal_date", "matched_target_weight", "reason_basis"])
    return frame, list(traces.values()), blocks


def summarize_case(identifier, signal, destination, *, observed_folder=None, industry_view=None):
    record, artifacts = replay_reports._read(identifier)
    detail = replay_reports.detail(identifier, signal, include_marks=True)
    original_folder = artifacts / record.get("ledger_folders", {}).get(signal, signal)
    folder = Path(observed_folder) if observed_folder is not None else original_folder
    account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    config = StrategyConfig(**detail["config"]).validate()
    profiles = record["protocol"].get("fund_profiles")
    asset_pnl(account, config, profiles)
    rows, traces, blocks = project_orders(account, action_map(record, identifier))
    from .style_exposure import load_industries
    target_days = [e["signal_date"] for e in account["events"] if e["kind"] == "targets_generated"]
    industries, industry_receipt = load_industries(industry_view, target_days)
    exposure, exposure_summary = daily_exposure(detail["daily_marks"])
    members, anchors, realization_summary = target_realization(account, traces, config,
                                                               industries=industries, profiles=profiles)
    from .close_lot_diagnostics import saved_close_lot_transitions
    lot_rows, lot_summary = saved_close_lot_transitions(account, traces, detail["daily_marks"], config, profiles=profiles)
    costs = order_costs(list(account["orders"].values()), config, profiles)
    merged = rows.merge(costs.drop(columns=["date", "symbol", "side", "filled_quantity"]),
                        on="order_id", how="left", validate="one_to_one")
    amounts = ["notional_cny", "fees_cny", "commission_cny", "sell_tax_cny", "transfer_cny",
               "minimum_commission_premium_cny"]
    merged[amounts] = merged[amounts].fillna(0)
    merged["at_minimum_commission"] = merged.at_minimum_commission.eq(True)
    merged["requested_notional_cny"] = [notional_cents(account["orders"][r["order_id"]]["limit_price"],
        r["requested_quantity"], execution_rule(r["symbol"], r["date"], profiles).price_unit) / 100
        for r in rows.to_dict("records")]
    groups = []
    for reason, part in merged.groupby("mechanical_reason", sort=True):
        groups.append({"mechanical_reason": reason, "parent_orders": len(part),
            "filled_orders": int(part.filled_quantity.gt(0).sum()),
            "orders_with_unfilled_quantity": int(part.unfilled_quantity.gt(0).sum()),
            "minimum_commission_orders": int(part.at_minimum_commission.sum()),
            **{key: round(float(part[key].sum()), 2) for key in ["requested_notional_cny", *amounts]}})
    if round(sum(r["fees_cny"] for r in groups) * 100) != sum(o["fees"] for o in account["orders"].values()):
        raise ValueError("Parent-reason costs do not reconcile the sole ledger")
    total = sum(r["notional_cny"] for r in groups)
    for row in groups:
        row["filled_turnover_fraction"] = row["notional_cny"] / total if total else None
    summary = {"case_id": identifier + ":" + signal, "status": detail["status"],
        "source_account": str(folder / "account.json"), "source_limitations": detail["source_limitations"],
        "sessions": len(detail["daily"]), "metrics": detail["metrics"], "parent_orders": len(rows),
        "filled_turnover_cny": total, "fees_cny": sum(o["fees"] for o in account["orders"].values()) / 100,
        "groups": groups, "blocked_attempts": dict(Counter(b["reason"] for b in blocks)),
        "execution_gap_records": len(traces), "quantity_gap_coverage": "saved_attempts_only" if traces else "legacy_unknown",
        "terminal_share_residual": 0, "cost_residual_cents": 0,
        "commission_crossover_cny": config.minimum_commission / config.commission if config.commission else None,
        "scope": "full_saved_account" if detail["status"] == "completed" else "successful_prefix_only",
        "account_replayed": False, "observed_account_consumed": observed_folder is not None,
        "original_account": str(original_folder / "account.json"),
        "daily_exposure": exposure_summary, "target_realization": realization_summary,
        "lot_allocation_policy": account.get("lot_allocation"),
        "close_lot_transition": lot_summary,
        "industry_source": str(Path(industry_view).resolve()) if industry_view is not None else None,
        "industry_source_status": industry_receipt["status"],
        "execution_admitted": False, "limitations": LIMITS}
    destination.mkdir(parents=True, exist_ok=False)
    merged.to_csv(destination / "parent_order_reasons.csv", index=False)
    pd.DataFrame(groups).to_csv(destination / "turnover_by_reason.csv", index=False)
    pd.DataFrame(blocks, columns=["date", "symbol", "side", "quantity", "reason", "event_sequence"]).to_csv(
        destination / "blocked_attempts.csv", index=False)
    gap_columns = ["day", "symbol", "mechanical_reason", "target_weight", "open_equity_cents", "engine_mark",
        "price_unit", "has_execution_open", "ideal_fractional_quantity", "engine_target_quantity", "held_before",
        "sellable_before", "minimum_buy_quantity", "quantity_step", "held_after", "remaining_quantity_gap",
        "cash_available_before_cents", "cash_available_after_cents", "requested_quantity", "filled_quantity",
        "constraint_flags", "order_ids"]
    pd.DataFrame(traces, columns=gap_columns).to_csv(destination / "target_execution_gaps.csv", index=False)
    exposure.to_csv(destination / "daily_invested_exposure.csv", index=False)
    members.to_csv(destination / "target_budget_realization.csv", index=False)
    anchors.to_csv(destination / "target_realization_anchors.csv", index=False)
    if not lot_rows.empty:
        lot_rows.to_csv(destination / "close_lot_to_open.csv", index=False)
    write_json(destination / "summary.json", summary)
    return summary


def run(identifiers, *, industry_view=None):
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ValueError("Specify distinct saved research runs")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/turnover_attribution" / identifier
    artifacts = settings.DATA_ROOT / "research/turnover_attribution" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    record = {"id": identifier, "status": "running", "source_runs": identifiers, "artifacts": str(artifacts),
              "cases": [], "accounts_replayed": False, "execution_admitted": False, "limitations": LIMITS}
    write_json(out / "run.json", record)
    try:
        for source in identifiers:
            saved, _ = replay_reports._read(source)
            for result in saved["results"]:
                signal = result["signal"]
                summary = summarize_case(source, signal, artifacts / (source + "_" + signal), industry_view=industry_view)
                record["cases"].append(summary)
                print(f"{summary['case_id']}: {summary['parent_orders']} parent orders, gap={summary['quantity_gap_coverage']}", flush=True)
        record["status"] = "completed_saved_parent_order_attribution"
    except Exception as error:
        record.update(status="failed_saved_parent_order_attribution", error=f"{type(error).__name__}: {error}")
        write_json(out / "run.json", record)
        raise
    write_json(out / "run.json", record)
    lines = ["# Saved Parent-Order Turnover Attribution", "", "Read-only diagnostics, no account replay; NO_TRADE.", "",
             "| Case | Mechanical reason | Parents | Filled turnover CNY | Turnover share | Fees CNY | Min-fee premium CNY |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for case in record["cases"]:
        for row in case["groups"]:
            share = f"{row['filled_turnover_fraction']:.2%}" if row["filled_turnover_fraction"] is not None else "UNKNOWN"
            lines.append(f"|{case['case_id']}|{row['mechanical_reason']}|{row['parent_orders']}|{row['notional_cny']:,.2f}|"
                         f"{share}|{row['fees_cny']:,.2f}|{row['minimum_commission_premium_cny']:,.2f}|")
    lines += ["", "## Saved Gap Coverage", ""]
    for case in record["cases"]:
        lines += [f"{case['case_id']}: {case['sessions']} sessions, {case['scope']}; quantity-gap coverage="
                  f"{case['quantity_gap_coverage']} ({case['execution_gap_records']} saved records). "
                  f"Blocked/deferred attempts: {case['blocked_attempts']}. Share and fee residuals=0.", ""]
    def fmt(value):
        return "UNKNOWN" if value is None else f"{value:.2%}"
    lines += ["## Saved Invested Exposure and Target Realization", "",
              "| Case | Mean invested / nonempty sessions | Mean holdings | Below-minimum budgets | Observed anchors | Mean engine target / quantity-rule-only / held-after weight |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    notes = []
    for case in record["cases"]:
        exposure, target = case["daily_exposure"], case["target_realization"]
        lines.append(f"|{case['case_id']}|{fmt(exposure['mean_invested_weight'])} / {fmt(exposure['mean_nonempty_invested_weight'])}|"
                     f"{exposure['mean_holdings']:.2f}|{target['below_minimum_budget_records']} / {target['target_records']}|"
                     f"{target['fully_observed_execution_anchors']} / {target['target_anchors']}|"
                     f"{fmt(target['mean_rounded_target_weight'])} / {fmt(target['mean_quantity_rule_only_target_weight'])} / "
                     f"{fmt(target['mean_selected_held_after_weight'])}|")
        notes += [f"{case['case_id']}: lot allocation policy {case['lot_allocation_policy'] or 'default_weight_floor'}; "
                  f"mean supplied gross target weight {fmt(target['mean_gross_target_weight'])}; "
                  f"explicit zero-budget slots {target['explicit_zero_budget_slots']}. "
                  f"Target budget share below minimum buy size {fmt(target['mean_below_minimum_budget_fraction'])}; "
                  f"mean signed budget-minus-engine-target gap {fmt(target['mean_rounding_gap_weight'])}, quantity-rule gap "
                  f"{fmt(target['mean_minimum_rule_gap_weight'])}, subsequent difference "
                  f"{fmt(target['mean_post_quantity_rule_gap_weight'])} of saved opening equity.",
                  f"Empty-holding sessions: {exposure['empty_holding_days']}; stale-mark sessions: {exposure['stale_valuation_days']}. "
                  f"Exact-date industry-complete anchors: {target['industry_complete_anchors']} / {target['target_anchors']}; "
                  f"maximum industry target share on those anchors {fmt(target['maximum_industry_fraction_on_complete_anchors'])}.", ""]
    lines += ["", *notes,
              "Quantity-rule-only applies the existing minimum/step/full-exit rules to saved desired-minus-held quantity; "
              "it ignores cash, risk caps, liquidity and prices after that attempt. It is NOT a new allocation or fill simulation.",
              "Default weight-floor absolute targets may be below STAR's200-share buy minimum; an explicit open-lot policy uses its saved integer target instead. Desired quantities and legal order deltas must not be conflated.",
              "Positive-target quantities exclude removed residual holdings; attempt-mark weights are NOT closing daily invested weights.",
              "Signed budget/quantity gaps do not measure missed profit. Open-lot targets can exceed an individual weight budget within unchanged portfolio caps; legacy CSV field names are retained. Fully observed anchors alone contribute to gap means; partial/legacy records remain UNKNOWN.", ""]
    lot_cases = [case for case in record["cases"] if case["close_lot_transition"]["status"] != "not_applicable"]
    if lot_cases:
        lines += ["## Saved Close-Lot to Open Transition", "",
                  "Each stage reports reductions / increases / unchanged among decomposed saved attempts. Positive gaps mean fewer shares; stages are not independent causal effects.", "",
                  "| Case | Attempts / estimates | Price | Equity | Stored-weight representation | Binary arithmetic | Minimum/step | Subsequent execution |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        from .close_lot_diagnostics import STAGES
        for case in lot_cases:
            lot = case["close_lot_transition"]
            counts = ["/".join(str(lot["stages"][stage][key]) for key in
                               ("reduction_records", "increase_records", "unchanged_records")) for stage in STAGES]
            lines.append(f"|{case['case_id']}|{lot['observed_records']} / {lot['estimate_records']}|" + "|".join(counts) + "|")
        for case in lot_cases:
            lot = case["close_lot_transition"]
            lines += [f"{case['case_id']}: statuses {lot['record_statuses']}; net exact-to-engine numeric changes "
                      f"{lot['numeric_net_counts']}. Positive close estimate -> zero open target: "
                      f"{lot['positive_close_to_zero_open_target_records']}; -> zero after quantity rule: "
                      f"{lot['positive_close_to_zero_quantity_rule_records']}. Missing fresh opens: "
                      f"{lot['missing_execution_open_records']}.", ""]
        lines += ["Exact close weights are reconstructed only from saved integer close quantity, cent mark and decision equity. Price-only precedes equity transition; corporate actions can affect both. Missing execution opens use only saved marks and remain explicitly marked, not fresh quotes.",
                  "Zero-budget slots without an attempt do not imply filled zero holdings. Per-attempt marked differences are repeated intent observations, not aggregate cash, opportunity cost, or profit. No orders, fills, cash or positions changed.", ""]
    lines += ["## Boundaries", "", *["- " + limitation for limitation in LIMITS], "",
              f"Detailed parent orders, reason groups, blocked attempts and saved execution gaps: `{artifacts}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(out), flush=True)
    return out, record


def trace_fixed_model(source_id, signal, *, resume_observation=None):
    from .alpha158_linear import ID, SIGNALS, saved_score
    from .bulk_feed import BulkResearchFeed
    from .experiment import ScoreFeed, export_scores, replay
    from ..report import metrics as marked_metrics

    source, source_artifacts = replay_reports._read(source_id)
    if (source.get("purpose") != ID or signal not in SIGNALS
            or source.get("status") != "completed_alpha158_linear_diagnostics"
            or source.get("execution_admitted") is not False):
        raise ValueError("Execution trace currently supports completed fixed Alpha158 linear/ensemble diagnostics only")
    detail = replay_reports.detail(source_id, signal, include_marks=True)
    config = StrategyConfig(**detail["config"]).validate()
    if detail["status"] != "completed" or detail["receipt"].get("commission_deadband", False) or detail["receipt"].get("lot_allocation"):
        raise ValueError("A completed original-policy fixed model account is required")
    original_folder = source_artifacts / source.get("ledger_folders", {}).get(signal, signal)
    original = json.loads((original_folder / "account.json").read_text(encoding="utf-8"))
    if original.get("broker_binding"):
        raise PermissionError("Broker-bound accounts cannot be replayed for diagnostics")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/turnover_attribution" / identifier
    artifacts = settings.DATA_ROOT / "research/turnover_attribution" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    shutil.copy2(settings.ROOT / "trading/engine.py", artifacts / "engine_implementation.py")
    record = {"id": identifier, "status": "running_execution_gap_observation", "source_run": source_id,
        "signal": signal, "artifacts": str(artifacts), "models_refit": False, "accounts_replayed": True,
        "parameters_searched": False, "execution_admitted": False, "economic_equivalence": None,
        "limitations": LIMITS}
    write_json(out / "run.json", record)
    feed = None
    try:
        inputs = source["protocol"]["stock_inputs"]
        score_path = source_artifacts / "predictions" / signal / "historical_scores.parquet"
        record.update(inputs=inputs, predictions=str(score_path), config=config.to_dict())
        dates = [day["date"] for day in detail["daily_marks"]]
        if resume_observation:
            if not replay_reports.RUN_ID.fullmatch(resume_observation):
                raise ValueError("Resume observation requires its saved run ID")
            prior = json.loads((settings.ROOT / "outputs/turnover_attribution" / resume_observation / "run.json").read_text(encoding="utf-8"))
            if (prior.get("status") not in {"failed_execution_gap_observation", "completed_execution_gap_observation"}
                    or prior.get("source_run") != source_id or prior.get("signal") != signal
                    or prior.get("inputs") != inputs or prior.get("config") != config.to_dict()
                    or prior.get("predictions") != str(score_path)):
                raise ValueError("Saved observation source/config/predictions changed")
            observed_folder = Path(prior.get("observed_account", str(Path(prior["artifacts"]) / "observed_account"))).resolve()
            observed_folder.relative_to((settings.DATA_ROOT / "research/turnover_attribution").resolve())
            if observed_folder.name != "observed_account":
                raise ValueError("Only the saved isolated observation account can be reused")
            receipt = json.loads((observed_folder / "replay.json").read_text(encoding="utf-8"))
            if (receipt.get("status") != "completed" or receipt.get("config") != config.to_dict()
                    or receipt.get("completed_sessions") != len(dates)):
                raise ValueError("Only a completed unchanged observation can be consumed without another replay")
            record.update(resume_observation=resume_observation, accounts_replayed=False,
                          observed_account=str(observed_folder), observation_reused_sessions=len(dates))
            print(f"Reuse completed observation: {len(dates)} sessions; no additional replay", flush=True)
        else:
            feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
            observed_folder = artifacts / "observed_account"
            record["observed_account"] = str(observed_folder)
            print(f"Observe original {signal}: {len(dates)} sessions; no model/selection/cost changes", flush=True)
            record["replay_result"] = replay(ScoreFeed(feed, export_scores(saved_score(score_path))), config, dates, observed_folder)
        observed = json.loads((observed_folder / "account.json").read_text(encoding="utf-8"))
        economic_keys = ["cash", "positions", "orders", "fills", "prices", "price_units", "entitlements",
                         "receivables", "targets", "pending_targets", "candidates", "day"]
        differences = [key for key in economic_keys if original.get(key) != observed.get(key)]
        original_marks = pd.DataFrame(detail["daily_marks"])
        observed_marks = pd.read_csv(observed_folder / "daily.csv", dtype={"date": str}, float_precision="round_trip")
        for key in ("date", "equity", "cash", "receivables", "holdings", "fees", "turnover_value", "new_orders"):
            left, right = original_marks[key].astype(str), observed_marks[key].astype(str)
            if key == "date":
                equal = left.tolist() == right.tolist()
            else:
                equal = pd.to_numeric(left).tolist() == pd.to_numeric(right).tolist()
            if not equal:
                differences.append("daily_" + key)
        observed_marks["stale_symbols"] = observed_marks.stale_symbols.map(ast.literal_eval)
        metrics = marked_metrics(observed_marks.to_dict("records"), config.initial_cash)
        metric_differences = [key for key, value in metrics.items() if (
            (value is None) != (detail["metrics"][key] is None)
            or value is not None and abs(value - detail["metrics"][key]) > 1e-8 * max(1, abs(value)))]
        if differences or metric_differences:
            raise ValueError("Observer changed saved economic outcomes: " + str(differences or ["metrics"]))
        record.update(economic_equivalence=True, metrics=metrics, economic_fields_compared=economic_keys)
        summary = summarize_case(source_id, signal, artifacts / "diagnostics", observed_folder=observed_folder)
        gap = pd.read_csv(artifacts / "diagnostics/target_execution_gaps.csv")
        if gap.empty:
            raise ValueError("Completed observation contains no execution-gap records")
        flags = Counter(flag for row in observed["events"] if row["kind"] == "target_execution_diagnostic"
                        for flag in row["constraint_flags"])
        record.update(status="completed_execution_gap_observation", case=summary,
            gap_summary={"records": len(gap), "nonzero_remaining_gap_records": int(gap.remaining_quantity_gap.ne(0).sum()),
                "below_minimum_no_entry_records": int(gap.mechanical_reason.eq("below_minimum_no_entry").sum()),
                "constraint_flags": dict(flags)})
        write_json(out / "run.json", record)
        lines = ["# Fixed Model Execution-Gap Observation", "", "Historical observation, NO_TRADE.", "",
            f"Source `{source_id}:{signal}`; {len(dates)} original sessions, predictions, data, capital and costs unchanged.",
            "One isolated same-engine replay collects the new observation fields. Original accounts are not modified.",
            "All saved orders, fills, cash, holdings, marks, entitlements, targets and daily economic amounts are identical.",
            "These are not a new strategy or an independent performance result.", "",
            f"Gap records: {len(gap)}; nonzero remaining gaps: {record['gap_summary']['nonzero_remaining_gap_records']}; "
            f"below-minimum no-entry records: {record['gap_summary']['below_minimum_no_entry_records']}.",
            f"Recorded constraint flags (may overlap): {dict(flags)}.", "",
            "The engine's rounded absolute target is distinct from legal submitted orders and actual fills. "
            "The fractional target uses that attempt's saved mark; missing execution opens are explicitly flagged.",
            "Global before/after available cash is an execution-stage observation, not an independent allocation model.", "",
            f"Details: `{artifacts / 'diagnostics/target_execution_gaps.csv'}`.", "", *["- " + item for item in LIMITS]]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed_execution_gap_observation", error=f"{type(error).__name__}: {error}")
        write_json(out / "run.json", record)
        raise
    finally:
        if feed is not None:
            feed.close()
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="append")
    group.add_argument("--trace-run", help="One fixed saved model; collect gaps via an economically identical replay")
    parser.add_argument("--trace-signal", choices=["alpha158_fixed_ridge", "alpha158_allseed_rank_mean"])
    parser.add_argument("--resume-observation", help="Consume an already completed observation without replaying it again")
    parser.add_argument("--industry-view", help="Reuse exact-date saved industry classifications, without acquisition")
    args = parser.parse_args(argv)
    if args.trace_run:
        if args.industry_view:
            parser.error("--industry-view applies to --run only")
        if not args.trace_signal:
            parser.error("--trace-run requires --trace-signal")
        trace_fixed_model(args.trace_run, args.trace_signal, resume_observation=args.resume_observation)
    elif args.trace_signal or args.resume_observation:
        parser.error("Trace options require --trace-run")
    else:
        run(args.run, industry_view=args.industry_view)
    return 0
