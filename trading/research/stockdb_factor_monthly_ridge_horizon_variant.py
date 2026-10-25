"""Run the prespecified 60-session same-lineage Ridge variant.

This is the R04 follow-up to the fixed 20-session mature-factor Ridge fit.  It
reuses the pinned feature/provider inputs and sends the resulting score panel
through the existing StockDB Ridge Engine/Ledger adapter.  The horizon is
intentionally fixed to 60; this module is not a parameter-search interface.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .factor_candidate_replay import fit_training, oriented_rank_features
from .factor_families import forward_open_labels, read_open_panel
from .stockdb_factor_features import load_stockdb_feed, resolve_candidates
from .stockdb_factor_monthly_ridge_diagnostic import _resolve_feature_run, _validate_panel
from .stockdb_factor_monthly_ridge_replay import replay, write_result


SCHEMA = "mature_factor_stockdb_monthly_ridge_horizon_variant_v1"
HORIZON = 60
PARENT_HORIZON = 20
VARIANT_ID = "mature_factor_balanced_ridge_h60"
FREEZE_BASIS = "R04_prespecified_60_session_variant_20261025"
RAW_SNAPSHOT = Path(
    r"D:\QuantificationData\trading\research\full_market\stockdb_2024_0101_1013_current_cutoff_v1"
)
DIAGNOSTIC_RECEIPT = "20261025_mature_factor_stockdb_monthly_ridge_h60_receipt.json"
REPLAY_RECEIPT = "20261025_mature_factor_stockdb_monthly_ridge_h60_replay_receipt.json"


def _read_json(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def fit_variant(feature_run: Path, provider_view: Path, *, output: Path | None = None) -> tuple[Path, dict]:
    """Fit the fixed 60-session model without changing the frozen inputs."""
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
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_factor_monthly_ridge_horizon60" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_factor_monthly_ridge_horizon60" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "schema": SCHEMA,
        "id": identifier,
        "variant_id": VARIANT_ID,
        "parent_variant_id": "factor_candidate_family_balanced_ridge",
        "parent_horizon_sessions": PARENT_HORIZON,
        "freeze_basis": FREEZE_BASIS,
        "status": "running_stockdb_factor_monthly_ridge_horizon_variant",
        "feature_run": str(feature_run),
        "feature_artifact": str(feature_path),
        "provider_view": str(provider_view),
        "provider_folder": str(feed.provider_folder),
        "candidate_file": str(Path(feature_record["candidate_file"]).resolve()),
        "label_source": "StockDB provider open.day.bin forward_open_labels",
        "horizon_sessions": HORIZON,
        "training_period": ["2024-01-02", "2024-12-31"],
        "feature_dates": [day.strftime("%Y-%m-%d") for day in sorted(raw.index.get_level_values("date").unique())],
        "artifacts": str(artifacts),
        "predeclared_variant": True,
        "selection_enabled": False,
        "execution_admitted": False,
        "strategy_validated": False,
        "model_trained": True,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "eligibility_applied": False,
        "shared_ledger_replay": False,
        "limitations": [
            "The 60-session horizon was fixed before this rerun; no horizon search or return-based tuning was performed.",
            "Open labels use the StockDB provider positive-volume research proxy, not execution prices.",
            "No dated listing, ST, liquidity, company-action, publication or revision/as-of PIT eligibility was applied.",
            "This fit and its replay remain research diagnostics and do not authorize paper or live execution.",
        ],
    }
    score_path = artifacts / "monthly_scores.parquet"
    scores.to_frame().to_parquet(score_path)
    train.to_parquet(artifacts / "training_rows.parquet")
    split.to_csv(artifacts / "training_split.csv", index=False)
    (artifacts / "model.json").write_text(json.dumps({
        "alpha": 1.0, "horizon_sessions": HORIZON, "candidate_count": len(candidates),
        "training_rows": len(train), "training_months": int(train.index.get_level_values("date").nunique()),
        "last_training_date": str(train.index.get_level_values("date").max()),
        "labels": "provider_open_forward_proxy", "execution_admitted": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {
        "status": "completed_stockdb_factor_monthly_ridge_horizon_variant",
        "score_path": str(score_path),
        "training_rows": len(train),
        "training_months": int(train.index.get_level_values("date").nunique()),
        "score_rows": len(scores),
        "score_dates": int(scores.index.get_level_values("date").nunique()),
        "finite_score_count": int(scores.notna().sum()),
        "feature_factor_count": len(candidates),
        "finite_label_cells": int(np.isfinite(forward.to_numpy()).sum()),
    }
    # Keep the existing replay adapter contract while retaining an explicit
    # variant status for downstream evidence consumers.
    record.update(status="completed_stockdb_factor_monthly_ridge_diagnostic",
                  variant_status=result["status"], result=result)
    receipt = {
        "schema": "mature_factor_stockdb_monthly_ridge_horizon_variant_receipt_v1",
        "status": "COMPLETED_PRESPECIFIED_60_SESSION_RESEARCH_DIAGNOSTIC",
        "variant_id": VARIANT_ID,
        "parent_variant_id": "factor_candidate_family_balanced_ridge",
        "freeze_basis": FREEZE_BASIS,
        "run": str(out / "run.json"),
        "feature_run": str(feature_run),
        "provider_view": str(provider_view),
        "label_source": record["label_source"],
        "training_period": record["training_period"],
        "horizon_sessions": HORIZON,
        "metrics": result,
        "predeclared_variant": True,
        "selection_enabled": False,
        "shared_ledger_replay": False,
        "execution_admitted": False,
        "strategy_validated": False,
        "blockers": [
            "PIT publication/revision evidence and company actions are not bound.",
            "This is an independent 60-session horizon diagnostic, not strategy validation or adoption.",
        ],
    }
    receipt_dir = ROOT / "outputs" / "pipeline_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / DIAGNOSTIC_RECEIPT).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# Prespecified 60-session mature-factor Ridge diagnostic", "",
        "This is a fixed-horizon same-lineage fit using the existing feature/provider snapshot; no data was fetched.",
        f"- Parent: `{PARENT_HORIZON}` sessions; variant horizon: `{HORIZON}` sessions.",
        f"- Training: `{record['training_period'][0]}..{record['training_period'][1]}`; months `{result['training_months']}`; rows `{result['training_rows']}`.",
        f"- Scores: `{result['score_dates']}` dates / `{result['score_rows']}` rows / `{result['finite_score_count']}` finite.",
        "- Selection enabled: `false`; execution admitted: `false`; strategy validated: `false`.", "", "## Limits", "",
        *["- " + item for item in record["limitations"]], "",
    ]) + "\n", encoding="utf-8")
    (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (artifacts / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "run.json", record


def replay_variant(ridge_run: Path, snapshot: Path = RAW_SNAPSHOT, *, output: Path) -> tuple[Path, dict]:
    """Replay fixed 60-session scores through the existing Engine/Ledger adapter."""
    ridge_run = Path(ridge_run).resolve()
    record = _read_json(ridge_run)
    if record.get("variant_id") != VARIANT_ID or record.get("horizon_sessions") != HORIZON:
        raise ValueError("A completed prespecified 60-session Ridge run is required")
    result = replay(ridge_run, Path(snapshot).resolve(), top_per_day=5, max_symbols=500,
                    holdings=5, initial_cash=140000.0)
    result.update(strategy_id=VARIANT_ID, variant_id=VARIANT_ID, horizon_sessions=HORIZON,
                  freeze_basis=FREEZE_BASIS, predeclared_variant=True, selection_enabled=False,
                  execution_admitted=False, strategy_validated=False, ledger_mutated=False)
    paths = write_result(
        result, Path(output).resolve(), receipt_name=REPLAY_RECEIPT,
        report_title="Prespecified 60-session mature-factor Ridge Engine/Ledger replay",
    )
    return Path(paths["result"]), result


def run_variant(feature_run: Path, provider_view: Path, *, fit_output: Path, replay_output: Path,
                snapshot: Path = RAW_SNAPSHOT) -> dict:
    fit_run, fit_record = fit_variant(feature_run, provider_view, output=fit_output)
    result_path, replay_record = replay_variant(fit_run, snapshot=snapshot, output=replay_output)
    return {"fit_run": str(fit_run), "fit": fit_record, "replay_result": str(result_path), "replay": replay_record}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fit = sub.add_parser("fit")
    fit.add_argument("--feature-run", required=True, type=Path)
    fit.add_argument("--provider-view", required=True, type=Path)
    fit.add_argument("--out", required=True, type=Path)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--ridge-run", required=True, type=Path)
    replay_parser.add_argument("--snapshot", type=Path, default=RAW_SNAPSHOT)
    replay_parser.add_argument("--out", required=True, type=Path)
    run = sub.add_parser("run")
    run.add_argument("--feature-run", required=True, type=Path)
    run.add_argument("--provider-view", required=True, type=Path)
    run.add_argument("--snapshot", type=Path, default=RAW_SNAPSHOT)
    run.add_argument("--fit-out", required=True, type=Path)
    run.add_argument("--replay-out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "fit":
        path, record = fit_variant(args.feature_run, args.provider_view, output=args.out)
        print(json.dumps({"run": str(path), "status": record["status"], "horizon_sessions": HORIZON}, ensure_ascii=False))
        return 0
    if args.command == "replay":
        path, result = replay_variant(args.ridge_run, snapshot=args.snapshot, output=args.out)
        print(json.dumps({"result": str(path), "status": result["status"], "net_return": result["metrics"]["net_return"]}, ensure_ascii=False))
        return 0
    payload = run_variant(args.feature_run, args.provider_view, fit_output=args.fit_out,
                          replay_output=args.replay_out, snapshot=args.snapshot)
    print(json.dumps({"fit_run": payload["fit_run"], "replay_result": payload["replay_result"],
                      "status": payload["replay"]["status"], "net_return": payload["replay"]["metrics"]["net_return"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
