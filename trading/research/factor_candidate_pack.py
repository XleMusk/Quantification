"""Build a training-only, redundancy-reduced factor candidate pack.

This stage converts the completed 718-definition diagnostic library into a
small, inspectable input for a later fixed-model experiment.  It deliberately
does not read validation/test scores for selection, fit a model, replay an
account, or authorize trading.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .experiment import write_json


EXPECTED_DEFINITIONS = 718
DEFAULT_HORIZON = 20
DEFAULT_MIN_IC_DAYS = 80
SOURCE_STATUS = "completed_research_diagnostics"


def _resolve_source(value):
    candidate = Path(value)
    if candidate.suffix == ".json" or candidate.name == "run.json":
        run_path = candidate
    else:
        run_path = ROOT / "outputs" / "factor_libraries" / str(value) / "run.json"
    if not run_path.exists():
        raise FileNotFoundError(f"factor-library run.json not found: {run_path}")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    artifacts = Path(run.get("artifacts", run_path.parent))
    return run_path, run, artifacts


def _validate_source(run, artifacts, horizon):
    if run.get("status") != SOURCE_STATUS:
        raise ValueError(f"Source is not a completed diagnostic run: {run.get('status')}")
    if run.get("definitions_completed") != EXPECTED_DEFINITIONS:
        raise ValueError("Source does not contain all 718 definitions")
    if horizon not in run.get("completed_horizons", []):
        raise ValueError(f"Horizon {horizon} is not complete in source run")
    for key in ("account_backtest", "execution_admitted", "fresh_holdout_claimed", "model_fit",
                "selection_parameters_searched"):
        if run.get(key) not in (False, None):
            raise ValueError(f"Source has unsafe or already-selected state: {key}={run.get(key)!r}")
    required = [artifacts / "decay_summary.csv", artifacts / "factor_profiles.csv"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing factor-library artifacts: " + ", ".join(missing))


def _numeric(frame, column):
    return pd.to_numeric(frame[column], errors="coerce")


def _bool_series(series):
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin({"1", "true", "yes"})


def build_candidate_pack(source_run, horizon=DEFAULT_HORIZON, min_ic_days=DEFAULT_MIN_IC_DAYS):
    run_path, run, artifacts = _resolve_source(source_run)
    _validate_source(run, artifacts, horizon)
    summary = pd.read_csv(artifacts / "decay_summary.csv")
    profiles = pd.read_csv(artifacts / "factor_profiles.csv")
    if len(profiles) != EXPECTED_DEFINITIONS or profiles["name"].nunique() != EXPECTED_DEFINITIONS:
        raise ValueError("Factor profile artifact is not a unique 718-definition catalog")
    expected_summary = EXPECTED_DEFINITIONS * len(run.get("completed_horizons", [])) * 3
    if len(summary) != expected_summary:
        raise ValueError(f"Incomplete decay summary: expected {expected_summary}, got {len(summary)}")
    if summary.duplicated(["factor", "horizon", "segment"]).any():
        raise ValueError("Decay summary contains duplicate factor/horizon/segment rows")

    train = summary.loc[
        summary["segment"].eq("train") & summary["horizon"].eq(horizon)
    ].copy()
    train["mean_rank_ic"] = _numeric(train, "mean_rank_ic")
    train["oriented_mean_rank_ic"] = _numeric(train, "oriented_mean_rank_ic")
    train["oriented_top_quintile_excess"] = _numeric(train, "oriented_top_quintile_excess")
    train["ic_days"] = _numeric(train, "ic_days")
    train["diagnostic_positive"] = _bool_series(train["diagnostic_positive"])
    train["cross_section_constant"] = train.get("cross_section_constant", False)
    train = train.merge(
        profiles[["name", "formula", "note", "original_name", "library", "family", "category",
                  "window_1", "window_2", "redundancy_cluster", "cluster_size", "status",
                  "cross_section_constant", "exact_formula_reuse", "structural_sign_mirror"]],
        left_on="factor", right_on="name", how="left", validate="one_to_one", suffixes=("", "_profile")
    )
    if train[["formula", "redundancy_cluster"]].isna().any().any():
        raise ValueError("Training diagnostic rows did not join to the factor catalog")
    eligible = train.loc[
        train["diagnostic_positive"]
        & train["oriented_mean_rank_ic"].notna()
        & train["oriented_top_quintile_excess"].notna()
        & train["ic_days"].ge(min_ic_days)
        & ~train["cross_section_constant_profile"].fillna(train["cross_section_constant"]).astype(bool)
        & train["status"].eq("SCREENED_DIAGNOSTIC_ONLY")
    ].copy()
    if eligible.empty:
        raise ValueError("No training-only diagnostic candidates satisfy the fixed eligibility rule")

    # One representative per training redundancy cluster.  This is a fixed
    # candidate-generation rule, not a tuned alpha-selection or deployment gate.
    eligible["oriented_mean_rank_ic"] = eligible["oriented_mean_rank_ic"].astype(float)
    eligible["oriented_top_quintile_excess"] = eligible["oriented_top_quintile_excess"].astype(float)
    eligible["cluster_size"] = pd.to_numeric(eligible["cluster_size"], errors="coerce").fillna(1).astype(int)
    selected = (
        eligible.sort_values(
            ["redundancy_cluster", "oriented_mean_rank_ic", "oriented_top_quintile_excess",
             "cluster_size", "factor"],
            ascending=[True, False, False, True, True], kind="mergesort"
        )
        .drop_duplicates("redundancy_cluster", keep="first")
        .copy()
    )
    eligible["candidate_decision"] = "eligible_but_cluster_representative_not_selected"
    selected_names = set(selected["factor"])
    eligible.loc[eligible["factor"].isin(selected_names), "candidate_decision"] = "selected_cluster_representative"
    eligible["selection_reason"] = "train_h20_positive_fixed_rule_one_per_redundancy_cluster"
    selected = eligible.loc[eligible["candidate_decision"].eq("selected_cluster_representative")].copy()
    selected = selected.sort_values(["category", "family", "factor"], kind="mergesort")
    return run_path, run, artifacts, eligible, selected


def _new_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _report(out, record, eligible, selected):
    counts = selected.groupby("category")["factor"].count().sort_values(ascending=False)
    category_lines = "\n".join(f"- {key}: {int(value)}" for key, value in counts.items()) or "- none"
    text = f"""# Factor Candidate Pack

