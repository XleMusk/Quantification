"""Fixed profitability/value-family candidates through the existing stock ledger."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time
import uuid

from ..config import StrategyConfig
from ..report import metrics as portfolio_metrics
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .capital_allocation_replay import SavedRankFeed, validate_control_cache
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .family_portfolios import START, END, bind_stock_inputs
from .market_replay import validate_prefix_inputs
from .profitability_inputs import QARP_ID, QARP_CASES, QARP_FORMULAS
from .profitability_portfolios import FIXED_PERIOD_ID, CASES as ORIGINAL_CASES
from .stock_defensive import stage_metrics


def validate_resume(previous, current):
    if (previous.get("research_family") != QARP_ID
            or previous.get("status") not in {"completed_qarp_diagnostics", "failed_qarp_diagnostics"}
            or any(previous.get(key) != current.get(key) for key in
                   ("source_run", "inputs_run", "rankings", "start", "end", "sessions", "symbols", "scope", "dataset"))
            or [row["signal"] for row in previous["results"]] != list(QARP_CASES)):
        raise ValueError("Same terminal fixed QARP candidates and original scope required")
    for key in ("formulas", "signal_portfolio_configs", "financial_view", "financial_receipt", "cohort_policy"):
        if previous["protocol"].get(key) != current["protocol"].get(key):
            raise ValueError("Fixed QARP continuation input changed: " + key)


def report(out, record):
    lines = ["# Fixed Profitability And Valuation / Sole Ledger", "",
        f"{START}..{END};140000CNY; fixed monthly20/rank30, original costs and execution. NO_TRADE.",
        "Two fixed equal-family candidates; no weights/thresholds/period search or new ledger.",
        "", "| Case | Status | Sessions | Net | MDD | Sharpe | Fees CNY | 2026 Carry Net | Stale Days |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in record["results"] + record["controls"]:
        if row["status"] != "completed":
            lines += [f"| {row['signal']} | UNKNOWN | {row['completed_sessions']}/{record['sessions']} | unknown | unknown | unknown | unknown | unknown | unknown |",
                      row.get("error", "Incomplete fixed account")]
        else:
            annual = next(stage for stage in row["annual_same_account"] if stage["year"] == "2026")
            status = ("saved control" if row.get("replayed") is False else
                      "refreshed control" if row.get("replayed") is True else "complete")
            lines.append(f"| {row['signal']} | {status} | "
                f"{row['completed_sessions']} | {row['net_return']:.2%} | {abs(row['max_drawdown']):.2%} | "
                f"{row['sharpe_zero_rf_252']:.4f} | {row['fees']:.2f} | {annual['net_return']:.2%} | "
                f"{row.get('stale_valuation_days', 'UNKNOWN')} |")
    lines += ["", "## Fixed Candidate Formulas", ""] + [f"- {key}: {formula}" for key, formula in QARP_FORMULAS.items()]
    lines += ["", "## Interpretation", "",
        "- Exact same financial pool. Pricing features come from its original contemporaneous value pool, not a different sample.",
        "- Existing raw-price Engine/Ledger, T+1, lots, costs, cash/position limits and company-action guards. Held unknown terms stop that case; no invented fractional credits.",
        "- Compatible financial controls are reused; explicitly refreshed affected controls are labelled separately. Annual windows carry the same account without cash resets.",
        "- Public QARP idea adapted using only two quarterly provider profitability proxies and two valuation yields. NOT annual six-measure QMJ or full QARP replication.",
        "- High PB candidate exposure is a descriptive observation, not a causal loss explanation or a guarantee that adding value will help.",
        "- Stale valuation days retain old marks; return, drawdown and Sharpe do not establish liquidation value or reliable tail protection.",
        "- Already-inspected2025/2026 development; no independent/PIT/forward validation, default change or execution admission.",
        f"Accounts/orders/fills/checkpoints: {record['artifacts']}"]
    lines += ["", "## Sources", ""] + [f"- [{s['read']}]({s['url']}): {s['finding']}" for s in record["protocol"]["sources"]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(inputs_id, control_id, *, event_view=None, reference_view=None, resume_run=None, restart_cases=(),
        refresh_controls=False):
    inputs_path = ROOT / "outputs/profitability_inputs" / inputs_id / "result.json"
    features = json.loads(inputs_path.read_text(encoding="utf-8"))
    spec = features["signal_specification"]
    if (features.get("status") != "completed_frozen_profitability_valuation"
            or features.get("execution_admitted") is not False or features.get("returns_evaluated") is not False
            or spec.get("id") != QARP_ID or spec.get("signals") != list(QARP_CASES)
            or spec.get("formulas") != QARP_FORMULAS):
        raise ValueError("Frozen fixed QARP ranks required, not a searched variant")
    source_path = Path(features["source_run"])
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("research_family") != FIXED_PERIOD_ID or source.get("execution_admitted") is not False
            or source.get("status") != "completed_profitability_diagnostics"
            or source["protocol"]["portfolio"] != spec["portfolio"]):
        raise ValueError("Original fixed financial scope and configuration required")
    cached = json.loads((ROOT / "outputs/market_replay" / control_id / "run.json").read_text(encoding="utf-8"))
    controls = validate_control_cache(cached, source_path, source)
    previous = (json.loads((ROOT / "outputs/market_replay" / resume_run / "run.json").read_text(encoding="utf-8"))
                if resume_run else None)
    restart_cases = set(restart_cases)
    if restart_cases and (previous is None or not restart_cases <= set(QARP_CASES)):
        raise ValueError("Explicit restart requires a prior terminal QARP case")
    config = StrategyConfig(**spec["portfolio"]).validate()
    inputs = bind_stock_inputs(cached["protocol"]["stock_inputs"], event_view, reference_view)
    name = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / name, DATA_ROOT / "research/market_replay" / name
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    protocol = {**source["protocol"], "id": QARP_ID, "signals": list(QARP_CASES), "formulas": QARP_FORMULAS,
        "signal_portfolio_configs": {case: config.to_dict() for case in QARP_CASES}, "stock_inputs": inputs,
        "sources": features["sources"], "inputs_run": str(inputs_path), "input_features": features["artifacts"],
        "fresh_holdout_claimed": False, "execution_admitted": False}
    record = {"id": name, "status": "running", "research_family": QARP_ID, "purpose": QARP_ID,
        **{key: source[key] for key in ("start", "end", "sessions", "symbols", "scope", "dataset")},
        "source_run": str(source_path), "inputs_run": str(inputs_path), "control_run": control_id,
        "rankings": features["rankings"], "artifacts": str(artifacts), "pool_summary": source["pool_summary"],
        "protocol": protocol, "results": [], "controls": [], "resume_run": resume_run,
        "refresh_controls": refresh_controls,
        "restart_cases": sorted(restart_cases), "strategy_validated": False, "execution_admitted": False}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    base, started = None, time.monotonic()
    try:
        base = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["protocol"]["stock_manifest"], base.manifest, None)
        dates = [day for day in base.calendar if START <= day <= END]
        if not dates or (dates[0], dates[-1], len(dates)) != (START, END, source["sessions"]):
            raise ValueError("Fixed financial calendar changed")
        protocol["stock_manifest"] = base.manifest
        if previous:
            validate_resume(previous, record)
        write_json(out / "protocol.json", protocol)
        for case in ORIGINAL_CASES:
            control = controls[case]
            original_config = StrategyConfig(**source["protocol"]["signal_portfolio_configs"][case]).validate()
            if (original_config != config or control["status"] != "completed"
                    or not control.get("comparable_under_current_sources")):
                raise ValueError("Complete compatible same-config financial controls required")
            manifest = cached["protocol"]["stock_manifest"]
            if previous:
                prior_control = next((row for row in previous["controls"] if row["signal"] == case), None)
                if (prior_control and prior_control["status"] == "completed"
                        and prior_control.get("comparable_under_current_sources")):
                    control, manifest = prior_control, previous["protocol"]["stock_manifest"]
            folder = Path(control["source_artifacts"])
            checkpoint = load_replay_checkpoint(folder, config, dates)
            if len(checkpoint["daily"]) != len(dates):
                raise ValueError("Saved matched control incomplete")
            try:
                ignored = validate_prefix_inputs(manifest, base.manifest, checkpoint["state"]["day"], checkpoint["state"])
                result = {**control, **portfolio_metrics(checkpoint["daily"], config.initial_cash),
                    "annual_same_account": stage_metrics(folder, config.initial_cash), "replayed": False,
                    "uninvolved_source_changes": ignored, "comparison_error": None}
            except ValueError as error:
                if not refresh_controls:
                    raise
                destination = artifacts / "controls" / case
                feed = SavedRankFeed(base, Path(source["rankings"]) / case, FIXED_PERIOD_ID + ":" + case, config, dates)
                result = {"signal": case, **baseline_ledger_result(feed, config, dates, destination),
                    "source_artifacts": str(destination), "original_source_artifacts": str(folder),
                    "replayed": True, "source_refresh_reason": str(error),
                    "comparable_under_current_sources": False}
                result["comparable_under_current_sources"] = result["status"] == "completed"
                if result["completed_sessions"]:
                    result["annual_same_account"] = stage_metrics(destination, config.initial_cash)
                print(f"Refreshed control {case}: {result['status']} {result['completed_sessions']}/{len(dates)}", flush=True)
            record["controls"].append(result)
            write_json(out / "run.json", record)
        for case in QARP_CASES:
            feed = SavedRankFeed(base, Path(features["rankings"]) / case, QARP_ID + ":" + case, config, dates)
            options = {}
            if previous and case not in restart_cases:
                prior = Path(previous["artifacts"]) / case
                checkpoint = load_replay_checkpoint(prior, config, dates)
                validate_prefix_inputs(previous["protocol"]["stock_manifest"], base.manifest,
                                       checkpoint["state"]["day"], checkpoint["state"])
                options["checkpoint"] = checkpoint
            destination = artifacts / case
            result = {"signal": case, **baseline_ledger_result(feed, config, dates, destination, **options)}
            if result["completed_sessions"]:
                result["annual_same_account"] = stage_metrics(destination, config.initial_cash)
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{case}: {result['status']} {result['completed_sessions']}/{len(dates)}; net={result.get('net_return')}", flush=True)
        record["status"] = ("completed_qarp_diagnostics" if all(row["status"] == "completed" for row in record["results"] + record["controls"])
                            else "failed_qarp_diagnostics")
        report(out, record)
        return out, record
    except Exception as exc:
        record.update(status="failed_qarp_preparation", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if base is not None:
            base.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
        print(str(out), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs-run")
    parser.add_argument("--control-run")
    parser.add_argument("--prepare-actions")
    parser.add_argument("--primary-view")
    parser.add_argument("--max-fresh", type=int, default=64)
    parser.add_argument("--event-view")
    parser.add_argument("--reference-view")
    parser.add_argument("--resume-run")
    parser.add_argument("--restart-case", action="append", default=[])
    parser.add_argument("--refresh-controls", action="store_true", help="Replay only source-incompatible controls; preserve original fixed ranks and costs")
    args = parser.parse_args(argv)
    if args.prepare_actions:
        if (not args.primary_view or any((args.inputs_run, args.control_run, args.event_view,
                                         args.reference_view, args.resume_run, args.restart_case, args.refresh_controls))):
            parser.error("Action preparation requires --primary-view and no replay options")
        from .value_portfolios import prepare_actions
        result = prepare_actions(args.prepare_actions, args.primary_view, args.max_fresh, qarp=True)
        return 0 if result["status"] == "completed_qarp_action_preparation" else 1
    if not args.inputs_run or not args.control_run or args.primary_view:
        parser.error("Replay requires --inputs-run and --control-run, without --primary-view")
    _, record = run(args.inputs_run, args.control_run, event_view=args.event_view, reference_view=args.reference_view,
                    resume_run=args.resume_run, restart_cases=args.restart_case, refresh_controls=args.refresh_controls)
    return 0 if record["status"] == "completed_qarp_diagnostics" else 1
