"""Replay a saved factor-candidate ranking with a fixed fee deadband.

The model, candidate membership, dates, inputs and portfolio configuration are
reused unchanged.  Only the existing ledger's explicit internal-rebalance
commission deadband is enabled, so this is a cost-path diagnostic rather than
a tuned strategy or a zero-cost addback.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

from .. import settings
from ..config import StrategyConfig
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .factor_candidate_cadence_replay import BimonthlyCandidateRankFeed
from .factor_candidate_replay import CandidateRankFeed


LIMITS = [
    "The deadband is derived from minimum commission / commission; it was not selected from replay returns",
    "Only internal reweights are deferred; new entries, full exits and risk-limit exits retain the existing ledger rules",
    "The candidate model, rankings, universe, dates, costs and data snapshot are reused; no model refit or parameter search occurs",
    "This changes the actual account path and is not a fixed-fill fee addback or zero-cost counterfactual",
    "The source snapshot cutoff is 2026-09-30 and the source replay was run after that data was inspected; 2026 remains development evidence",
    "No independent post-cutoff holdout, PIT-complete financial source, paper observation or broker verification is claimed",
]


def _resolve_run(value, requested_signal=None):
    candidate = Path(value)
    if candidate.name == "run.json":
        run_path = candidate
    else:
        candidates = [
            settings.ROOT / "outputs" / "factor_candidate_replays" / str(value) / "run.json",
            settings.ROOT / "outputs" / "factor_candidate_library_ablations" / str(value) / "run.json",
        ]
        run_path = next((path for path in candidates if path.exists()), candidates[0])
    if not run_path.exists():
        raise FileNotFoundError(f"factor candidate replay not found: {run_path}")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    artifacts = Path(record["artifacts"])
    status = record.get("status")
    cadence_mode = False
    if status == "completed_factor_candidate_replay":
        source_result = record.get("result") or {}
        if source_result.get("status") != "completed":
            raise ValueError("Completed factor candidate ledger required")
        source_artifacts = artifacts
        inputs = record["stock_inputs"]
        signal = source_result["signal"]
    elif status == "completed_factor_candidate_library_ablation":
        results = record.get("results") or {}
        selected = [item for key, item in results.items()
                    if isinstance(item, dict)
                    and item.get("status") == "completed"
                    and (requested_signal is None
                         or requested_signal in {key, item.get("signal")})]
        if len(selected) != 1:
            raise ValueError("Library ablation requires --signal selecting one completed account")
        source_result = dict(selected[0])
        signal = source_result["signal"]
        folder_name = {"Alpha158": "alpha158", "Alpha360": "alpha360", "Templates": "templates"}.get(
            next((key for key, item in results.items() if item is selected[0]), ""))
        if not folder_name:
            folder_name = next((name for name in ("alpha158", "alpha360", "templates")
                                if (artifacts / name / "predictions" / "rankings.csv").exists()), None)
        if not folder_name:
            raise FileNotFoundError("Library ablation rankings folder not found")
        source_artifacts = artifacts / folder_name
        inputs = record["inputs"]
    elif status == "completed_factor_candidate_family_replay":
        source_result = record.get("result") or {}
        if source_result.get("status") != "completed":
            raise ValueError("Completed factor candidate family ledger required")
        source_artifacts = artifacts
        inputs = record["inputs"]
        signal = source_result["signal"]
    elif status == "completed_factor_candidate_cadence_replay":
        source_result = record.get("result") or {}
        if source_result.get("status") != "completed":
            raise ValueError("Completed factor candidate cadence ledger required")
        # Cadence artifacts contain the realized account, while rankings are
        # intentionally reused from the immutable family replay source.
        source_artifacts = Path(record["source_artifacts"])
        inputs = record["inputs"]
        signal = record["control_signal"]
        cadence_mode = True
    else:
        raise ValueError("Completed factor candidate replay, family replay, cadence replay or library ablation required")
    # Library-ablation receipts predate the explicit policy fields; inherit
    # them from the exact source replay instead of silently changing settlement.
    policy_source = record
    if not policy_source.get("rights_replay_policy") or not policy_source.get("merger_replay_policy"):
        source_run = policy_source.get("source_run")
        if source_run and Path(source_run).exists():
            policy_source = json.loads(Path(source_run).read_text(encoding="utf-8"))
    rights_policy = policy_source.get("rights_replay_policy")
    merger_policy = policy_source.get("merger_replay_policy")
    fraction_contract = record.get("fractional_allocation") or policy_source.get("fractional_allocation")
    fractional_policy = (fraction_contract.get("rounding") if isinstance(fraction_contract, dict)
                         else fraction_contract)
    rankings = source_artifacts / "predictions"
    if not (rankings / "rankings.csv").exists():
        raise FileNotFoundError(f"Saved candidate rankings not found: {rankings}")
    return (run_path, record, source_artifacts, signal, rankings, inputs,
            source_result, rights_policy, merger_policy, fractional_policy, cadence_mode)


def run(value, requested_signal=None):
    (run_path, source, source_artifacts, signal, rankings, inputs, source_result,
     rights_policy, merger_policy, fractional_policy, cadence_mode) = _resolve_run(
        value, requested_signal=requested_signal)
    config = StrategyConfig(**source["portfolio"]).validate()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "factor_candidate_cost_replays" / identifier
    artifacts = settings.DATA_ROOT / "research" / "factor_candidate_cost_replays" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running_factor_candidate_cost_replay",
        "research_family": "factor_candidate_cost_aware_execution",
        "source_run": str(run_path),
        "source_artifacts": str(source_artifacts),
        "signal": signal + "_cost_band",
        "control_signal": signal,
        "inputs": inputs,
        "start": source["start"],
        "end": source["end"],
        "sessions": source_result["sessions"],
        "portfolio": config.to_dict(),
        "execution_policy": {
            "id": "minimum_commission_internal_reweights_v1",
            "minimum_internal_trade_cny": config.minimum_commission / config.commission,
            "derivation": "Configured minimum commission / proportional commission rate; not optimized on returns",
            "exemptions": "New entries, complete/forced exits, and sells when marked single/gross limits are exceeded",
        },
        "candidate_membership_changed": False,
        "model_refit": False,
        "parameters_searched": False,
        "fresh_holdout_claimed": False,
        "execution_admitted": False,
        "fractional_allocation": source.get("fractional_allocation"),
        "artifacts": str(artifacts),
        "result": None,
        "limitations": LIMITS,
    }
    write_json(out / "run.json", record)
    feed = None
    try:
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        dates = [day for day in feed.calendar if source["start"] <= day <= source["end"]]
        if len(dates) != source_result["sessions"]:
            raise ValueError("Pinned source session count differs from the completed factor replay")
        rank_feed = BimonthlyCandidateRankFeed if cadence_mode else CandidateRankFeed
        ranked = rank_feed(
            feed, rankings, signal, config, dates,
            fractional_allocation=fractional_policy,
            rights_replay_policy=rights_policy,
            merger_replay_policy=merger_policy,
        )
        result = baseline_ledger_result(
            ranked, config, dates, artifacts / (signal + "_cost_band"),
            commission_deadband=True,
        )
        result.update(signal=signal + "_cost_band", control_case=str(run_path),
                      control_metrics=source_result,
                      net_return_difference=(result.get("net_return") - source_result["net_return"])
                      if result.get("status") == "completed" else None,
                      fees_difference=(result.get("fees") - source_result["fees"])
                      if result.get("status") == "completed" else None,
                      turnover_difference=(result.get("turnover_value") - source_result["turnover_value"])
                      if result.get("status") == "completed" else None)
        record["result"] = result
        record["status"] = "completed_factor_candidate_cost_replay" if result["status"] == "completed" else "failed_factor_candidate_cost_ledger"
    except Exception as error:
        record.update(status="failed_factor_candidate_cost_replay", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if feed is not None:
            feed.close()
        write_json(out / "run.json", record)
        result = record.get("result") or {}
        if result.get("status") == "completed":
            performance = (f"net {result['net_return']:.2%}, MDD {result['max_drawdown']:.2%}, "
                           f"Sharpe {result['sharpe_zero_rf_252']:.4f}, fees {result['fees']:.2f} CNY")
        else:
            performance = f"ledger status {result.get('status', 'unknown')}: {record.get('error', 'unknown')}"
        lines = [
            "# Factor Candidate Cost-Aware Replay", "", "Research development only; `NO_TRADE`.", "",
            f"Source ranking/model replay: `{run_path}`; rankings and model were reused without refit.",
            f"Execution policy: `{record['execution_policy']['id']}`; fixed internal trade band CNY{record['execution_policy']['minimum_internal_trade_cny']:.2f}.",
            f"Cost-aware ledger: {performance}.", "",
            "## Same-Basis Difference", "",
            f"Net return difference versus source: {result.get('net_return_difference', 'UNKNOWN'):.2%}" if isinstance(result.get('net_return_difference'), (int, float)) else "Net return difference versus source: UNKNOWN",
            f"Fee difference CNY{(result.get('fees_difference') or 0):,.2f}; turnover difference CNY{(result.get('turnover_difference') or 0):,.2f}.",
            "A positive cost-path result would not establish factor alpha: the deadband changes holdings, turnover, cash and future exposures.", "",
            "## Limits", "", *["- " + item for item in LIMITS], "",
            f"Artifacts: `{artifacts}`", "",
        ]
        (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Completed factor candidate replay ID or run.json")
    parser.add_argument("--signal", help="Select one account from a completed library ablation")
    args = parser.parse_args(argv)
    run(args.run, requested_signal=args.signal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
