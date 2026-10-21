"""Replay fixed candidate models with one selected library removed at a time.

This is a predeclared same-basis diagnostic for library-level incremental value.
It refits only on the pinned 2024 training rows, keeps the candidate pack,
dates, portfolio and costs fixed, and sends every ablation through the shared
Engine/Ledger.  It is not a selector or an admission gate.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd
from sklearn.linear_model import Ridge

from .. import settings
from ..config import StrategyConfig
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .factor_candidate_replay import CandidateRankFeed, save_rankings
from .continuous_momentum import fractional_allocation_contract


LIMITS = [
    "Library removal is a predeclared diagnostic, not a return-selected model or a new candidate pack",
    "Candidate membership, 2024 training rows, horizon, alpha, dates, portfolio, costs and source views remain fixed",
    "The 2025/2026 ledger is already inspected development evidence, not an independent holdout",
    "A positive leave-out difference is not causal proof; a negative difference does not prove the removed library is useless",
    "PIT-complete financial inputs, prospective paper observation and broker verification remain absent",
]


def _resolve_run(value):
    candidate = Path(value)
    run_path = (candidate if candidate.name == "run.json"
                else settings.ROOT / "outputs" / "factor_candidate_replays" / str(value) / "run.json")
    if not run_path.exists():
        raise FileNotFoundError(f"factor candidate replay not found: {run_path}")
    source = json.loads(run_path.read_text(encoding="utf-8"))
    if source.get("status") != "completed_factor_candidate_replay" or source["result"].get("status") != "completed":
        raise ValueError("Completed factor candidate ledger required")
    artifacts = Path(source["artifacts"])
    candidate_path = Path(source["candidate_run"])
    if not candidate_path.exists():
        raise FileNotFoundError(f"candidate pack not found: {candidate_path}")
    candidates = pd.read_csv(candidate_path.parent / "candidate_factors.csv")
    feature_path = artifacts / "monthly_candidate_features.parquet"
    training_path = artifacts / "training_rows.parquet"
    if not feature_path.exists() or not training_path.exists():
        raise FileNotFoundError("Saved candidate features/training rows are required")
    return run_path, source, artifacts, candidates, feature_path, training_path


def _safe_name(value):
    return value.lower().replace(" ", "_").replace("/", "_")


def run(value, *, event_view=None, reference_view=None, ordinary_fraction_policy=None):
    run_path, source, source_artifacts, candidates, feature_path, training_path = _resolve_run(value)
    factors = candidates.factor.tolist()
    if candidates.factor.duplicated().any() or set(candidates.library) != {"Alpha158", "Alpha360", "Templates"}:
        raise ValueError("The fixed three-library candidate pack is required")
    train = pd.read_parquet(training_path)
    features = pd.read_parquet(feature_path)
    if set(factors) - set(train.columns) or set(factors) - set(features.columns):
        raise ValueError("Saved feature artifacts do not cover the fixed candidate pack")
    ranking_dates = pd.read_csv(source_artifacts / "predictions" / "rankings.csv").date.unique().tolist()
    wanted = pd.to_datetime(ranking_dates)
    evaluation = features.loc[features.index.get_level_values("date").isin(wanted), factors]
    if evaluation.empty or evaluation.index.get_level_values("date").nunique() != len(wanted):
        raise ValueError("Saved candidate features do not cover the fixed ranking dates")

    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "factor_candidate_library_ablations" / identifier
    artifacts = settings.DATA_ROOT / "research" / "factor_candidate_library_ablations" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    config = StrategyConfig(**source["portfolio"]).validate()
    stock_inputs = dict(source["stock_inputs"])
    if (event_view is None) != (reference_view is None):
        raise ValueError("Event/reference overrides must be supplied together")
    if event_view is not None:
        stock_inputs["event_view"] = str(Path(event_view).resolve())
        stock_inputs["reference_view"] = str(Path(reference_view).resolve())

    record = {
        "id": identifier,
        "status": "running_factor_candidate_library_ablation",
        "research_family": "factor_candidate_library_leave_out",
        "source_run": str(run_path),
        "source_artifacts": str(source_artifacts),
        "libraries": sorted(candidates.library.unique().tolist()),
        "candidate_count": len(factors),
        "candidate_membership_changed": False,
        "training_rows_reused": True,
        "model_refit": True,
        "parameters_searched": False,
        "start": source["start"],
        "end": source["end"],
        "portfolio": config.to_dict(),
        "inputs": stock_inputs,
        "artifacts": str(artifacts),
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
        "fractional_allocation": fractional_allocation_contract(ordinary_fraction_policy),
        "limitations": LIMITS,
        "results": {},
    }
    write_json(out / "run.json", record)
    feed = None
    try:
        feed = BulkResearchFeed(stock_inputs["price_view"], stock_inputs["event_view"],
                                stock_inputs["reference_view"], stock_inputs["listing_master"])
        dates = [day for day in feed.calendar if source["start"] <= day <= source["end"]]
        if len(dates) != source["result"]["sessions"]:
            raise ValueError("Pinned source session count differs from the completed factor replay")
        control = source["result"]
        for library in record["libraries"]:
            selected = candidates.loc[candidates.library.ne(library), "factor"].tolist()
            if not selected:
                raise ValueError(f"Cannot remove all candidates for {library}")
            train_subset = train[selected]
            medians = train_subset.median().fillna(0)
            model = Ridge(alpha=1.0, fit_intercept=True, solver="cholesky")
            model.fit(train_subset.fillna(medians), train.target_rank, sample_weight=train.sample_weight)
            scores = pd.Series(model.predict(evaluation[selected].fillna(medians)),
                               index=evaluation.index, name="score")
            name = _safe_name(library)
            prediction_folder = artifacts / name / "predictions"
            save_rankings(scores, prediction_folder, config, list(wanted))
            write_json(artifacts / name / "model.json", {
                "excluded_library": library,
                "selected_factor_count": len(selected),
                "alpha": 1.0,
                "horizon": source["horizon"],
                "training_rows": len(train),
                "training_months": train.index.get_level_values("date").nunique(),
                "execution_admitted": False,
            })
            signal = f"stock_factor_candidate_leave_out_{name}"
            ranked = CandidateRankFeed(feed, prediction_folder, signal, config, dates,
                                       fractional_allocation=ordinary_fraction_policy,
                                       rights_replay_policy=source.get("rights_replay_policy"),
                                       merger_replay_policy=source.get("merger_replay_policy"))
            result = baseline_ledger_result(ranked, config, dates, artifacts / name / signal)
            result.update(
                signal=signal,
                excluded_library=library,
                selected_factor_count=len(selected),
                control_case=str(run_path),
                control_metrics=control,
                net_return_difference=(result.get("net_return") - control["net_return"])
                if result.get("status") == "completed" else None,
                fees_difference=(result.get("fees") - control["fees"])
                if result.get("status") == "completed" else None,
                turnover_difference=(result.get("turnover_value") - control["turnover_value"])
                if result.get("status") == "completed" else None,
                sharpe_difference=(result.get("sharpe_zero_rf_252") - control["sharpe_zero_rf_252"])
                if result.get("status") == "completed" else None,
            )
            record["results"][library] = result
            write_json(out / "run.json", record)
        result_statuses = [result.get("status") for result in record["results"].values()]
        record["status"] = (
            "completed_factor_candidate_library_ablation"
            if result_statuses and all(status == "completed" for status in result_statuses)
            else "completed_factor_candidate_library_ablation_with_failures"
        )
    except Exception as error:
        record.update(status="failed_factor_candidate_library_ablation",
                      error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if feed is not None:
            feed.close()
        write_json(out / "run.json", record)
        rows = []
        for library, result in record["results"].items():
            rows.append({"excluded_library": library, "status": result.get("status"),
                         "completed_sessions": result.get("completed_sessions"),
                         "last_completed_session": result.get("last_completed_session"),
                         "attempted_session": result.get("attempted_session"),
                         "error": result.get("error"),
                         "net_return": result.get("net_return"),
                         "net_return_difference": result.get("net_return_difference"),
                         "max_drawdown": result.get("max_drawdown"),
                         "sharpe": result.get("sharpe_zero_rf_252"),
                         "sharpe_difference": result.get("sharpe_difference"),
                         "fees": result.get("fees"), "fees_difference": result.get("fees_difference"),
                         "turnover_value": result.get("turnover_value"),
                         "turnover_difference": result.get("turnover_difference")})
        pd.DataFrame(rows).to_csv(out / "summary.csv", index=False)
        lines = ["# Factor Candidate Library Leave-Out Replay", "", "Research development only; `NO_TRADE`.", "",
                 f"Source: `{run_path}`; fixed {len(factors)}-candidate pack; same {source['result']['sessions']} sessions, portfolio and costs.",
                 "Each row removes one library, refits Ridge(alpha=1) on the pinned 2024 training rows, and replays the resulting rankings through the shared Engine/Ledger.", "",
                 "| Excluded library | Status | Completed sessions | Last complete | Blocked at | Net return | Delta vs full | MDD | Sharpe | Sharpe delta | Fees |", "|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|"]
        for row in rows:
            def pct(value):
                return "UNKNOWN" if not isinstance(value, (int, float)) else f"{value:.2%}"
            def num(value):
                return "UNKNOWN" if not isinstance(value, (int, float)) else f"{value:.4f}"
            def text(value):
                return "UNKNOWN" if value in (None, "") else str(value).replace("|", "\\|")
            lines.append(f"| {row['excluded_library']} | {row['status']} | {text(row['completed_sessions'])} | {text(row['last_completed_session'])} | {text(row['attempted_session'])} | {pct(row['net_return'])} | {pct(row['net_return_difference'])} | {pct(row['max_drawdown'])} | {num(row['sharpe'])} | {num(row['sharpe_difference'])} | {row['fees'] if isinstance(row['fees'], (int, float)) else 'UNKNOWN'} |")
        lines += ["", "## Data-gap status", "",
                  "No comparative metric is produced for a failed variant. The frontier below is retained so the next data repair or independent validation can resume from the actual blocked session."]
        for row in rows:
            if row["status"] != "completed":
                lines.append(f"- {row['excluded_library']}: completed {text(row['completed_sessions'])} sessions; last complete `{text(row['last_completed_session'])}`; blocked at `{text(row['attempted_session'])}`; error: {text(row['error'])}")
        lines += ["", "## Limits", "", *["- " + item for item in LIMITS], "",
                  f"Summary CSV: `{out / 'summary.csv'}`", f"Artifacts: `{artifacts}`", ""]
        (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Completed factor candidate replay ID or run.json")
    parser.add_argument("--event-view")
    parser.add_argument("--reference-view")
    parser.add_argument("--ordinary-fraction-policy", choices=["floor", "ceil"],
                        help="Replay-only integer remainder sensitivity; not an account allocation receipt")
    args = parser.parse_args(argv)
    run(args.run, event_view=args.event_view, reference_view=args.reference_view,
        ordinary_fraction_policy=args.ordinary_fraction_policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
