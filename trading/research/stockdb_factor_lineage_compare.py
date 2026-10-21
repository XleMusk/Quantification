"""Compare the frozen 190-factor panel across two research lineages.

This is an input-lineage diagnostic only.  It reconstructs the same fixed
candidate definitions once through ``BulkResearchFeed`` and once through the
unadmitted StockDB provider, then reports coverage, pairwise correlation and
distribution shifts on one pinned overlap session.  It deliberately does not
fit a model, rank securities, build holdings, or write orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp, pearsonr, spearmanr

from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .factor_candidate_replay import build_monthly_features, oriented_rank_features
from .stockdb_factor_features import resolve_candidates


def _read_json(path: Path) -> dict:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _resolve_old_run(value: Path) -> tuple[Path, dict, dict]:
    run_path = Path(value).resolve()
    if run_path.is_dir():
        run_path /= "run.json"
    record = _read_json(run_path)
    inputs = record.get("stock_inputs") or record.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("Old BulkResearchFeed run must declare stock_inputs")
    required = {"price_view", "event_view", "reference_view", "listing_master", "native_view"}
    if not required <= set(inputs):
        raise ValueError(f"Old BulkResearchFeed inputs missing: {sorted(required - set(inputs))}")
    if record.get("execution_admitted") is True:
        raise ValueError("Old comparison source must remain research-only")
    return run_path, record, {key: str(Path(inputs[key]).resolve()) for key in required}


def _load_stockdb_feed(provider_view: Path):
    """Validate an unadmitted provider using the existing feature-path gate."""
    # Import lazily to keep pure frame diagnostics lightweight for unit tests.
    from .stockdb_factor_features import load_stockdb_feed
    return load_stockdb_feed(provider_view)


def _one_date_raw(feed, native_view: Path, candidates: pd.DataFrame, date: str) -> pd.DataFrame:
    if date not in feed.calendar:
        raise ValueError(f"Pinned overlap date is absent from source calendar: {date}")
    raw, _ = build_monthly_features(
        feed, {"bulk_inputs": {"native_view": str(Path(native_view).resolve())}},
        candidates, [pd.Timestamp(date)])
    expected = pd.MultiIndex.from_product(
        [[pd.Timestamp(date)], feed.symbols], names=["date", "symbol"])
    if not raw.index.equals(expected) or raw.columns.tolist() != candidates.factor.tolist():
        raise ValueError("Lineage feature grid or frozen factor identities changed")
    if raw.index.has_duplicates or raw.columns.duplicated().any():
        raise ValueError("Lineage features must have distinct index and columns")
    return raw


def _safe_corr(left: np.ndarray, right: np.ndarray, method: str) -> float:
    if len(left) < 2 or np.ptp(left) == 0 or np.ptp(right) == 0:
        return float("nan")
    result = pearsonr(left, right) if method == "pearson" else spearmanr(left, right)
    return float(result.statistic)


def compare_frames(old_raw: pd.DataFrame, new_raw: pd.DataFrame,
                   candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return per-factor raw/rank diagnostics plus common-symbol identity."""

    if old_raw.index.names != ["date", "symbol"] or new_raw.index.names != ["date", "symbol"]:
        raise ValueError("Feature indices must be date/symbol MultiIndexes")
    if old_raw.index.get_level_values("date").unique().tolist() != new_raw.index.get_level_values("date").unique().tolist():
        raise ValueError("Feature panels must use the same pinned date")
    common = sorted(set(old_raw.index.get_level_values("symbol"))
                    & set(new_raw.index.get_level_values("symbol")))
    if not common:
        raise ValueError("Lineage panels have no common symbols")
    date = old_raw.index.get_level_values("date")[0]
    common_index = pd.MultiIndex.from_product([[date], common], names=["date", "symbol"])
    old = old_raw.reindex(common_index)
    new = new_raw.reindex(common_index)
    old_rank = oriented_rank_features(old, candidates)
    new_rank = oriented_rank_features(new, candidates)
    rows = []
    for row in candidates.itertuples(index=False):
        name = row.factor
        left = pd.to_numeric(old[name], errors="coerce").to_numpy(dtype=float)
        right = pd.to_numeric(new[name], errors="coerce").to_numpy(dtype=float)
        pair = np.isfinite(left) & np.isfinite(right)
        l, r = left[pair], right[pair]
        lr = old_rank[name].to_numpy(dtype=float)[pair]
        rr = new_rank[name].to_numpy(dtype=float)[pair]
        old_finite = int(np.isfinite(left).sum())
        new_finite = int(np.isfinite(right).sum())
        item = {
            "factor": name,
            "library": row.library,
            "family": row.family,
            "old_finite": old_finite,
            "new_finite": new_finite,
            "common_symbols": len(common),
            "pair_finite": int(pair.sum()),
            "old_coverage": old_finite / len(common),
            "new_coverage": new_finite / len(common),
            "pair_coverage": int(pair.sum()) / len(common),
            "raw_pearson": _safe_corr(l, r, "pearson") if len(l) else float("nan"),
            "raw_spearman": _safe_corr(l, r, "spearman") if len(l) else float("nan"),
            "raw_old_mean": float(np.mean(l)) if len(l) else float("nan"),
            "raw_new_mean": float(np.mean(r)) if len(r) else float("nan"),
            "raw_old_std": float(np.std(l, ddof=1)) if len(l) > 1 else float("nan"),
            "raw_new_std": float(np.std(r, ddof=1)) if len(r) > 1 else float("nan"),
            "raw_old_median": float(np.median(l)) if len(l) else float("nan"),
            "raw_new_median": float(np.median(r)) if len(r) else float("nan"),
            "raw_mean_delta": float(np.mean(r) - np.mean(l)) if len(l) else float("nan"),
            "raw_median_delta": float(np.median(r) - np.median(l)) if len(l) else float("nan"),
            "raw_mae": float(np.mean(np.abs(r - l))) if len(l) else float("nan"),
            "raw_q05_delta": float(np.quantile(r, .05) - np.quantile(l, .05)) if len(l) else float("nan"),
            "raw_q95_delta": float(np.quantile(r, .95) - np.quantile(l, .95)) if len(l) else float("nan"),
            "raw_ks_stat": float(ks_2samp(l, r).statistic) if len(l) else float("nan"),
            "rank_pearson": _safe_corr(lr, rr, "pearson") if len(lr) else float("nan"),
            "rank_spearman": _safe_corr(lr, rr, "spearman") if len(lr) else float("nan"),
            "rank_mae": float(np.mean(np.abs(rr - lr))) if len(lr) else float("nan"),
            "rank_median_abs_delta": float(np.median(np.abs(rr - lr))) if len(lr) else float("nan"),
        }
        if len(l):
            item.update({
                "rank_old_median": float(np.median(lr)),
                "rank_new_median": float(np.median(rr)),
            })
        else:
            item.update({"rank_old_median": float("nan"), "rank_new_median": float("nan")})
        rows.append(item)
    metrics = pd.DataFrame(rows)
    symbols = pd.DataFrame({"symbol": common, "in_old": True, "in_new": True})
    return metrics, symbols, pd.concat({"old": old_rank, "new": new_rank}, names=["lineage"])


