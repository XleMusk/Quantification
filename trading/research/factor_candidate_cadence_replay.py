"""Replay the saved factor candidate on a fixed bimonthly signal cadence.

This is a predeclared implementation-path diagnostic for the candidate's high
replacement turnover.  It reuses the exact saved rankings, model result,
universe, dates, costs and shared Engine/Ledger.  Every other month-end signal
is skipped; there is no cadence sweep or return-based selection.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

from .. import settings
from ..config import StrategyConfig
from ..contracts import SignalUnavailable, TradingMode
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .factor_candidate_replay import CandidateRankFeed


CADENCE_MONTHS = 2

LIMITS = [
    "The two-month cadence is a fixed implementation diagnostic, not a return-selected parameter",
    "Saved rankings, candidate model, universe, dates, costs and portfolio selector are reused without refit",
    "Skipping a signal can leave existing holdings unchanged; the shared Engine/Ledger still applies all fills, limits and corporate-action rules",
    "This changes the account path and is not a fee addback, zero-cost counterfactual or causal factor attribution",
    "The source snapshot cutoff is 2026-09-30 and 2026 remains development evidence",
    "No independent post-cutoff holdout, PIT-complete financial source, paper observation or broker verification is claimed",
]


def _resolve_run(value):
    candidate = Path(value)
    if candidate.name == "run.json":
        run_path = candidate
    else:
        candidates = [
            settings.ROOT / "outputs" / "factor_candidate_replays" / str(value) / "run.json",
            settings.ROOT / "outputs" / "factor_candidate_family_replays" / str(value) / "run.json",
        ]
        run_path = next((path for path in candidates if path.exists()), candidates[0])
    if not run_path.exists():
        raise FileNotFoundError(f"factor candidate replay not found: {run_path}")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    family = record.get("research_family")
    supported_statuses = {
        "factor_candidate_ridge": "completed_factor_candidate_replay",
        "factor_candidate_family_balanced_ridge": "completed_factor_candidate_family_replay",
    }
    expected_status = supported_statuses.get(family)
    if expected_status is None or record.get("status") != expected_status:
        raise ValueError("Completed original or family-balanced factor replay required")
    if (record.get("result") or {}).get("status") != "completed":
        raise ValueError("Completed factor candidate ledger required")
    artifacts = Path(record["artifacts"])
    signal = record["result"]["signal"]
    # Both replay families store their immutable rank buffer below the data
    # artifact root recorded in the run manifest.
    rankings = artifacts / "predictions"
    if not (rankings / "rankings.csv").exists():
        raise FileNotFoundError(f"Saved candidate rankings not found: {rankings}")
    return run_path, record, artifacts, signal, rankings


class BimonthlyCandidateRankFeed(CandidateRankFeed):
    """Use the saved rank buffer only on alternating fixed month ends."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.decision_days = sorted(self.ranks)
        self.selected_days = {
            day for index, day in enumerate(self.decision_days)
            if index % CADENCE_MONTHS == 0
        }

    def signal_frame(self, day, config, context):
        if day in self.ranks and day not in self.selected_days:
            if context.mode != TradingMode.REPLAY or config != self.config:
                raise ValueError("Saved candidate cadence requires the same replay selector config")
            return SignalUnavailable("fixed_bimonthly_cadence")
        return super().signal_frame(day, config, context)

    def cadence_rows(self):
        return [
            {"day": day, "signal_used": day in self.selected_days,
             "reason": "saved_rank_signal" if day in self.selected_days else "fixed_bimonthly_cadence"}
            for day in self.decision_days
        ]


