"""Read-only concentration diagnostics for a saved historical account."""
import argparse
import ast
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from .. import replay_reports, settings
from .experiment import write_json


LIMITS = [
    "Observed symbol PnL is saved cash-flow plus terminal-mark accounting, not time-weighted asset return or causal factor alpha",
    "Daily stress rows identify account-level bad days; they do not allocate a bad day to individual securities",
    "Concentration is computed from the saved account and attribution artifacts, not a counterfactual rebalanced portfolio",
    "Stale marks and blocked exits remain unknown economic loss, never zero loss",
    "Historical research only; no paper/live/broker admission and NO_TRADE",
]


def _top_share(values, n, positive=False):
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if positive:
        series = series[series > 0].sort_values(ascending=False)
    else:
        series = -series[series < 0].sort_values(ascending=True)
    total = float(series.sum())
    return (float(series.head(n).sum()) / total) if total > 0 else None


def symbol_concentration(symbol_pnl, symbol_costs, order_costs):
    """Merge saved symbol PnL, turnover and costs without inventing missing fields."""
    required = {"symbol", "observed_pnl_cny", "fees_cny"}
    if not required.issubset(symbol_pnl.columns):
        raise ValueError("symbol_pnl is missing required columns")
    frame = symbol_pnl.copy()
    frame["observed_pnl_cny"] = pd.to_numeric(frame["observed_pnl_cny"], errors="raise")
    frame["fees_cny"] = pd.to_numeric(frame["fees_cny"], errors="raise")
    if symbol_costs is not None and len(symbol_costs):
        costs = symbol_costs.copy()
        for column in ("notional_cny", "matched_price_difference_cny", "unmatched_fills"):
            if column in costs:
                costs[column] = pd.to_numeric(costs[column], errors="coerce")
        keep = [column for column in ("symbol", "notional_cny", "matched_price_difference_cny", "unmatched_fills")
                if column in costs]
        frame = frame.merge(costs[keep], on="symbol", how="left", validate="one_to_one")
    else:
        frame["notional_cny"] = np.nan
        frame["matched_price_difference_cny"] = np.nan
        frame["unmatched_fills"] = np.nan
    if order_costs is not None and len(order_costs):
        orders = order_costs.copy()
        orders["notional_cny"] = pd.to_numeric(orders["notional_cny"], errors="raise")
        counts = orders.groupby("symbol", as_index=False).agg(
            filled_order_count=("order_id", "nunique"),
            filled_notional_cny=("notional_cny", "sum"),
        )
        frame = frame.merge(counts, on="symbol", how="left", validate="one_to_one")
    else:
        frame["filled_order_count"] = np.nan
        frame["filled_notional_cny"] = np.nan
    frame["pnl_rank"] = frame["observed_pnl_cny"].rank(method="min", ascending=False).astype(int)
    frame["loss_rank"] = frame["observed_pnl_cny"].rank(method="min", ascending=True).astype(int)
    gross_losses = -frame.loc[frame.observed_pnl_cny < 0, "observed_pnl_cny"].sum()
    gross_gains = frame.loc[frame.observed_pnl_cny > 0, "observed_pnl_cny"].sum()
    frame["gross_loss_share"] = np.where(frame.observed_pnl_cny < 0,
                                          -frame.observed_pnl_cny / gross_losses if gross_losses else np.nan,
                                          np.nan)
    frame["gross_gain_share"] = np.where(frame.observed_pnl_cny > 0,
                                          frame.observed_pnl_cny / gross_gains if gross_gains else np.nan,
                                          np.nan)
    return frame.sort_values(["observed_pnl_cny", "symbol"], ascending=[True, True]).reset_index(drop=True)


