"""Read all fixed saved models for head overlap and score-boundary diagnostics."""
import argparse
from datetime import datetime
from itertools import combinations
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .alpha158_linear import SEEDS, SIGNALS, saved_score
from .experiment import write_json


HEAD_SIZES = (5, 10, 20)
CASES = tuple(f"alpha158_seed{seed}" for seed in SEEDS) + SIGNALS
ID = "saved_alpha158_fixed_head_stability_v1"


def overlap(left, right):
    if len(left) != len(right) or not left:
        raise ValueError("Equal nonempty heads required")
    shared = sorted(left & right)
    return {"intersection": len(shared), "shared_fraction": len(shared) / len(left),
            "jaccard": len(shared) / len(left | right), "intersection_symbols": shared}


def extract_heads(score, signal):
    heads, members, boundaries = {}, [], []
    for day, part in score.groupby(level="date", sort=True):
        values = part.droplevel("date")
        if values.index.has_duplicates or len(values) <= max(HEAD_SIZES) or not np.isfinite(values).all():
            raise ValueError("Distinct finite score pool larger than fixed heads required")
        ranking = values.rename("score").reset_index().sort_values(["score", "symbol"], ascending=[False, True])
        spread = float(values.quantile(.75) - values.quantile(.25))
        heads[day] = {n: set(ranking.symbol.iloc[:n]) for n in HEAD_SIZES}
        members.extend({"signal": signal, "date": day, "rank": i + 1, "symbol": row.symbol, "score": row.score}
                       for i, row in enumerate(ranking.head(max(HEAD_SIZES)).itertuples(index=False)))
        for n in HEAD_SIZES:
            last, next_ = ranking.iloc[n - 1], ranking.iloc[n]
            gap = float(last.score - next_.score)
            boundaries.append({"signal": signal, "date": day, "head_size": n, "pool_rows": len(values),
                               "last_in_symbol": last.symbol, "first_out_symbol": next_.symbol,
                               "last_in_score": last.score, "first_out_score": next_.score,
                               "score_gap": gap, "score_iqr": spread,
                               "score_gap_over_iqr": gap / spread if spread > 0 else None,
                               "pool_ties_at_boundary_score": int(values.eq(last.score).sum()),
                               "boundary_tied": gap == 0})
    return heads, pd.DataFrame(members), pd.DataFrame(boundaries)


def pairwise_heads(heads, dates, cadence):
    rows = []
    for day in dates:
        for first, second in combinations(heads, 2):
            for n in HEAD_SIZES:
                rows.append({"date": day, "cadence": cadence, "signal_a": first, "signal_b": second,
                             "head_size": n, "seed_pair": first.startswith("alpha158_seed") and second.startswith("alpha158_seed"),
                             **overlap(heads[first][day][n], heads[second][day][n])})
    return rows


def temporal_heads(heads, dates, cadence):
    rows = []
    for previous, day in zip(dates, dates[1:]):
        for signal, values in heads.items():
            for n in HEAD_SIZES:
                rows.append({"previous_date": previous, "date": day, "cadence": cadence, "signal": signal,
                             "head_size": n, **overlap(values[previous][n], values[day][n])})
    return rows


