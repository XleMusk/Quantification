"""Fixed own-total-return ten-month fund timing through the existing ledger."""
import argparse
from ast import literal_eval
from collections import Counter
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..feed import HistoricalFeed
from ..report import metrics as portfolio_metrics
from ..settings import DATA_ROOT, ROOT
from .etf_reconciliation import archived
from .experiment import baseline_ledger_result, write_json
from .fund_replay_snapshot import SCOPE, FUND_SCOPE
from .monthly_timing import implementation_hashes, month_ends, trend_table

SYMBOLS = tuple(code + ".SSE" for code in ("510050", "510300", "510500", "510880", "512100", "588000"))
CROSS_ASSET_SYMBOLS = ("510300.SSE", "511010.SSE", "518880.SSE")
CROSS_ASSET_INPUTS = set(SYMBOLS) | set(CROSS_ASSET_SYMBOLS)
VARIANTS = ("monthly_fixed_sleeves", "own_total_return_10m")
SOURCE = "https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf"
START, END = "2024-01-02", "2025-12-31"


def total_return_growth(base, symbols, end=END):
    """Cash is recognized at ex-date; no future-normalized qfq price is used."""
    days = [d for d in base.calendar if d <= end]
    close = base.raw.loc[base.raw.date.le(end) & base.raw.symbol.isin(symbols)].pivot(
        index="date", columns="symbol", values="close").reindex(index=days, columns=symbols)
    if not np.isfinite(close).all().all() or close.le(0).any().any():
        raise ValueError("Every declared fund session needs a positive observed raw close")
    dividend = pd.DataFrame(0., index=days, columns=symbols)
    for action in base.actions:
        if action["symbol"] not in symbols or action["dividOperateDate"] > end:
            continue
        if action["dividOperateDate"] in dividend.index:
            dividend.at[action["dividOperateDate"], action["symbol"]] += float(action["dividCashPsBeforeTax"])
    # This theoretical reinvestment index is a signal, not account cash/fills.
    growth = (close + dividend).div(close.shift())
    growth.iloc[0] = 1.
    if not np.isfinite(growth).all().all() or growth.le(0).any().any():
        raise ValueError("Invalid causal total-return input")
    return growth


def total_return_table(base, symbols, end=END):
    index = total_return_growth(base, symbols, end).cumprod()
    rows = []
    for symbol in symbols:
        table = trend_table(index[symbol], base.calendar, end)
        on = False
        for row in table.to_dict("records"):
            if row["gate"] in {"risk_on", "risk_off"}:
                on = row["gate"] == "risk_on"
            rows.append({**row, "symbol": symbol, "desired_on": on})
    return pd.DataFrame(rows)


class FundTrendFeed:
    def __init__(self, base, table, variant, symbols=SYMBOLS, end=END):
        if (variant not in VARIANTS or not symbols or len(set(symbols)) != len(symbols)
                or date.fromisoformat(end).isoformat() != end or end > base.manifest["end"]):
            raise ValueError("Unknown fixed fund variant/universe or period boundary")
        self.base, self.variant, self.symbols, self.end = base, variant, tuple(symbols), end
        self.ends = set(month_ends(base.calendar, end))
        self.gates = {(row["date"], row["symbol"]): row for row in table.to_dict("records")}
        self.audit = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Fund diagnostic is not a paper/live signal")
        if day > self.end:
            raise ValueError("Fixed fund diagnostic cutoff exceeded")
        n = len(self.symbols)
        if (config.signal != "equal_weight" or config.rebalance_sessions != 1 or config.holdings != n
                or config.retain_rank != n or abs(config.single_limit - config.gross_limit / n) > 1e-12):
            raise ValueError("Fund policy requires equal fixed sleeves without reallocating idle cash")
        if day not in self.ends:
            return SignalUnavailable("not_month_end")
        candidates = []
        for symbol in self.symbols:
            gate = self.gates.get((day, symbol))
            if gate is None:
                raise ValueError("Missing declared fund month-end input")
            on = self.variant == VARIANTS[0] or gate["desired_on"]
            self.audit.append({"date": day, "symbol": symbol, "variant": self.variant,
                               "gate": gate["gate"], "desired_weight": config.single_limit if on else 0,
                               "total_return_index": gate["index_close"], "sma_10_months": gate["sma_10_months"]})
            if on:
                candidates.append({"symbol": symbol, "score": 1.})
        return SignalFrame.from_legacy(candidates, day, "research:" + self.variant, context)


