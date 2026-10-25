"""Fit a research-only Ridge on the current StockDB monthly factor panel.

The panel, open labels and factor directions all come from one pinned StockDB
provider/native lineage.  This module deliberately stops before eligibility,
ranking, target holdings, paper orders, the Ledger or any broker integration.
PIT publication events and corporate actions are not inferred from the proxy
provider and remain explicit promotion blockers.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from ..settings import DATA_ROOT, ROOT
from .factor_candidate_replay import fit_training, oriented_rank_features
from .factor_families import forward_open_labels, read_open_panel
from .stockdb_factor_features import load_stockdb_feed, resolve_candidates


HORIZON = 20
TRAIN_START = "2024-01-02"
TRAIN_END = "2024-12-31"
LIMITATIONS = [
    "Open labels use the StockDB provider positive-volume research proxy, not execution prices.",
    "No dated listing, ST, liquidity, company-action, publication or revision/as-of PIT eligibility was applied.",
    "This fit writes scores and training diagnostics only; no ranking, target, paper order, Ledger or broker artifact is generated.",
    "The result is a research diagnostic and is not strategy validation or permission to trade.",
]


def _read_json(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _resolve_feature_run(value: Path) -> tuple[Path, dict, Path]:
    path = Path(value).resolve()
    if path.is_dir():
        path = path / "run.json"
    record = _read_json(path)
    if record.get("status") != "completed_stockdb_factor_features":
        raise ValueError("Completed StockDB factor feature run required")
    flags = ("execution_admitted", "strategy_validated", "model_trained",
             "ranking_generated", "paper_orders_generated", "target_holdings_generated")
    if any(record.get(flag) is not False for flag in flags):
        raise ValueError("StockDB feature run has a non-research flag")
    result = record.get("result") or {}
    feature_path = Path(result.get("feature_path", "")).resolve()
    if not feature_path.is_file() or result.get("factors") != 190:
        raise ValueError("StockDB feature run does not contain the fixed 190-factor artifact")
    return path, record, feature_path


def _validate_panel(raw: pd.DataFrame, candidates: pd.DataFrame) -> None:
    expected = candidates.factor.tolist()
    if raw.index.names != ["date", "symbol"] or raw.index.has_duplicates:
        raise ValueError("Monthly factor panel must have a unique date/symbol index")
    if raw.columns.tolist() != expected:
        raise ValueError("Monthly factor panel factor identity differs from the frozen pack")
    dates = raw.index.get_level_values("date")
    if dates.nunique() < 8 or not dates.is_monotonic_increasing:
        raise ValueError("Monthly panel needs at least eight ordered dates")


def run(feature_run: Path, provider_view: Path, *, output: Path | None = None) -> tuple[Path, dict]:
    feature_run, feature_record, feature_path = _resolve_feature_run(feature_run)
    provider_view = Path(provider_view).resolve()
    feed = load_stockdb_feed(provider_view)
    if Path(feature_record.get("provider_view", "")).resolve() != provider_view:
        raise ValueError("Feature and provider views differ")
    candidates = resolve_candidates(Path(feature_record["candidate_file"]))
    raw = pd.read_parquet(feature_path)
    _validate_panel(raw, candidates)
    calendar = pd.DatetimeIndex(pd.to_datetime(feed.calendar))
    opening = read_open_panel(feed.provider_folder, feed.symbols, calendar)
    forward = forward_open_labels(opening, HORIZON)
    features = oriented_rank_features(raw, candidates)
    model, medians, train, split = fit_training(features, forward, calendar)
    scores = pd.Series(model.predict(features[medians.index].fillna(medians)),
                       index=features.index, name="score")

    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_factor_monthly_ridge_diagnostics" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_factor_monthly_ridge_diagnostics" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running_stockdb_factor_monthly_ridge_diagnostic",
        "feature_run": str(feature_run),
        "feature_artifact": str(feature_path),
        "provider_view": str(provider_view),
        "provider_folder": str(feed.provider_folder),
        "candidate_file": str(Path(feature_record["candidate_file"]).resolve()),
        "label_source": "StockDB provider open.day.bin forward_open_labels",
        "horizon_sessions": HORIZON,
        "training_period": [TRAIN_START, TRAIN_END],
        "feature_dates": [day.strftime("%Y-%m-%d") for day in sorted(raw.index.get_level_values("date").unique())],
        "artifacts": str(artifacts),
        "execution_admitted": False,
        "strategy_validated": False,
        "model_trained": True,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "eligibility_applied": False,
        "shared_ledger_replay": False,
        "limitations": LIMITATIONS,
    }
    try:
        score_path = artifacts / "monthly_scores.parquet"
        scores.to_frame().to_parquet(score_path)
        train.to_parquet(artifacts / "training_rows.parquet")
        split.to_csv(artifacts / "training_split.csv", index=False)
        (artifacts / "model.json").write_text(json.dumps({
            "alpha": 1.0,
            "horizon_sessions": HORIZON,
            "candidate_count": len(candidates),
            "training_rows": len(train),
            "training_months": int(train.index.get_level_values("date").nunique()),
            "last_training_date": str(train.index.get_level_values("date").max()),
            "labels": "provider_open_forward_proxy",
            "execution_admitted": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = {
            "status": "completed_stockdb_factor_monthly_ridge_diagnostic",
            "score_path": str(score_path),
            "training_rows": len(train),
            "training_months": int(train.index.get_level_values("date").nunique()),
            "score_rows": len(scores),
            "score_dates": int(scores.index.get_level_values("date").nunique()),
            "finite_score_count": int(scores.notna().sum()),
            "feature_factor_count": len(candidates),
            "finite_label_cells": int(np.isfinite(forward.to_numpy()).sum()),
        }
        record.update(status=result["status"], result=result)
        receipt = {
            "schema": "mature_factor_stockdb_monthly_ridge_receipt_v1",
            "status": "COMPLETED_SAME_LINEAGE_RESEARCH_DIAGNOSTIC",
            "run": str(out / "run.json"),
            "provider_view": str(provider_view),
            "feature_run": str(feature_run),
            "label_source": record["label_source"],
            "training_period": record["training_period"],
            "horizon_sessions": HORIZON,
            "metrics": result,
            "ranking_generated": False,
            "target_holdings_generated": False,
            "paper_orders_generated": False,
            "shared_ledger_replay": False,
            "execution_admitted": False,
            "strategy_validated": False,
            "blockers": [
                "No dated listing/ST/liquidity eligibility or action/PIT/as-of events were bound.",
                "The provider open labels are a research proxy and are not executable prices.",
                "No account or paper promotion is allowed from this diagnostic alone.",
            ],
        }
        (ROOT / "outputs" / "pipeline_receipts").mkdir(parents=True, exist_ok=True)
        (ROOT / "outputs" / "pipeline_receipts" / "20261024_mature_factor_stockdb_monthly_ridge_receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out / "REPORT.md").write_text("\n".join([
            "# StockDB monthly mature-factor Ridge diagnostic", "",
            "Same provider/native lineage research fit only; no eligibility, ranking, target, paper or Ledger route.",
            f"- Training: `{TRAIN_START}..{TRAIN_END}`; horizon `{HORIZON}` sessions; months `{result['training_months']}`; rows `{result['training_rows']}`.",
            f"- Scores: `{result['score_dates']}` dates / `{result['score_rows']}` rows / `{result['finite_score_count']}` finite.",
            f"- Labels: `{record['label_source']}`.",
            "- Execution admitted: `false`; strategy validated: `false`.", "", "## Limits", "",
            *["- " + item for item in LIMITATIONS], "",
        ]) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed_stockdb_factor_monthly_ridge_diagnostic",
                      error=f"{type(error).__name__}: {error}")
        raise
    finally:
        (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "run.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-run", required=True, type=Path)
    parser.add_argument("--provider-view", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.feature_run, args.provider_view, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0 if result["status"] == "completed_stockdb_factor_monthly_ridge_diagnostic" else 1


if __name__ == "__main__":
    raise SystemExit(main())
