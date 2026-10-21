"""Run frozen Alpha158/LightGBM inference on a StockDB native panel.

This is the smallest bridge from the newly rebuilt StockDB native Alpha158
features to the existing frozen LightGBM weights.  It deliberately stops at
diagnostic scores (and an optional gated rank preview): no labels are fitted,
no Engine/Ledger state is opened, and no paper or broker payload is created.
The native StockDB values are adjusted research proxies, so this module cannot
promote them to a current trading signal.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import lightgbm as lgb
import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from ..symbols import normalize_symbol


SEEDS = (17, 29, 43)
SCORE_NORMALIZATION_METHOD = "date_local_cross_sectional_seed_percentile_rank_mean_v1"
LIMITATIONS = [
    "Frozen LightGBM weights were trained on the older BulkResearchFeed lineage; no refit or new labels are used.",
    "StockDB native Alpha158 values are based on a cumulative pct_chg research proxy, not raw RMB order prices.",
    "Observed-history eligibility is a conservative proxy; StockDB membership, PIT listing data and company actions remain unverified.",
    "Scores and rank previews do not enter SignalFrame, Engine, Ledger, paper orders, broker quotes or Tonghuashun.",
]


def _json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _run_json(value: Path) -> tuple[Path, dict]:
    path = Path(value).resolve()
    if path.is_dir():
        path /= "result.json"
    return path, _json(path)


def _resolve_native(value: Path) -> tuple[Path, dict, Path]:
    path, record = _run_json(value)
    if record.get("status") != "full_native_features_completed":
        raise ValueError("Completed native StockDB Alpha158 feature run required")
    if record.get("execution_admitted") is not False or record.get("model_trained") is not False:
        raise ValueError("Native feature run must remain research-only and unfitted")
    if record.get("features") != 158 or not record.get("partitions"):
        raise ValueError("Native feature run does not contain 158-feature partitions")
    root = path.parent
    catalog = root / "alpha158_catalog.csv"
    if not catalog.is_file():
        raise FileNotFoundError(f"Native Alpha158 catalog not found: {catalog}")
    return path, record, root


def _resolve_model(value: Path) -> tuple[Path, dict, dict[str, Path]]:
    path, record = _run_json(value)
    if record.get("status") != "completed" or record.get("experiment_kind") != "alpha158_ingestion_ledger_comparison":
        raise ValueError("Completed fixed Alpha158/LightGBM ingestion diagnostic required")
    if any(record.get(flag) is not False for flag in ("live_ready", "strategy_validated", "fresh_holdout_claimed")):
        raise ValueError("LightGBM source run has a non-research flag")
    if record.get("config", {}).get("seeds") != list(SEEDS):
        raise ValueError("Frozen Alpha158 model seeds must be 17, 29 and 43")
    paths = {}
    for seed in SEEDS:
        source = Path(record.get("model_sources", {}).get(f"alpha158_seed{seed}", "")).resolve()
        if not (source / "model.txt").is_file() or not (source / "preprocessing.csv").is_file():
            raise FileNotFoundError(f"Frozen seed {seed} model/preprocessing not found: {source}")
        paths[seed] = source
    return path, record, paths


def _latest_date(native_record: dict) -> pd.Timestamp:
    provider = Path(native_record["provider_view"]).resolve() / "result.json"
    payload = _json(provider)
    calendar = payload.get("calendar") or []
    if not calendar:
        raise ValueError("Native provider calendar is empty")
    return pd.Timestamp(calendar[-1])


def _read_native_day(root: Path, record: dict, date: pd.Timestamp, columns: list[str]) -> pd.DataFrame:
    pieces = []
    for part in record["partitions"]:
        frame = pd.read_parquet(root / part["file"])
        if frame.index.names != ["date", "symbol"] or frame.columns.tolist() != columns:
            raise ValueError(f"Native partition identity changed: {part['file']}")
        day = frame.loc[frame.index.get_level_values("date") == date]
        if not day.empty:
            pieces.append(day)
    if not pieces:
        raise ValueError(f"Native feature date is absent: {date.date()}")
    result = pd.concat(pieces).sort_index()
    if result.index.has_duplicates or result.index.get_level_values("date").nunique() != 1:
        raise ValueError("Native day has duplicate or mixed-date rows")
    return result


def _load_eligibility(value: Path, date: pd.Timestamp) -> tuple[pd.Series, dict]:
    path, record = _run_json(value)
    if record.get("status") != "completed_research_only_eligibility_compatibility" or record.get("execution_admitted") is not False:
        raise ValueError("Completed research-only StockDB eligibility receipt required")
    if pd.Timestamp(record.get("latest_session")) != date:
        raise ValueError("Eligibility date does not match native feature date")
    csv_path = Path(record.get("artifacts", "")) / "latest_eligibility_observation.csv"
    observations = pd.read_csv(csv_path, dtype={"symbol": str})
    compatibility = "observed_compatibility_known" if "observed_compatibility_known" in observations.columns else "compatibility_known"
    eligible = "observed_eligible_proxy" if "observed_eligible_proxy" in observations.columns else "eligible_proxy"
    if compatibility not in observations or eligible not in observations:
        raise ValueError("Eligibility receipt lacks compatibility/eligibility fields")
    symbols = observations.loc[observations[compatibility].astype(bool) & observations[eligible].astype(bool), "symbol"]
    canonical = symbols.map(lambda value: normalize_symbol(value).vt_symbol)
    return pd.Series(True, index=pd.Index(canonical.unique(), name="symbol")), {
        "receipt": str(path), "compatibility_field": compatibility, "eligibility_field": eligible,
        "eligible_count": int(canonical.nunique()),
    }


def _frozen_predictions(frame: pd.DataFrame, model_paths: dict[int, Path], columns: list[str]) -> pd.DataFrame:
    predictions = {}
    for seed, folder in model_paths.items():
        booster = lgb.Booster(model_file=str(folder / "model.txt"))
        if booster.feature_name() != columns:
            raise ValueError(f"Seed {seed} model feature order differs from native Alpha158 catalog")
        preprocessing = pd.read_csv(folder / "preprocessing.csv", index_col=0)
        if "train_median" not in preprocessing.columns or preprocessing.index.tolist() != columns:
            raise ValueError(f"Seed {seed} preprocessing feature order differs from native Alpha158 catalog")
        medians = preprocessing["train_median"].astype("float64")
        if not np.isfinite(medians.to_numpy()).all():
            raise ValueError(f"Seed {seed} preprocessing contains nonfinite medians")
        values = frame[columns].fillna(medians)
        values = values.replace([np.inf, -np.inf], np.nan).fillna(medians)
        scores = booster.predict(values, num_threads=2)
        if not np.isfinite(scores).all():
            raise ValueError(f"Seed {seed} produced nonfinite scores")
        predictions[f"score_seed{seed}"] = scores
    result = pd.DataFrame(predictions, index=frame.index)
    # A dated score must not depend on any other session in the inference
    # batch.  In particular, later distributions can otherwise change the
    # relative contribution of each seed and reverse today's ensemble rank.
    result["rank_mean"] = (result[list(predictions)].groupby(level="date", sort=False)
                           .rank(pct=True).mean(axis=1))
    return result


def run(native_run: Path, model_run: Path, *, cutoff: str | None = None,
        eligibility_receipt: Path | None = None, compare_score_run: Path | None = None,
        top: int = 30, output: Path | None = None) -> tuple[Path, dict]:
    native_path, native, native_root = _resolve_native(native_run)
    model_path, model, model_paths = _resolve_model(model_run)
    catalog = pd.read_csv(native_root / "alpha158_catalog.csv", keep_default_na=False)
    if len(catalog) != 158 or catalog.name.duplicated().any() or catalog.formula.eq("").any():
        raise ValueError("Native Alpha158 catalog is incomplete")
    columns = catalog.name.tolist()
    date = pd.Timestamp(cutoff) if cutoff else _latest_date(native)
    frame = _read_native_day(native_root, native, date, columns)
    predictions = _frozen_predictions(frame, model_paths, columns)
    eligibility_meta = {"applied": False}
    if eligibility_receipt is not None:
        eligible, metadata = _load_eligibility(eligibility_receipt, date)
        allowed = frame.index.get_level_values("symbol").isin(eligible.index)
        predictions = predictions.loc[allowed]
        eligibility_meta = {"applied": True, **metadata, "native_rows_before": int(len(frame)),
                            "native_rows_after": int(len(predictions))}
    if predictions.empty:
        raise ValueError("No native rows remain after optional eligibility gate")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_alpha158_lgbm_diagnostics" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_alpha158_lgbm_diagnostics" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    score_path = artifacts / "diagnostic_scores.parquet"
    predictions.to_parquet(score_path)
    ranking = predictions.sort_values(["rank_mean"], ascending=False, kind="stable").head(top).reset_index()
    ranking.insert(0, "rank", range(1, len(ranking) + 1))
    ranking_path = artifacts / "diagnostic_ranking.csv"
    ranking.to_csv(ranking_path, index=False)
    comparison = None
    if compare_score_run is not None:
        compare_path, compare_record = _run_json(compare_score_run)
        score_file = Path((compare_record.get("result") or {}).get("score_path", ""))
        if not score_file.is_file():
            raise FileNotFoundError(f"Comparison score artifact not found: {score_file}")
        baseline = pd.read_parquet(score_file)
        if baseline.index.names != ["date", "symbol"] or "score" not in baseline.columns:
            raise ValueError("Comparison score artifact must use date/symbol index and score column")
        baseline_day = baseline.loc[baseline.index.get_level_values("date") == date, "score"]
        joined = predictions["rank_mean"].rename("lgbm").to_frame().join(baseline_day.rename("baseline"), how="inner").dropna()
        lgbm_top = set(predictions["rank_mean"].nlargest(top).index.get_level_values("symbol"))
        baseline_top = set(baseline_day.nlargest(top).index.get_level_values("symbol"))
        comparison = {"run": str(compare_path), "rows": int(len(joined)),
                      "spearman": float(joined.lgbm.corr(joined.baseline, method="spearman")) if len(joined) > 1 else None,
                      "top_n": int(top), "top_overlap": int(len(lgbm_top & baseline_top)),
                      "top_overlap_ratio": float(len(lgbm_top & baseline_top) / top) if top else None}
        (artifacts / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record = {
        "id": identifier,
        "status": "completed_stockdb_alpha158_lgbm_diagnostic",
        "native_run": str(native_path), "model_run": str(model_path),
        "date": date.strftime("%Y-%m-%d"), "native_rows": int(len(frame)),
        "score_rows": int(len(predictions)), "finite_score_rows": int(np.isfinite(predictions.to_numpy()).all(axis=1).sum()),
        "seed_models": {str(seed): str(path) for seed, path in model_paths.items()},
        "feature_count": 158, "eligibility": eligibility_meta, "comparison": comparison,
        "score_path": str(score_path), "ranking_path": str(ranking_path),
        "score_normalization_method": SCORE_NORMALIZATION_METHOD,
        "score_normalization_group_key": "date",
        "execution_admitted": False, "strategy_validated": False, "model_trained": False,
        "diagnostic_ranking_generated": True, "ranking_generated": False,
        "target_holdings_generated": False, "paper_orders_generated": False,
        "ths_payload_generated": False, "cross_lineage_diagnostic": True,
        "limitations": LIMITATIONS,
    }
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# StockDB Alpha158/LightGBM diagnostic", "", "Frozen model inference on current StockDB native features; research-only.",
             f"- Date: `{record['date']}`; native rows: `{len(frame)}`; scored rows: `{len(predictions)}`; finite: `{record['finite_score_rows']}`.",
             f"- Seed models: `{', '.join(str(seed) for seed in SEEDS)}`; features: `158`; no refit or labels.",
             f"- Diagnostic ranking: `{ranking_path}`; eligibility applied: `{eligibility_meta['applied']}`.",
             "- Execution admitted: `false`; target/paper/THS payload generated: `false`.", ""]
    if comparison:
        lines += [f"- Same-date score comparison rows: `{comparison['rows']}`; Spearman: `{comparison['spearman']}`; top{top} overlap: `{comparison['top_overlap']}/{top}`.", ""]
    lines += ["## Limits", "", *["- " + item for item in LIMITATIONS], "", f"Artifacts: `{artifacts}`"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-run", required=True, type=Path)
    parser.add_argument("--model-run", required=True, type=Path)
    parser.add_argument("--cutoff")
    parser.add_argument("--eligibility-receipt", type=Path)
    parser.add_argument("--compare-score-run", type=Path)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.native_run, args.model_run, cutoff=args.cutoff,
                       eligibility_receipt=args.eligibility_receipt,
                       compare_score_run=args.compare_score_run, top=args.top, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