def account_identity(folder, initial_cash):
    state = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    cash = round(initial_cash * 100)
    for fill in state["fills"].values():
        cash += fill["notional"] * (1 if fill["side"] == "sell" else -1) - fill["fees"]
    cash += sum(e["cash"] for e in state["events"] if e["kind"] == "dividend_paid")
    if cash != state["cash"]:
        raise ValueError("Saved fills/fees/dividend credits do not reconcile account cash")
    return {"cash_residual_cents": state["cash"] - cash,
            "dividend_payments": sum(e["kind"] == "dividend_paid" for e in state["events"])}


def extended_period_metrics(folder, initial_cash):
    daily = pd.read_csv(Path(folder) / "daily.csv", converters={"stale_symbols": literal_eval})
    prefix, extension = daily.loc[daily.date.le(END)], daily.loc[daily.date.gt(END)]
    if prefix.empty or extension.empty or prefix.iloc[-1].date != END:
        raise ValueError("The original period and its closing account mark are required")
    return {"2024_2025_same_account_prefix": portfolio_metrics(prefix.to_dict("records"), initial_cash),
            "2026_same_account_extension": portfolio_metrics(
                extension.to_dict("records"), float(prefix.iloc[-1].equity))}


def run(dataset, paper, *, cross_asset=False, end=END, risk_parity=False, allocation_controls=False):
    if allocation_controls and not risk_parity:
        raise ValueError("Allocation controls require the fixed risk-parity comparison")
    if risk_parity and not cross_asset:
        raise ValueError("The declared risk-parity adaptation uses the three existing asset classes")
    base = HistoricalFeed(dataset)
    symbols = CROSS_ASSET_SYMBOLS if cross_asset else SYMBOLS
    required = CROSS_ASSET_INPUTS if cross_asset else set(SYMBOLS)
    if (base.manifest.get("scope") not in {SCOPE, FUND_SCOPE} or not base.manifest.get("replay_only")
            or set(base.instrument_profiles) != required):
        raise ValueError("The predetermined funds need the reviewed replay-only fund or mixed snapshot")
    for name, digest in base.manifest["file_hashes"].items():
        archived(base.folder, {"file": name, "sha256": digest})
    paper = Path(paper)
    if not paper.read_bytes().startswith(b"%PDF-"):
        raise ValueError("Archive the author's source paper before running the fixed adaptation")
    if (date.fromisoformat(end).isoformat() != end or end < END
            or base.manifest["start"] > START or base.manifest["end"] < end
            or START not in base.calendar or end not in base.calendar):
        raise ValueError("The complete fixed diagnostic period is required")
    n = len(symbols)
    config = StrategyConfig(initial_cash=140000, signal="equal_weight", rebalance_sessions=1,
                            holdings=n, retain_rank=n, single_limit=.8 if risk_parity else .8 / n,
                            gross_limit=.8).validate()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/market_replay" / identifier
    artifacts = DATA_ROOT / "research/fund_trend" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    variants = VARIANTS
    calibration = None
    if risk_parity:
        from .fund_risk import FundRiskFeed, risk_table, SOURCE as RISK_SOURCE
        table = risk_table(base, symbols, end)
        variants = ("monthly_fixed_sleeves", "monthly_equal_risk")
        if allocation_controls:
            from .fund_risk import CONTROL_VARIANTS, static_training_allocation
            calibration = static_training_allocation(base, symbols)
            variants = CONTROL_VARIANTS
    else:
        table = total_return_table(base, symbols, end)
    dates = [day for day in base.calendar if START <= day <= end]
    protocol = {"id": "three_asset_class_own_total_return_10m_v1" if cross_asset else "six_equity_etf_own_total_return_10m_v1", "symbols": symbols,
        "variants": VARIANTS, "source": SOURCE, "paper_sha256": sha256(paper.read_bytes()).hexdigest(),
        "source_review": "Original PDF rules, implementation, equal-asset-class allocation and practical-risk sections read (physical pages21,22,29,35ff); not a full-paper replication",
        "universe_basis": ("Three declared asset classes: existing CSI300 equity benchmark510300, sole reviewed sovereign-bond511010 and gold518880. Semantic class representatives, not ranked by past returns. Other equity styles remain in the source snapshot, not added as distinct classes."
                           if cross_asset else "Six predeclared domestic equity styles, not distinct global asset classes"),
        "snapshot_funds_retained": sorted(base.instrument_profiles),
        "dataset": str(base.folder.resolve()), "dataset_manifest_sha256": sha256((base.folder / "manifest.json").read_bytes()).hexdigest(),
        "config": config.to_dict(), "start": START, "end": end, "sessions": len(dates),
        "rule": "Own causal total-return index above10completed monthly-close SMA: own fixed sleeve on; below: cash; equal retains previous on/off; first9months remain cash. SMA includes current completed month.",
        "allocation": f"Each of {n} predeclared funds has0.8/{n} target; inactive sleeves stay in zero-interest cash; no redistribution, leverage or parameter search",
        "execution": "Both rules first decide2024Jan31close, next-session raw-open simulation through same Engine/Ledger. Explicit dated profiles govern T1 equity or T0 bond/gold resale,100units/mill quotes/limits/commission/slippage/participation. No same-close fills. Partial/blocked orders are not assumed filled or retried between month-ends.",
        "differences": ("Three domestic-listed equity/sovereign-bond/gold classes, NOT original five global asset classes; gold is not the diversified commodity index. " if cross_asset else "Six correlated domestic equity styles, NOT original five global asset classes. ") + "Total-return signal reinvests dividend notionally on ex-date, account receives actual cash only on payment date.80percent gross ceiling, zero cash yield and explicit costs differ from the paper.",
        "terminal_month": "A terminal month-end without a following snapshot session does not invent a target or fill",
        "strategy_validated": False, "execution_admitted": False, "fresh_holdout_claimed": False,
        "limits": base.manifest["limitations"] + ["2024-2025 already inspected diagnostic, not independent validation",
            "2026 market outcomes were previously inspected; a later-date extension is not a fresh untouched selection test",
            "Monthly trend cannot guarantee black-swan protection or exit a suspended/limit-down market",
            f"Snapshot contains {base.manifest.get('stock_symbols', 0)} stocks but this fixed candidate uses {n} declared funds, not all-market strategy ranking",
            "Historical instrument/reference/status/auction-amount evidence remains diagnostic; no paper/live admission"],
        "implementation_sha256": {**implementation_hashes(), "trading/research/fund_trend.py": sha256(Path(__file__).read_bytes()).hexdigest()}}
    if risk_parity:
        protocol.update(id="three_asset_equal_risk_1y_v1", variants=variants, source=RISK_SOURCE,
            source_review="Author PDF physical pages4-8,11 and21-22: risk contributions, long-only ERC, convex formulation and monthly rolling one-year implementation reviewed; not full-paper reproduction",
            rule="Completed month-end: estimate sample covariance from the last252 causal daily total returns, solve positive ERC via CVXPY/CLARABEL and normalize contributions equally across the same3funds",
            allocation="Both cases80percent gross and20percent zero-yield cash, no leverage/shorts or expected-return forecast. Control has equal capital sleeves; challenger has equal estimated variance-risk contributions. No added single-fund diversification cap: maximum80percent; bond capital concentration may be high.",
            execution="Both cases first decide2024Jan31close and use next-session raw-open simulation in the sole Engine/Ledger; same cash/costs/lot/tick/T1-or-T0/limit/participation rules. Partial/blocked orders are not assumed filled or retried between month-ends.",
            differences="Domestic CSI300/sovereign-bond/gold basket rather than the paper universes;252sessions approximates one year; fixed80percent unlevered risk budget, zero-interest cash and explicit costs; theoretical total-return covariance versus actual payment-date cash. This tests covariance-aware allocation, not market timing or maximum-Sharpe optimization.")
        protocol["implementation_sha256"]["trading/research/fund_risk.py"] = sha256(
            Path(__file__).with_name("fund_risk.py").read_bytes()).hexdigest()
        protocol["solver"] = {"library": "CVXPY", "solver": "CLARABEL", "covariance": "sample_ddof1",
                              "window_sessions": 252, "risk_share_error_maximum": 1e-5}
        protocol["limits"] += ["Estimated equal risk is not realized equal risk or a guarantee of tail-loss protection",
                               "Bond concentration and estimation error remain; cash/share rounding can distort realized contributions"]
        if allocation_controls:
            protocol.update(id="three_asset_equal_risk_controls_v1", variants=variants,
                static_2023_calibration=calibration,
                controls="Static ERC uses only all observed2023 warmup returns and freezes its weights, rebalancing monthly. Bond control targets80percent511010/20percentcash. Equal-capital and dynamic252-session ERC controls are unchanged; all four first decide2024Jan31 and share the sole Ledger/costs/caps/calendar.")
            protocol["limits"] += [f"Static calibration uses{calibration['observations']} real daily returns in2023, not an invented252-day window; its covariance sample differs from the rolling ERC window",
                                   "Controls were specified after earlier historical outcomes were inspected, not an independent selection test",
                                   "Sovereign-bond ETF is a rate-risk portfolio, not risk-free cash or a deposit benchmark"]
    write_json(out / "protocol.json", protocol)
    shutil_paper = artifacts / ("Maillard_Roncalli_Teiletche_2009.pdf" if risk_parity else "Faber_2013.pdf")
    shutil_paper.write_bytes(paper.read_bytes())
    table.to_csv(artifacts / "month_end_signals.csv", index=False)
    if calibration is not None:
        write_json(artifacts / "static_2023_allocation.json", calibration)
    record = {"status": "running", "artifacts": str(artifacts), "protocol": protocol, "results": []}
    write_json(out / "run.json", record)
    for variant in variants:
        wrapper = (FundRiskFeed(base, table, variant, symbols, end, static_allocation=calibration)
                   if risk_parity else FundTrendFeed(base, table, variant, symbols, end))
        options = {"portfolio_policy": wrapper.allocate} if risk_parity else {}
        metrics = baseline_ledger_result(wrapper, config, dates, artifacts / variant, **options)
        if metrics["status"] == "completed":
            metrics["account_checks"] = account_identity(artifacts / variant, config.initial_cash)
            if end > END:
                metrics["period_metrics"] = extended_period_metrics(artifacts / variant, config.initial_cash)
        metrics.update(signal=variant, decisions=dict(Counter(r["gate"] for r in wrapper.audit)))
        pd.DataFrame(wrapper.audit).to_csv(artifacts / (variant + "_decisions.csv"), index=False)
        record["results"].append(metrics)
        write_json(out / "run.json", record)
        print(json.dumps(metrics), flush=True)
    record["status"] = "completed_fund_trend_diagnostic" if all(r["status"] == "completed" for r in record["results"]) else "failed_fund_trend_diagnostic"
    write_json(out / "run.json", record)
    pd.DataFrame(record["results"]).to_csv(out / "metrics.csv", index=False)
    report(out, record)
    return out, record