Status: `completed_research_candidate_pack`  
Source: `{record['source_run']}`  
Selection horizon: `{record['horizon']}` sessions  

## Result

- All-library source: {record['source_definitions']} definitions.
- Training rows passing the fixed diagnostic rule: {len(eligible)}.
- Redundancy-reduced representatives: {len(selected)} across {record['selected_clusters']} clusters.
- Selection rule: training segment only, diagnostic-positive, nonconstant, at least {record['minimum_ic_days']} IC days, then one representative per training redundancy cluster.

Selected representatives by category:
{category_lines}

## Boundary

This is a candidate input for a later fixed-model experiment. Validation and test
rows were not used for selection. No model was fit, no account replay was run,
no incremental net value was measured, and no signal or trading admission was
created. The source remains diagnostic-only; cluster representatives are not
factor winners.
"""
    (out / "REPORT.md").write_text(text, encoding="utf-8")


def run(source_run, horizon=DEFAULT_HORIZON, min_ic_days=DEFAULT_MIN_IC_DAYS):
    source_path, source, artifacts, eligible, selected = build_candidate_pack(
        source_run, horizon=horizon, min_ic_days=min_ic_days
    )
    identifier = _new_id()
    out = ROOT / "outputs" / "factor_candidate_packs" / identifier
    data_out = DATA_ROOT / "research" / "factor_candidate_packs" / identifier
    out.mkdir(parents=True, exist_ok=False)
    data_out.mkdir(parents=True, exist_ok=False)
    columns = ["factor", "library", "family", "category", "formula", "note", "original_name",
               "window_1", "window_2", "redundancy_cluster", "cluster_size", "train_direction",
               "mean_rank_ic", "oriented_mean_rank_ic", "oriented_top_quintile_excess", "ic_days",
               "positive_oriented_ic_fraction", "hac_t_horizon", "pairs", "missing_forward_labels",
               "candidate_decision", "selection_reason"]
    for frame, name in ((eligible, "eligible_factors.csv"), (selected, "candidate_factors.csv")):
        keep = [column for column in columns if column in frame.columns]
        frame[keep].to_csv(data_out / name, index=False)
        frame[keep].to_csv(out / name, index=False)
    record = {
        "id": identifier,
        "status": "completed_research_candidate_pack",
        "source_run": str(source_path),
        "source_artifacts": str(artifacts),
        "source_status": source.get("status"),
        "source_definitions": int(source.get("definitions_completed", 0)),
        "source_bulk_inputs": source.get("bulk_inputs"),
        "source_segments": source.get("segments"),
        "source_completed_horizons": source.get("completed_horizons"),
        "source_cutoff": source.get("quality", {}).get("source_cutoff"),
        "horizon": int(horizon),
        "minimum_ic_days": int(min_ic_days),
        "eligible_factors": int(len(eligible)),
        "selected_factors": int(len(selected)),
        "selected_clusters": int(selected["redundancy_cluster"].nunique()),
        "account_backtest": False,
        "model_fit": False,
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
        "selection_parameters_searched": False,
        "diagnostic_only": True,
        "candidate_rule": "train_only_diagnostic_positive_nonconstant_min_ic_days_one_per_redundancy_cluster",
        "data_artifacts": str(data_out),
    }
    write_json(data_out / "run.json", record)
    write_json(out / "run.json", record)
    _report(out, record, eligible, selected)
    (data_out / "REPORT.md").write_text((out / "REPORT.md").read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps({"root": str(out), **record}, ensure_ascii=False), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, help="factor-library run ID or path to run.json")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--minimum-ic-days", type=int, default=DEFAULT_MIN_IC_DAYS)
    args = parser.parse_args(argv)
    run(args.source_run, horizon=args.horizon, min_ic_days=args.minimum_ic_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
