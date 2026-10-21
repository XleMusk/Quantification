"""Reuse native Alpha158 for family redundancy and horizon diagnostics, not PnL."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .bulk_model_inputs import load_inputs
from .experiment import write_json
from .factors import catalog, daily_ic, qcode


HORIZONS = (1, 5, 10, 20, 60)
MIN_PAIRS = 100
MIN_DAYS = 80
REDUNDANCY_STRIDE = 20
REDUNDANCY_THRESHOLD = .9


def forward_open_labels(opening, horizon):
    if horizon not in HORIZONS or not opening.index.is_unique or not opening.index.is_monotonic_increasing:
        raise ValueError("Fixed horizon and unique ordered full calendar required")
    valid = opening.where(np.isfinite(opening) & opening.gt(0))
    # Do not compress missing dates: a halt at either scheduled endpoint stays unknown.
    return valid.shift(-(horizon + 1)) / valid.shift(-1) - 1


def read_open_panel(provider, symbols, calendar):
    columns = []
    for symbol in symbols:
        values = np.fromfile(provider / "features" / qcode(symbol).lower() / "open.day.bin", dtype="<f4")
        if len(values) != len(calendar) + 1 or values[0] != 0:
            raise ValueError("Pinned provider open header/calendar changed")
        columns.append(values[1:].astype(float))
    return pd.DataFrame(np.column_stack(columns), index=calendar, columns=symbols)


def rank_group_means(features, labels, min_pairs=MIN_PAIRS):
    rows = []
    for day, frame in features.groupby(level="date", sort=True):
        ranks = frame.rank(pct=True).to_numpy()
        x = frame.to_numpy()
        y = labels.reindex(frame.index).to_numpy()
        paired = np.isfinite(x) & np.isfinite(y[:, None])
        count = paired.sum(axis=0)
        def mean(mask):
            numbers = np.where(mask, y[:, None], np.nan)
            n = mask.sum(axis=0)
            return np.divide(np.nansum(numbers, axis=0), n, out=np.full(frame.shape[1], np.nan), where=n > 0)
        q1, q5 = mean(paired & (ranks <= .2)), mean(paired & (ranks > .8))
        universe = mean(paired)
        for i, name in enumerate(frame.columns):
            enough = count[i] >= min_pairs and frame[name].nunique() >= 5
            rows.append({"date": day, "factor": name, "signal_rows": int(np.isfinite(x[:, i]).sum()),
                         "pairs": int(count[i]), "missing_labels": int((np.isfinite(x[:, i]) & ~np.isfinite(y)).sum()),
                         "q1_excess": q1[i] - universe[i] if enough else np.nan,
                         "q5_excess": q5[i] - universe[i] if enough else np.nan})
    return pd.DataFrame(rows)


def hac_mean_t(series, lag):
    values = series.dropna().to_numpy(dtype=float)
    n = len(values)
    if n < 2:
        return None
    z = values - values.mean()
    lag = min(lag, n - 1)
    variance = np.dot(z, z) / n
    for offset in range(1, lag + 1):
        variance += 2 * (1 - offset / (lag + 1)) * np.dot(z[offset:], z[:-offset]) / n
    se = np.sqrt(max(0, variance) / n)
    return float(values.mean() / se) if se > 0 else None


def summarize_decay(daily, definitions, min_days=MIN_DAYS):
    metadata = definitions.set_index("name")
    rows = []
    for (name, horizon), sample in daily.groupby(["factor", "horizon"], sort=True):
        training = sample.loc[sample.segment.eq("train"), "rank_ic"]
        direction = (1 if training.mean() > 0 else -1) if training.count() >= min_days and training.mean() != 0 else 0
        for segment in ("train", "valid", "test"):
            part = sample.loc[sample.segment.eq(segment)]
            oriented = part.rank_ic * direction if direction else part.rank_ic * np.nan
            excess = part["q5_excess" if direction > 0 else "q1_excess"] if direction else part.q1_excess * np.nan
            rows.append({"factor": name, "family": metadata.loc[name, "family"],
                         "category": metadata.loc[name, "category"], "horizon": int(horizon), "segment": segment,
                         "train_direction": "high" if direction > 0 else "low" if direction < 0 else "unknown",
                         "ic_days": int(part.rank_ic.count()), "mean_rank_ic": part.rank_ic.mean(),
                         "oriented_mean_rank_ic": oriented.mean(), "ic_std": oriented.std(),
                         "hac_t_horizon": hac_mean_t(oriented, int(horizon)),
                         "oriented_top_quintile_excess": excess.mean(),
                         "positive_oriented_ic_fraction": float(oriented.dropna().gt(0).mean()) if oriented.count() else np.nan,
                         "pairs": int(part.pairs.sum()), "missing_forward_labels": int(part.missing_labels.sum()),
                         "diagnostic_positive": bool(direction and part.rank_ic.count() >= min_days
                                                     and oriented.mean() > 0 and excess.mean() > 0),
                         "net_incremental_value": None, "account_ready": False})
    return pd.DataFrame(rows)


def redundancy(features, anchor_dates, definitions, min_pairs=MIN_PAIRS):
    names = definitions.name.tolist()
    sums = np.zeros((len(names), len(names)))
    counts = np.zeros(sums.shape, dtype=int)
    observed = []
    for day in anchor_dates:
        if day not in features.index.get_level_values("date"):
            continue
        # Ranks use each signal's available universe, not future labels or pair-conditioned reranking.
        ranks = features.loc[day, names].rank(pct=True)
        matrix = ranks.corr(min_periods=min_pairs).to_numpy()
        finite = np.isfinite(matrix)
        sums += np.where(finite, matrix, 0)
        counts += finite
        observed.append(pd.Timestamp(day).strftime("%Y-%m-%d"))
    average = np.divide(sums, counts, out=np.full(sums.shape, np.nan), where=counts > 0)
    parent = {name: name for name in names}
    def find(name):
        while parent[name] != name:
            name = parent[name]
        return name
    edges = []
    for i, name in enumerate(names):
        for j in range(i + 1, len(names)):
            if counts[i, j] >= 3 and abs(average[i, j]) >= REDUNDANCY_THRESHOLD:
                other = names[j]
                edges.append({"factor_a": name, "factor_b": other, "mean_rank_correlation": average[i, j],
                              "training_anchor_dates": int(counts[i, j])})
                first, second = sorted((find(name), find(other)))
                parent[second] = first
    profiles = definitions.copy()
    profiles["redundancy_cluster"] = profiles.name.map(find)
    profiles["cluster_size"] = profiles.groupby("redundancy_cluster").name.transform("size")
    profiles["status"] = "SCREENED_DIAGNOSTIC_ONLY"
    profiles["net_incremental_value"] = None
    profiles["account_ready"] = False
    return profiles, pd.DataFrame(edges, columns=["factor_a", "factor_b", "mean_rank_correlation", "training_anchor_dates"]), observed


def report(folder, record, summary, profiles):
    lines = ["# Native Alpha158 Family / Decay Diagnostics", "", "Research-only; NO_TRADE. No model fit, orders, account reset or net-PnL claim.", "",
             f"{record['definitions_completed']}/158 definitions; {record['quality']['model_rows']} dated eligible feature rows; "
             f"{record['quality']['symbols']} supported source stocks.",
             "Direction fixed separately for each horizon using 2024 training only. 2025 and inspected 2026 are diagnostics, NOT fresh holdout.", "",
             "| Horizon | Train positive | Valid positive | Inspected 2026 positive | All 3 positive | Represented training components |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    checks = []
    clusters = profiles.set_index("name").redundancy_cluster
    for horizon, part in summary.groupby("horizon"):
        positives = part.pivot(index="factor", columns="segment", values="diagnostic_positive")
        stable = positives.all(axis=1)
        result = {"horizon": int(horizon), "train_positive": int(positives.train.sum()),
                  "valid_positive": int(positives.valid.sum()), "inspected_2026_positive": int(positives.test.sum()),
                  "all_three_positive": int(stable.sum()),
                  "represented_training_components": int(clusters.reindex(stable.index[stable]).nunique())}
        checks.append(result)
        lines.append(f"| {horizon} | {result['train_positive']} | {result['valid_positive']} | "
                     f"{result['inspected_2026_positive']} | {result['all_three_positive']} | {result['represented_training_components']} |")
    lines += ["", f"Redundancy: {record['redundancy_edges']} high-|correlation| training pairs; "
              f"{profiles.redundancy_cluster.nunique()} connected components (NOT mutually equivalent factor sets).",
              "Correlation is the date-equal mean Pearson correlation of within-date percentile ranks on pairwise-finite rows; "
              "each column ranked in its own available universe. Anchors every 20 training exchange sessions, minimum 3 observed anchors, |mean|>=0.9.",
              "All factors retained; components do not choose a profitable representative or remove definitions.", "",
              "## Boundaries", "",
              "- Labels are normalized adjusted next-open to horizon+1-open returns, NOT cash-account returns, observed fills or guaranteed tradability.",
              "- Endpoint-missing labels remain unknown. Group membership uses current finite signals, not future-label availability; observed group means can still have attrition bias.",
              "- Labels crossing each segment end are purged. Longer horizons have overlapping observations; HAC lag equals horizon, not independent replications.",
              "- Positive counts use IC and observed top-quintile excess only; no multiple-testing correction, net costs, industry/style neutrality or admission.",
              "- Components are descriptive train-sample connections, not independent alpha estimates. Many positive ICs can be small or statistically uncertain.",
              "- ROC60 is Ref(close,60)/close: high means weaker past60-session returns, NOT positive momentum. BETA is a normalized TIME slope, NOT market beta.",
              "- All 158 consumed; Alpha360 360 and local templates 200 not consumed on this native snapshot yet. No all718-library-complete claim.",
              "- Existing non-PIT listing/price/action limitations remain. No broker or current-market signal generated.", "",
              f"Detailed daily IC, group means, decay, profiles and training redundancy edges: `{record['artifacts']}`."]
    (folder / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checks


def run(source_id):
    source_path = ROOT / "outputs/factor_mining" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed" or source.get("config", {}).get("scope") != BULK_SCOPE
            or not source.get("ingestion_only") or source.get("fresh_holdout_claimed") is not False):
        raise ValueError("Completed full-source native Alpha158 baseline required")
    if source["config"]["segments"] != {"train": ["2024-01-02", "2024-12-31"],
                                          "valid": ["2025-01-01", "2025-12-31"],
                                          "test": ["2026-01-01", "2026-09-30"]}:
        raise ValueError("Fixed inspected2024/2025/2026 development segments required")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/factor_families" / identifier
    artifacts = DATA_ROOT / "research/factor_families" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {"id": identifier, "status": "running", "source_run": str(source_path),
              "artifacts": str(artifacts), "horizons": list(HORIZONS), "definitions_completed": 0,
              "segments": source["config"]["segments"], "bulk_inputs": source["bulk_inputs"],
              "minimum_ic_pairs": MIN_PAIRS, "minimum_ic_days": MIN_DAYS,
              "redundancy_stride": REDUNDANCY_STRIDE, "redundancy_threshold": REDUNDANCY_THRESHOLD,
              "account_backtest": False, "execution_admitted": False, "fresh_holdout_claimed": False,
              "model_fit": False, "selection_parameters_searched": False,
              "other_libraries": {"Alpha360": {"definitions": 360, "status": "not_consumed_on_this_snapshot"},
                                  "local_templates": {"definitions": 200, "status": "not_consumed_on_this_snapshot"}}}
    write_json(out / "run.json", record)
    feed = None
    started = time.monotonic()
    try:
        inputs = source["bulk_inputs"]
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        definitions = catalog()
        config = StrategyConfig(**source["portfolio_config"]).validate()
        features, _, quality = load_inputs(feed, config, inputs["native_view"], definitions, segments=record["segments"])
        record["quality"] = quality
        if quality["model_rows"] != source["data_quality"]["model_rows"]:
            raise ValueError("Diagnostic pool changed from the completed model baseline")
        calendar = pd.to_datetime(feed.calendar)
        native_receipt = json.loads((Path(inputs["native_view"]) / "result.json").read_text(encoding="utf-8"))
        provider_view = Path(native_receipt["provider_view"]).resolve()
        provider_receipt = json.loads((provider_view / "result.json").read_text(encoding="utf-8"))
        provider = Path(provider_receipt["provider_folder"]).resolve()
        provider.relative_to(provider_view)
        opening = read_open_panel(provider, feed.symbols, calendar)
        days = pd.Series(calendar, index=calendar)
        label_panels = {h: forward_open_labels(opening, h) for h in HORIZONS}
        labels, segment_by_date = {}, {}
        for segment, (start, end) in record["segments"].items():
            segment_days = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
            segment_by_date.update({day: segment for day in segment_days})
        for horizon, panel in label_panels.items():
            endpoint = days.shift(-(horizon + 1))
            mask = pd.Series(False, index=calendar)
            for start, end in record["segments"].values():
                mask |= (days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end)) & endpoint.le(pd.Timestamp(end))
            panel.loc[~mask] = np.nan
            labels[horizon] = panel.stack(future_stack=True).rename("label").reindex(features.index)
        del opening, label_panels
        training_dates = calendar[(calendar >= pd.Timestamp(record["segments"]["train"][0]))
                                  & (calendar <= pd.Timestamp(record["segments"]["train"][1]))][::REDUNDANCY_STRIDE]
        profiles, edges, anchors = redundancy(features, training_dates, definitions)
        profiles.to_csv(artifacts / "factor_profiles.csv", index=False)
        edges.to_csv(artifacts / "redundancy_edges.csv", index=False)
        record.update(redundancy_edges=len(edges), redundancy_components=int(profiles.redundancy_cluster.nunique()),
                      training_anchor_dates=anchors)
        write_json(out / "run.json", record)
        daily_parts = []
        for offset in range(0, len(definitions), 16):
            names = definitions.name.iloc[offset:offset + 16].tolist()
            batch = features[names]
            for horizon in HORIZONS:
                ics = daily_ic(batch, labels[horizon], min_pairs=MIN_PAIRS)
                ics = ics.rename_axis(columns="factor").stack(future_stack=True).rename("rank_ic").reset_index()
                daily = rank_group_means(batch, labels[horizon]).merge(ics, on=["date", "factor"], validate="one_to_one")
                daily["horizon"] = horizon
                daily["segment"] = daily.date.map(segment_by_date)
                daily_parts.append(daily)
            record["definitions_completed"] = min(offset + len(names), len(definitions))
            write_json(out / "run.json", record)
            print(f"Family/decay: {record['definitions_completed']}/158 definitions, all5 horizons", flush=True)
        daily = pd.concat(daily_parts, ignore_index=True)
        daily.to_parquet(artifacts / "daily_diagnostics.parquet", index=False)
        summary = summarize_decay(daily, definitions)
        summary.to_csv(artifacts / "decay_summary.csv", index=False)
        record.update(status="completed_research_diagnostics", daily_rows=len(daily), summary_rows=len(summary),
                      completed_horizons=list(HORIZONS), family_counts=definitions.groupby("category").size().to_dict())
        record["diagnostic_counts"] = report(out, record, summary, profiles)
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
    parser.add_argument("--run", required=True, help="Saved completed full-source native Alpha158 baseline ID")
    args = parser.parse_args(argv)
    run(args.run)
    return 0