def report(out, record):
    protocol = record["protocol"]
    risk_parity = protocol["id"] in {"three_asset_equal_risk_1y_v1", "three_asset_equal_risk_controls_v1"}
    lines = ["# Fixed fund allocation / " + ("equal-risk comparison" if risk_parity else "own-trend comparison"), "",
             f"Status: {record['status']}. NO_TRADE.",
             f"{protocol['start']} to {protocol['end']}, {protocol['sessions']} sessions,140000CNY.",
             ("Same declared universe,80percent gross, month-end decisions, initial cash, costs and sole Engine/Ledger. Only capital allocation differs; all allow the same single-fund cap."
              if risk_parity else "Same declared universe, sleeves, month-end decisions, initial cash, costs and sole Engine/Ledger. Only own-asset trend gating differs."),
             "Inspected historical diagnostic, not an untouched test or execution admission.", "",
             "| Variant | Status | Net Return | Max Drawdown | Sharpe Zero RF | Worst Day | Orders | Fills |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for result in record["results"]:
        if result["status"] != "completed":
            lines.append(f"| {result['signal']} | failed: {result['error']} | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |")
            continue
        sharpe = result["sharpe_zero_rf_252"]
        lines.append(f"| {result['signal']} | completed | {result['net_return']:.2%} | {abs(result['max_drawdown']):.2%} | "
                     f"{sharpe if sharpe is None else round(sharpe, 4)} | {result['worst_day']:.2%} | {result['orders']} | {result['fills']} |")
    lines += ["", "## Rules And Boundaries", "", protocol["universe_basis"], protocol["rule"],
              protocol["allocation"], protocol["execution"], protocol["differences"],
              *protocol["limits"], "", f"Pinned snapshot: `{protocol['dataset']}`.",
              f"Saved sole-ledger accounts: `{record['artifacts']}`.",
              "No parameter tuning, paper/live/default account changes or native THS operations.",
              f"Primary method: [author PDF]({protocol['source']}).", ""]
    if "static_2023_calibration" in protocol:
        calibration = protocol["static_2023_calibration"]
        lines += ["## Fixed Controls", "", protocol["controls"],
                  f"Static covariance: {calibration['observations']} daily returns, {calibration['window_first']}..{calibration['window_last']}.",
                  "Static requested weights (not time-weighted filled holdings): " + ", ".join(
                      f"{symbol}={.8 * fraction:.4%}" for symbol, fraction in calibration["weight_fractions"].items()) + "; cash=20%.", ""]
    if protocol["end"] > END:
        lines += ["## 2026 Extension Of The Same Account", "",
                  "This carries the2025 closing cash/holdings; it is not a fresh140000CNY account or untouched test.",
                  "Rules, securities and friction remain fixed;2024-2025 is also retained in period_metrics. No retrospective parameter search.", "",
                  "| Variant | Net Return | Max Drawdown | Sharpe Zero RF |",
                  "|---|---:|---:|---:|"]
        for result in record["results"]:
            if "period_metrics" in result:
                values = result["period_metrics"]["2026_same_account_extension"]
                sharpe = values["sharpe_zero_rf_252"]
                lines.append(f"| {result['signal']} | {values['net_return']:.2%} | {abs(values['max_drawdown']):.2%} | "
                             f"{sharpe if sharpe is None else round(sharpe, 4)} |")
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--paper", type=Path, required=True)
    parser.add_argument("--cross-asset", action="store_true",
                        help="Fixed CSI300/sovereign-bond/gold class representatives; default six-equity recipe unchanged")
    parser.add_argument("--end", default=END,
                        help="Explicit extension cutoff on a reviewed snapshot; no change to the strategy rules")
    parser.add_argument("--risk-parity", action="store_true",
                        help="Fixed one-year covariance-aware ERC challenger on the existing three asset classes")
    parser.add_argument("--allocation-controls", action="store_true",
                        help="Add frozen2023 ERC weights and80percent sovereign-bond controls; requires --risk-parity")
    args = parser.parse_args(argv)
    out, result = run(args.dataset, args.paper, cross_asset=args.cross_asset, end=args.end,
                      risk_parity=args.risk_parity, allocation_controls=args.allocation_controls)
    print(out, flush=True)
    return 0 if result["status"] == "completed_fund_trend_diagnostic" else 1
