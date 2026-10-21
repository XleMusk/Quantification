"""Score the frozen family-balanced factor model on the latest pinned session.

This is an inference-only consumer of a completed factor-family replay.  It
reconstructs the exact fixed Ridge fit from the saved training rows, builds
features for one newer cutoff session, and writes a current ranking bundle for
the existing paper-preview path.  It does not search, refit on new labels, or
admit orders.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import dated_eligibility
from .experiment import write_json
from .factor_candidate_replay import build_monthly_features, oriented_rank_features, save_rankings


SIGNAL = "stock_factor_candidate_family_balanced_ridge"
LIMITS = [
    "The frozen 2024 training rows and candidate membership are reused; no new labels or parameter search enter scoring",
    "The current score is a research snapshot through the pinned cutoff, not a broker quote or execution admission",
    "The underlying source-covered SH/SZ reconstruction is not PIT-complete and financial inputs are not used here",
    "The paper preview still requires a current account observation and same-session RMB quotes before adapter preparation",
]


def _resolve_family_run(value):
    candidate = Path(value)
    run_path = (candidate if candidate.name == "run.json"
                else ROOT / "outputs" / "factor_candidate_family_replays" / str(value) / "run.json")
    if not run_path.exists():
        raise FileNotFoundError(f"Factor-family replay not found: {run_path}")
    family = json.loads(run_path.read_text(encoding="utf-8"))
    artifacts = Path(family["artifacts"]).resolve()
    required = ("completed_factor_candidate_family_replay", SIGNAL)
    if family.get("status") != required[0] or family.get("result", {}).get("signal") != required[1]:
        raise ValueError("Completed family-balanced factor replay required")
    return run_path, family, artifacts


def _resolve_candidate_file(base_run, base, exclude_library=None):
    candidate_run = Path(base["candidate_run"])
    candidate_file = candidate_run.parent / "candidate_factors.csv"
    if not candidate_file.exists():
        candidate_file = Path(base.get("candidate_data_artifacts", "")) / "candidate_factors.csv"
    if not candidate_file.exists():
        raise FileNotFoundError(f"Fixed candidate factor file not found: {candidate_file}")
    candidates = pd.read_csv(candidate_file)
    if len(candidates) != base.get("candidate_count") or candidates.factor.duplicated().any():
        raise ValueError("Fixed candidate factor membership changed")
    if not candidates.candidate_decision.eq("selected_cluster_representative").all():
        raise ValueError("Current scoring requires the selected cluster-representative pack")
    if exclude_library is not None:
        if exclude_library not in {"Alpha158", "Alpha360", "Templates"}:
            raise ValueError(f"Unsupported factor library exclusion: {exclude_library}")
        candidates = candidates.loc[candidates.library.ne(exclude_library)].copy()
        if candidates.empty:
            raise ValueError(f"Factor library exclusion removed every candidate: {exclude_library}")
    return candidate_file, candidates


def run(value, cutoff, *, output_root=None, exclude_library=None):
    family_run, family, family_artifacts = _resolve_family_run(value)
    cutoff = str(cutoff)
    base_run = Path(family["source_run"]).resolve()
    base = json.loads(base_run.read_text(encoding="utf-8"))
    candidate_file, candidates = _resolve_candidate_file(base_run, base, exclude_library)
    training_path = Path(base["artifacts"]).resolve() / "training_rows.parquet"
    if not training_path.exists():
        raise FileNotFoundError(f"Frozen training rows not found: {training_path}")

    inputs = dict(family["inputs"])
    config = StrategyConfig(**family["portfolio"]).validate()
    signal = (f"stock_factor_candidate_leave_out_{exclude_library.lower()}"
              if exclude_library else SIGNAL)
    stocks = None
    started = time.monotonic()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = (Path(output_root).resolve() if output_root else ROOT / "outputs" / "factor_current_scores" / identifier)
    artifacts = DATA_ROOT / "research" / "factor_current_scores" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running_factor_candidate_current_score",
        "research_family": ("factor_candidate_library_leave_out"
                             if exclude_library else family["research_family"]),
        "signal": signal,
        "source_run": str(family_run),
        "model_source_run": str(base_run),
        "source_artifacts": str(artifacts),
        "artifacts": family["artifacts"],
        "inputs": inputs,
        "portfolio": config.to_dict(),
        "candidate_file": str(candidate_file),
        "candidate_count": len(candidates),
        "excluded_library": exclude_library,
        "cutoff": cutoff,
        "end": cutoff,
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
        "score_only": True,
        "result": None,
        "limitations": LIMITS,
    }
    write_json(out / "run.json", record)
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"],
                                  inputs["reference_view"], inputs["listing_master"])
        if cutoff not in stocks.calendar:
            raise ValueError(f"Current cutoff is not a pinned exchange session: {cutoff}")
        train = pd.read_parquet(training_path)
        factors = candidates.factor.tolist()
        required = set(factors) | {"target_rank", "sample_weight"}
        if not required <= set(train.columns):
            raise ValueError("Frozen training rows do not cover the selected candidate factors")
        groups = candidates.groupby(["library", "family"], sort=False).size()
        scale = pd.Series(
            [1 / np.sqrt(groups.loc[(row.library, row.family)]) for row in candidates.itertuples(index=False)],
            index=factors,
        )
        medians = train[factors].median().fillna(0)
        model = Ridge(alpha=1.0, fit_intercept=True, solver="cholesky")
        model.fit(train[factors].fillna(medians).mul(scale, axis="columns"),
                  train.target_rank, sample_weight=train.sample_weight)

        # Reuse the same factor construction and eligibility rule as the
        # historical replay, but request only the new terminal session.
        feature_source = {"bulk_inputs": inputs}
        raw, _ = build_monthly_features(stocks, feature_source, candidates, [pd.Timestamp(cutoff)])
        eligibility = dated_eligibility(stocks, config, dates=[cutoff])
        raw = raw.where(eligibility.reindex(raw.index, fill_value=False))
        features = oriented_rank_features(raw, candidates)
        if features.empty or features.index.get_level_values("date").nunique() != 1:
            raise ValueError("Current feature construction produced no complete cutoff session")
        matrix = features[factors].fillna(medians).mul(scale, axis="columns")
        scores = pd.Series(model.predict(matrix), index=features.index, name="score")
        if scores.groupby(level="date").count().iloc[0] < config.retain_rank:
            raise ValueError("Current score session has fewer finite candidates than retain_rank")
        prediction_folder = artifacts / "predictions"
        ranking = save_rankings(scores, prediction_folder, config, [pd.Timestamp(cutoff)])
        features.to_parquet(artifacts / "current_features.parquet")
        scores.to_frame().to_parquet(artifacts / "current_scores.parquet")
        model_record = {
            "alpha": 1.0,
            "candidate_count": len(factors),
            "training_rows": len(train),
            "training_months": int(train.index.get_level_values("date").nunique()),
            "last_training_date": str(train.index.get_level_values("date").max()),
            "feature_scale": scale.to_dict(),
            "medians": medians.to_dict(),
            "coefficients_on_scaled_features": dict(zip(factors, model.coef_)),
            "intercept": float(model.intercept_),
            "model_reconstructed_from_frozen_rows": True,
            "new_labels_used": False,
            "execution_admitted": False,
        }
        write_json(artifacts / "model.json", model_record)
        result = {
            "status": "completed",
            "signal": signal,
            "cutoff": cutoff,
            "rank_count": len(ranking),
            "rank_day": cutoff,
            "rank_is_current_for_execution_day": True,
            "finite_score_count": int(scores.notna().sum()),
            "source_price_cutoff": stocks.manifest["end"],
            "ranking_path": str(prediction_folder / "rankings.csv"),
        }
        record.update(status="completed_factor_candidate_current_score", result=result,
                      model=model_record)
        report = [
            "# Current factor score bundle",
            "",
            "Research-only current inference; `NO_TRADE`.",
            f"- Frozen family replay: `{family_run}`",
            f"- Score cutoff: `{cutoff}`; candidates ranked: {len(ranking)}",
            f"- Model: Ridge(alpha=1), {len(factors)} fixed factors, {len(train)} frozen training rows",
            f"- Excluded library: `{exclude_library or 'none'}`; new labels used: `false`",
            f"- Last training date: `{model_record['last_training_date']}`",
            "- This bundle is current relative to the pinned research snapshot and is not a user account observation, live quote, or order admission.",
            "",
            "## Limits",
            "",
            *["- " + item for item in LIMITS],
            "",
            f"Artifacts: `{artifacts}`",
        ]
        (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed_factor_candidate_current_score",
                      error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, help="Completed family-balanced replay ID or run.json")
    parser.add_argument("--cutoff", required=True, help="Latest pinned exchange session to score")
    parser.add_argument("--out", type=Path, help="Optional output directory")
    parser.add_argument("--exclude-library", choices=["Alpha158", "Alpha360", "Templates"],
                        help="Leave one fixed factor library out of current inference")
    args = parser.parse_args(argv)
    _, record = run(args.source_run, args.cutoff, output_root=args.out,
                    exclude_library=args.exclude_library)
    return 0 if record["status"] == "completed_factor_candidate_current_score" else 1


if __name__ == "__main__":
    raise SystemExit(main())