def run(value, event_view=None, reference_view=None):
    run_path, source, source_artifacts, signal, rankings = _resolve_run(value)
    inputs = dict(source.get("inputs") or source["stock_inputs"])
    if event_view is not None:
        inputs["event_view"] = str(Path(event_view).resolve())
    if reference_view is not None:
        inputs["reference_view"] = str(Path(reference_view).resolve())
    config = StrategyConfig(**source["portfolio"]).validate()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "factor_candidate_cadence_replays" / identifier
    artifacts = settings.DATA_ROOT / "research" / "factor_candidate_cadence_replays" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running_factor_candidate_cadence_replay",
        "research_family": "factor_candidate_fixed_cadence",
        "source_research_family": source.get("research_family"),
        "source_run": str(run_path),
        "source_artifacts": str(source_artifacts),
        "event_view_override": str(Path(event_view).resolve()) if event_view is not None else None,
        "reference_view_override": str(Path(reference_view).resolve()) if reference_view is not None else None,
        "signal": signal + "_bimonthly",
        "control_signal": signal,
        "inputs": inputs,
        "start": source["start"],
        "end": source["end"],
        "sessions": source["result"]["sessions"],
        "portfolio": config.to_dict(),
        "execution_policy": {
            "id": "fixed_bimonthly_signal_cadence_v1",
            "signal_months": CADENCE_MONTHS,
            "selection_rule": "Use the first saved month-end, then every second saved month-end",
            "derivation": "Predeclared turnover diagnostic; no return-based cadence search",
        },
        "candidate_membership_changed": False,
        "model_refit": False,
        "parameters_searched": False,
        "fresh_holdout_claimed": False,
        "execution_admitted": False,
        "artifacts": str(artifacts),
        "result": None,
        "limitations": LIMITS,
    }
    write_json(out / "run.json", record)
    feed = None
    try:
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        dates = [day for day in feed.calendar if source["start"] <= day <= source["end"]]
        if len(dates) != source["result"]["sessions"]:
            raise ValueError("Pinned source session count differs from the completed factor replay")
        ranked = BimonthlyCandidateRankFeed(
            feed, rankings, signal, config, dates,
            rights_replay_policy=source.get("rights_replay_policy"),
            merger_replay_policy=source.get("merger_replay_policy"),
        )
        import pandas as pd
        pd.DataFrame(ranked.cadence_rows()).to_csv(artifacts / "cadence.csv", index=False)
        result = baseline_ledger_result(
            ranked, config, dates, artifacts / (signal + "_bimonthly"),
        )
        result.update(signal=signal + "_bimonthly", control_case=str(run_path),
                      selected_month_ends=len(ranked.selected_days),
                      skipped_month_ends=len(ranked.decision_days) - len(ranked.selected_days),
                      control_metrics=source["result"],
                      net_return_difference=(result.get("net_return") - source["result"]["net_return"])
                      if result.get("status") == "completed" else None,
                      fees_difference=(result.get("fees") - source["result"]["fees"])
                      if result.get("status") == "completed" else None,
                      turnover_difference=(result.get("turnover_value") - source["result"]["turnover_value"])
                      if result.get("status") == "completed" else None)
        record["result"] = result
        record["status"] = ("completed_factor_candidate_cadence_replay"
                             if result["status"] == "completed"
                             else "failed_factor_candidate_cadence_ledger")
    except Exception as error:
        record.update(status="failed_factor_candidate_cadence_replay", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if feed is not None:
            feed.close()
        write_json(out / "run.json", record)
        result = record.get("result") or {}
        fee_difference = result.get("fees_difference")
        turnover_difference = result.get("turnover_difference")
        if result.get("status") == "completed":
            performance = (f"net {result['net_return']:.2%}, MDD {result['max_drawdown']:.2%}, "
                           f"Sharpe {result['sharpe_zero_rf_252']:.4f}, fees {result['fees']:.2f} CNY")
        else:
            performance = f"ledger status {result.get('status', 'unknown')}: {record.get('error') or result.get('error', 'unknown')}"
        lines = [
            "# Factor Candidate Fixed-Cadence Replay", "", "Research development only; `NO_TRADE`.", "",
            f"Source ranking/model replay: `{run_path}`; rankings and model were reused without refit.",
            f"Execution policy: `{record['execution_policy']['id']}`; one saved signal every {CADENCE_MONTHS} month ends.",
            f"Cadence ledger: {performance}.", "",
            "## Same-Basis Difference", "",
            f"Net return difference versus source: {result.get('net_return_difference', 'UNKNOWN'):.2%}" if isinstance(result.get('net_return_difference'), (int, float)) else "Net return difference versus source: UNKNOWN",
            (f"Fee difference CNY{fee_difference:,.2f}; turnover difference CNY{turnover_difference:,.2f}."
             if isinstance(fee_difference, (int, float)) and isinstance(turnover_difference, (int, float))
             else "Fee/turnover difference versus source: UNKNOWN because the cadence ledger did not complete."),
            "A positive cadence result would not establish factor alpha: skipped signals change holdings, turnover, cash and future exposures.", "",
            "## Limits", "", *["- " + item for item in LIMITS], "",
            f"Artifacts: `{artifacts}`", "",
        ]
        (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Completed factor candidate replay ID or run.json")
    parser.add_argument("--event-view", type=Path, help="Explicit amended event view for a bounded research-only replay")
    parser.add_argument("--reference-view", type=Path, help="Aligned amended reference view for a bounded research-only replay")
    args = parser.parse_args(argv)
    if (args.event_view is None) != (args.reference_view is None):
        parser.error("--event-view and --reference-view must be supplied together")
    run(args.run, args.event_view, args.reference_view)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
