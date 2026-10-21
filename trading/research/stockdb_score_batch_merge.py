"""Join immutable saved raw-seed batches without model inference or labels."""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .stockdb_alpha158_lgbm_batch_diagnostic import _calendar
from .stockdb_alpha158_lgbm_diagnostic import SCORE_NORMALIZATION_METHOD


SEEDS = ["score_seed17", "score_seed29", "score_seed43"]
FALSE_FLAGS = ("model_refit", "future_labels_used_for_score", "execution_admitted", "strategy_validated",
               "ledger_mutated", "target_holdings_generated", "paper_orders_generated", "ths_payload_generated")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _sha(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def merge_batches(batch_runs, *, start, end, out):
    if len(batch_runs) < 2:
        raise ValueError("Explicit existing batches and missing-date batch required")
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError(out)
    frames, refs, identity = [], [], None
    for value in batch_runs:
        path = Path(value).resolve()
        if path.is_dir():
            path /= "result.json"
        record = _json(path)
        if record.get("status") != "completed_research_only_dated_score_batch" or any(record.get(k) is not False for k in FALSE_FLAGS):
            raise ValueError("Only unadmitted frozen score batches can be joined")
        current_identity = (str(Path(record["native_run"]).resolve()), str(Path(record["model_run"]).resolve()))
        if identity is None:
            identity = current_identity
        elif current_identity != identity:
            raise ValueError("Native/model identity differs between saved batches")
        score_path = Path(record["score_path"]).resolve()
        frame = pd.read_parquet(score_path, columns=["date", "symbol", *SEEDS])
        frame["date"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
        frame["symbol"] = frame.symbol.astype(str)
        if len(frame) != record["score_rows"] or sorted(frame.date.unique()) != record["score_dates"]:
            raise ValueError("Saved batch count or exact dates changed")
        for field in SEEDS:
            frame[field] = pd.to_numeric(frame[field], errors="raise")
        if not np.isfinite(frame[SEEDS].to_numpy()).all():
            raise ValueError("Saved raw seed scores must be finite")
        frame = frame.loc[frame.date.between(start, end)].copy()
        if frame.empty:
            raise ValueError("A supplied batch contributes no requested dates")
        frames.append(frame)
        refs.append({"batch": str(path), "batch_sha256": _sha(path), "score_path": str(score_path),
                     "score_sha256": _sha(score_path), "used_rows": len(frame), "used_dates": sorted(frame.date.unique()),
                     "saved_normalization": record.get("score_normalization_method", "legacy_whole_batch_rank")})
    scores = pd.concat(frames, ignore_index=True)
    if scores.duplicated(["date", "symbol"]).any():
        raise ValueError("Overlapping saved batches; refuse duplicate date/symbol rather than choose a winner")
    native = _json(identity[0])
    expected = [day.strftime("%Y-%m-%d") for day in _calendar(native) if start <= day.strftime("%Y-%m-%d") <= end]
    cohort = {s for part in native["partitions"] for s in part["symbols"]}
    if not expected or sorted(scores.date.unique()) != expected:
        raise ValueError("Merged batch has an exact native-calendar gap or extra date")
    if len(cohort) != native["symbols"] or any(set(group.symbol) != cohort for _, group in scores.groupby("date")):
        raise ValueError("Every date must retain the complete fixed native grid, not selected candidates")
    scores["rank_mean"] = scores.groupby("date", sort=False)[SEEDS].rank(pct=True).mean(axis=1)
    scores = scores.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    ranking = scores.sort_values(["date", "rank_mean", "symbol"], ascending=[True, False, True], kind="stable").groupby("date").head(30)
    out.mkdir(parents=True, exist_ok=False)
    scores.to_parquet(out / "dated_scores.parquet", index=False)
    ranking.to_csv(out / "dated_ranking.csv", index=False)
    record = {"schema": "a_share_stockdb_alpha158_lgbm_dated_score_batch_v1",
        "status": "completed_research_only_dated_score_batch", "method": "join_saved_raw_seed_predictions_without_inference_v1",
        "created_at": datetime.now().astimezone().isoformat(), "native_run": identity[0], "model_run": identity[1],
        "start": start, "end": end, "score_dates": expected, "score_rows": len(scores), "feature_count": 158,
        "score_path": str(out / "dated_scores.parquet"), "ranking_path": str(out / "dated_ranking.csv"),
        "ranking_rows": len(ranking), "top_per_date": 30, "source_batches": refs,
        "score_normalization_method": SCORE_NORMALIZATION_METHOD, "score_normalization_group_key": "exact_date",
        "saved_rank_columns_used": False, "inference_performed": False, "engine_ledger_replay_completed": False,
        "cross_lineage_diagnostic": True, "new_untouched_test": False,
        **{k: False for k in FALSE_FLAGS},
        "limitations": ["Source hashes captured at merge; do not backdate them as original historical publication seals.",
                        "Retrospective inspected StockDB migration diagnostic, not PIT or account-ready signals.",
                        "Saved per-seed raw predictions reused exactly; old aggregate ranks are never joined as signals."]}
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-run", type=Path, action="append", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = merge_batches(args.batch_run, start=args.start, end=args.end, out=args.out)
    print(json.dumps({"result": str(args.out / "result.json"), "score_rows": result["score_rows"], "score_dates": len(result["score_dates"])}))
    return 0