def daily_stress(daily):
    required = {"date", "equity", "cash", "holdings", "stale_symbols"}
    if not required.issubset(daily.columns):
        raise ValueError("daily account file is missing required columns")
    frame = daily.copy()
    frame["equity"] = pd.to_numeric(frame["equity"], errors="raise")
    frame["daily_return"] = frame.equity.pct_change()
    frame["peak_equity"] = frame.equity.cummax()
    frame["drawdown"] = frame.equity / frame.peak_equity - 1.0
    frame["stale_count"] = frame["stale_symbols"].map(lambda value: len(ast.literal_eval(value))
                                                        if isinstance(value, str) and value else 0)
    frame["stale_count"] = pd.to_numeric(frame["stale_count"], errors="raise").astype(int)
    return frame.sort_values(["daily_return", "date"], na_position="last").reset_index(drop=True)


def summarize(symbols, stress, case, attribution_id):
    pnl = pd.to_numeric(symbols.observed_pnl_cny, errors="raise")
    losses = pnl[pnl < 0]
    gains = pnl[pnl > 0]
    daily_returns = stress.daily_return.dropna()
    return {
        "status": "completed_saved_account_concentration",
        "case_id": case["case_id"],
        "attribution_run": attribution_id,
        "source_account": case.get("source_account"),
        "sessions": int(len(stress)),
        "symbols": int(len(symbols)),
        "positive_symbol_count": int((pnl > 0).sum()),
        "negative_symbol_count": int((pnl < 0).sum()),
        "observed_account_pnl_cny": round(float(pnl.sum()), 2),
        "gross_symbol_losses_cny": round(float(-losses.sum()), 2),
        "gross_symbol_gains_cny": round(float(gains.sum()), 2),
        "top_5_loss_share": _top_share(pnl, 5),
        "top_10_loss_share": _top_share(pnl, 10),
        "top_5_gain_share": _top_share(pnl, 5, positive=True),
        "max_single_symbol_loss_cny": round(float(losses.min()), 2) if len(losses) else None,
        "max_single_symbol_gain_cny": round(float(gains.max()), 2) if len(gains) else None,
        "worst_daily_return": float(daily_returns.min()) if len(daily_returns) else None,
        "worst_daily_drawdown": float(stress.drawdown.min()) if len(stress) else None,
        "worst_day": str(stress.loc[stress.daily_return.idxmin(), "date"]) if len(daily_returns) else None,
        "worst_drawdown_day": str(stress.loc[stress.drawdown.idxmin(), "date"]) if len(stress) else None,
        "stale_valuation_days": int(stress.stale_count.gt(0).sum()),
        "blocked_attempts": case.get("blocked_attempts", {}),
        "execution_admitted": False,
        "strategy_validated": False,
        "limitations": LIMITS,
    }


