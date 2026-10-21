"""Fixed size-bin incremental diagnostics on saved2023 scores and labels."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .factor_families import MIN_PAIRS, rank_group_means
from .factors import daily_ic
from .stockdb_factor_diagnostics import BLEND, DIRECTIONS, SCOPES, scoped_labels, summarize


BINS, HORIZON = 10, 20
SIZE = "log_float_mv_candidate"
CONDITIONED = "value_activity_within_size_equal_rank_v1"


def conditioned_scores(signals):
    if signals.index.has_duplicates or signals.index.names != ["date", "symbol"]:
        raise ValueError("Unique decision-day security scores required")
    common = signals.loc[np.isfinite(signals[list(DIRECTIONS) + [BLEND]]).all(axis=1)].copy()
    # Size bins and every signal rank are decided before joining any outcomes.
    size_rank = common[SIZE].groupby(level="date").rank(pct=True, method="average")
    bins = ((size_rank * BINS).apply(np.ceil) - 1).clip(0, BINS - 1).astype(int).rename("size_bin")
    groups = [common.index.get_level_values("date"), bins]
    ranked = common[list(DIRECTIONS)].groupby(groups).rank(pct=True, method="average")
    common[CONDITIONED] = (ranked[["book_to_price_candidate", "earnings_to_price_candidate"]].mean(axis=1)
                           + ranked["turnover_pct_candidate"]) / 2
    return common[[BLEND, SIZE, CONDITIONED]], bins


def run(source_run):
    source_run = Path(source_run).resolve()
    parent = json.loads((source_run / "result.json").read_text(encoding="utf-8"))
    if parent["status"] != "completed_research_diagnostics":
        raise ValueError("Completed saved factor diagnostic source required")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/stockdb_size_diagnostics" / identifier
    out = ROOT / "outputs/stockdb_size_diagnostics" / identifier
    root.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True, exist_ok=False)
    plan = {"id": identifier, "source_run": str(source_run), "size_bins": BINS, "horizon": HORIZON,
            "cases": [BLEND, SIZE, CONDITIONED], "scopes": SCOPES,
            "conditional_formula": "(mean(within-bin rank(+B/P),within-bin rank(+E/P)) + within-bin rank(-turnover))/2",
            "binning": "date-only average size ranks, ten equal percentile intervals; ties stay together",
            "comparison": "equal bins/equal dates, only dates with all ten bins and all three IC/group results; retain every diagnostic row",
            "min_pairs_per_bin": MIN_PAIRS, "source_outcomes_already_inspected": True,
            "parameters_searched": False, "fresh_holdout_claimed": False, "execution_admitted": False}
    write_json(root / "plan.json", plan)
    shutil.copy2(__file__, root / "implementation.py")
    result = {"id": identifier, "status": "running", "artifacts": str(root), "plan": plan, "execution_admitted": False}
    try:
        signals = pd.read_parquet(source_run / "fixed_signals.parquet")
        scores, bins = conditioned_scores(signals)
        columns = ["date", "symbol", "decision_eligible", "source_is_dated_code", f"label_h{HORIZON}", f"exit_h{HORIZON}"]
        parts = [pd.read_parquet(source_run / "labeled_inputs" / f"2023-{month:02d}.parquet", columns=columns)
                 for month in range(1, 13)]
        labeled = pd.concat(parts, ignore_index=True)
        labeled["date"] = pd.to_datetime(labeled.date)
        labeled = labeled.loc[labeled.decision_eligible & labeled.source_is_dated_code].set_index(["date", "symbol"])
        if labeled.index.has_duplicates or not scores.index.isin(labeled.index).all():
            raise ValueError("Saved common-pool label identities changed")
        label = labeled[f"label_h{HORIZON}"].reindex(scores.index)
        exits = pd.to_datetime(labeled[f"exit_h{HORIZON}"].reindex(scores.index))
        scores.assign(size_bin=bins).to_parquet(root / "conditioned_scores.parquet")
        daily_parts = []
        for scope, (start, end) in SCOPES.items():
            days = scores.index.get_level_values("date")
            scoped = scores.loc[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))]
            scoped_label = scoped_labels(label.reindex(scoped.index), exits.reindex(scoped.index), start, end)
            for size_bin in range(BINS):
                selected = scoped.loc[bins.reindex(scoped.index).eq(size_bin)]
                if selected.empty:
                    continue
                labels = scoped_label.reindex(selected.index)
                ic = daily_ic(selected, labels, min_pairs=MIN_PAIRS).reset_index().melt(
                    id_vars="date", var_name="factor", value_name="rank_ic")
                groups = rank_group_means(selected, labels, min_pairs=MIN_PAIRS)
                part = groups.merge(ic, on=["date", "factor"], validate="one_to_one")
                part["scope"], part["size_bin"], part["horizon"] = scope, size_bin, HORIZON
                daily_parts.append(part)
            print(f"{scope}: fixed ten-bin diagnostics, no label-selected ranking", flush=True)
        daily = pd.concat(daily_parts, ignore_index=True)
        coverage = daily.groupby(["scope", "date"]).agg(
            rows=("factor", "size"), bins=("size_bin", "nunique"),
            known_ic=("rank_ic", "count"), known_group=("q5_excess", "count"))
        coverage["comparable"] = (coverage.rows.eq(3 * BINS) & coverage.bins.eq(BINS)
                                  & coverage.known_ic.eq(3 * BINS) & coverage.known_group.eq(3 * BINS))
        daily = daily.merge(coverage[["comparable"]], left_on=["scope", "date"], right_index=True, validate="many_to_one")
        daily.to_parquet(root / "bin_daily_diagnostics.parquet", index=False)
        coverage.reset_index().to_csv(root / "coverage.csv", index=False)
        shared = daily.loc[daily.comparable].groupby(["scope", "date", "factor", "horizon"]).agg(
            rank_ic=("rank_ic", "mean"), q5_excess=("q5_excess", "mean"), q1_excess=("q1_excess", "mean"),
            signal_rows=("signal_rows", "sum"), pairs=("pairs", "sum"), missing_labels=("missing_labels", "sum")).reset_index()
        shared["q5_minus_q1"] = shared.q5_excess - shared.q1_excess
        shared.to_parquet(root / "matched_daily_diagnostics.parquet", index=False)
        summary = summarize(shared)
        summary.to_csv(root / "summary.csv", index=False)
        result.update(status="completed_size_matched_diagnostics", common_rows=len(scores),
                      comparable_days=coverage.groupby(level="scope").comparable.sum().to_dict(),
                      summaries=summary.to_dict("records"))
        lines = ["# 2023 Size-Matched Incremental Diagnostics", "", "NO_TRADE; already inspected retrospective development, not net-account alpha.",
                 f"Source: {source_run}", f"Artifacts: {root}", "",
                 "Ten decision-time size bins; same finite four inputs; ties retained; fixed20-session next-open labels.",
                 "Every day in the headline uses all ten bins and all three cases. Scores never depend on future outcome availability.",
                 "Bin IC and quintile excess are equally averaged by bin, then by date; missing-bin dates remain in coverage.", "",
                 "| Scope | Signal | Common days | Within-bin rank IC | Within-bin Q5 excess |",
                 "| --- | --- | ---: | ---: | ---: |"]
        for row in summary.itertuples():
            lines.append(f"| {row.scope} | {row.factor} | {row.ic_days} | {row.mean_rank_ic:.6f} | {row.mean_q5_excess:.6f} |")
        lines += ["", "- This coarse size conditioning is not exact size/industry/risk neutrality or a causal attribution.",
                  "- Original blend retains within-bin size variation. The value/activity diagnostic drops the explicit size family.",
                  "- Labels use saved reconstructed adjusted opens, not cash/share accounting, fees, fills or executable returns.",
                  "- Valuation revisions, listing/membership, missing-outcome bias and overlapping-horizon uncertainty remain.",
                  "- No parameter search, model fit, event repair, account mutation, current recommendation or THS operation."]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as error:
        result.update(status="failed_size_matched_diagnostics", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(root / "result.json", result)
    print(str(out / "REPORT.md"), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    args = parser.parse_args(argv)
    run(args.source_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
