"""Generate dated frozen Alpha158/LightGBM score batches for research only.

This consumer deliberately stops at score artifacts.  It uses the existing
native StockDB Alpha158 partitions and frozen LightGBM weights without refit
or labels, so later consumers can prove that a score date has a following raw
bar before attempting an Engine/Ledger replay.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .stockdb_alpha158_lgbm_diagnostic import (SCORE_NORMALIZATION_METHOD,
    _resolve_model, _resolve_native, _frozen_predictions)


def _calendar(native_record: dict) -> list[pd.Timestamp]:
    provider = Path(native_record["provider_view"]).resolve() / "result.json"
    payload = json.loads(provider.read_text(encoding="utf-8"))
    values = payload.get("calendar") or []
    if not values:
        raise ValueError("Native provider calendar is empty")
    result = []
    for value in values:
        text = str(value)
        if len(text) != 8 or not text.isdigit():
            raise ValueError(f"Invalid provider calendar value: {value!r}")
        result.append(pd.to_datetime(text, format="%Y%m%d"))
    if result != sorted(set(result)):
        raise ValueError("Native provider calendar is not ordered and unique")
    return result


def _read_range(native_root: Path, native_record: dict, dates: set[pd.Timestamp], columns: list[str]) -> pd.DataFrame:
    pieces = []
    for partition in native_record["partitions"]:
        frame = pd.read_parquet(native_root / partition["file"])
        if frame.index.names != ["date", "symbol"] or frame.columns.tolist() != columns:
            raise ValueError(f"Native partition identity changed: {partition['file']}")
        mask = frame.index.get_level_values("date").isin(dates)
        if mask.any():
            pieces.append(frame.loc[mask])
    if not pieces:
        raise ValueError("No native feature rows in requested date range")
    result = pd.concat(pieces).sort_index()
    if result.index.has_duplicates:
        raise ValueError("Dated native score input contains duplicate date/symbol rows")
    return result


def run(native_run: Path, model_run: Path, *, start: str, end: str,
        top: int = 30, output: Path | None = None) -> tuple[Path, dict]:
    if top < 1:
        raise ValueError("top must be positive")
    native_path, native, native_root = _resolve_native(native_run)
    model_path, model, model_paths = _resolve_model(model_run)
    catalog = pd.read_csv(native_root / "alpha158_catalog.csv", keep_default_na=False)
    if len(catalog) != 158 or catalog.name.duplicated().any() or catalog.formula.eq("").any():
        raise ValueError("Native Alpha158 catalog is incomplete")
    columns = catalog.name.tolist()
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    requested = {day for day in _calendar(native) if start_ts <= day <= end_ts}
    if not requested:
        raise ValueError("No native sessions in requested range")
    frame = _read_range(native_root, native, requested, columns)
    predictions = _frozen_predictions(frame, model_paths, columns)
    scores = predictions.reset_index()
    scores["date"] = pd.to_datetime(scores["date"]).dt.strftime("%Y-%m-%d")
    scores["symbol"] = scores["symbol"].astype(str)
    scores = scores.sort_values(["date", "rank_mean", "symbol"], ascending=[True, False, True], kind="stable")
    ranking = scores.groupby("date", sort=True, group_keys=False).head(top).copy()
    ranking.insert(0, "rank", ranking.groupby("date", sort=False).cumcount() + 1)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(output).resolve() if output else Path(r"D:\Quantification\outputs\stockdb_alpha158_lgbm_batch_diagnostics") / identifier
    out.mkdir(parents=True, exist_ok=False)
    score_path = out / "dated_scores.parquet"
    ranking_path = out / "dated_ranking.csv"
    scores.to_parquet(score_path, index=False)
    ranking.to_csv(ranking_path, index=False)
    record = {
        "schema": "a_share_stockdb_alpha158_lgbm_dated_score_batch_v1",
        "status": "completed_research_only_dated_score_batch",
        "native_run": str(native_path), "model_run": str(model_path),
        "start": start_ts.strftime("%Y-%m-%d"), "end": end_ts.strftime("%Y-%m-%d"),
        "score_dates": sorted(scores["date"].unique().tolist()),
        "score_rows": int(len(scores)), "ranking_rows": int(len(ranking)),
        "top_per_date": int(top), "feature_count": 158,
        "score_path": str(score_path), "ranking_path": str(ranking_path),
        "score_normalization_method": SCORE_NORMALIZATION_METHOD,
        "score_normalization_group_key": "date",
        "model_refit": False, "future_labels_used_for_score": False,
        "engine_ledger_replay_completed": False, "target_holdings_generated": False,
        "paper_orders_generated": False, "ths_payload_generated": False,
        "execution_admitted": False, "strategy_validated": False,
        "ledger_mutated": False, "cross_lineage_diagnostic": True,
        "limitations": [
            "Frozen weights use the older model lineage; no refit or labels were used.",
            "Scores are not admitted until dated eligibility, PIT membership, actions and following raw bars are bound.",
            "This artifact is a score batch, not a current signal, target, paper order or THS payload.",
        ],
    }
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {
        "schema": "a_share_stockdb_alpha158_lgbm_dated_score_batch_audit_v1",
        "status": "passed_research_only_gates",
        "result": str(out / "result.json"),
        "score_path": str(score_path),
        "score_normalization_method": SCORE_NORMALIZATION_METHOD,
        "checks": {
            "frozen_model_only": True, "model_refit": False,
            "future_labels_used_for_score": False, "execution_admitted": False,
            "strategy_validated": False, "ledger_mutated": False,
            "target_holdings_generated": False, "paper_orders_generated": False,
            "ths_payload_generated": False,
        },
    }
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# Dated Alpha158/LightGBM score batch", "",
        f"Dates: `{record['start']}`..`{record['end']}`; sessions: `{len(record['score_dates'])}`; rows: `{record['score_rows']}`.",
        f"Top rows per date: `{top}`; model refit: `false`; future labels used: `false`.",
        "This is a research score batch only. It has not entered Engine/Ledger, paper orders, THS or a broker.",
        f"Audit: `{out / 'audit.json'}`.",
        "", "## Limits", "", *["- " + item for item in record["limitations"]],
    ]) + "\n", encoding="utf-8")
    return out / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-run", required=True, type=Path)
    parser.add_argument("--model-run", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    path, record = run(args.native_run, args.model_run, start=args.start, end=args.end,
                       top=args.top, output=args.out)
    print(json.dumps({"result": str(path), "status": record["status"],
                      "score_rows": record["score_rows"], "score_dates": len(record["score_dates"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
