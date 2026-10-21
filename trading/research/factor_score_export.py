"""Export a full frozen score panel from a completed factor-library ablation.

The leave-out replay stores the top-30 ranks used by its ledger, but a board
scope projection needs the full cross-section before it can rebuild a new
top-30 buffer.  This exporter reconstructs the already-declared Ridge fit from
the source replay's frozen feature/training rows; it does not search, label, or
change parameters.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
import uuid

import pandas as pd
from sklearn.linear_model import Ridge

from ..settings import DATA_ROOT, ROOT


def _new_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _resolve(value):
    candidate = Path(value)
    path = candidate if candidate.name == "run.json" else ROOT / "outputs" / "factor_candidate_library_ablations" / str(value) / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"factor-library ablation not found: {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("status") != "completed_factor_candidate_library_ablation":
        raise ValueError("A completed factor-library ablation is required")
    return path.resolve(), record


def _resolve_family(value):
    candidate = Path(value)
    path = (candidate if candidate.name == "run.json" else
            ROOT / "outputs" / "factor_candidate_family_replays" / str(value) / "run.json")
    if not path.exists():
        raise FileNotFoundError(f"factor-family replay not found: {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("status") != "completed_factor_candidate_family_replay":
        raise ValueError("A completed factor-family replay is required")
    return path.resolve(), record


def run(ablation_run, excluded_library):
    ablation_path, ablation = _resolve(ablation_run)
    result = ablation.get("results", {}).get(excluded_library)
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise ValueError(f"Completed ablation result not found for library: {excluded_library}")
    source_path = Path(ablation["source_run"]).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source_artifacts = Path(source["artifacts"]).resolve()
    pack_path = Path(source["candidate_run"]).resolve()
    candidates_path = pack_path.parent / "candidate_factors.csv"
    candidates = pd.read_csv(candidates_path)
    selected = candidates.loc[candidates.library.ne(excluded_library), "factor"].tolist()
    expected = int(result["selected_factor_count"])
    if len(selected) != expected or len(set(selected)) != len(selected):
        raise ValueError("Ablation candidate membership does not match its recorded selected count")

    training_path = source_artifacts / "training_rows.parquet"
    feature_path = source_artifacts / "monthly_candidate_features.parquet"
    full_scores_path = source_artifacts / "monthly_scores.parquet"
    for path in (training_path, feature_path, full_scores_path):
        if not path.exists():
            raise FileNotFoundError(f"Source replay artifact missing: {path}")

    identifier = _new_id()
    out = ROOT / "outputs" / "factor_score_exports" / identifier
    artifacts = DATA_ROOT / "research" / "factor_score_exports" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running",
        "research_family": "factor_score_export",
        "ablation_run": str(ablation_path),
        "source_run": str(source_path),
        "excluded_library": excluded_library,
        "selected_factor_count": len(selected),
        "training_rows": None,
        "alpha": 1.0,
        "start": source["start"],
        "end": source["end"],
        "source_score_artifact": str(full_scores_path),
        "artifacts": str(artifacts),
        "fresh_holdout_claimed": False,
        "execution_admitted": False,
        "result": None,
    }
    (out / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    started = time.monotonic()
    try:
        training = pd.read_parquet(training_path)
        features = pd.read_parquet(feature_path)
        source_scores = pd.read_parquet(full_scores_path, columns=["score"])
        record["training_rows"] = len(training)
        if training.index.names != ["date", "symbol"] or features.index.names != ["date", "symbol"]:
            raise ValueError("Source feature/training index contract changed")
        if not set(selected) <= set(training.columns) or not set(selected) <= set(features.columns):
            raise ValueError("Selected ablation factors are missing from frozen source artifacts")
        if "target_rank" not in training.columns or "sample_weight" not in training.columns:
            raise ValueError("Frozen training rows are missing target_rank/sample_weight")
        medians = training[selected].median().fillna(0)
        model = Ridge(alpha=1.0, fit_intercept=True, solver="cholesky")
        model.fit(training[selected].fillna(medians), training["target_rank"],
                  sample_weight=training["sample_weight"])
        dates = source_scores.index.get_level_values("date").unique()
        evaluation = features.loc[features.index.get_level_values("date").isin(dates), selected]
        predicted = pd.Series(model.predict(evaluation.fillna(medians)), index=evaluation.index, name="score")
        predicted.to_frame().sort_index().to_parquet(artifacts / "monthly_scores.parquet")
        model_record = {
            "excluded_library": excluded_library,
            "selected_factor_count": len(selected),
            "alpha": 1.0,
            "training_rows": len(training),
            "training_months": int(training.index.get_level_values("date").nunique()),
            "coefficients": dict(zip(selected, model.coef_)),
            "intercept": float(model.intercept_),
            "medians": medians.to_dict(),
            "reconstructed_from_frozen_rows": True,
            "new_labels_used": False,
            "parameters_searched": False,
            "execution_admitted": False,
        }
        (artifacts / "model.json").write_text(json.dumps(model_record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        record["result"] = {"status": "completed", "rows": len(predicted),
                             "dates": int(predicted.index.get_level_values("date").nunique()),
                             "symbols_per_date": int(predicted.groupby(level="date").size().min())}
        record["status"] = "completed_factor_score_export"
    except Exception as error:
        record.update(status="failed_factor_score_export", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (out / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        lines = ["# Frozen Factor Score Export", "", "Research-only; NO_TRADE.",
                 f"Ablation: `{ablation_path}`; excluded library: `{excluded_library}`.",
                 "Reconstructed the predeclared Ridge(alpha=1) fit from the source replay's frozen training/features; no new labels or parameter search.",
                 f"Score artifact: `{artifacts / 'monthly_scores.parquet'}`.",
                 "This artifact is intended for same-ledger execution-scope projections, not live admission."]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(str(out), flush=True)
    return out, record


def run_family(family_run):
    family_path, family = _resolve_family(family_run)
    source_path = Path(family["source_run"]).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source_artifacts = Path(source["artifacts"]).resolve()
    model_path = Path(family["artifacts"]).resolve() / "model.json"
    training_path = source_artifacts / "training_rows.parquet"
    feature_path = source_artifacts / "monthly_candidate_features.parquet"
    for path in (model_path, training_path, feature_path):
        if not path.exists():
            raise FileNotFoundError(f"Family score source artifact missing: {path}")

    model_record = json.loads(model_path.read_text(encoding="utf-8"))
    coefficients = model_record.get("coefficients_on_scaled_features") or {}
    scales = model_record.get("feature_scale") or {}
    factors = list(coefficients)
    if not factors or set(factors) != set(scales):
        raise ValueError("Family model does not contain a complete fixed coefficient/scale panel")

    identifier = _new_id()
    out = ROOT / "outputs" / "factor_score_exports" / identifier
    artifacts = DATA_ROOT / "research" / "factor_score_exports" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running",
        "research_family": "factor_score_export",
        "family_run": str(family_path),
        "source_run": str(source_path),
        "selected_factor_count": len(factors),
        "training_rows": None,
        "alpha": model_record.get("alpha"),
        "family_scaling": model_record.get("family_scaling"),
        "start": family["start"],
        "end": family["end"],
        "artifacts": str(artifacts),
        "fresh_holdout_claimed": False,
        "execution_admitted": False,
        "result": None,
    }
    (out / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    started = time.monotonic()
    try:
        training = pd.read_parquet(training_path, columns=factors)
        features = pd.read_parquet(feature_path, columns=factors)
        if training.index.names != ["date", "symbol"] or features.index.names != ["date", "symbol"]:
            raise ValueError("Family score source index contract changed")
        medians = training[factors].median().fillna(0)
        matrix = features[factors].fillna(medians).mul(pd.Series(scales), axis="columns")
        coef = pd.Series(coefficients, dtype=float).reindex(factors)
        intercept = float(model_record.get("intercept", 0.0))
        scores = (matrix.mul(coef, axis="columns").sum(axis="columns") + intercept).rename("score")
        scores.to_frame().sort_index().to_parquet(artifacts / "monthly_scores.parquet")
        export_model = {
            "family_run": str(family_path),
            "source_run": str(source_path),
            "candidate_count": len(factors),
            "training_rows": len(training),
            "training_months": int(training.index.get_level_values("date").nunique()),
            "alpha": model_record.get("alpha"),
            "family_scaling": model_record.get("family_scaling"),
            "feature_scale": scales,
            "coefficients_on_scaled_features": coefficients,
            "intercept": intercept,
            "medians": medians.to_dict(),
            "reconstructed_from_frozen_family_model": True,
            "new_labels_used": False,
            "parameters_searched": False,
            "execution_admitted": False,
        }
        (artifacts / "model.json").write_text(json.dumps(export_model, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        record["training_rows"] = len(training)
        record["result"] = {
            "status": "completed",
            "rows": len(scores),
            "dates": int(scores.index.get_level_values("date").nunique()),
            "symbols_per_date_min": int(scores.groupby(level="date").size().min()),
        }
        record["status"] = "completed_factor_score_export"
    except Exception as error:
        record.update(status="failed_factor_score_export", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (out / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        lines = [
            "# Frozen Family Factor Score Export", "", "Research-only; NO_TRADE.",
            f"Family replay: `{family_path}`; source replay: `{source_path}`.",
            "Reconstructed the complete monthly score panel from the saved family model and frozen features; no new labels or parameter search.",
            f"Score artifact: `{artifacts / 'monthly_scores.parquet'}`.",
            "This artifact is intended for same-ledger execution-scope projections, not live admission.",
        ]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ablation-run", help="Completed factor-library ablation run.json or ID")
    group.add_argument("--family-run", help="Completed family-balanced factor replay run.json or ID")
    parser.add_argument("--excluded-library", choices=["Alpha158", "Alpha360", "Templates"])
    args = parser.parse_args(argv)
    if args.family_run:
        _, record = run_family(args.family_run)
    else:
        if not args.excluded_library:
            parser.error("--excluded-library is required with --ablation-run")
        _, record = run(args.ablation_run, args.excluded_library)
    return 0 if record["status"] == "completed_factor_score_export" else 1


if __name__ == "__main__":
    raise SystemExit(main())
