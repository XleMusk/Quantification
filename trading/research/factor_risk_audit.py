"""Read-only factor coverage, Sharpe and stress audit of completed research runs."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import ROOT
from .experiment import write_json
from .full_market import verify_chunks


ANNUAL_SESSIONS = 252
SHARPE_MIN = 1.5
LATER_SHARPE_MIN = 1.0
MAX_DRAWDOWN = -.10
WORST_DAY = -.03
SHOCK_LOSS_CAP = .10
SHOCK_DAYS = 2
SHOCK_DAILY_RETURN = -.20
STRESS_DATES = 10


def normalize_dates(series):
    return pd.to_datetime(series.astype(str), format="%Y%m%d" if series.astype(str).str.len().eq(8).all() else None).dt.strftime("%Y-%m-%d")


def daily_metrics(daily, *, initial_cash=140000):
    equity = pd.to_numeric(daily.equity, errors="raise")
    if equity.empty or equity.isna().any() or equity.le(0).any():
        raise ValueError("Invalid daily research equity")
    returns = equity.pct_change().fillna(equity.iloc[0] / initial_cash - 1)
    volatility = float(returns.std(ddof=1)) if len(returns) > 1 else float("nan")
    sharpe = float(np.sqrt(ANNUAL_SESSIONS) * returns.mean() / volatility) if volatility > 0 else None
    peaks = equity.cummax().clip(lower=initial_cash)
    five = equity / equity.shift(5) - 1
    tail = float(returns.quantile(.05))
    return {"sessions": len(equity), "return": float(equity.iloc[-1] / initial_cash - 1),
            "sharpe_zero_rf_252": sharpe, "annualized_volatility": float(volatility * np.sqrt(ANNUAL_SESSIONS))
            if np.isfinite(volatility) else None,
            "max_drawdown": float((equity / peaks - 1).min()),
            "worst_day": float(returns.min()), "worst_5_session_window": float(five.min()) if five.notna().any() else None,
            "daily_5pct_tail_mean": float(returns[returns.le(tail)].mean()),
            "final_equity": float(equity.iloc[-1])}


def no_exit_shock(gross, daily_drop, sessions):
    if not 0 <= gross <= 1 or not -1 <= daily_drop <= 0 or sessions < 1:
        raise ValueError("Invalid no-exit shock inputs")
    return gross * (1 - (1 + daily_drop) ** sessions)


def max_gross_for_shock(loss_cap, daily_drop, sessions):
    return min(1., loss_cap / no_exit_shock(1., daily_drop, sessions))


def market_stress(snapshot, manifest, out):
    verify_chunks(snapshot, manifest)
    rows = []
    for group, prefixes in (("sh_sz_all_equities", {"0", "3", "6"}), ("bse_equities", {"9"})):
        paths = [snapshot / item["parquet_file"] for item in manifest["chunks"]
                 if item["prefix"] in prefixes and item["month"].startswith("2026")]
        panel = pd.concat((pd.read_parquet(path, columns=["date", "pct_chg", "volume", "is_st"])
                           for path in paths), ignore_index=True)
        panel = panel.loc[panel.volume.gt(0) & panel.is_st.eq(False) & panel.pct_chg.notna()]
        grouped = panel.groupby("date").pct_chg
        summary = grouped.agg(median_pct="median", mean_pct="mean", members="size",
                              falling_share=lambda x: float(x.lt(0).mean())).reset_index()
        summary["group"] = group
        summary["date"] = normalize_dates(summary.date)
        summary.sort_values("date").to_csv(out / f"{group}_market_daily.csv", index=False)
        rows.extend(summary.nsmallest(STRESS_DATES, "median_pct").to_dict("records"))
    worst = pd.DataFrame(rows).sort_values(["group", "median_pct", "date"])
    worst.to_csv(out / "market_stress_days.csv", index=False)
    return worst


def load_variant_daily(surface, root, artifacts, group, variant, period):
    if surface == "broad_proxy":
        path = root / f"{group}_{variant}_{period}_daily.csv"
    else:
        folder = {"selection": "selection", "later_check": "later_check",
                  "full_period_descriptive": "full_period"}[period]
        path = artifacts / folder / variant / "daily.csv"
    frame = pd.read_csv(path)
    frame["date"] = normalize_dates(frame.date)
    if frame.date.duplicated().any():
        raise ValueError(f"Duplicate daily account/proxy date: {path}")
    return frame


def audit_row(surface, root, artifacts, group, variant, stress_dates):
    periods = {}
    for period in ("selection", "later_check", "full_period_descriptive"):
        daily = load_variant_daily(surface, root, artifacts, group, variant, period)
        periods[period] = daily_metrics(daily)
        if period == "full_period_descriptive":
            returns = pd.Series(daily.equity.pct_change().fillna(0).to_numpy(), index=daily.date)
            stressed = returns.reindex(stress_dates)
            if stressed.isna().any():
                raise ValueError(f"Missing market-stress dates for {group}/{variant}")
            stress = {"worst_market_day_strategy_return": float(stressed.min()),
                      "ten_market_stress_days_compounded": float(np.prod(1 + stressed.to_numpy()) - 1),
                      "positive_on_market_stress_days": int(stressed.gt(0).sum())}
    full, later = periods["full_period_descriptive"], periods["later_check"]
    # Illustrative research screen, not a personalized suitability or trade gate.
    historical_passed = bool(full["sharpe_zero_rf_252"] is not None
                             and later["sharpe_zero_rf_252"] is not None
                             and full["sharpe_zero_rf_252"] >= SHARPE_MIN
                             and later["sharpe_zero_rf_252"] >= LATER_SHARPE_MIN
                             and full["max_drawdown"] >= MAX_DRAWDOWN
                             and full["worst_day"] >= WORST_DAY)
    passed = historical_passed and no_exit_shock(.8, SHOCK_DAILY_RETURN, SHOCK_DAYS) <= SHOCK_LOSS_CAP
    return {"surface": surface, "group": group, "variant": variant,
            "selection_sharpe": periods["selection"]["sharpe_zero_rf_252"],
            "later_sharpe": later["sharpe_zero_rf_252"],
            "later_return": later["return"],
            "full_sharpe": full["sharpe_zero_rf_252"],
            "full_return": full["return"], "full_max_drawdown": full["max_drawdown"],
            "full_worst_day": full["worst_day"], "full_worst_5_session_window": full["worst_5_session_window"],
            "full_daily_5pct_tail_mean": full["daily_5pct_tail_mean"],
            "full_annualized_volatility": full["annualized_volatility"],
            **stress, "historical_metric_screen_passed": historical_passed,
            "illustrative_risk_screen_passed": passed}


def report(out, record, metrics, market_days):
    lines = ["# Factor coverage, Sharpe and stress audit", "",
             "This is a read-only assessment of two different research surfaces. Do not compare",
             "the broad StockDB return proxy with the 250-stock admitted engine/ledger account",
             "as if only the signal changed. Neither surface authorizes a simulated or live order.", "",
             "## Coverage", "",
             "- Completed: seven fixed public-factor variants in each of two broad equity groups",
             "  (42 primary period replays plus 14 no-queue-veto sensitivities).",
             "- Completed: eight variants, including the existing-rule reference, in the 250-stock",
             "  admitted-account replay (24 period runs).",
             "- Completed separately: ten generic broad-universe signal families as IC/top-decile",
             "  diagnostics only; fund-code WorldQuant#101 diagnostics on July-September data.",
             "- NOT exhausted: Qlib's 158 features, WorldQuant's 101 alphas, AQR value/quality",
             "  definitions, MSCI's full risk/minimum-variance model, all-factor combinations,",
             "  or a verified total-return ETF account backtest.", "",
             "Annualized Sharpe below uses zero risk-free return and sqrt(252) times the sample",
             "daily mean/standard deviation. It is descriptive over 178 sessions, not a",
             "statistically certified or selection-bias-corrected Sharpe.", "",
             "| Surface | Group | Variant | Full Sharpe | Later Sharpe | Full return | Max DD | Worst day | Worst 5 sessions | Worst market-day return |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in metrics.to_dict("records"):
        lines.append(f"| {row['surface']} | {row['group']} | {row['variant']} | "
                     f"{row['full_sharpe']:.2f} | {row['later_sharpe']:.2f} | "
                     f"{row['full_return']:.2%} | {row['full_max_drawdown']:.2%} | "
                     f"{row['full_worst_day']:.2%} | {row['full_worst_5_session_window']:.2%} | "
                     f"{row['worst_market_day_strategy_return']:.2%} |")
    lines += ["", "## Realized market stress", "",
              "The ten dates per group are the lowest cross-sectional median daily return",
              "among non-ST, positive-volume stocks in the pinned 2026 snapshot. They are",
              "observed selloffs, not proof of true black-swan robustness.", "",
              "| Group | Worst date | Median stock return | Falling share | Members |",
              "|---|---|---:|---:|---:|"]
    for group, part in market_days.groupby("group"):
        row = part.sort_values("median_pct").iloc[0]
        lines.append(f"| {group} | {row['date']} | {row['median_pct']:.2f}% | "
                     f"{row['falling_share']:.1%} | {row['members']:,} |")
    lines += ["", "## No-exit gap scenarios", "",
              "With 80% invested in the same five stocks and 20% cash, assuming no sales",
              "or hedge can execute during the shock, the incremental portfolio losses are:", "",
              "| Held-stock shock | Sessions | Incremental portfolio loss |",
              "|---|---:|---:|"]
    for shock, sessions in ((-.10, 1), (-.10, 2), (-.20, 1), (-.20, 2), (-.20, 3)):
        lines.append(f"| {shock:.0%} per session | {sessions} | {no_exit_shock(.8, shock, sessions):.2%} |")
    cap = max_gross_for_shock(SHOCK_LOSS_CAP, SHOCK_DAILY_RETURN, SHOCK_DAYS)
    lines += ["", f"To bound the two-session -20% basket scenario to a 10% incremental loss",
              f"without hedges, gross exposure would have to be at most {cap:.1%},",
              "not the 80% used in these replays. This is arithmetic, not a calibrated",
              "market forecast or a guarantee against other losses.", "",
              "## Research boundary", "",
              f"Illustrative thresholds: full Sharpe >= {SHARPE_MIN}, later Sharpe >= {LATER_SHARPE_MIN},",
              f"full max drawdown >= {MAX_DRAWDOWN:.0%}, worst day >= {WORST_DAY:.0%},",
              "and two-session -20% no-exit loss <= 10%. The user has not confirmed",
              "these as personal risk limits. No variant passes the historical-metric",
              "screen even before the no-exit scenario; none passes all tests.",
              "The 2026 sample was previously inspected and the number of informal strategy",
              "trials is unknown; a high observed Sharpe would remain vulnerable to",
              "selection bias and unstable annualization. More point-in-time history,",
              "validated ETF distributions and executable fill rules are needed.", "",
              "Method references: [Lo, Sharpe-ratio statistics](https://alo.mit.edu/research-page/the-statistics-of-sharpe-ratios/);",
              "[Bailey and Lopez de Prado, deflated Sharpe](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf);",
              "[Federal Reserve stress-testing guidance](https://www.federalreserve.gov/frrs/guidance/interagency-supervisory-guidance-on-stress-testing-for-banking-organizations-with-total-consolidated-assets-of-more-than-10.htm)."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(broad_output, account_output, snapshot, out=None):
    broad_output, account_output, snapshot = map(Path, (broad_output, account_output, snapshot))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(out) if out else ROOT / "outputs" / "factor_risk_audit" / identifier
    out.mkdir(parents=True, exist_ok=False)
    broad = json.loads((broad_output / "protocol.json").read_text(encoding="utf-8"))
    account = json.loads((account_output / "protocol.json").read_text(encoding="utf-8"))
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    if broad["status"] != "completed_non_executable_research_proxy" or account["status"] != "completed_research_only":
        raise ValueError("Both referenced runs must be complete")
    if sha256(manifest_bytes).hexdigest() != broad["snapshot_manifest_sha256"]:
        raise ValueError("Full-market snapshot manifest changed since the broad proxy")
    record = {"status": "running", "broad_output": str(broad_output), "account_output": str(account_output),
              "snapshot": str(snapshot), "snapshot_manifest_sha256": broad["snapshot_manifest_sha256"],
              "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "risk_free_daily": 0,
              "annual_sessions": ANNUAL_SESSIONS, "market_stress_dates_per_group": STRESS_DATES,
              "illustrative_thresholds": {"full_sharpe_min": SHARPE_MIN, "later_sharpe_min": LATER_SHARPE_MIN,
                                          "max_drawdown_floor": MAX_DRAWDOWN, "worst_day_floor": WORST_DAY,
                                          "two_day_20pct_basket_loss_cap": SHOCK_LOSS_CAP},
              "trading_authorized": False, "fresh_holdout_claimed": False}
    write_json(out / "protocol.json", record)
    try:
        stress = market_stress(snapshot, json.loads(manifest_bytes), out)
        artifacts = Path(account["artifacts"])
        rows = []
        for surface, root, variants in (("broad_proxy", broad_output, broad["weights"]),
                                        ("admitted_250_account", account_output, [*account["weights"], "existing_rule_reference"])):
            groups = broad["groups"] if surface == "broad_proxy" else {"fixed_250": None}
            for group in groups:
                stress_group = group if surface == "broad_proxy" else "sh_sz_all_equities"
                stress_dates = stress.loc[stress.group.eq(stress_group), "date"].tolist()
                for variant in variants:
                    rows.append(audit_row(surface, root, artifacts, group, variant, stress_dates))
        metrics = pd.DataFrame(rows)
        metrics.to_csv(out / "risk_metrics.csv", index=False)
        record["status"] = "complete_read_only_risk_audit"
        record["audited_variants"] = len(metrics)
        record["historical_metric_screen_passed"] = int(metrics.historical_metric_screen_passed.sum())
        record["illustrative_screen_passed"] = int(metrics.illustrative_risk_screen_passed.sum())
        record["no_exit_80pct_gross_two_day_20pct_loss"] = no_exit_shock(.8, SHOCK_DAILY_RETURN, SHOCK_DAYS)
        record["gross_cap_for_10pct_two_day_20pct_loss"] = max_gross_for_shock(SHOCK_LOSS_CAP, SHOCK_DAILY_RETURN, SHOCK_DAYS)
        report(out, record, metrics, stress)
    except Exception as error:
        record["status"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_json(out / "protocol.json", record)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broad-output", type=Path, required=True)
    parser.add_argument("--account-output", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    print(run(args.broad_output, args.account_output, args.snapshot, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
