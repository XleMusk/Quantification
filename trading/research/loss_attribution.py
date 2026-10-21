"""Explain saved ledger losses without creating a hypothetical gross account."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .. import replay_reports, settings
from ..config import StrategyConfig
from ..engine import shares_for_target
from ..feed import HistoricalFeed
from ..ledger import Ledger
from ..report import quote_rmb
from ..rules import cents, execution_rule, fees
from ..symbols import normalize_symbol
from .experiment import write_json


LIMITS = [
    "Fee/slippage addbacks hold executed quantities and dates fixed: NOT an alternate zero-cost account, return, Sharpe or drawdown",
    "Same-open price differences measure this simulator's implementation friction, not observed broker slippage or market impact",
    "Security/class PnL includes recorded net distributions and share-adjusted terminal holdings; it is NOT asset time-weighted return or market/style/industry alpha",
    "Distribution tax reserves follow the saved ledger assumptions; tax effects and opportunity costs are not independently attributed",
    "Unknown/missing raw-open matches make aggregate slippage and its addback unavailable; no subset extrapolation",
    "Price staleness can understate liquidation loss; completed replay does not imply execution admission",
]


def order_costs(orders, config, profiles=None):
    rows = []
    for order in orders:
        if order["filled"] <= 0:
            continue
        notional = order["notional"]
        rule = execution_rule(order["symbol"], order["day"], profiles)
        proportional = round(notional * config.commission)
        commission = max(cents(config.minimum_commission), proportional)
        transfer = round(notional * config.transfer_fee) if not rule.is_fund else 0
        expected = fees(notional, order["side"], config, equity_etf=rule.is_fund, day=order["day"])
        tax = expected - commission - transfer
        if expected != order["fees"] or tax < 0 or (order["side"] != "sell" and tax != 0):
            raise ValueError("Saved cumulative order fees disagree with configured fees")
        rows.append({"order_id": order["id"], "date": order["day"], "symbol": order["symbol"],
            "side": order["side"], "filled_quantity": order["filled"], "notional_cny": notional / 100,
            "fees_cny": order["fees"] / 100, "commission_cny": commission / 100,
            "sell_tax_cny": tax / 100, "transfer_cny": transfer / 100,
            "minimum_commission_premium_cny": (commission - proportional) / 100,
            "at_minimum_commission": commission == cents(config.minimum_commission)})
    return pd.DataFrame(rows, columns=["order_id", "date", "symbol", "side", "filled_quantity",
        "notional_cny", "fees_cny", "commission_cny", "sell_tax_cny", "transfer_cny",
        "minimum_commission_premium_cny", "at_minimum_commission"])


def raw_open_slippage(fills, raw_view, profiles=None, *, fund_dataset=None):
    if not fills:
        return {"status": "no_fills", "fills": 0, "matched_fills": 0, "slippage_cny": 0}, pd.DataFrame()
    frame = pd.DataFrame(fills)
    if raw_view is None and fund_dataset is None:
        return {"status": "raw_view_unavailable", "fills": len(frame), "matched_fills": 0,
                "slippage_cny": None}, pd.DataFrame()
    symbols = set(frame.symbol)
    fund_symbols = {row["symbol"] for row in fills
                    if execution_rule(row["symbol"], row["day"], profiles).is_fund} if fund_dataset else set()
    path = Path(raw_view) / "raw_daily.parquet" if raw_view is not None else None
    pieces = []
    # Canonical IDs are compared, not heuristically assigned to historical aliases.
    codes = [("sh." if symbol.endswith(".SSE") else "sz.") + symbol.split(".")[0]
             for symbol in symbols - fund_symbols]
    if path is not None and codes:
        raw = pq.read_table(path, columns=["date", "code", "open"], filters=[
            ("code", "in", codes), ("date", ">=", frame.day.min()), ("date", "<=", frame.day.max())]).to_pandas()
        raw["symbol"] = raw.code.map(lambda code: normalize_symbol(code).vt_symbol)
        raw["raw_open_source"] = str(path)
        pieces.append(raw[["date", "symbol", "open", "raw_open_source"]])
    if fund_symbols:
        fund = HistoricalFeed(fund_dataset)
        if any(fund.instrument_profiles.get(symbol) != (profiles or {}).get(symbol) for symbol in fund_symbols):
            raise ValueError("Saved fund profile differs from the pinned raw-open dataset")
        raw = fund.raw.loc[fund.raw.symbol.isin(fund_symbols) & fund.raw.date.between(frame.day.min(), frame.day.max()),
                           ["date", "symbol", "open"]].copy()
        raw["raw_open_source"] = str(fund.folder / "raw.parquet")
        pieces.append(raw)
    raw = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(
        columns=["date", "symbol", "open", "raw_open_source"])
    joined = frame.merge(raw, left_on=["day", "symbol"], right_on=["date", "symbol"],
                         how="left", validate="many_to_one")
    joined["open"] = pd.to_numeric(joined.open, errors="coerce")
    matched = np.isfinite(joined.open) & joined.open.gt(0)
    joined["raw_open_matched"] = matched
    joined["fill_rmb"] = [quote_rmb(row["price"], execution_rule(row["symbol"], row["day"], profiles).price_unit)
                          for row in joined.to_dict("records")]
    joined["slippage_cny"] = (joined.qty * (joined.fill_rmb - joined.open)
                              * joined.side.map({"buy": 1, "sell": -1})).where(matched)
    complete = bool(matched.all())
    return {"status": "all_matched" if complete else "incomplete_raw_open_matches",
        "raw_view": str(path) if path is not None else None,
        "fund_dataset": str(fund_dataset) if fund_symbols else None,
        "fills": len(frame), "matched_fills": int(matched.sum()),
        "slippage_cny": round(float(joined.slippage_cny.sum()), 2) if complete else None,
        "favorable_price_difference_fills": int(joined.slippage_cny.lt(-1e-8).sum())}, joined


def asset_pnl(account, config, profiles=None):
    """Decompose the saved cash-only account, using its integer-cent cash flows."""
    if account.get("broker_binding"):
        raise PermissionError("Asset PnL consumes unbound historical research accounts only")
    book = Ledger(account, config, instrument_profiles=profiles)
    flows = {}
    def row(symbol):
        if symbol not in flows:
            rule = book.rule(symbol, account["day"])
            asset = ({"511010.SSE": "sovereign_bond_etf", "518880.SSE": "gold_etf"}.get(symbol, "other_fund")
                     if rule.is_fund else "stock")
            flows[symbol] = {"symbol": symbol, "asset_class": asset, "buys": 0, "sales": 0,
                             "fees": 0, "paid": 0, "receivable": 0, "terminal": 0}
        return flows[symbol]
    for order in account["orders"].values():
        flow = row(order["symbol"])
        flow["buys" if order["side"] == "buy" else "sales"] += order["notional"]
        flow["fees"] += order["fees"]
    for event in account["events"]:
        if event["kind"] == "dividend_paid":
            row(event["symbol"])["paid"] += event["cash"]
    for key, amount in account["receivables"].items():
        row(key.rsplit(":", 1)[0])["receivable"] += amount
    for symbol in account["positions"]:
        flow = row(symbol)
        if book.quantity(symbol):
            if (account["prices"].get(symbol, 0) <= 0
                    or account.get("price_units", {}).get(symbol, "cent") != book.rule(symbol, account["day"]).price_unit):
                raise ValueError("Terminal holding needs its saved positive mark and correct quote unit")
            flow["terminal"] = book.position_value(symbol)
    initial = cents(config.initial_cash)
    expected_cash = initial + sum(f["sales"] - f["buys"] - f["fees"] + f["paid"] for f in flows.values())
    if expected_cash != account["cash"]:
        raise ValueError("Saved cash-only asset PnL has unexplained opening balances or cash movements")
    rows = []
    for flow in sorted(flows.values(), key=lambda item: item["symbol"]):
        mark_trade = flow["terminal"] + flow["sales"] - flow["buys"]
        pnl = mark_trade + flow["paid"] + flow["receivable"] - flow["fees"]
        rows.append({"symbol": flow["symbol"], "asset_class": flow["asset_class"],
            "buy_notional_cny": flow["buys"] / 100, "sell_notional_cny": flow["sales"] / 100,
            "terminal_market_value_cny": flow["terminal"] / 100,
            "terminal_value_plus_sales_minus_buys_cny": mark_trade / 100,
            "paid_net_distribution_cny": flow["paid"] / 100,
            "unpaid_net_distribution_cny": flow["receivable"] / 100,
            "fees_cny": flow["fees"] / 100, "observed_pnl_cny": pnl / 100,
            "account_return_contribution": pnl / initial})
    net = sum(f["terminal"] + f["sales"] - f["buys"] + f["paid"] + f["receivable"] - f["fees"]
              for f in flows.values())
    if net != book.equity() - initial:
        raise ValueError("Security PnL does not reconcile the saved ledger equity")
    frame = pd.DataFrame(rows, columns=["symbol", "asset_class", "buy_notional_cny", "sell_notional_cny",
        "terminal_market_value_cny", "terminal_value_plus_sales_minus_buys_cny", "paid_net_distribution_cny",
        "unpaid_net_distribution_cny", "fees_cny", "observed_pnl_cny", "account_return_contribution"])
    classes = frame.groupby("asset_class", sort=True).sum(numeric_only=True).reset_index().to_dict("records")
    return {"observed_account_pnl_cny": net / 100, "cash_residual_cents": 0, "equity_residual_cents": 0,
            "classes": classes, "basis": "Saved integer-cent orders, net distribution credits/receivables and terminal marked holdings; cash yield zero"}, frame


def case(identifier, signal, destination):
    record, artifacts = replay_reports._read(identifier)
    value = replay_reports.detail(identifier, signal, include_marks=True)
    folder = artifacts / record.get("ledger_folders", {}).get(signal, signal)
    account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    if account.get("broker_binding"):
        raise PermissionError("Attribution consumes unbound historical research accounts only")
    config = StrategyConfig(**value["config"]).validate()
    profiles = record["protocol"].get("fund_profiles")
    orders = list(account["orders"].values())
    fills = list(account["fills"].values())
    costs = order_costs(orders, config, profiles)
    if sum(o["fees"] for o in orders) != sum(f["fees"] for f in fills):
        raise ValueError("Order and fill fee totals disagree")
    raw_view = record.get("inputs", record["protocol"].get("stock_inputs", {})).get("raw_view")
    if raw_view is None:
        raw_view = record["protocol"].get("stock_manifest", {}).get("raw_view")
    if raw_view is None and record.get("research_family") == "alpha158_lightgbm":
        original = json.loads((settings.ROOT / "outputs/factor_mining" / identifier / "run.json").read_text(encoding="utf-8"))
        raw_view = original.get("dataset_manifest", {}).get("raw_view")
        if raw_view is None and original.get("bulk_inputs"):
            price_manifest = json.loads((Path(original["bulk_inputs"]["price_view"]) / "manifest.json").read_text(encoding="utf-8"))
            raw_view = price_manifest.get("raw_view")
    slippage, fill_details = raw_open_slippage(fills, raw_view, profiles,
                                            fund_dataset=record["protocol"].get("fund_dataset"))
    daily = pd.DataFrame(value["daily_marks"])
    exposure = (daily.equity - daily.cash - pd.to_numeric(daily.receivables)) / daily.equity
    stale = Counter(symbol for row in value["daily_marks"] for symbol in row["stale_symbols"])
    blocked = Counter((event.get("side", "unknown"), event["reason"]) for event in account["events"]
                      if event.get("kind") == "order_blocked" and "reason" in event)
    equity = round(float(daily.equity.iloc[-1]) * 100)
    target_rows = []
    for symbol, weight in account["targets"].items():
        rule = execution_rule(symbol, account["day"], profiles)
        price = account["prices"].get(symbol, 0)
        qty = sum(lot["qty"] for lot in account["positions"].get(symbol, []))
        desired = shares_for_target(equity, weight, price, rule)
        target_rows.append({"symbol": symbol, "target_weight": weight, "held_quantity": qty,
            "last_mark_rmb": quote_rmb(price, rule.price_unit), "target_quantity_at_last_mark": desired,
            "minimum_buy_quantity": rule.quantity.minimum,
            "minimum_lot_cny": rule.quantity.minimum * quote_rmb(price, rule.price_unit),
            "target_budget_cny": equity * weight / 100,
            "unheld": qty == 0, "target_below_minimum_lot": desired < rule.quantity.minimum,
            "basis": "Terminal close diagnostic, NOT next execution price or a complete causal explanation"})
    totals = {key: round(float(costs[key].sum()), 2) for key in (
        "fees_cny", "commission_cny", "sell_tax_cny", "transfer_cny", "minimum_commission_premium_cny")}
    net_pnl = round(float(daily.equity.iloc[-1]) - config.initial_cash, 2)
    complete = value["status"] == "completed"
    assets, asset_details = asset_pnl(account, config, profiles)
    if round(assets["observed_account_pnl_cny"] * 100) != round(net_pnl * 100):
        raise ValueError("Asset PnL does not match the saved daily account marks")
    assets.update(scope="full_saved_account" if complete else "successful_prefix_only",
                  net_pnl_cny=net_pnl if complete else None)
    summary = {"case_id": identifier + ":" + signal, "status": value["status"],
        "source": str(folder), "start": str(daily.date.iloc[0]), "end": str(daily.date.iloc[-1]),
        "sessions": len(daily), "config": config.to_dict(), "metrics": value["metrics"],
        "partial_account_pnl_cny": net_pnl if not complete else None,
        "net_pnl_cny": net_pnl if complete else None, "costs": totals, "asset_pnl": assets,
        "filled_orders": len(costs), "minimum_commission_orders": int(costs.at_minimum_commission.sum()),
        "average_filled_order_cny": float(costs.notional_cny.mean()) if len(costs) else None,
        "median_filled_order_cny": float(costs.notional_cny.median()) if len(costs) else None,
        "raw_open_slippage": slippage,
        "fixed_fill_fee_addback_cny": round(net_pnl + totals["fees_cny"], 2) if complete else None,
        "fixed_fill_fee_and_slip_addback_cny": round(net_pnl + totals["fees_cny"] + slippage["slippage_cny"], 2)
            if complete and slippage["slippage_cny"] is not None else None,
        "median_marked_holdings_exposure": float(exposure.median()),
        "median_holdings": float(pd.to_numeric(daily.holdings).median()),
        "terminal_targets": len(target_rows), "terminal_positive_holdings": sum(
            sum(lot["qty"] for lot in lots) > 0 for lots in account["positions"].values()),
        "stale_symbol_days": dict(stale), "blocked_orders": [
            {"side": side, "reason": reason, "attempts": count} for (side, reason), count in sorted(blocked.items())],
        "annual_same_account": value["segments"], "source_limitations": value["source_limitations"],
        "commission_deadband": value["receipt"].get("commission_deadband", False),
        "strategy_validated": False, "execution_admitted": False, "limitations": LIMITS}
    stock_details = asset_details[asset_details["asset_class"] == "stock"]
    summary["top_loss_symbols"] = stock_details.nsmallest(5, "observed_pnl_cny")[[
        "symbol", "observed_pnl_cny", "fees_cny"
    ]].to_dict("records")
    summary["top_gain_symbols"] = stock_details.nlargest(5, "observed_pnl_cny")[[
        "symbol", "observed_pnl_cny", "fees_cny"
    ]].to_dict("records")
    if record.get("research_family") == "stockdb_fixed_factors_2023_v1":
        summary["fixed_factor_basis"] = {"source_run": record["source_run"],
            "stock_inputs": record["protocol"]["stock_inputs"], "raw_view": raw_view,
            "config": config.to_dict(), "cost_profile": record["protocol"]["cost_profile"],
            "pool_counts": record["pool_counts"], "unavailable_decisions": record["unavailable_decisions"],
            "start": record["start"], "end": record["end"], "sessions": record["sessions"],
            "execution_policy": value["receipt"].get("execution_policy"),
            "opening_basis": value["receipt"].get("opening_basis"),
            "lot_allocation": value["receipt"].get("lot_allocation")}
    destination.mkdir(parents=True, exist_ok=False)
    costs.to_csv(destination / "order_costs.csv", index=False)
    fill_details.to_csv(destination / "fill_price_differences.csv", index=False)
    asset_details.assign(scope=assets["scope"]).to_csv(destination / "symbol_pnl.csv", index=False)
    pd.DataFrame(assets["classes"]).assign(scope=assets["scope"]).to_csv(destination / "asset_class_pnl.csv", index=False)
    pd.DataFrame(target_rows).to_csv(destination / "terminal_target_gaps.csv", index=False)
    per_symbol = costs.groupby("symbol")[["notional_cny", *totals]].sum()
    if len(fill_details):
        per_symbol["matched_price_difference_cny"] = fill_details.groupby("symbol").slippage_cny.sum(min_count=1)
        per_symbol["unmatched_fills"] = (~fill_details.raw_open_matched).groupby(fill_details.symbol).sum()
    per_symbol.to_csv(destination / "symbol_costs.csv")
    costs.assign(year=costs.date.str[:4]).groupby("year")[["notional_cny", *totals]].sum().to_csv(destination / "annual_costs.csv")
    write_json(destination / "summary.json", summary)
    return summary


def fixed_factor_comparison(rows):
    signals = [row["case_id"].split(":", 1)[1] for row in rows]
    blend, control = "value_size_activity_equal_rank_v1", "small_size_common_pool_control"
    if (len(rows) != 2 or set(signals) != {blend, control}
            or not all(row.get("fixed_factor_basis") for row in rows)):
        return None
    left, right = (rows[signals.index(signal)] for signal in (blend, control))
    basis = left["fixed_factor_basis"]
    differences = [key for key in basis if basis[key] != right["fixed_factor_basis"].get(key)]
    if differences or any(row["status"] != "completed" for row in rows):
        return {"status": "unavailable_mismatched_or_incomplete_ledgers", "basis_differences": differences,
                "net_difference": None, "execution_admitted": False}
    net = round(left["net_pnl_cny"] - right["net_pnl_cny"], 2)
    fees_gap = round(left["costs"]["fees_cny"] - right["costs"]["fees_cny"], 2)
    slips = [row["raw_open_slippage"]["slippage_cny"] for row in (left, right)]
    slip_gap = round(slips[0] - slips[1], 2) if all(value is not None for value in slips) else None
    return {"status": "matched_completed_saved_ledgers", "blend_case": left["case_id"],
            "control_case": right["case_id"], "basis": basis,
            "net_difference": left["metrics"]["net_return"] - right["metrics"]["net_return"],
            "net_pnl_difference_cny": net, "fee_difference_cny": fees_gap,
            "simulated_raw_open_slippage_difference_cny": slip_gap,
            "fixed_fill_addback_difference_cny": round(net + fees_gap + slip_gap, 2) if slip_gap is not None else None,
            "drawdown_magnitude_difference": abs(left["metrics"]["max_drawdown"]) - abs(right["metrics"]["max_drawdown"]),
            "positive_net_increment": net > 0, "strategy_validated": False, "execution_admitted": False,
            "interpretation": "Same saved inputs and account assumptions, but different selected holdings and cash paths; fixed-fill addbacks are NOT a counterfactual account or causal alpha attribution"}


def run(run_ids):
    if not run_ids or len(set(run_ids)) != len(run_ids):
        raise ValueError("Specify distinct saved research runs")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/loss_attribution" / identifier
    artifacts = settings.DATA_ROOT / "research/loss_attribution" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    rows = []
    try:
        for run_id in run_ids:
            record, _ = replay_reports._read(run_id)
            for result in record["results"]:
                signal = result["signal"]
                print(f"Attribute {run_id}:{signal}", flush=True)
                rows.append(case(run_id, signal, artifacts / (run_id + "_" + signal)))
    except Exception as error:
        write_json(out / "run.json", {"id": identifier, "status": "failed_saved_account_attribution",
            "cases": rows, "artifacts": str(artifacts), "error": f"{type(error).__name__}: {error}",
            "backtest_run": False, "execution_admitted": False})
        raise
    report = {"id": identifier, "status": "completed_saved_account_attribution", "cases": rows,
        "artifacts": str(artifacts), "backtest_run": False, "execution_admitted": False, "limitations": LIMITS}
    report["fixed_factor_comparison"] = fixed_factor_comparison(rows)
    write_json(out / "run.json", report)
    lines = ["# Saved Ledger Loss Attribution", "", "Read-only historical diagnostics; NO_TRADE.", "",
        "| Case | Net PnL CNY | Fees | Min-commission premium | Open slippage | Fixed-fill addback |",
        "| --- | ---: | ---: | ---: | ---: | ---: |"]
    def fmt(value):
        return "UNKNOWN" if value is None else f"{value:,.2f}"
    for row in rows:
        lines.append(f"| {row['case_id']} | {fmt(row['net_pnl_cny'])} | {fmt(row['costs']['fees_cny'])} | "
            f"{fmt(row['costs']['minimum_commission_premium_cny'])} | {fmt(row['raw_open_slippage']['slippage_cny'])} | "
            f"{fmt(row['fixed_fill_fee_and_slip_addback_cny'])} |")
    lines += ["", "## Saved Asset-Class Net Contributions", "",
        "Amounts include actual saved fees, net distributions and terminal holdings. They reconcile account PnL, not asset returns or alpha.",
        "Incomplete accounts retain prefix detail but full-period contributions below remain UNKNOWN.", "",
        "| Case | Stocks CNY | Sovereign bond ETF CNY | Gold ETF CNY | Other fund CNY |",
        "| --- | ---: | ---: | ---: | ---: |"]
    for row in rows:
        amounts = {item["asset_class"]: item["observed_pnl_cny"] for item in row["asset_pnl"]["classes"]}
        cells = [fmt(amounts.get(asset, 0) if row["status"] == "completed" else None)
                 for asset in ("stock", "sovereign_bond_etf", "gold_etf", "other_fund")]
        lines.append("| " + row["case_id"] + " | " + " | ".join(cells) + " |")
    lines += ["", "## Top Symbol Contributors", "",
        "These are saved-account security contributions, not standalone factor returns or causal explanations.", ""]
    for row in rows:
        def symbol_list(items):
            return "; ".join(
                f"{item['symbol']} {item['observed_pnl_cny']:,.2f} CNY (fees {item['fees_cny']:,.2f})"
                for item in items
            ) or "none"
        lines += [f"- `{row['case_id']}` top losses: {symbol_list(row['top_loss_symbols'])}",
                  f"- `{row['case_id']}` top gains: {symbol_list(row['top_gain_symbols'])}"]
    comparison = report["fixed_factor_comparison"]
    if comparison:
        lines += ["", "## Fixed Factor Same-Basis Comparison", "", "Status: " + comparison["status"]]
        if comparison["status"] == "matched_completed_saved_ledgers":
            lines += [f"Blend minus small-size control net return: {comparison['net_difference']:.6%}; "
                      f"net PnL CNY{comparison['net_pnl_difference_cny']:,.2f}.",
                      f"Additional blend fees CNY{comparison['fee_difference_cny']:,.2f}; "
                      "simulated raw-open price differences CNY" + fmt(comparison["simulated_raw_open_slippage_difference_cny"]) + ".",
                      "Difference after fixed-fill fee/price addback: CNY" + fmt(comparison["fixed_fill_addback_difference_cny"]) + ".",
                      comparison["interpretation"]]
    lines += ["", *["- " + limitation for limitation in LIMITS], "", "Artifacts: `" + str(artifacts) + "`", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(str(out), flush=True)
    return out, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="Saved run ID; repeat to cover other accounts")
    args = parser.parse_args(argv)
    run(args.run)
    return 0
