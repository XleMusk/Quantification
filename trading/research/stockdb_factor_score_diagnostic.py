"""Score a fixed factor model against a StockDB feature panel for diagnosis only.

This is an explicit cross-lineage diagnostic.  It reconstructs the frozen Ridge
fit and writes scores, but it does not apply dated eligibility, create rankings,
target holdings, paper orders, or Tonghuashun payloads.  The StockDB provider
uses a research price proxy and the frozen training rows come from the older
BulkResearchFeed lineage; the result therefore cannot enter the current-score
or paper route.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from ..settings import DATA_ROOT, ROOT
from .factor_candidate_replay import oriented_rank_features
from .factor_current_score import _resolve_candidate_file, _resolve_family_run


LIMITATIONS = [
    "This is a cross-lineage score diagnostic, not a current strategy signal.",
    "Frozen training rows use the older BulkResearchFeed/action/calendar lineage; StockDB features use a pct_chg cumulative-return proxy.",
    "No dated listing, ST, liquidity, company-action or same-ledger eligibility was applied.",
    "No ranking, target holdings, paper orders, broker quotes or Tonghuashun payload were generated.",
]


def _read_json(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _resolve_feature_run(value: Path) -> tuple[Path, dict]:
    path = Path(value).resolve()
    if path.is_dir():
        path = path / "run.json"
    record = _read_json(path)
    if record.get("status") != "completed_stockdb_factor_features":
        raise ValueError("Completed StockDB raw 190-factor feature run required")
    flags = ("execution_admitted", "strategy_validated", "model_trained",
             "ranking_generated", "paper_orders_generated", "target_holdings_generated")
    if any(record.get(flag) is not False for flag in flags):
        raise ValueError("StockDB feature run has a non-research flag")
    result = record.get("result") or {}
    feature_path = Path(result.get("feature_path", "")).resolve()
    if not feature_path.is_file() or result.get("factors") != 190:
        raise ValueError("StockDB feature run does not contain the fixed 190-factor artifact")
    return path, record


def run(feature_run: Path, family_run: Path, *, cutoff: str | None = None,
        output: Path | None = None) -> tuple[Path, dict]:
    feature_path, feature_record = _resolve_feature_run(feature_run)
    family_path, family, _ = _resolve_family_run(family_run)
    base_run = Path(family["source_run"]).resolve()
    base = _read_json(base_run)
    candidate_file, candidates = _resolve_candidate_file(base_run, base)
    if feature_record.get("candidate_file") and Path(feature_record["candidate_file"]).resolve() != candidate_file.resolve():
        raise ValueError("Feature run candidate membership differs from frozen model candidate pack")
    raw = pd.read_parquet(Path(feature_record["result"]["feature_path"]))
    expected = candidates.factor.tolist()
    if raw.index.names != ["date", "symbol"] or raw.columns.tolist() != expected or raw.index.has_duplicates:
        raise ValueError("StockDB raw feature identity does not match the frozen candidate pack")
    dates = sorted(raw.index.get_level_values("date").unique())
    if cutoff is not None:
        requested = pd.Timestamp(cutoff)
        if requested not in dates:
            raise ValueError("Diagnostic cutoff is absent from the StockDB feature panel")
        raw = raw.loc[[requested]]
        dates = [requested]
    training_path = Path(base["artifacts"]).resolve() / "training_rows.parquet"
    if not training_path.is_file():
        raise FileNotFoundError(f"Frozen training rows not found: {training_path}")
    train = pd.read_parquet(training_path)
    factors = candidates.factor.tolist()
    if not set(factors) | {"target_rank", "sample_weight"} <= set(train.columns):
        raise ValueError("Frozen training rows do not cover selected factors")
    groups = candidates.groupby(["library", "family"], sort=False).size()
    scale = pd.Series(
        [1 / np.sqrt(groups.loc[(row.library, row.family)]) for row in candidates.itertuples(index=False)],
        index=factors,
    )
    medians = train[factors].median().fillna(0)
    model = Ridge(alpha=1.0, fit_intercept=True, solver="cholesky")
    model.fit(train[factors].fillna(medians).mul(scale, axis="columns"),
              train.target_rank, sample_weight=train.sample_weight)
    features = oriented_rank_features(raw, candidates)
    matrix = features[factors].fillna(medians).mul(scale, axis="columns")
    scores = pd.Series(model.predict(matrix), index=features.index, name="score")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_factor_score_diagnostics" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_factor_score_diagnostics" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running_stockdb_factor_score_diagnostic",
        "feature_run": str(feature_path),
        "family_run": str(family_path),
        "model_source_run": str(base_run),
        "candidate_file": str(candidate_file),
        "cutoff": cutoff,
        "dates": [day.strftime("%Y-%m-%d") for day in dates],
        "training_rows": len(train),
        "training_months": int(train.index.get_level_values("date").nunique()),
        "artifacts": str(artifacts),
        "execution_admitted": False,
        "strategy_validated": False,
        "model_trained": False,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "eligibility_applied": False,
        "cross_lineage_diagnostic": True,
        "limitations": LIMITATIONS,
    }
    try:
        score_path = artifacts / "diagnostic_scores.parquet"
        scores.to_frame().to_parquet(score_path)
        model_path = artifacts / "model.json"
        model_path.write_text(json.dumps({
            "alpha": 1.0,
            "candidate_count": len(factors),
            "training_rows": len(train),
            "training_months": int(train.index.get_level_values("date").nunique()),
            "last_training_date": str(train.index.get_level_values("date").max()),
            "new_labels_used": False,
            "execution_admitted": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = {
            "status": "completed_stockdb_factor_score_diagnostic",
            "score_path": str(score_path),
            "score_rows": len(scores),
            "finite_score_count": int(scores.notna().sum()),
            "factor_count": len(factors),
            "score_min": float(scores.min()),
            "score_max": float(scores.max()),
            "feature_dates": len(dates),
        }
        record.update(status=result["status"], result=result)
        (out / "REPORT.md").write_text("\n".join([
            "# StockDB fixed-factor score diagnostic", "",
            "Cross-lineage research diagnostic only; no ranking or paper route.",
            f"- Feature dates: `{record['dates']}`; score rows: `{len(scores)}`; finite: `{int(scores.notna().sum())}`",
            f"- Frozen model: `{family_path}`; factors: `{len(factors)}`; training rows: `{len(train)}`",
            f"- Diagnostic score artifact: `{score_path}`",
            "- Eligibility applied: `false`; execution admitted: `false`.", "", "## Limits", "",
            *["- " + item for item in LIMITATIONS], "",
        ]) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed_stockdb_factor_score_diagnostic",
                      error=f"{type(error).__name__}: {error}")
        raise
    finally:
        (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "run.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-run", required=True, type=Path)
    parser.add_argument("--family-run", required=True, type=Path)
    parser.add_argument("--cutoff")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.feature_run, args.family_run, cutoff=args.cutoff, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0 if result["status"] == "completed_stockdb_factor_score_diagnostic" else 1


if __name__ == "__main__":
    raise SystemExit(main())
