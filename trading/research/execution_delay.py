"""Fixed Alpha158 execution-delay diagnostics; no account returns or selection."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .bulk_model_inputs import load_inputs, native_binding
from .experiment import write_json
from .factor_families import HORIZONS, MIN_DAYS, MIN_PAIRS, hac_mean_t, read_open_panel
from .factors import catalog


DELAYS = (0, 1, 2)
ID = "alpha158_fixed_execution_delay_v1"


def delayed_outcomes(opening, offset, horizon, end_offset):
    """Keep exchange-calendar endpoints; never roll a missing quote forward."""
    if horizon not in HORIZONS or offset < 0 or offset >= len(opening):
        raise ValueError("Fixed horizon and valid signal session required")
    rows = []
    purged = offset + horizon + 1 + max(DELAYS) > end_offset
    for delay in DELAYS:
        entry, exit_ = offset + 1 + delay, offset + 1 + delay + horizon
        if purged:
            rows.append(np.full(opening.shape[1], np.nan))
            continue
        first, last = opening[entry], opening[exit_]
        known = np.isfinite(first) & np.isfinite(last) & (first > 0) & (last > 0)
        values = np.full(opening.shape[1], np.nan)
        np.divide(last, first, out=values, where=known)
        rows.append(values - 1)
    outcomes = np.column_stack(rows)
    return outcomes, np.isfinite(outcomes).all(axis=1), purged


def paired_daily(frame, outcomes, *, min_pairs=MIN_PAIRS):
    if outcomes.shape != (len(frame), len(DELAYS)) or np.isinf(outcomes).any():
        raise ValueError("Aligned finite-or-missing fixed-delay outcomes required")
    x = frame.to_numpy(dtype=float)
    finite = np.isfinite(x)
    common = np.isfinite(outcomes).all(axis=1)
    paired = finite & common[:, None]
    count = paired.sum(axis=0)
    ranks = frame.rank(pct=True).to_numpy()
    # Membership is today's feature rank; all three outcomes share the same finite pairs.
    rx = pd.DataFrame(np.where(paired, x, np.nan)).rank().to_numpy()
    rx -= np.nansum(rx, axis=0) / np.maximum(count, 1)
    x_variance = np.nansum(rx ** 2, axis=0)
    enough = (count >= min_pairs) & (frame.nunique().to_numpy() >= 5)
    parts = []
    for column, delay in enumerate(DELAYS):
        y = outcomes[:, column]
        ry = pd.DataFrame(np.where(paired, y[:, None], np.nan)).rank().to_numpy()
        ry -= np.nansum(ry, axis=0) / np.maximum(count, 1)
        denominator = np.sqrt(x_variance * np.nansum(ry ** 2, axis=0))
        ic = np.divide(np.nansum(rx * ry, axis=0), denominator,
                       out=np.full(len(frame.columns), np.nan),
                       where=(denominator > 0) & (count >= min_pairs))

        def mean(mask):
            total = mask.sum(axis=0)
            return np.divide(np.where(mask, y[:, None], 0).sum(axis=0), total,
                             out=np.full(len(frame.columns), np.nan), where=total > 0)

        pool = mean(paired)
        parts.append(pd.DataFrame({
            "factor": frame.columns, "delay_sessions": delay,
            "signal_rows": finite.sum(axis=0), "pairs": count,
            "individually_observed_pairs": (finite & np.isfinite(y[:, None])).sum(axis=0),
            "missing_common_labels": (finite & ~common[:, None]).sum(axis=0),
            "rank_ic": ic,
            "q1_excess": np.where(enough, mean(paired & (ranks <= .2)) - pool, np.nan),
            "q5_excess": np.where(enough, mean(paired & (ranks > .8)) - pool, np.nan),
        }))
    usable_ic = np.isfinite(np.column_stack([part.rank_ic for part in parts])).all(axis=1)
    for part in parts:
        part.loc[~usable_ic, "rank_ic"] = np.nan
    return pd.concat(parts, ignore_index=True)


def frozen_directions(original, definitions):
    training = original.loc[original.segment.eq("train"), ["factor", "horizon", "train_direction"]]
    expected = pd.MultiIndex.from_product([definitions.name, HORIZONS], names=["factor", "horizon"])
    training = training.set_index(["factor", "horizon"])
    if (training.index.has_duplicates or set(training.index) != set(expected)
            or not training.train_direction.isin(["high", "low", "unknown"]).all()):
        raise ValueError("One frozen original training direction per factor/horizon required")
    return training.train_direction.reindex(expected)


def summarize(daily, definitions, directions, *, min_days=MIN_DAYS):
    metadata = definitions.set_index("name")
    rows = []
    for (name, horizon, segment, delay), part in daily.groupby(
            ["factor", "horizon", "segment", "delay_sessions"], sort=True):
        orientation = directions.loc[(name, horizon)]
        direction = {"high": 1, "low": -1, "unknown": 0}[orientation]
        ic = part.rank_ic * direction if direction else part.rank_ic * np.nan
        excess = part["q5_excess" if direction > 0 else "q1_excess"] if direction else part.q1_excess * np.nan
        rows.append({
            "factor": name, "family": metadata.loc[name, "family"],
            "category": metadata.loc[name, "category"], "horizon": int(horizon),
            "segment": segment, "delay_sessions": int(delay), "train_direction": orientation,
            "ic_days": int(ic.count()), "oriented_mean_rank_ic": ic.mean(),
            "oriented_top_quintile_excess": excess.mean(),
            "hac_t_horizon": hac_mean_t(ic, int(horizon)),
            "pairs": int(part.pairs.sum()),
            "individually_observed_pairs": int(part.individually_observed_pairs.sum()),
            "missing_common_labels": int(part.missing_common_labels.sum()),
            "diagnostic_positive": bool(direction and ic.count() >= min_days and ic.mean() > 0 and excess.mean() > 0),
            "account_ready": False,
        })
    result = pd.DataFrame(rows)
    keys = ["factor", "horizon", "segment"]
    baseline = result.loc[result.delay_sessions.eq(0), keys + [
        "oriented_mean_rank_ic", "oriented_top_quintile_excess", "ic_days", "pairs"]].rename(columns={
            "oriented_mean_rank_ic": "paired_delay0_ic",
            "oriented_top_quintile_excess": "paired_delay0_excess",
            "ic_days": "paired_delay0_days", "pairs": "paired_delay0_pairs"})
    result = result.merge(baseline, on=keys, validate="many_to_one")
    if not result.ic_days.eq(result.paired_delay0_days).all() or not result.pairs.eq(result.paired_delay0_pairs).all():
        raise ValueError("Paired delays must retain the same IC dates and finite pair counts")
    result["ic_change_from_delay0"] = result.oriented_mean_rank_ic - result.paired_delay0_ic
    result["excess_change_from_delay0"] = result.oriented_top_quintile_excess - result.paired_delay0_excess
    return result


def report(out, record, summary):
    counts = []
    lines = ["# Fixed Alpha158 Execution Delay", "",
             "Research-only diagnostics; NO_TRADE. No training, account replay or strategy selection.", "",
             f"158 definitions, {record['quality']['model_rows']:,} dated eligible feature rows; "
             f"{record['quality']['symbols']:,} supported source stocks.",
             "Horizon is the return window; extra delay moves BOTH entry and exit by 0/1/2 exchange sessions. "
             "It does not change the original five-session rebalance/actual holding cycle.",
             "Direction is reused from original delay0/2024 training separately per horizon, never refitted after a delay.", "",
             "Positive = oriented mean IC >0 AND observed oriented quintile excess >0, minimum80 IC dates/100 daily pairs. "
             "Not a significance/multiple-testing or net-account admission criterion.", "",
             "| Horizon | Extra Delay | Train Positive | 2025 Positive | Inspected 2026 Positive | All Three Positive |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for (horizon, delay), part in summary.groupby(["horizon", "delay_sessions"]):
        positives = part.pivot(index="factor", columns="segment", values="diagnostic_positive")
        result = {"horizon": int(horizon), "delay_sessions": int(delay),
                  "train_positive": int(positives.train.sum()), "valid_positive": int(positives.valid.sum()),
                  "inspected_2026_positive": int(positives.test.sum()), "all_three_positive": int(positives.all(axis=1).sum())}
        counts.append(result)
        lines.append(f"| {horizon} | {delay} | {result['train_positive']} | {result['valid_positive']} | "
                     f"{result['inspected_2026_positive']} | {result['all_three_positive']} |")
    lines += ["", "## Controlled Delay Change", "",
              "All factors are retained. Medians below use all factors with known orientation/outcomes, not winners.",
              "| Horizon | Segment | Extra Delay | Factors | Median IC Change | Median Quintile Excess Change (pp) |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for (horizon, segment, delay), part in summary.loc[summary.delay_sessions.gt(0)].groupby(
            ["horizon", "segment", "delay_sessions"]):
        values = part.dropna(subset=["ic_change_from_delay0", "excess_change_from_delay0"])
        lines.append(f"| {horizon} | {segment} | {delay} | {len(values)} | "
                     f"{values.ic_change_from_delay0.median():.6f} | {100 * values.excess_change_from_delay0.median():.6f} |")
    lines += ["", "## Boundaries", "",
              "- Same signal dates, individual feature availability and COMMON finite endpoint outcomes across all three delays. "
              "This conditions on future coverage for diagnosis only; it is not a tradable candidate filter.",
              "- All three delays are purged together when the latest exit crosses its original segment end. "
              "Original unmatched delay0 summary is retained separately and is not mixed into paired deltas.",
              "- Individual endpoint coverage, common coverage and discarded signals are retained. Missing halt/price endpoints are not advanced or zero-filled.",
              "- Quintile membership ranks current signals BEFORE outcomes; pairwise-finite reranking for Spearman IC matches the existing diagnostic method.",
              "- Overlapping labels are not compounded NAV, realized holdings, order fills, net alpha or independent trials. HAC lag equals horizon.",
              "- Delay can alter observed predictive association but this is not a causal account-PnL explanation; price adjustment, PIT, "
              "tradability, costs, multiple testing and head selection remain separate limitations.",
              "- 2025 and inspected2026 are development diagnostics, not untouched/prospective validation. "
              "Alpha360/local200 are NOT included in this delay consumer; their prior undelayed scan is unchanged.",
              f"- Detailed outputs: `{record['artifacts']}`. No account/config/default strategy changed."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return counts


def run(family_run):
    baseline_path = ROOT / "outputs/factor_families" / family_run / "run.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if (baseline.get("status") != "completed_research_diagnostics" or baseline.get("definitions_completed") != 158
            or baseline.get("completed_horizons") != list(HORIZONS) or baseline.get("fresh_holdout_claimed") is not False):
        raise ValueError("Completed fixed Alpha158 five-horizon diagnostics required")
    source = json.loads(Path(baseline["source_run"]).read_text(encoding="utf-8"))
    if (source.get("status") != "completed" or source.get("config", {}).get("scope") != BULK_SCOPE
            or source.get("ingestion_only") is not True or source["bulk_inputs"] != baseline["bulk_inputs"]
            or source["config"]["segments"] != baseline["segments"]):
        raise ValueError("Original full-source model pool and fixed segments must match")
    definitions = catalog()
    original = pd.read_csv(Path(baseline["artifacts"]) / "decay_summary.csv")
    directions = frozen_directions(original, definitions)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/execution_delay" / identifier, DATA_ROOT / "research/execution_delay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    record = {"id": identifier, "purpose": ID, "status": "running", "source_run": str(baseline_path),
              "artifacts": str(artifacts), "bulk_inputs": baseline["bulk_inputs"], "segments": baseline["segments"],
              "horizons": list(HORIZONS), "extra_delay_sessions": list(DELAYS),
              "minimum_ic_pairs": MIN_PAIRS, "minimum_ic_days": MIN_DAYS,
              "orientation_basis": "original_delay0_2024_training_only_per_horizon",
              "coverage_basis": "paired_common_finite_outcomes_for_all_delays_not_tradable_filter",
              "account_backtest": False, "execution_admitted": False, "fresh_holdout_claimed": False,
              "model_fit": False, "selection_parameters_searched": False, "definitions_completed": 0}
    write_json(out / "run.json", record)
    feed, started = None, time.monotonic()
    try:
        inputs = baseline["bulk_inputs"]
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        features, _, quality = load_inputs(feed, StrategyConfig(**source["portfolio_config"]).validate(),
                                           inputs["native_view"], definitions, segments=record["segments"])
        if quality != baseline["quality"]:
            raise ValueError("Native pool/quality differs from the fixed family diagnostic")
        record["quality"] = quality
        _, _, provider = native_binding(feed, inputs["native_view"], definitions)
        calendar = pd.to_datetime(feed.calendar)
        opening = read_open_panel(provider, feed.symbols, calendar).to_numpy()
        date_offsets = {day: i for i, day in enumerate(calendar)}
        symbol_offsets = {symbol: i for i, symbol in enumerate(feed.symbols)}
        segments = {}
        for segment, (start, end) in record["segments"].items():
            end_offset = int(calendar.searchsorted(pd.Timestamp(end), side="right") - 1)
            segments.update({day: (segment, end_offset) for day in calendar
                             if pd.Timestamp(start) <= day <= pd.Timestamp(end)})
        original.to_csv(artifacts / "original_unmatched_decay_summary.csv", index=False)
        parts, coverage = [], []
        for completed, (day, frame) in enumerate(features.groupby(level="date", sort=True), 1):
            offsets = [symbol_offsets[s] for s in frame.index.get_level_values("symbol")]
            segment, end_offset = segments[day]
            for horizon in HORIZONS:
                full, _, purged = delayed_outcomes(opening, date_offsets[day], horizon, end_offset)
                outcomes = full[offsets]
                common = np.isfinite(outcomes).all(axis=1)
                daily = paired_daily(frame, outcomes)
                daily["date"], daily["horizon"], daily["segment"] = day, horizon, segment
                parts.append(daily)
                coverage.append({"date": day, "segment": segment, "horizon": horizon, "pool_rows": len(frame),
                                 "purged_all_delays": purged, "common_endpoint_rows": int(common.sum()),
                                 **{f"delay{delay}_endpoint_rows": int(np.isfinite(outcomes[:, delay]).sum()) for delay in DELAYS}})
            if completed % 50 == 0:
                record["signal_dates_completed"] = completed
                write_json(out / "run.json", record)
                print(f"Execution-delay: {completed} signal dates, 158 definitions, all5 horizons/all3 delays", flush=True)
        daily = pd.concat(parts, ignore_index=True)
        daily.to_parquet(artifacts / "paired_daily_diagnostics.parquet", index=False)
        pd.DataFrame(coverage).to_csv(artifacts / "endpoint_coverage.csv", index=False)
        summary = summarize(daily, definitions, directions)
        summary.to_csv(artifacts / "paired_delay_summary.csv", index=False)
        record.update(status="completed_research_diagnostics", definitions_completed=158,
                      signal_dates_completed=int(features.index.get_level_values("date").nunique()),
                      daily_rows=len(daily), summary_rows=len(summary))
        record["diagnostic_counts"] = report(out, record, summary)
        return out, record
    except Exception as error:
        record.update(status="failed_research_diagnostics", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if feed is not None:
            feed.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
        print(str(out), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family-run", required=True, help="Completed fixed Alpha158 family diagnostic ID")
    args = parser.parse_args(argv)
    run(args.family_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
