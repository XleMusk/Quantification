"""One fixed positive 12-1 momentum gate after the original month-end selector."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
import math
from pathlib import Path
import time
import uuid

import pandas as pd

from ..config import StrategyConfig
from ..contracts import IntentKind, TradingMode
from ..report import metrics as portfolio_metrics
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .capital_allocation_replay import SavedRankFeed, source_layout, validate_control_cache
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .family_portfolios import START, END, bind_stock_inputs
from .market_replay import validate_prefix_inputs
from .stock_defensive import stage_metrics

FAMILY = "fixed_positive_momentum_exit_v1"
CASES = ("profitability_only", "profit_matched_low_vol_half_momentum")
SOURCE = "https://www.naaim.org/wp-content/uploads/2013/10/00D_Absolute-Momentum_gary_antonacci.pdf"


class PositiveMomentumExit:
    def __init__(self, folder, config, source_run):
        if (config.lookback, config.skip, config.holdings, config.retain_rank) != (252, 21, 20, 30):
            raise ValueError("Gate requires the unchanged 252/21,20/rank30 source")
        ranks = pd.read_csv(Path(folder) / "rankings.csv", dtype={"date": str, "symbol": str})
        self.by_day, self.audit = {}, []
        for day, rows in ranks.groupby("date", sort=True):
            if (rows.symbol.duplicated().any()
                    or not rows.momentum.map(lambda value: pd.notna(value) and math.isfinite(value)).all()
                    or not rows.source_pub_date.lt(day).all()
                    or not rows.source_stat_date.le(rows.source_pub_date).all()):
                raise ValueError("Gate needs finite, publication-lagged saved momentum on every rank")
            self.by_day[day] = dict(zip(rows.symbol, rows.momentum))
        self.contract = {
            "id": FAMILY, "config": config.to_dict(), "source_run": str(source_run),
            "rankings_path": str(Path(folder) / "rankings.csv"),
            "feature": "adjusted_close.shift(21)/adjusted_close.shift(252)-1",
            "threshold": 0, "cash_return": 0, "timing": "after_original_close_selector",
            "replacement": False, "rescaling": False,
            "decision_momentum": self.by_day,
        }

    def __call__(self, intent):
        if (intent.context is None or intent.context.mode != TradingMode.REPLAY
                or intent.kind != IntentKind.FULL_TARGET):
            raise ValueError("Fixed exit is only an unbound historical full-target overlay")
        day = pd.Timestamp(intent.decided_at).tz_convert("Asia/Shanghai").date().isoformat()
        if day not in self.by_day:
            raise ValueError("Gate decision outside the saved monthly ranks")
        values, targets, rows = self.by_day[day], {}, []
        for symbol, weight in intent.weights:
            if symbol not in values:
                raise ValueError("Selected momentum unknown; never inferred as zero")
            value = values[symbol]
            targets[symbol] = weight if value > 0 else 0.0
            rows.append({"date": day, "symbol": symbol, "momentum": value,
                         "original_weight": weight, "supplied_weight": targets[symbol],
                         "gate_passed": value > 0})
        self.audit.extend(rows)
        return replace(intent, weights=tuple(targets.items()), source="research:" + FAMILY,
                       reason="nonpositive_saved_momentum_to_cash_no_replacement")


def validate_resume(previous, current):
    if (previous.get("status") not in {"completed_momentum_exit_diagnostics", "failed_momentum_exit_diagnostics"}
            or previous.get("research_family") != FAMILY
            or any(previous.get(key) != current.get(key) for key in
                   ("source_run", "start", "end", "sessions", "symbols", "scope", "dataset"))
            or [row["signal"] for row in previous["results"]] != list(CASES)):
        raise ValueError("Continuation requires the same terminal fixed-exit pair")
    for key in ("signal_portfolio_configs", "target_policy_contracts", "financial_view",
                "financial_receipt", "cohort_policy", "formulas", "source_rankings"):
        if previous["protocol"].get(key) != current["protocol"].get(key):
            raise ValueError(f"Fixed-exit continuation changed: {key}")


def report(out, record):
    controls = {row["signal"]: row for row in record["controls"]}
    lines = ["# Fixed Positive-Momentum Exit / Matched Ledger Comparison", "",
             f"{START}..{END};140000CNY; NO_TRADE. Two fixed cases, one existing Engine/Ledger.", "",
             "| Case | Status | Net | MDD | Sharpe | Net vs control pp | MDD change pp | 2026 carry net |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in record["results"]:
        if row["status"] != "completed":
            lines += [f"| {row['signal']} | UNKNOWN {row['completed_sessions']}/{record['sessions']} | unknown | unknown | unknown | unknown | unknown | unknown |",
                      row["error"]]
            continue
        control = controls[row["signal"]]
        row["net_return_difference"] = row["net_return"] - control["net_return"]
        row["drawdown_magnitude_difference"] = abs(row["max_drawdown"]) - abs(control["max_drawdown"])
        annual = next(stage for stage in row["annual_same_account"] if stage["year"] == "2026")
        lines.append(f"| {row['signal']} | complete | {row['net_return']:.2%} | {abs(row['max_drawdown']):.2%} | "
                     f"{row['sharpe_zero_rf_252']:.4f} | {100*row['net_return_difference']:.2f} | "
                     f"{100*row['drawdown_magnitude_difference']:.2f} | {annual['net_return']:.2%} |")
    lines += ["", "## Rule and Interpretation", "",
              "- After the original month-end20/rank30 selector, keep each selected weight only when its saved252-minus21 adjusted-price momentum is strictly positive. Otherwise supply explicit zero weight; cash earns zero. No replacement, reranking or normalization.",
              "- Same financial cohort, scores, selector, raw-price execution, T+1, board lots, caps, fees, slippage, reference and corporate-action guards. No new lot allocation or fee buffer. Failed prefixes are not full returns.",
              "- The gate can change later holdings and hence buffer retention; this is a complete policy-path comparison, not isolated attribution of a single exit or causal proof of negative momentum.",
              "- Controls are source-compatible saved accounts, not newly replayed. Annual results carry previous-year equity; no cash reset.",
              f"- Method inspiration: [Antonacci, Absolute Momentum (2013)]({SOURCE}), introduction/method/formation sections read, NOT the full paper. Original uses asset excess return vs90-day Treasury bills/monthly costs and month-end drawdown. This stock252-minus21/zero-cash/daily-ledger adaptation is NOT an exact reproduction.",
              "- Threshold, window, pair and costs fixed before this run, but motivated by already-inspected2025/2026 losses. Not preregistered, independent validation, black-swan protection or expected profitability. No deployment/default change.",
              f"Saved accounts/orders/fills/gate observations: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id, control_id, *, event_view=None, reference_view=None, resume_run=None, restart_cases=()):
    source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    _, _, profitability = source_layout(source)
    if not profitability:
        raise ValueError("Fixed exit requires the reporting-calendar financial source")
    cached = json.loads((ROOT / "outputs/market_replay" / control_id / "run.json").read_text(encoding="utf-8"))
    controls = validate_control_cache(cached, source_path, source)
    previous = (json.loads((ROOT / "outputs/market_replay" / resume_run / "run.json").read_text(encoding="utf-8"))
                if resume_run else None)
    restart_cases = set(restart_cases)
    if restart_cases and (previous is None or not restart_cases <= set(CASES)):
        raise ValueError("Restart must explicitly name existing terminal fixed-exit cases")
    inputs = bind_stock_inputs(cached["protocol"]["stock_inputs"], event_view, reference_view)
    configs = {case: StrategyConfig(**source["protocol"]["signal_portfolio_configs"][case]).validate() for case in CASES}
    gates = {case: PositiveMomentumExit(Path(source["rankings"]) / case, configs[case], source_path) for case in CASES}
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    protocol = {**source["protocol"], "id": FAMILY, "signals": list(CASES), "stock_inputs": inputs,
                "source_rankings": source["rankings"], "signal_source_family": source["research_family"],
                "signal_portfolio_configs": {case: config.to_dict() for case, config in configs.items()},
                "target_policy_contracts": {case: gate.contract for case, gate in gates.items()},
                "method_source": SOURCE, "policy_parameters_searched": False, "lot_allocation": None,
                "fresh_holdout_claimed": False, "execution_admitted": False,
                "limitations": source["protocol"].get("limitations", []) + [
                    "Positive252-minus21 stock momentum vs zero-yield cash is an adaptation, not the original Treasury-bill excess-return method",
                    "Reduced exposure and changed retention paths are not isolated alpha or causal exit attribution",
                    "Already-inspected development; threshold/window/costs fixed, no independent validation or deployment",
                ]}
    record = {"id": identifier, "status": "running", "research_family": FAMILY, "purpose": FAMILY,
              **{key: source[key] for key in ("start", "end", "sessions", "symbols", "scope", "dataset")},
              "source_run": str(source_path), "control_run": control_id, "artifacts": str(artifacts),
              "protocol": protocol, "results": [], "controls": [], "resume_run": resume_run,
              "restart_cases": sorted(restart_cases), "strategy_validated": False, "execution_admitted": False}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    base, started = None, time.monotonic()
    try:
        base = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["protocol"]["stock_manifest"], base.manifest, None)
        protocol["stock_manifest"] = base.manifest
        dates = [day for day in base.calendar if START <= day <= END]
        if not dates or (dates[0], dates[-1], len(dates)) != (START, END, source["sessions"]):
            raise ValueError("Fixed-exit calendar changed")
        if previous:
            validate_resume(previous, record)
        write_json(out / "protocol.json", protocol)
        for case in CASES:
            config, gate = configs[case], gates[case]
            control = controls[case]
            if control["status"] != "completed" or not control.get("comparable_under_current_sources"):
                raise ValueError("Fixed exit needs complete, comparable saved controls")
            folder = Path(control["source_artifacts"])
            control_checkpoint = load_replay_checkpoint(folder, config, dates)
            if len(control_checkpoint["daily"]) != len(dates):
                raise ValueError("Matched control is incomplete")
            ignored = validate_prefix_inputs(cached["protocol"]["stock_manifest"], base.manifest,
                                             control_checkpoint["state"]["day"], control_checkpoint["state"])
            record["controls"].append({**control, **portfolio_metrics(control_checkpoint["daily"], config.initial_cash),
                                       "replayed": False, "uninvolved_source_changes": ignored})
            feed = SavedRankFeed(base, Path(source["rankings"]) / case, case, config, dates)
            options, prior_rows = {}, pd.DataFrame()
            if previous and case not in restart_cases:
                prior = Path(previous["artifacts"]) / case
                checkpoint = load_replay_checkpoint(prior, config, dates, target_policy_contract=gate.contract)
                validate_prefix_inputs(previous["protocol"]["stock_manifest"], base.manifest,
                                       checkpoint["state"]["day"], checkpoint["state"])
                options["checkpoint"] = checkpoint
                prior_rows = pd.read_csv(prior / "gate.csv", dtype={"date": str, "symbol": str})
            destination = artifacts / case
            result = {"signal": case, "control": case,
                      **baseline_ledger_result(feed, config, dates, destination, target_policy=gate,
                                              target_policy_contract=gate.contract, **options)}
            receipt = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
            audit = pd.DataFrame(gate.audit, columns=("date", "symbol", "momentum", "original_weight",
                                                      "supplied_weight", "gate_passed"))
            if len(audit):
                last = receipt["last_completed_session"]
                audit = audit.loc[audit.date.le(last)] if last is not None else audit.iloc[:0]
            audit = pd.concat([prior_rows, audit], ignore_index=True)
            audit.to_csv(destination / "gate.csv", index=False)
            result.update(gated_selected_slots=int(audit.supplied_weight.eq(0).sum()) if len(audit) else 0,
                          selected_slots=len(audit), decision_anchors=int(audit.date.nunique()) if len(audit) else 0)
            if result["completed_sessions"]:
                result["annual_same_account"] = stage_metrics(destination, config.initial_cash)
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{case}: {result['status']} {result['completed_sessions']}/{len(dates)}; net={result.get('net_return')}", flush=True)
        record["status"] = ("completed_momentum_exit_diagnostics" if all(row["status"] == "completed" for row in record["results"])
                            else "failed_momentum_exit_diagnostics")
        report(out, record)
        return out, record
    except Exception as error:
        record.update(status="failed_momentum_exit_preparation", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if base is not None:
            base.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
        print(str(out), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--control-run", required=True)
    parser.add_argument("--event-view")
    parser.add_argument("--reference-view")
    parser.add_argument("--resume-run")
    parser.add_argument("--restart-case", action="append", default=[])
    args = parser.parse_args(argv)
    _, record = run(args.source_run, args.control_run, event_view=args.event_view, reference_view=args.reference_view,
                    resume_run=args.resume_run, restart_cases=args.restart_case)
    return 0 if record["status"] == "completed_momentum_exit_diagnostics" else 1
