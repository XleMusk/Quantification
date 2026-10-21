"""Fixed continuous-information momentum diagnostic through the existing ledger."""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..engine import ordinary_share_terms
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .low_risk import LowRiskFeed, SKIP, WINDOW, features, portfolio
from .market_replay import validate_prefix_inputs
from .stock_defensive import saved_stock_controls, stage_metrics

START, END = "2024-01-02", "2026-09-30"
CONTROL = "momentum_12_1_20"
VARIANT = "continuous_momentum_12_1_20"
SOURCE_URL = "https://academicweb.nd.edu/~zda/Frog.pdf"
FRACTION_SOURCE = "https://static.cninfo.com.cn/finalpage/2024-07-12/1220614824.PDF"


def fractional_allocation_contract(policy):
    if policy is None:
        return None
    if policy not in {"floor", "ceil"}:
        raise ValueError("Choose a fixed floor/ceil ordinary-share sensitivity, not an allocation claim")
    return {"id": f"ordinary_fraction_{policy}_v1", "rounding": policy,
            "source": FRACTION_SOURCE, "mode": "historical_replay_only",
            "basis": "Fixed assumption for every fractional ordinary bonus/transfer; NOT observed account allocation",
            "limits": "Registry-wide remainder ordering/ties are unavailable. Two paths are sensitivities, NOT a global worst-case envelope; future orders can differ",
            "execution_admitted": False}


def check_fractional_continuation(previous, protocol, checkpoint, case):
    old, new = previous.get("fractional_allocation"), protocol.get("fractional_allocation")
    if old == new:
        return
    receipts = checkpoint["state"].get("corporate_action_reconciliations", {}).values()
    if (old is not None or new is None
            or "fractional" not in case.get("error", "").lower()
            or any(row.get("kind") == "ordinary_share_allocation" for row in receipts)
            or any(row.get("allocation_source_kind") for row in checkpoint["state"]["events"])):
        raise ValueError("Cannot change an applied fractional-share policy or reinterpret the ledger prefix")


def information_discreteness(closes):
    """Use exactly the daily returns underlying P[t-21]/P[t-252]."""
    returns = closes.pct_change(fill_method=None)
    returns = returns.where(np.isfinite(returns))
    signs = np.sign(returns)
    # Zero-return trading days stay in the denominator; missing days do not.
    balance = -signs.rolling(WINDOW - SKIP, min_periods=WINDOW - SKIP).mean().shift(SKIP)
    momentum = closes.shift(SKIP) / closes.shift(WINDOW) - 1
    return balance * np.sign(momentum)