def run(attribution_id, signal=None):
    if not attribution_id or not all(character.isalnum() or character in "_-" for character in attribution_id):
        raise ValueError("Specify a plain attribution run id")
    factor_report_path = settings.ROOT / "outputs/factor_candidate_attributions" / attribution_id / "run.json"
    is_factor_attribution = factor_report_path.exists()
    report_path = (factor_report_path if is_factor_attribution else
                   settings.ROOT / "outputs/loss_attribution" / attribution_id / "run.json")
    if not report_path.exists():
        raise FileNotFoundError(f"Attribution report not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if is_factor_attribution:
        if signal is not None and signal != report.get("signal"):
            raise ValueError("Requested signal is not present in the factor attribution run")
        case = dict(report)
        case["case_id"] = f"factor_candidate_attribution:{report.get('signal')}"
        case["status"] = "completed"
    else:
        cases = report.get("cases", [])
        if signal is None:
            if len(cases) != 1:
                raise ValueError("Specify --signal when the attribution run has multiple cases")
            case = cases[0]
        else:
            case = next((row for row in cases if row["case_id"].rsplit(":", 1)[-1] == signal), None)
            if case is None:
                raise ValueError("Requested signal is not present in the attribution run")
    if case.get("status") != "completed" or case.get("execution_admitted") is not False:
        raise ValueError("Only a completed unbound attribution case can be consumed")
    if is_factor_attribution:
        artifact_dir = Path(report["artifacts"])
        source_account = Path(report["source_artifacts"])
    else:
        source_run_id, source_signal = case["case_id"].split(":", 1)
        artifact_dir = Path(report["artifacts"]) / f"{source_run_id}_{source_signal}"
    if not (artifact_dir / "symbol_pnl.csv").exists():
        raise FileNotFoundError(f"Saved attribution detail not found: {artifact_dir}")
    symbol_pnl = pd.read_csv(artifact_dir / "symbol_pnl.csv")
    symbol_costs = pd.read_csv(artifact_dir / "symbol_costs.csv")
    order_costs = pd.read_csv(artifact_dir / "order_costs.csv")
    if not is_factor_attribution:
        source_record, _ = replay_reports._read(source_run_id)
        if source_signal != case["case_id"].rsplit(":", 1)[-1]:
            raise ValueError("Saved case identity is malformed")
        source_account = Path(source_record["artifacts"]) / source_signal
    if not (source_account / "daily.csv").exists():
        raise FileNotFoundError(f"Saved source account daily file not found: {source_account}")
    case = dict(case)
    case["source_account"] = str(source_account)
    account = json.loads((source_account / "account.json").read_text(encoding="utf-8"))
    blocked = Counter(event.get("reason", "unknown") for event in account.get("events", [])
                      if event.get("kind") == "order_blocked")
    case["blocked_attempts"] = dict(sorted(blocked.items()))
    daily = pd.read_csv(source_account / "daily.csv", dtype={"date": str, "stale_symbols": str})
    symbols = symbol_concentration(symbol_pnl, symbol_costs, order_costs)
    stress = daily_stress(daily)
    summary = summarize(symbols, stress, case, attribution_id)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/risk_concentration" / identifier
    artifacts = settings.DATA_ROOT / "research/risk_concentration" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    symbols.to_csv(artifacts / "symbol_concentration.csv", index=False)
    stress.head(20).to_csv(artifacts / "worst_daily_days.csv", index=False)
    write_json(artifacts / "summary.json", summary)
    write_json(out / "run.json", {"id": identifier, **summary, "artifacts": str(artifacts)})
    lines = ["# Saved Account Risk Concentration", "", "Read-only historical diagnostics; NO_TRADE.", "",
             f"Case: `{case['case_id']}`", f"Attribution: `{attribution_id}`", "",
             "| Metric | Value |", "| --- | ---: |",
             f"| Observed account PnL CNY | {summary['observed_account_pnl_cny']:,.2f} |",
             f"| Gross symbol losses CNY | {summary['gross_symbol_losses_cny']:,.2f} |",
             f"| Gross symbol gains CNY | {summary['gross_symbol_gains_cny']:,.2f} |",
             f"| Top 5 loss share | {summary['top_5_loss_share']:.2%} |" if summary["top_5_loss_share"] is not None else "| Top 5 loss share | UNKNOWN |",
             f"| Top 10 loss share | {summary['top_10_loss_share']:.2%} |" if summary["top_10_loss_share"] is not None else "| Top 10 loss share | UNKNOWN |",
             f"| Worst daily return | {summary['worst_daily_return']:.4%} |" if summary["worst_daily_return"] is not None else "| Worst daily return | UNKNOWN |",
             f"| Worst daily drawdown | {summary['worst_daily_drawdown']:.4%} |" if summary["worst_daily_drawdown"] is not None else "| Worst daily drawdown | UNKNOWN |",
             f"| Stale valuation days | {summary['stale_valuation_days']} |", "",
             "## Interpretation", "",
             "The symbol table is a concentration diagnostic for the observed saved account. It is not a factor, industry, or security-level causal attribution; the daily table identifies stress dates without assigning their loss to individual holdings.", "",
             *["- " + value for value in LIMITS], "", f"Artifacts: `{artifacts}`", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(str(out), flush=True)
    return out, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attribution", required=True, help="Saved loss-attribution run id")
    parser.add_argument("--signal", help="Signal when the attribution run has multiple cases")
    args = parser.parse_args(argv)
    run(args.attribution, args.signal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
