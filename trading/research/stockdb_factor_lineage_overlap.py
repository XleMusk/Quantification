"""Compare fixed factor ranks across the old and StockDB provider lineages."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd
from scipy.stats import spearmanr

from ..settings import DATA_ROOT, ROOT


def _read_frame(path: Path, date: str) -> pd.DataFrame:
    frame = pd.read_parquet(Path(path).resolve())
    if frame.index.names != ["date", "symbol"] or frame.index.has_duplicates:
        raise ValueError("Feature panel must use a distinct date/symbol index")
    day = pd.Timestamp(date)
    if day not in frame.index.get_level_values("date"):
        raise ValueError(f"Feature panel does not contain overlap date: {date}")
    return frame.xs(day, level="date")


def compare_feature_panels(old_path: Path, new_path: Path, candidate_file: Path,
                           date: str) -> tuple[pd.DataFrame, dict]:
    candidates = pd.read_csv(Path(candidate_file).resolve())
    required = {"factor", "library", "train_direction"}
    if not required <= set(candidates.columns) or candidates.factor.duplicated().any():
        raise ValueError("Candidate factor identity is missing or duplicated")
    old = _read_frame(old_path, date)
    new = _read_frame(new_path, date)
    factors = candidates.factor.tolist()
    if set(factors) != set(old.columns) or set(factors) != set(new.columns):
        raise ValueError("Old/new feature panels do not match the candidate factor set")
    common = sorted(set(old.index) & set(new.index))
    old, new = old.reindex(common), new.reindex(common)
    rows = []
    for row in candidates.itertuples(index=False):
        a, b = pd.to_numeric(old[row.factor], errors="coerce"), pd.to_numeric(new[row.factor], errors="coerce")
        pair = a.notna() & b.notna()
        n = int(pair.sum())
        corr = (float(spearmanr(a[pair], b[pair]).statistic)
                if n >= 3 and a[pair].nunique() > 1 and b[pair].nunique() > 1 else None)
        sign = -1 if row.train_direction == "low" else 1
        aa, bb = a[pair] * sign, b[pair] * sign
        k = min(30, n)
        overlap = (len(set(aa.nlargest(k, keep="all").index) & set(bb.nlargest(k, keep="all").index)) / k
                   if k else None)
        rows.append({"factor": row.factor, "library": row.library, "direction": row.train_direction,
                     "common_symbols": len(common), "old_finite": int(a.notna().sum()),
                     "new_finite": int(b.notna().sum()), "paired_finite": n,
                     "spearman_rank_corr": corr, "top30_overlap": overlap})
    summary = pd.DataFrame(rows)
    valid, overlaps = summary.spearman_rank_corr.dropna(), summary.top30_overlap.dropna()
    record = {
        "status": "completed_factor_lineage_overlap_diagnostic",
        "old_feature_path": str(Path(old_path).resolve()),
        "new_feature_path": str(Path(new_path).resolve()),
        "candidate_file": str(Path(candidate_file).resolve()),
        "date": date,
        "old_symbols": int(old.shape[0]),
        "new_symbols": int(new.shape[0]),
        "common_symbols": len(common),
        "factors": len(summary),
        "paired_finite_total": int(summary.paired_finite.sum()),
        "median_spearman_rank_corr": float(valid.median()) if not valid.empty else None,
        "mean_spearman_rank_corr": float(valid.mean()) if not valid.empty else None,
        "median_top30_overlap": float(overlaps.median()) if not overlaps.empty else None,
        "finite_pair_ge_0_9": int(valid.ge(.9).sum()),
        "finite_pair_lt_0_5": int(valid.lt(.5).sum()),
        "execution_admitted": False,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "limitations": [
            "Old and new feature values use different provider/action contracts; correlation is diagnostic, not validation.",
            "The comparison may use an already inspected overlap, not an untouched holdout.",
            "No dated eligibility, model refit, ranking, target holdings, Ledger or THS artifact was generated.",
        ],
    }
    return summary, record


def run(old_feature: Path, new_feature: Path, candidate_file: Path, date: str,
        output: Path | None = None) -> tuple[Path, dict]:
    summary, record = compare_feature_panels(old_feature, new_feature, candidate_file, date)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "factor_lineage_overlap" / identifier
    artifacts = DATA_ROOT / "research" / "factor_lineage_overlap" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    summary.to_csv(out / "factor_overlap.csv", index=False)
    record["summary_path"] = str((out / "factor_overlap.csv").resolve())
    record["artifacts"] = str(artifacts)
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# Fixed 190-factor lineage overlap diagnostic", "",
        f"Date: `{date}`; common symbols: `{record['common_symbols']}`; factors: `{record['factors']}`.",
        f"Median Spearman rank correlation: `{record['median_spearman_rank_corr']:.4f}`; median top-30 overlap: `{record['median_top30_overlap']:.2%}`.",
        f"Factors with rank correlation >=0.9: `{record['finite_pair_ge_0_9']}`; <0.5: `{record['finite_pair_lt_0_5']}`.",
        "", "This is a research-only contract comparison. It does not validate the new provider for current scoring or trading.",
    ]) + "\n", encoding="utf-8")
    return out / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-feature", required=True, type=Path)
    parser.add_argument("--new-feature", required=True, type=Path)
    parser.add_argument("--candidate-file", required=True, type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, record = run(args.old_feature, args.new_feature, args.candidate_file, args.date, args.out)
    print(json.dumps({"run": str(path), **record}, ensure_ascii=False))
    return 0 if record["status"] == "completed_factor_lineage_overlap_diagnostic" else 1


if __name__ == "__main__":
    raise SystemExit(main())