def continuous_rank(pool):
    """Fixed sequential quintiles, then momentum; symbol breaks every tie."""
    winners = sorted(pool, key=lambda row: (-row["momentum"], row["symbol"]))
    winners = winners[:(len(winners) + 4) // 5]
    continuous = sorted(winners, key=lambda row: (row["information_discreteness"], row["symbol"]))
    continuous = continuous[:(len(continuous) + 4) // 5]
    return sorted([{**row, "score": row["momentum"]} for row in continuous],
                  key=lambda row: (-row["score"], row["symbol"]))


def control_basis(control_run, base, config, dates, refresh_changed_inputs):
    try:
        return saved_stock_controls(control_run, base, config, dates, (CONTROL,)), None
    except ValueError as error:
        # Only the existing source-prefix guard can justify a new control ledger.
        reasons = ("Past economic action changed for an involved symbol; full replay required",
                   "Past price-reference diagnostics changed; full replay required")
        if not refresh_changed_inputs or str(error) not in reasons:
            raise
        return [], str(error)


class ContinuousMomentumFeed:
    def __init__(self, base, values, information, config, fractional_allocation=None):
        self.base, self.config = base, config
        self.fractional_allocation = fractional_allocation_contract(fractional_allocation)
        self.pool_feed = LowRiskFeed(base, values, CONTROL, END, diagnostic_extension=True)
        self.audit, self.ranking_rows, self.planned = [], [], {}
        for day in sorted(self.pool_feed.ends):
            if not START <= day <= END:
                continue
            pool = self.pool_feed.pool(day, config)
            if isinstance(pool, SignalUnavailable):
                self.planned[day] = (pool.reason, None, None)
                continue
            annotated = [{**row, "information_discreteness": float(information.at[day, row["symbol"]])}
                         for row in pool]
            if any(not np.isfinite(row["information_discreteness"]) for row in annotated):
                raise ValueError("ID must be complete on the unchanged control-eligible pool, not drop stocks")
            ranked = continuous_rank(annotated)
            self.planned[day] = ("monthly_continuous_ranking" if ranked else "known_empty_eligible_pool",
                                 len(pool), ranked)
            self.ranking_rows.extend({"date": day, "rank": i, **row}
                                     for i, row in enumerate(ranked[:config.retain_rank], start=1))

    def __getattr__(self, name):
        return getattr(self.base, name)

    def ordinary_share_allocation_receipt(self, action, quantity, context):
        contract = self.fractional_allocation
        if contract is None:
            return None
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Fractional share sensitivity is historical-replay-only")
        terms = ordinary_share_terms(action)
        exact = quantity * (Decimal(terms["dividStocksPs"]) + Decimal(terms["dividReserveToStockPs"]))
        rounding = ROUND_FLOOR if contract["rounding"] == "floor" else ROUND_CEILING
        return {"kind": "ordinary_share_allocation", "account_id": context.account_id,
                "mode": context.mode.value, "registered_quantity": quantity, "action_terms": terms,
                "allocated_shares": int(exact.to_integral_value(rounding=rounding)),
                "source_kind": "explicit_replay_assumption", "source_ref": contract["id"],
                "allocation_policy": contract["id"]}

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config:
            raise ValueError("Continuous momentum is replay-only with the fixed portfolio/cost config")
        if not START <= day <= END or day not in self.base.day_index:
            raise ValueError("Decision outside the pinned covered calendar")
        reason, size, candidates = self.planned.get(day, ("not_month_end", None, None))
        self.audit.append({"date": day, "variant": VARIANT, "decision": reason,
                           "eligible_count": size,
                           "candidate_count": len(candidates) if candidates is not None else None,
                           "intent": "NO_CHANGE" if candidates is None else "FULL_TARGET"})
        if candidates is None:
            return SignalUnavailable(reason)
        return SignalFrame.from_legacy(candidates, day, "research:" + VARIANT, context)


def run(price_view, event_view, reference_view, listing_master, control_run, source_pdf,
        initial_cash=140000, *, refresh_changed_inputs=False, resume_run=None, fractional_allocation=None):
    config = portfolio(initial_cash)
    fraction_contract = fractional_allocation_contract(fractional_allocation)
    source_pdf = Path(source_pdf).resolve()
    if not source_pdf.read_bytes().startswith(b"%PDF"):
        raise ValueError("Expected saved author PDF, not a blocked HTML response")
    base = BulkResearchFeed(price_view, event_view, reference_view, listing_master)
    try:
        if (base.manifest["scope"] != BULK_SCOPE or START not in base.calendar
                or END not in base.calendar or base.manifest["end"] < END):
            raise ValueError("Require covered full-source diagnostic stock inputs")
        dates = [day for day in base.calendar if START <= day <= END]
        controls, refresh_reason = control_basis(control_run, base, config, dates, refresh_changed_inputs)
        inputs = {key: base.manifest[key] for key in ("price_view", "event_view", "reference_view",
            "raw_view", "listing_master", "source_codes", "canonical_codes", "unsupported_instruments", "limitations")}
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        out = ROOT / "outputs/market_replay" / identifier
        artifacts = DATA_ROOT / "research/market_replay" / identifier
        out.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        signals = [CONTROL, VARIANT] if refresh_reason else [VARIANT]
        protocol = {"id": "continuous_information_momentum_quintiles_v1",
            "portfolio": config.to_dict(), "signals": signals, "saved_control": CONTROL,
            "signal_portfolio_configs": {signal: config.to_dict() for signal in signals}, "bulk_inputs": inputs,
            "source": {"url": SOURCE_URL, "local_pdf": str(source_pdf),
                       "review": "Author PDF sections1-2 and sequential double-sort methodology reviewed; not full-paper replication"},
            "common_pool": "Unchanged control pool: supported ordinary SH/SZ, active non-ST, listing age365, complete252 daily-return volatility,252-minus21 momentum, median20-day amount>=20mCNY",
            "information": "ID=sign(momentum)*(negative_count-positive_count)/231; daily returns at t-251..t-21, matching P[t-21]/P[t-252]. Zero trading returns count in231; missing/halts never become zero. Exactly zero momentum has ID0",
            "selection": "Top ceil(N/5) momentum; within winners lowest ceil(M/5) ID; then descending momentum. Symbol breaks ties at each boundary.20 names/rank30 retention, equal4percent targets/gross80percent. No positive-momentum filter",
            "execution": "Existing Engine/Ledger, completed month-end signals and next-session raw open; stock T1/lots/limits/actions/liquidity/costs unchanged; cash earns zero",
            "paper_differences": "Local long-only20-name portfolio/rank buffer/monthly replacement, not US CRSP long-short quintile baskets or overlapping six-month cohorts.252/21-session windows rather than calendar-month returns; no US5USD price filter, delisting return feed or factor-adjusted alpha",
            "control_basis": ("Existing prefix guard rejected changed past inputs for involved symbols. Original control preserved, same fixed momentum control replayed once on the new common inputs; no signal tuning or curve blending"
                              if refresh_reason else "Completed same-source/cost/dates stock account read-only; existing prefix-input guard retained. No rerun, curve blending or account reset"),
            "hypothesis": "Conditional continuous-information selection may improve net Sharpe/drawdown over simple momentum; retain failures and negative comparisons without tuning",
            "basis": "Fixed literature-inspired adaptation on already-inspected2024-2026; not independent/prospective or untouched validation",
            "tuning": False, "fresh_holdout_claimed": False, "selection_is_point_in_time": False,
            "execution_admitted": False, "limitations": [*base.manifest["limitations"],
                "ID describes return signs, not news arrival directly, and does not measure jump magnitude",
                "Price-derived formation returns are not independently verified dividend-reinvestment total returns",
                "No all-ETF/BSE/PIT coverage, black-swan guarantee, profitability or broker readiness"]}
        protocol["fractional_allocation"] = fraction_contract
        if fraction_contract:
            protocol["limitations"].extend([fraction_contract["basis"], fraction_contract["limits"]])
        record = {"status": "running", "research_family": "continuous_information_momentum",
            "purpose": "fixed_continuous_momentum_paired_diagnostic", "dataset": str(base.folder.resolve()),
            "artifacts": str(artifacts.resolve()), "scope": base.manifest["scope"], "symbols": len(base.symbols),
            "start": START, "end": END, "sessions": len(dates), "inputs": inputs, "protocol": protocol,
            "results": [], "saved_controls": controls, "source_control_run": str(Path(control_run).resolve()),
            "control_refresh_reason": refresh_reason, "strategy_validated": False, "live_ready": False,
            "started_at": datetime.now().astimezone().isoformat()}
        checkpoint = None
        if resume_run is not None:
            previous = json.loads((Path(resume_run) / "run.json").read_text(encoding="utf-8"))
            fields = ("id", "portfolio", "source", "common_pool", "information", "selection", "execution",
                      "paper_differences", "hypothesis", "basis", "tuning", "fresh_holdout_claimed")
            cases = [row for row in previous["results"] if row["signal"] == VARIANT]
            if (previous["status"] != "failed_continuous_momentum_diagnostic"
                    or previous["research_family"] != record["research_family"]
                    or previous["start"] != START or previous["end"] != END
                    or any(previous["protocol"][key] != protocol[key] for key in fields)
                    or len(cases) != 1 or cases[0]["status"] != "failed"):
                raise ValueError("Resume requires the failed unchanged fixed continuous-information account")
            checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / VARIANT, config, dates)
            check_fractional_continuation(previous["protocol"], protocol, checkpoint, cases[0])
            ignored = validate_prefix_inputs(previous["inputs"], base.manifest, checkpoint["state"]["day"], checkpoint["state"])
            record.update(resume_run=str(Path(resume_run).resolve()), prefix_uninvolved_changes=ignored)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        wrapper = None
        try:
            print("Compute fixed full-source monthly continuous-information ranks", flush=True)
            values = features(base, END)
            closes = base.closes.loc[:END].where(base.volumes.loc[:END].gt(0))
            closes = closes.where(np.isfinite(closes) & closes.gt(0))
            information = information_discreteness(closes).reindex(values["momentum"].index)
            wrapper = ContinuousMomentumFeed(base, values, information, config, fractional_allocation)
            if checkpoint is not None:
                prefix = pd.read_csv(Path(previous["artifacts"]) / (VARIANT + "_decisions.csv"))
                prefix = prefix.loc[prefix.date.le(checkpoint["state"]["day"])].copy()
                if prefix.date.tolist() != dates[:len(checkpoint["daily"])]:
                    raise ValueError("Saved decision prefix does not match the continued ledger")
                wrapper.audit.extend(prefix.to_dict("records"))
            pd.DataFrame(wrapper.ranking_rows).to_csv(artifacts / "source_target_ranks.csv", index=False)
            source_symbols = sorted({row["symbol"] for row in wrapper.ranking_rows})
            write_json(artifacts / "source_plan.json", {"start": START, "end": END,
                "source_symbols": source_symbols, "rank_rows": len(wrapper.ranking_rows),
                "basis": "Fixed whole-universe ranks for acquisition only, not a universe cap or executed holdings",
                "execution_admitted": False})
            if refresh_reason:
                print("Changed source inputs require one new fixed momentum control ledger", flush=True)
                control = baseline_ledger_result(LowRiskFeed(base, values, CONTROL, END, diagnostic_extension=True),
                                                 config, dates, artifacts / CONTROL)
                control.update(signal=CONTROL, label="Fixed momentum control (same updated inputs)")
                if control["status"] == "completed":
                    control["annual_same_account"] = stage_metrics(artifacts / CONTROL, initial_cash)
                record["results"].append(control)
                write_json(out / "run.json", record)
            print(f"Replay continuous-information ledger; {len(source_symbols)} source-priority symbols", flush=True)
            result = baseline_ledger_result(wrapper, config, dates, artifacts / VARIANT, checkpoint=checkpoint)
            result.update(signal=VARIANT, label="Continuous-information momentum (fixed sequential quintiles)",
                          decisions=dict(Counter(row["decision"] for row in wrapper.audit)))
            if fraction_contract:
                result["label"] += f" / fractional shares {fractional_allocation} ASSUMPTION"
                result["fractional_allocation_assumption"] = fraction_contract
            if result["status"] == "completed":
                result["annual_same_account"] = stage_metrics(artifacts / VARIANT, initial_cash)
            record["results"].append(result)
            terminal = "completed" if all(row["status"] == "completed" for row in record["results"]) else "failed"
            record["status"] = terminal + "_continuous_momentum_diagnostic"
        except Exception as error:
            record.update(status="failed_continuous_momentum_preparation", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            if wrapper is not None:
                pd.DataFrame(wrapper.audit).to_csv(artifacts / (VARIANT + "_decisions.csv"), index=False)
            record["completed_at"] = datetime.now().astimezone().isoformat()
            write_json(out / "run.json", record)
            report(out, record)
        print(json.dumps({"output": str(out), "status": record["status"], "results": record["results"],
                          "saved_controls": controls}, ensure_ascii=False), flush=True)
        return out
    finally:
        base.close()


def report(out, record):
    rows = record["saved_controls"] + record["results"]
    lines = ["# Fixed Continuous-Information Momentum", "", f"{record['status']}. NO_TRADE.",
        f"{record['symbols']} source identities; source-covered ordinary SH/SZ, not independently all A-shares/ETF. {START}..{END}, {record['sessions']} sessions.",
        record["protocol"]["control_basis"], "",
        "| Case | Net return | Max drawdown | Sharpe zero rf | Fees | Stale days |",
        "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        if row["status"] == "completed":
            sharpe = row["sharpe_zero_rf_252"]
            lines.append(f"| {row['signal']} | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {sharpe if sharpe is None else round(sharpe,4)} | {row['fees']:.2f} | {row['stale_valuation_days']} |")
        else:
            lines.append(f"| {row['signal']} | failed after {row['completed_sessions']} sessions: {row.get('error')} | unavailable | unavailable | unavailable | unavailable |")
    lines += ["", "## Carried Annual Account", "", "| Case | Year | Net return | Max drawdown | Sharpe zero rf |",
              "|---|---|---:|---:|---:|"]
    for row in rows:
        for stage in row.get("annual_same_account", []):
            sharpe = stage["sharpe_zero_rf_252"]
            lines.append(f"| {row['signal']} | {stage['year']} | {stage['net_return']:.2%} | {stage['max_drawdown']:.2%} | {sharpe if sharpe is None else round(sharpe,4)} |")
    lines += ["", "## Rules And Boundaries", "", *[record["protocol"][key] for key in (
        "common_pool", "information", "selection", "execution", "paper_differences", "control_basis", "basis")],
        *record["protocol"]["limitations"], f"Author methods: [PDF]({SOURCE_URL})",
        f"Saved ledger/ranks: `{record['artifacts']}`."]
    if record.get("error"):
        lines.append(record["error"])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("price-view", "event-view", "reference-view", "listing-master", "control-run", "source-pdf"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, default=140000)
    parser.add_argument("--refresh-changed-inputs", action="store_true",
                        help="Replay the fixed control only if existing prefix guard rejects changed economic/reference inputs")
    parser.add_argument("--resume-run", type=Path, help="Continue a failed unchanged case after a compatible source correction")
    parser.add_argument("--fractional-allocation", choices=("floor", "ceil"),
                        help="Fixed replay-only ordinary-share remainder sensitivity; not actual settlement proof")
    args = parser.parse_args(argv)
    out = run(args.price_view, args.event_view, args.reference_view, args.listing_master,
              args.control_run, args.source_pdf, args.initial_cash, refresh_changed_inputs=args.refresh_changed_inputs,
              resume_run=args.resume_run, fractional_allocation=args.fractional_allocation)
    return 0 if json.loads((out / "run.json").read_text(encoding="utf-8"))["status"].startswith("completed_") else 1


if __name__ == "__main__":
    raise SystemExit(main())
