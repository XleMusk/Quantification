"""Explain a completed factor-candidate replay from its saved ledger.

This is a read-only diagnostic consumer.  It deliberately reuses the normal
loss-attribution helpers and never creates a counterfactual zero-cost account.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from .. import settings
from ..config import StrategyConfig
from .experiment import write_json
from .loss_attribution import LIMITS, asset_pnl, order_costs, raw_open_slippage


def _resolve_run(value, requested_signal=None):
    candidate = Path(value)
    if candidate.name == "run.json":
        run_path = candidate
    else:
        candidates = [
            settings.ROOT / "outputs" / "factor_candidate_replays" / str(value) / "run.json",
            settings.ROOT / "outputs" / "factor_candidate_family_replays" / str(value) / "run.json",
            settings.ROOT / "outputs" / "factor_candidate_library_ablations" / str(value) / "run.json",
        ]
        run_path = next((path for path in candidates if path.exists()), candidates[0])
    if not run_path.exists():
        raise FileNotFoundError(f"factor candidate replay not found: {run_path}")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    if record.get("status") not in {
        "completed_factor_candidate_replay",
        "completed_factor_candidate_family_replay",
        "completed_factor_candidate_cadence_replay",
        "completed_factor_candidate_cost_replay",
        "completed_factor_candidate_library_ablation",
    }:
        raise ValueError("Completed factor candidate replay, family replay, cadence, cost replay or library ablation required")
    artifacts = Path(record["artifacts"])
    if record.get("status") == "completed_factor_candidate_library_ablation":
        results = record.get("results") or {}
        completed = [value for value in results.values()
                     if isinstance(value, dict) and value.get("status") == "completed"]
        selected = [item for key, item in results.items()
                    if isinstance(item, dict)
                    and item.get("status") == "completed"
                    and (requested_signal is None
                         or requested_signal in {key, item.get("signal")})]
        if len(selected) == 1:
            result = selected[0]
        elif len(completed) != 1:
            raise ValueError(
                "Library ablation contains multiple accounts; pass --signal to select one"
            )
        else:
            result = completed[0]
    else:
        result = record.get("result") or {}
    signal = result.get("signal", "stock_factor_candidate_ridge")
    folder = artifacts / signal
    account_path = folder / "account.json"
    if not account_path.exists():
        candidates = sorted(artifacts.rglob("account.json"))
        matching = [path for path in candidates if path.parent.name == signal]
        if len(matching) == 1:
            account_path = matching[0]
            folder = account_path.parent
        elif len(candidates) == 1:
            account_path = candidates[0]
            folder = account_path.parent
        else:
            raise FileNotFoundError(f"unambiguous saved account not found below {artifacts}")
    return run_path, record, folder, signal, result


def _raw_view(record):
    inputs = record.get("stock_inputs") or record.get("inputs") or {}
    price_view = Path(inputs["price_view"])
    manifest = price_view / "result.json"
    if not manifest.exists():
        return None
    value = json.loads(manifest.read_text(encoding="utf-8"))
    raw_view = value.get("raw_view")
    return str(Path(raw_view).resolve()) if raw_view else None


def _fmt(value):
    return "UNKNOWN" if value is None else f"{value:,.2f}"


def _stale_days(daily):
    if "stale_symbols" not in daily:
        return None
    def has_stale(value):
        if not isinstance(value, str):
            return bool(value)
        try:
            return bool(json.loads(value))
        except json.JSONDecodeError:
            return bool(value.strip("[] "))
    return int(daily.stale_symbols.map(has_stale).sum())


def run(value, requested_signal=None):
    run_path, record, folder, signal, result = _resolve_run(value, requested_signal)
    account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    replay = json.loads((folder / "replay.json").read_text(encoding="utf-8"))
    config = StrategyConfig(**replay["config"]).validate()
    if account.get("broker_binding"):
        raise PermissionError("Attribution consumes unbound historical research accounts only")

    orders = list(account["orders"].values())
    fills = list(account["fills"].values())
    if sum(order["fees"] for order in orders) != sum(fill["fees"] for fill in fills):
        raise ValueError("Order and fill fee totals disagree")
    costs = order_costs(orders, config)
    slippage, fill_details = raw_open_slippage(fills, _raw_view(record))
    assets, asset_details = asset_pnl(account, config)

    daily_path = folder / "daily.csv"
    daily = pd.read_csv(daily_path) if daily_path.exists() else pd.DataFrame()
    initial_cash = float(config.initial_cash)
    final_equity = float(result["final_equity"])
    net_pnl = round(final_equity - initial_cash, 2)
    if abs(float(assets["observed_account_pnl_cny"]) - net_pnl) > 0.01:
        raise ValueError("Asset PnL does not reconcile the saved factor-candidate result")

    totals = {key: round(float(costs[key].sum()), 2) for key in (
        "fees_cny", "commission_cny", "sell_tax_cny", "transfer_cny",
        "minimum_commission_premium_cny")}
    stale = _stale_days(daily)
    exposure = None
    if {"equity", "cash", "receivables"} <= set(daily.columns):
        equity = pd.to_numeric(daily.equity, errors="coerce")
        exposure_values = (equity - pd.to_numeric(daily.cash, errors="coerce")
                           - pd.to_numeric(daily.receivables, errors="coerce")) / equity
        exposure = float(exposure_values.median())

    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "factor_candidate_attributions" / identifier
    artifacts = settings.DATA_ROOT / "research" / "factor_candidate_attributions" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)

    if costs.empty:
        symbol_costs = pd.DataFrame()
    else:
        columns = ["notional_cny", *totals]
        symbol_costs = costs.groupby("symbol")[columns].sum()
        if not fill_details.empty:
            symbol_costs["matched_price_difference_cny"] = fill_details.groupby(
                "symbol").slippage_cny.sum(min_count=1)
            symbol_costs["unmatched_fills"] = (~fill_details.raw_open_matched).groupby(
                fill_details.symbol).sum()
    symbol_costs.to_csv(artifacts / "symbol_costs.csv")
    costs.to_csv(artifacts / "order_costs.csv", index=False)
    fill_details.to_csv(artifacts / "fill_price_differences.csv", index=False)
    asset_details.to_csv(artifacts / "symbol_pnl.csv", index=False)
    pd.DataFrame(assets["classes"]).to_csv(artifacts / "asset_class_pnl.csv", index=False)
    if not costs.empty:
        costs.assign(year=costs.date.str[:4]).groupby("year")[
            ["notional_cny", *totals]
        ].sum().to_csv(artifacts / "annual_costs.csv")
    else:
        pd.DataFrame().to_csv(artifacts / "annual_costs.csv")

    fixed_fill_addback = round(net_pnl + totals["fees_cny"], 2)
    fee_and_slip_addback = None
    if slippage["slippage_cny"] is not None:
        fee_and_slip_addback = round(fixed_fill_addback + slippage["slippage_cny"], 2)
    summary = {
        "id": identifier,
        "status": "completed_factor_candidate_attribution",
        "source_run": str(run_path),
        "source_artifacts": str(folder),
        "signal": signal,
        "source_status": record["status"],
        "account_day": account["day"],
        "sessions": int(result["sessions"]),
        "start": record["start"],
        "end": record["end"],
        "metrics": {key: result[key] for key in (
            "net_return", "max_drawdown", "sharpe_zero_rf_252", "annualized_volatility",
            "worst_day", "final_equity", "stale_valuation_days", "turnover_value",
            "turnover_over_initial", "orders", "fills")},
        "net_pnl_cny": net_pnl,
        "costs": totals,
        "filled_orders": len(costs),
        "minimum_commission_orders": int(costs.at_minimum_commission.sum()) if not costs.empty else 0,
        "average_filled_order_cny": float(costs.notional_cny.mean()) if not costs.empty else None,
        "median_filled_order_cny": float(costs.notional_cny.median()) if not costs.empty else None,
        "raw_open_slippage": slippage,
        "fixed_fill_fee_addback_cny": fixed_fill_addback,
        "fixed_fill_fee_and_slip_addback_cny": fee_and_slip_addback,
        "median_holdings_exposure": exposure,
        "stale_symbol_days": stale,
        "asset_pnl": assets,
        "strategy_validated": False,
        "execution_admitted": False,
        "limitations": LIMITS,
    }
    write_json(artifacts / "summary.json", summary)
    write_json(out / "run.json", {**summary, "artifacts": str(artifacts)})

    top = asset_details.nlargest(5, "observed_pnl_cny") if not asset_details.empty else asset_details
    bottom = asset_details.nsmallest(5, "observed_pnl_cny") if not asset_details.empty else asset_details
    lines = [
        "# Factor Candidate Ledger Attribution",
        "",
        "Read-only historical diagnostic; `NO_TRADE`. This explains the saved ledger path; it does not prove factor alpha or create a counterfactual account.",
        "",
        f"- Source replay: `{run_path}`",
        f"- Period: {record['start']} to {record['end']} ({result['sessions']} sessions)",
        f"- Net PnL: CNY{net_pnl:,.2f}; net return: {result['net_return']:.2%}; MDD: {result['max_drawdown']:.2%}; Sharpe: {result['sharpe_zero_rf_252']:.3f}",
        f"- Turnover: CNY{result['turnover_value']:,.2f} ({result['turnover_over_initial']:.2f}x initial cash); orders/fills: {result['orders']}/{result['fills']}",
        "",
        "## Cost Decomposition",
        "",
        f"- Fees: CNY{totals['fees_cny']:,.2f}; commission: CNY{totals['commission_cny']:,.2f}; sell tax: CNY{totals['sell_tax_cny']:,.2f}; transfer fee: CNY{totals['transfer_cny']:,.2f}.",
        f"- Minimum-commission premium: CNY{totals['minimum_commission_premium_cny']:,.2f} across {summary['minimum_commission_orders']} filled orders.",
        f"- Same-day raw-open price difference: CNY{_fmt(slippage['slippage_cny'])}; matched {slippage['matched_fills']}/{slippage['fills']} fills.",
        f"- Fixed-fill fee addback: CNY{fixed_fill_addback:,.2f}; fee-plus-price addback: {_fmt(fee_and_slip_addback)}. These are diagnostic sensitivities, not a tradable zero-cost result.",
        "",
        "## Saved Asset Contributions",
        "",
        "| Asset class | Observed PnL CNY |",
        "| --- | ---: |",
    ]
    for row in assets["classes"]:
        lines.append(f"| {row['asset_class']} | {row['observed_pnl_cny']:,.2f} |")
    lines += ["", "Top saved security contributions:", ""]
    for row in top.itertuples(index=False):
        lines.append(f"- `{row.symbol}`: CNY{row.observed_pnl_cny:,.2f}")
    lines += ["", "Bottom saved security contributions:", ""]
    for row in bottom.itertuples(index=False):
        lines.append(f"- `{row.symbol}`: CNY{row.observed_pnl_cny:,.2f}")
    lines += ["", "## Limits", "", *["- " + item for item in LIMITS],
              "- Candidate membership was fixed from training diagnostics, but this replay is not an independent post-cutoff holdout.",
              "- The attribution is accounting decomposition of one saved path; it does not identify causal factor alpha, style exposure or market impact.",
              "- The source snapshot cutoff is 2026-09-30 and the run was initiated after that data was inspected; 2026 observations remain development evidence.",
              "", f"Artifacts: `{artifacts}`", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(str(out), flush=True)
    return out, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Factor candidate replay ID or run.json")
    parser.add_argument("--signal", help="Select one signal from a library-ablation run")
    args = parser.parse_args(argv)
    run(args.run, args.signal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
