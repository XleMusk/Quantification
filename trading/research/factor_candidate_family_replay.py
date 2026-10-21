"""Replay the fixed factor candidate pack with predeclared family balancing.

The original candidate fit gives every selected feature equal model access, so
the 56 Alpha360 volume variants can dominate the 190-feature design.  This
consumer applies the remediation-plan rule ``1/sqrt(family_size)`` inside a
single fixed Ridge fit.  It reuses the saved training rows and source inputs;
there is no return-driven search or candidate replacement.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from .. import settings
from ..config import StrategyConfig
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .factor_candidate_replay import CandidateRankFeed, save_rankings


LIMITS = [
    "Family balancing is a predeclared implementation diagnostic, not a parameter selected from 2025/2026 returns",
    "The candidate membership, training rows, horizon, dates, portfolio, costs and source snapshots remain fixed",
    "The 2024 training sample is short and the 2025/2026 evaluation was already inspected; this is not an independent holdout",
    "Family balancing changes model scores and the actual account path; it is not a coefficient-only attribution",
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


def run(value, *, event_view=None, reference_view=None):
    run_path, source, source_artifacts, candidates, feature_path, training_path = _resolve_run(value)
    factors = candidates.factor.tolist()
    if candidates.factor.duplicated().any():
        raise ValueError("Candidate factors are duplicated")
    groups = candidates.groupby(["library", "family"], sort=False).size()
    group_keys = list(zip(candidates.library, candidates.family))
    scale = pd.Series([1 / np.sqrt(groups.loc[key]) for key in group_keys], index=factors)
    train = pd.read_parquet(training_path)
    required = set(factors) | {"target_rank", "sample_weight"}
    if not required <= set(train.columns):
        raise ValueError("Saved training rows do not contain the fixed candidate feature columns")
    medians = train[factors].median().fillna(0)
    x_train = train[factors].fillna(medians).mul(scale, axis="columns")
    model = Ridge(alpha=1.0, fit_intercept=True, solver="cholesky")
    model.fit(x_train, train.target_rank, sample_weight=train.sample_weight)

    features = pd.read_parquet(feature_path)
    ranking_dates = pd.read_csv(source_artifacts / "predictions" / "rankings.csv").date.unique().tolist()
    wanted = pd.to_datetime(ranking_dates)
    mask = features.index.get_level_values("date").isin(wanted)
    evaluation = features.loc[mask, factors]
    if evaluation.empty or evaluation.index.get_level_values("date").nunique() != len(wanted):
        raise ValueError("Saved candidate features do not cover the fixed ranking dates")
    scores = pd.Series(model.predict(evaluation.fillna(medians).mul(scale, axis="columns")),
                       index=evaluation.index, name="score")

    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "factor_candidate_family_replays" / identifier
    artifacts = settings.DATA_ROOT / "research" / "factor_candidate_family_replays" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    prediction_folder = artifacts / "predictions"
    config = StrategyConfig(**source["portfolio"]).validate()
    save_rankings(scores, prediction_folder, config, list(wanted))
    write_json(artifacts / "model.json", {
        "alpha": 1.0,
        "family_scaling": "1/sqrt(selected_candidate_count_per_library_family)",
        "candidate_count": len(factors),
        "training_rows": len(train),
        "training_months": train.index.get_level_values("date").nunique(),
        "groups": {f"{library}/{family}": int(size) for (library, family), size in groups.items()},
        "feature_scale": scale.to_dict(),
        "coefficients_on_scaled_features": dict(zip(factors, model.coef_)),
        "execution_admitted": False,
    })
    stock_inputs = dict(source["stock_inputs"])
    if event_view is not None:
        stock_inputs["event_view"] = str(Path(event_view).resolve())
    if reference_view is not None:
        stock_inputs["reference_view"] = str(Path(reference_view).resolve())
    record = {
        "id": identifier,
        "status": "running_factor_candidate_family_replay",
        "research_family": "factor_candidate_family_balanced_ridge",
        "source_run": str(run_path),
        "source_artifacts": str(source_artifacts),
        "candidate_count": len(factors),
        "candidate_membership_changed": False,
        "training_rows_reused": True,
        "model_refit": True,
        "parameters_searched": False,
        "family_scaling": "1/sqrt(selected_candidate_count_per_library_family)",
        "start": source["start"],
        "end": source["end"],
        "portfolio": config.to_dict(),
        "inputs": stock_inputs,
        "rights_replay_policy": source.get("rights_replay_policy"),
        "merger_replay_policy": source.get("merger_replay_policy"),
        "artifacts": str(artifacts),
        "result": None,
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
        "limitations": LIMITS,
    }
    write_json(out / "run.json", record)
    feed = None
    try:
        feed = BulkResearchFeed(stock_inputs["price_view"], stock_inputs["event_view"],
                                stock_inputs["reference_view"], stock_inputs["listing_master"])
        dates = [day for day in feed.calendar if source["start"] <= day <= source["end"]]
        if len(dates) != source["result"]["sessions"]:
            raise ValueError("Pinned source session count differs from the saved candidate replay")
        signal = "stock_factor_candidate_family_balanced_ridge"
        ranked = CandidateRankFeed(feed, prediction_folder, signal, config, dates,
                                   rights_replay_policy=source.get("rights_replay_policy"),
                                   merger_replay_policy=source.get("merger_replay_policy"))
        result = baseline_ledger_result(ranked, config, dates, artifacts / signal)
        control = source["result"]
        result.update(signal=signal, control_case=str(run_path), control_metrics=control,
                      net_return_difference=(result.get("net_return") - control["net_return"])
                      if result.get("status") == "completed" else None,
                      max_drawdown_difference=(result.get("max_drawdown") - control["max_drawdown"])
                      if result.get("status") == "completed" else None,
                      sharpe_difference=(result.get("sharpe_zero_rf_252") - control["sharpe_zero_rf_252"])
                      if result.get("status") == "completed" else None)
        record["result"] = result
        record["status"] = "completed_factor_candidate_family_replay" if result["status"] == "completed" else "failed_factor_candidate_family_ledger"
    except Exception as error:
        record.update(status="failed_factor_candidate_family_replay", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if feed is not None:
            feed.close()
        write_json(out / "run.json", record)
        result = record.get("result") or {}
        if result.get("status") == "completed":
            lines = [
                "# Family-Balanced Factor Candidate Replay", "", "Research development only; `NO_TRADE`.", "",
                f"Source: `{run_path}`; {len(factors)} candidates, saved 2024 training rows reused.",
                "Family rule: each selected library/family group is scaled by 1/sqrt(group size) before the fixed Ridge(alpha=1) fit.",
                f"Result: net {result['net_return']:.2%}, MDD {result['max_drawdown']:.2%}, Sharpe {result['sharpe_zero_rf_252']:.4f}, fees CNY{result['fees']:.2f}.",
                f"Versus original: net {result['net_return_difference']:.2%}, MDD delta {result['max_drawdown_difference']:.2%}, Sharpe delta {result['sharpe_difference']:.4f}.",
                "This is a predeclared family-balance diagnostic, not a promoted strategy or independent validation.", "",
                "## Limits", "", *["- " + item for item in LIMITS], "",
                f"Artifacts: `{artifacts}`", "",
            ]
        else:
            error = result.get("error", record.get("error", "unknown"))
            lines = ["# Family-Balanced Factor Candidate Replay", "", "Research development only; `NO_TRADE`.", "",
                     f"Ledger status: {result.get('status', 'unknown')}; {error}", "",
                     "## Limits", "", *["- " + item for item in LIMITS], "", f"Artifacts: `{artifacts}`", ""]
        (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Completed factor candidate replay ID or run.json")
    parser.add_argument("--event-view", help="Optional immutable reviewed event view")
    parser.add_argument("--reference-view", help="Optional immutable reviewed reference view")
    args = parser.parse_args(argv)
    run(args.run, event_view=args.event_view, reference_view=args.reference_view)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