def report(out, record):
    lines = ["# Fixed Saved Model Head Stability", "", "Research-only; NO_TRADE.", "",
             f"All five fixed models; {record['prediction_rows_per_model']:,} identical date/security rows each, "
             f"{record['signal_dates']} dates and {record['rebalance_dates']} original rebalance decisions.",
             "Heads5/10/20 were fixed before this diagnostic. No labels, returns, training, new account or best-seed/head search.",
             "Final unexecuted close decision remains included in membership diagnostics and explicitly marked; not a tradable order.", "",
             "| Seed Pair | Head | Cadence | Dates | Mean Shared Fraction | Mean Jaccard | Zero-Overlap Fraction |",
             "| --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    pairs = pd.DataFrame(record["pairwise_summary"])
    for row in pairs.loc[pairs.seed_pair & pairs.cadence.eq("rebalance")].itertuples(index=False):
        lines.append(f"| {row.signal_a}/{row.signal_b} | {row.head_size} | {row.cadence} | {row.dates} | "
                     f"{row.mean_shared_fraction:.2%} | {row.mean_jaccard:.2%} | {row.zero_overlap_fraction:.2%} |")
    lines += ["", "## Within-Model Rebalance Membership Retention", "",
              "Head membership retention is NOT actual target retention, turnover or broker fills.",
              "| Model | Head | Transitions | Mean Retained Fraction | Zero-Overlap Fraction |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for row in record["temporal_summary"]:
        if row["cadence"] == "rebalance":
            lines.append(f"| {row['signal']} | {row['head_size']} | {row['transitions']} | "
                         f"{row['mean_shared_fraction']:.2%} | {row['zero_overlap_fraction']:.2%} |")
    lines += ["", "## Fifth / Sixth Score Boundary", "",
              "Gap/IQR is within each model's own score distribution, not calibrated expected returns or CNY.",
              "| Model | Rebalance Dates | Median Gap/IQR | Tied Boundary Dates |",
              "| --- | ---: | ---: | ---: |"]
    for row in record["boundary_summary"]:
        if row["cadence"] == "rebalance" and row["head_size"] == 5:
            gap = row["median_score_gap_over_iqr"]
            display = f"{gap:.6f}" if gap is not None else "unknown"
            lines.append(f"| {row['signal']} | {row['dates']} | {display} | {row['tied_boundary_dates']} |")
    lines += ["", "## Boundaries", "",
              "- Ranking uses original finite predictions, descending score then ascending symbol, as in the existing funnel. "
              "Ties do not silently expand the head or pick by future outcomes.",
              "- All models use exactly the same dated pool. Daily and five-session rebalance transitions are reported separately.",
              "- All ten model pairs are saved. Only three LightGBM seed pairs are described as seed sensitivity; Ridge differs in method, "
              "and the ensemble is constructed from those seeds, not an independent replication.",
              "- Raw or IQR-normalized score gaps are not model uncertainty estimates, calibrated economic advantages or confidence thresholds.",
              "- No top10/top20 portfolio backtest or performance claim. No seed/head selected or default strategy changed.",
              "- Inspected2026, retrieval-time universe and non-PIT input limitations remain. No independent validation or execution admission.",
              f"- Detailed per-date memberships, overlaps, boundary gaps and receipt: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(funnel_run):
    path = (ROOT / "outputs/prediction_funnel" / funnel_run / "run.json").resolve()
    path.relative_to((ROOT / "outputs/prediction_funnel").resolve())
    source = json.loads(path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed_saved_prediction_funnel" or source.get("models_refit") is not False
            or source.get("accounts_replayed") is not False or source.get("execution_admitted") is not False
            or [case["signal"] for case in source["cases"]] != list(CASES)):
        raise ValueError("Completed all-five-model saved funnel required")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/head_stability" / identifier, DATA_ROOT / "research/head_stability" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    record = {"id": identifier, "purpose": ID, "status": "running", "source_run": str(path),
              "artifacts": str(artifacts), "head_sizes": list(HEAD_SIZES), "cases": list(CASES),
              "models_refit": False, "accounts_replayed": False, "parameters_searched": False,
              "future_labels_consumed": False, "fresh_holdout_claimed": False, "execution_admitted": False}
    write_json(out / "run.json", record)
    try:
        master_index, master_anchors, heads, boundaries = None, None, {}, []
        for case in source["cases"]:
            signal = case["signal"]
            score = saved_score(case["source_predictions"])
            if master_index is not None and not score.index.equals(master_index):
                raise ValueError("All five original prediction pools must be identical")
            master_index = score.index
            targets = pd.read_csv(Path(source["artifacts"]) / signal / "actual_targets.csv",
                                  usecols=["date", "has_execution_session_in_sample"])
            if not targets.has_execution_session_in_sample.isin([True, False]).all():
                raise ValueError("Original execution availability must remain explicit")
            if targets.groupby("date").has_execution_session_in_sample.nunique().ne(1).any():
                raise ValueError("Target execution availability disagrees within a decision date")
            anchors = targets.drop_duplicates("date").set_index("date").sort_index().has_execution_session_in_sample
            if master_anchors is not None and not anchors.equals(master_anchors):
                raise ValueError("All five models must share the original rebalance decision dates")
            master_anchors = anchors
            values, members, gaps = extract_heads(score, signal)
            heads[signal] = values
            members.to_csv(artifacts / (signal + "_head20_members.csv"), index=False)
            boundaries.append(gaps)
        dates = list(heads[CASES[0]])
        anchors = list(pd.to_datetime(master_anchors.index))
        if anchors != dates[::source["portfolio"]["rebalance_sessions"]] or len(dates) != source["cases"][0]["sessions"]:
            raise ValueError("Original dated cadence/calendar changed")
        pairs = pd.DataFrame(pairwise_heads(heads, dates, "daily") + pairwise_heads(heads, anchors, "rebalance"))
        temporal = pd.DataFrame(temporal_heads(heads, dates, "daily") + temporal_heads(heads, anchors, "rebalance"))
        boundary = pd.concat(boundaries, ignore_index=True)
        boundary["rebalance_decision"] = boundary.date.isin(anchors)
        boundary["has_execution_session_in_sample"] = boundary.date.map({
            pd.Timestamp(day): value for day, value in master_anchors.items()})
        pairs.to_csv(artifacts / "pairwise_head_overlap.csv", index=False)
        temporal.to_csv(artifacts / "temporal_head_retention.csv", index=False)
        boundary.to_csv(artifacts / "score_boundaries.csv", index=False)
        record["pairwise_summary"] = [{"signal_a": first, "signal_b": second, "head_size": int(n),
            "cadence": cadence, "seed_pair": bool(part.seed_pair.iloc[0]), "dates": len(part),
            "mean_shared_fraction": float(part.shared_fraction.mean()), "mean_jaccard": float(part.jaccard.mean()),
            "zero_overlap_fraction": float(part.intersection.eq(0).mean())}
            for (first, second, n, cadence), part in pairs.groupby(["signal_a", "signal_b", "head_size", "cadence"])]
        record["temporal_summary"] = [{"signal": signal, "head_size": int(n), "cadence": cadence,
            "transitions": len(part), "mean_shared_fraction": float(part.shared_fraction.mean()),
            "zero_overlap_fraction": float(part.intersection.eq(0).mean())}
            for (signal, n, cadence), part in temporal.groupby(["signal", "head_size", "cadence"])]
        record["boundary_summary"] = []
        for cadence, frame in (("daily", boundary), ("rebalance", boundary.loc[boundary.rebalance_decision])):
            for (signal, n), part in frame.groupby(["signal", "head_size"]):
                value = part.score_gap_over_iqr.median()
                record["boundary_summary"].append({"signal": signal, "head_size": int(n), "cadence": cadence,
                    "dates": len(part), "median_score_gap_over_iqr": float(value) if pd.notna(value) else None,
                    "unknown_normalized_gap_dates": int(part.score_gap_over_iqr.isna().sum()),
                    "tied_boundary_dates": int(part.boundary_tied.sum())})
        record.update(status="completed_saved_head_stability", prediction_rows_per_model=len(master_index),
                      signal_dates=len(dates), rebalance_dates=len(anchors),
                      unexecuted_rebalance_dates=master_anchors.index[~master_anchors].tolist())
        report(out, record)
    except Exception as error:
        record.update(status="failed_saved_head_stability", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(out / "run.json", record)
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--funnel-run", required=True)
    args = parser.parse_args(argv)
    run(args.funnel_run)
    return 0