def run(old_run: Path, provider_view: Path, native_view: Path, candidate_file: Path,
        date: str, *, output: Path | None = None) -> tuple[Path, dict]:
    date = pd.Timestamp(date).strftime("%Y-%m-%d")
    old_path, old_record, old_inputs = _resolve_old_run(old_run)
    candidates = resolve_candidates(candidate_file)
    old_feed = BulkResearchFeed(old_inputs["price_view"], old_inputs["event_view"],
                                old_inputs["reference_view"], old_inputs["listing_master"])
    new_feed = _load_stockdb_feed(Path(provider_view).resolve())
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_factor_lineage_compare" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_factor_lineage_compare" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "running_stockdb_factor_lineage_compare",
        "date": date,
        "old_run": str(old_path),
        "old_inputs": old_inputs,
        "provider_view": str(Path(provider_view).resolve()),
        "native_view": str(Path(native_view).resolve()),
        "candidate_file": str(Path(candidate_file).resolve()),
        "candidate_count": len(candidates),
        "artifacts": str(artifacts),
        "research_only": True,
        "execution_admitted": False,
        "model_trained": False,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "result": None,
    }
    (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        old_raw = _one_date_raw(old_feed, Path(old_inputs["native_view"]), candidates, date)
        new_raw = _one_date_raw(new_feed, Path(native_view), candidates, date)
        metrics, symbols, ranks = compare_frames(old_raw, new_raw, candidates)
        old_raw.to_parquet(artifacts / "old_bulk_raw_features.parquet")
        new_raw.to_parquet(artifacts / "new_stockdb_raw_features.parquet")
        ranks.loc["old"].to_parquet(artifacts / "old_bulk_oriented_rank_features.parquet")
        ranks.loc["new"].to_parquet(artifacts / "new_stockdb_oriented_rank_features.parquet")
        metrics.to_csv(artifacts / "factor_metrics.csv", index=False)
        symbols.to_csv(artifacts / "common_symbols.csv", index=False)
        result = {
            "status": "completed_stockdb_factor_lineage_compare",
            "date": date,
            "old_symbols": len(old_feed.symbols),
            "new_symbols": len(new_feed.symbols),
            "common_symbols": len(symbols),
            "factors": len(metrics),
            "old_finite_cells": int(old_raw.notna().sum().sum()),
            "new_finite_cells": int(new_raw.notna().sum().sum()),
            "pair_finite_cells": int(metrics.pair_finite.sum()),
            "metrics_path": str(artifacts / "factor_metrics.csv"),
            "common_symbols_path": str(artifacts / "common_symbols.csv"),
            "old_raw_path": str(artifacts / "old_bulk_raw_features.parquet"),
            "new_raw_path": str(artifacts / "new_stockdb_raw_features.parquet"),
        }
        record.update(status=result["status"], result=result)
        report = [
            "# BulkResearchFeed vs StockDB 190-factor lineage comparison", "",
            "Research-only overlap diagnostic; no model fit, ranking, target holding, paper order or Tonghuashun package was generated.",
            f"- Pinned date: `{date}`", f"- Old BulkResearchFeed symbols: `{len(old_feed.symbols)}`; StockDB symbols: `{len(new_feed.symbols)}`; common: `{len(symbols)}`",
            f"- Factors compared: `{len(metrics)}`; old finite cells: `{int(old_raw.notna().sum().sum()):,}`; new finite cells: `{int(new_raw.notna().sum().sum()):,}`",
            f"- Per-factor diagnostics: `{artifacts / 'factor_metrics.csv'}`",
            "- Raw metrics use the fixed factor formulas; oriented-rank metrics apply the frozen train direction independently within the common cross-section.",
            "- StockDB values remain a physical `pct_chg` cumulative-return research proxy. Coverage, action semantics, PIT and eligibility are not reconciled by this comparison.",
            "- `execution_admitted=false`, `model_trained=false`, `ranking_generated=false`, `target_holdings_generated=false`, `paper_orders_generated=false`.",
        ]
        (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed_stockdb_factor_lineage_compare",
                      error=f"{type(error).__name__}: {error}")
        raise
    finally:
        old_feed.close()
        record["research_only"] = True
        (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "run.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-run", required=True, type=Path,
                        help="Completed factor replay run.json containing BulkResearchFeed inputs")
    parser.add_argument("--provider-view", required=True, type=Path,
                        help="Completed unadmitted StockDB Qlib provider view")
    parser.add_argument("--native-view", required=True, type=Path,
                        help="Completed unadmitted native Alpha158 view for the StockDB provider")
    parser.add_argument("--candidate-file", required=True, type=Path,
                        help="Frozen 190-factor candidate_factors.csv")
    parser.add_argument("--date", required=True, help="One pinned overlap session, e.g. 2026-09-30")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, record = run(args.old_run, args.provider_view, args.native_view,
                       args.candidate_file, args.date, output=args.out)
    print(json.dumps({"run": str(path), **record}, ensure_ascii=False))
    return 0 if record["status"] == "completed_stockdb_factor_lineage_compare" else 1


if __name__ == "__main__":
    raise SystemExit(main())
