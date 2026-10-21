"""Diagnose report-period mixing in saved profitability ranks, without reranking."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import ROOT
from .experiment import write_json
from .profitability_portfolios import ID


def period_counts(pool, ranks):
    required = {"date", "symbol", "source_stat_date", "source_pub_date"}
    if not required <= set(pool) or not {"date", "symbol", "rank"} <= set(ranks):
        raise ValueError("Saved financial periods and candidate ranks required")
    if pool.duplicated(["date", "symbol"]).any() or ranks.duplicated(["date", "symbol"]).any():
        raise ValueError("Distinct same-day securities required")
    if (pool[list(required)].isna().any().any() or pool[list(required)].eq("").any().any()
            or not pool.source_pub_date.lt(pool.date).all()
            or not pool.source_stat_date.le(pool.source_pub_date).all()):
        raise ValueError("Observed report chronology required; missing periods stay unknown")
    if ranks.duplicated(["date", "rank"]).any() or set(ranks.date) != set(pool.date):
        raise ValueError("Distinct saved ranks and identical decision dates required")
    matched = ranks[["date", "symbol", "rank"]].merge(pool, on=["date", "symbol"], validate="one_to_one")
    if len(matched) != len(ranks):
        raise ValueError("Every ranked candidate must belong to the frozen common pool")
    rows = []
    for day, frame in pool.groupby("date", sort=True):
        head = matched.loc[matched.date.eq(day) & matched["rank"].le(20)]
        if sorted(head["rank"].tolist()) != list(range(1, 21)):
            raise ValueError("Exactly the original first20 candidates required, not holdings")
        for period, count in frame.source_stat_date.value_counts().sort_index().items():
            candidate_count = int(head.source_stat_date.eq(period).sum())
            pool_share, head_share = int(count) / len(frame), candidate_count / len(head)
            rows.append({"date": day, "source_stat_date": period, "pool_count": int(count),
                         "pool_total": len(frame), "pool_share": pool_share,
                         "head20_count": candidate_count, "head20_share": head_share,
                         "head20_minus_pool_share": head_share - pool_share})
    return pd.DataFrame(rows)


def run(identifier):
    source = ROOT / "outputs/market_replay" / identifier / "run.json"
    record = json.loads(source.read_text(encoding="utf-8"))
    if record.get("research_family") != ID or record.get("execution_admitted") is not False:
        raise ValueError("Saved unadmitted fixed profitability experiment required")
    rankings = Path(record["rankings"])
    pool = pd.read_parquet(rankings.parent / "monthly_profitability_pool.parquet")
    candidates = pd.read_csv(rankings / "profitability_only/rankings.csv", dtype={"date": str, "symbol": str})
    counts = period_counts(pool, candidates)
    if (len(pool) != record["pool_summary"]["common_rows"]
            or pool.date.nunique() != record["pool_summary"]["decision_months"]):
        raise ValueError("Original fixed pool counts changed")
    folder = ROOT / "outputs/profitability_periods" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    counts.to_csv(folder / "period_counts.csv", index=False)
    periods = counts.groupby("date").size()
    result = {"status": "completed_saved_profitability_period_diagnostic", "source_run": str(source),
              "pool_rows": len(pool), "distinct_symbols": int(pool.symbol.nunique()),
              "decision_months": len(periods), "mixed_period_months": int(periods.gt(1).sum()),
              "max_periods_per_month": int(periods.max()),
              "max_absolute_head20_share_difference": float(counts.head20_minus_pool_share.abs().max()),
              "rankings_changed": False, "account_replayed": False, "execution_admitted": False,
              "limits": ["First20 ranked candidates are not actual target holdings; rank30 retention remains in the original ledger",
                         "Different reporting periods are observed provenance, not proof of biased scores or return causation",
                         "No annualization, cumulative-to-single-quarter conversion or financial restatement substitution",
                         "Provider definitions and revision-safe PIT remain unverified; no historical reranking or admission"]}
    write_json(folder / "result.json", result)
    lines = ["# Saved Profitability Report-Period Diagnostic", "", "NO_TRADE. Frozen pool/ranks, no account rerun.",
             f"{len(periods)} months; {result['mixed_period_months']} mix report periods; {len(pool)} observations.", "",
             "| Decision | Report Period End | Pool N | Pool Share | First20 N | First20 Share | Difference pp |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    lines += [f"| {r.date} | {r.source_stat_date} | {r.pool_count} | {r.pool_share:.2%} | {r.head20_count} | {r.head20_share:.2%} | {100*r.head20_minus_pool_share:.2f} |"
              for r in counts.itertuples(index=False)]
    lines += ["", "## Boundaries", ""] + ["- " + item for item in result["limits"]]
    (folder / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    folder, result = run(args.run)
    print(json.dumps({"folder": str(folder), **result}, ensure_ascii=False))
    return 0
