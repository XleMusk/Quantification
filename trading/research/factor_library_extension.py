"""Consume remaining public definitions on the completed native diagnostic pool."""
import argparse
from datetime import datetime
import gc
import json
from pathlib import Path
import re
import time
import uuid

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .bulk_model_inputs import load_inputs
from .experiment import write_json
from .factor_families import (HORIZONS, MIN_DAYS, MIN_PAIRS, REDUNDANCY_STRIDE,
                              REDUNDANCY_THRESHOLD, forward_open_labels, read_open_panel,
                              redundancy, summarize_decay)
from .factors import candidate_batches, catalog, features_from_provider, qcode


WINDOWS = (5, 10, 20, 30, 60)
BATCH = 12


def qualify(definitions, library):
    result = definitions.copy()
    result["original_name"] = result.name
    result["library"] = library
    result["name"] = library + "/" + result.name
    if "category" not in result:
        result["category"] = "price_volume_templates" if library == "Templates" else "lagged_price_volume"
    return result


def alpha360_catalog(native):
    from qlib.contrib.data.loader import Alpha360DL
    formulas, names = Alpha360DL.get_feature_config()
    if len(names) != 360 or len(set(names)) != 360:
        raise ValueError("Installed Alpha360 must contain 360 unique definitions")
    definitions = pd.DataFrame({"name": names, "formula": formulas})
    definitions["family"] = definitions.name.str.replace(r"\d+$", "", regex=True)
    native_by_formula = dict(zip(native.formula, native.name))
    definitions["reused_from"] = definitions.formula.map(native_by_formula)
    return qualify(definitions, "Alpha360")


def read_field(provider, field, symbols, calendar):
    columns = []
    for symbol in symbols:
        values = np.fromfile(provider / "features" / qcode(symbol).lower() / f"{field}.day.bin", dtype="<f4")
        if len(values) != len(calendar) + 1 or values[0] != 0:
            raise ValueError(f"Pinned provider {field} header/calendar changed")
        columns.append(values[1:])
    return pd.DataFrame(np.column_stack(columns), index=calendar, columns=symbols)


def score360(source, close, volume, item):
    match = re.fullmatch(r"(CLOSE|OPEN|HIGH|LOW|VWAP|VOLUME)(\d+)", item.original_name)
    if match is None:
        raise ValueError("Unrecognized Alpha360 name")
    field, lag_text = match.groups()
    lag = int(lag_text)
    numerator = f"Ref(${field.lower()}, {lag})" if lag else f"${field.lower()}"
    denominator = "($volume+1e-12)" if field == "VOLUME" else "$close"
    if item.formula != numerator + "/" + denominator or not 0 <= lag <= 59:
        raise ValueError("Installed Alpha360 formula changed")
    # Keep float32 Qlib field arithmetic and the full exchange calendar, including missing sessions.
    denom = volume + 1e-12 if field == "VOLUME" else close
    return (source.shift(lag) / denom).replace([np.inf, -np.inf], np.nan)


def verify_provider_translation(provider, symbols, calendar, definitions):
    selected = [symbols[0], symbols[-1]]
    check = definitions.loc[definitions.original_name.str.extract(r"(\d+)$", expand=False).astype(int).isin([0, 1, 59])]
    start, end = calendar[80], calendar[100]
    official = features_from_provider(provider, selected, calendar[80:101], check, [])
    close = read_field(provider, "close", selected, calendar)
    volume = read_field(provider, "volume", selected, calendar)
    for field, part in check.groupby("family", sort=False):
        source = read_field(provider, field.lower(), selected, calendar)
        for item in part.itertuples(index=False):
            actual = score360(source, close, volume, item).loc[start:end]
            actual.index.name, actual.columns.name = "date", "symbol"
            actual = actual.stack(future_stack=True).reindex(official.index)
            np.testing.assert_allclose(actual.to_numpy(), official[item.name].to_numpy(), rtol=0, atol=0, equal_nan=True)
    return {"engine": "installed_qlib", "symbols": len(selected), "sessions": 21,
            "definitions": len(check), "exact_float32_match": True}


def diagnostics_batch(features, labels, segment_by_date, min_pairs=MIN_PAIRS):
    """Same pairwise-finite Spearman and signal-only quintiles, grouped once for all horizons."""
    aligned = {h: label.reindex(features.index).to_numpy() for h, label in labels.items()}
    results = []
    names = features.columns.to_numpy()
    for day, positions in features.groupby(level="date", sort=True).indices.items():
        frame = features.iloc[positions]
        x = frame.to_numpy(dtype=float)
        finite = np.isfinite(x)
        rank = frame.rank(pct=True).to_numpy()
        unique = frame.nunique().to_numpy()
        signal_count = finite.sum(axis=0)
        for horizon, target in aligned.items():
            y = target[positions]
            valid = finite & np.isfinite(y[:, None])
            count = valid.sum(axis=0)
            rx = pd.DataFrame(np.where(valid, x, np.nan)).rank().to_numpy()
            ry = pd.DataFrame(np.where(valid, y[:, None], np.nan)).rank().to_numpy()
            rx -= np.nansum(rx, axis=0) / np.maximum(count, 1)
            ry -= np.nansum(ry, axis=0) / np.maximum(count, 1)
            denominator = np.sqrt(np.nansum(rx ** 2, axis=0) * np.nansum(ry ** 2, axis=0))
            ic = np.divide(np.nansum(rx * ry, axis=0), denominator,
                           out=np.full(len(names), np.nan), where=(denominator > 0) & (count >= min_pairs))
            def mean(mask):
                return np.divide(np.nansum(np.where(mask, y[:, None], np.nan), axis=0), mask.sum(axis=0),
                                 out=np.full(len(names), np.nan), where=mask.sum(axis=0) > 0)
            universe = mean(valid)
            enough = (count >= min_pairs) & (unique >= 5)
            results.append(pd.DataFrame({"date": day, "factor": names, "signal_rows": signal_count,
                                        "pairs": count, "missing_labels": signal_count - count,
                                        "q1_excess": np.where(enough, mean(valid & (rank <= .2)) - universe, np.nan),
                                        "q5_excess": np.where(enough, mean(valid & (rank > .8)) - universe, np.nan),
                                        "rank_ic": ic, "horizon": horizon, "segment": segment_by_date[day]}))
    return pd.concat(results, ignore_index=True)


def report(out, record, summary, profiles):
    lines = ["# Current-Snapshot Public Factor Library", "", "Research-only; NO_TRADE. No new model fit or account replay.", "",
             f"{record['definitions_completed']}/718 definitions consumed: Alpha158 (158) + Alpha360 (360) + local templates (200).",
             f"Same {record['quality']['model_rows']} dated eligible rows / {record['quality']['symbols']} supported stocks; "
             "2024 training / 2025 validation / already inspected 2026 through Sep30.",
             "Directions fixed per horizon using training only; segment-crossing and missing-endpoint labels remain missing.", "",
             "| Library | Horizon | Train positive | Valid positive | Inspected2026 positive | All3 positive | Training components |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    checks = []
    clusters = profiles.set_index("name").redundancy_cluster
    for library, subset in [(name, summary.loc[summary.factor.str.startswith(name + "/")])
                            for name in ("Alpha158", "Alpha360", "Templates")] + [("All", summary)]:
        for horizon, part in subset.groupby("horizon"):
            positive = part.pivot(index="factor", columns="segment", values="diagnostic_positive")
            stable = positive.all(axis=1)
            row = {"library": library, "horizon": int(horizon),
                   "train_positive": int(positive.train.sum()), "valid_positive": int(positive.valid.sum()),
                   "inspected_2026_positive": int(positive.test.sum()), "all_three_positive": int(stable.sum()),
                   "represented_training_components": int(clusters.reindex(stable.index[stable]).nunique())}
            checks.append(row)
            lines.append(f"| {library} | {horizon} | {row['train_positive']} | {row['valid_positive']} | "
                         f"{row['inspected_2026_positive']} | {row['all_three_positive']} | {row['represented_training_components']} |")
    lines += ["", f"Exact formula reuse: {record['alpha360_exact_formula_reuses']} Alpha360 definitions reused Alpha158 daily statistics.",
              f"Training redundancy: {record['redundancy_edges']} high-|rank-correlation| edges, "
              f"{record['redundancy_components']} connected components on {len(record['training_anchor_dates'])} fixed training anchors.",
              "These are connected components, NOT independent alphas or pairwise-equivalent sets; all definitions retained.",
              f"Definitions constant within every current dated cross-section: {record['cross_section_constant_definitions']}.", "",
              "## Limits", "",
              "- Coverage completed does not mean useful net alpha. Positive checks only test oriented IC and observed quintile excess, with at least80 IC dates.",
              "- No multiple-testing correction, industry/size neutrality, family incremental net-PnL or fresh independent holdout completed here.",
              "- All-horizon labels are normalized adjusted next-open to horizon+1-open, not cash accounts, guaranteed fills or tradability.",
              "- Missing endpoints stay unknown; signal-only group membership avoids future-conditioned reranking, but observed means can retain attrition bias.",
              "- HAC lag equals horizon; overlapping outcomes are not independent replications. Train-sign selection and repeated definitions remain visible.",
              "- Training rank-correlation uses each factor's own finite ranks, pairwise Pearson, date-equal anchors every20 sessions; |mean|>=0.9 with3anchors.",
              "- ROC high means weaker past performance; BETA is a time-trend slope, not market beta. Templates are existing local formulas, not institutional secret factors.",
              "- Scope is supported ordinary SH/SZ stocks, not allA/BSE/ETF/PIT; existing price/action/listing limits remain.",
              "- Real-account operations disabled; native Esc-stop unchanged. Constant/duplicate/mirror definitions retained, not silently removed.", "",
              f"Detailed catalogs, daily diagnostics, decay and cross-library training edges: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checks


def run(previous_id):
    previous_path = ROOT / "outputs/factor_families" / previous_id / "run.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    if (previous.get("status") != "completed_research_diagnostics" or previous.get("definitions_completed") != 158
            or previous.get("completed_horizons") != list(HORIZONS) or previous.get("fresh_holdout_claimed") is not False):
        raise ValueError("Completed five-horizon native158 diagnostic run required")
    source = json.loads(Path(previous["source_run"]).read_text(encoding="utf-8"))
    if (source.get("status") != "completed" or source["config"].get("scope") != BULK_SCOPE
            or not source.get("ingestion_only") or source.get("fresh_holdout_claimed") is not False
            or source["bulk_inputs"] != previous["bulk_inputs"] or source["config"]["segments"] != previous["segments"]):
        raise ValueError("Prior native diagnostics and completed model source must share bindings")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/factor_libraries" / identifier
    artifacts = DATA_ROOT / "research/factor_libraries" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {"id": identifier, "status": "running_research_diagnostics", "previous_diagnostics": str(previous_path),
              "source_run": previous["source_run"], "artifacts": str(artifacts), "bulk_inputs": previous["bulk_inputs"],
              "segments": previous["segments"], "horizons": list(HORIZONS), "definitions_completed": 158,
              "libraries": {"Alpha158": 158, "Alpha360": 0, "Templates": 0},
              "account_backtest": False, "execution_admitted": False, "fresh_holdout_claimed": False,
              "model_fit": False, "selection_parameters_searched": False, "net_incremental_value": None,
              "minimum_ic_pairs": MIN_PAIRS, "minimum_ic_days": MIN_DAYS,
              "redundancy_stride": REDUNDANCY_STRIDE, "redundancy_threshold": REDUNDANCY_THRESHOLD}
    write_json(out / "run.json", record)
    started, feed, writer = time.monotonic(), None, None
    try:
        native = catalog()
        native_q = qualify(native, "Alpha158")
        alpha = alpha360_catalog(native)
        definitions = [native_q, alpha]
        inputs = source["bulk_inputs"]
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        config = StrategyConfig(**source["portfolio_config"]).validate()
        features, unused_labels, quality = load_inputs(feed, config, inputs["native_view"], native, segments=record["segments"])
        del unused_labels
        if quality != previous["quality"]:
            raise ValueError("Pinned diagnostic eligibility/model quality changed")
        record["quality"] = quality
        calendar = pd.to_datetime(feed.calendar)
        native_receipt = json.loads((Path(inputs["native_view"]) / "result.json").read_text(encoding="utf-8"))
        provider_view = Path(native_receipt["provider_view"]).resolve()
        provider = Path(json.loads((provider_view / "result.json").read_text(encoding="utf-8"))["provider_folder"]).resolve()
        provider.relative_to(provider_view)
        record["provider_translation_check"] = verify_provider_translation(provider, feed.symbols, calendar, alpha)
        index = features.index
        training_dates = pd.to_datetime(previous["training_anchor_dates"])
        anchor_mask = index.get_level_values("date").isin(training_dates)
        anchor_parts = [features.loc[anchor_mask].rename(columns=dict(zip(native.name, native_q.name)))]
        structural = {"Alpha158/" + name: bool(constant)
                      for name, constant in features.groupby(level="date").nunique().le(1).all().items()}
        needed = [f"{family}{w}" for family in ("ROC", "VMA", "RSV", "BETA", "CORR") for w in WINDOWS]
        features = features[needed].copy()
        gc.collect()
        segments, days = {}, pd.Series(calendar, index=calendar)
        for segment, (start, end) in record["segments"].items():
            segments.update({day: segment for day in calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]})
        opening = read_open_panel(provider, feed.symbols, calendar)
        labels = {}
        for horizon in HORIZONS:
            panel = forward_open_labels(opening, horizon)
            endpoint = days.shift(-(horizon + 1))
            mask = pd.Series(False, index=calendar)
            for start, end in record["segments"].values():
                mask |= days.ge(pd.Timestamp(start)) & days.le(pd.Timestamp(end)) & endpoint.le(pd.Timestamp(end))
            panel.loc[~mask] = np.nan
            panel.index.name, panel.columns.name = "date", "symbol"
            labels[horizon] = panel.stack(future_stack=True).reindex(index)
        del opening, panel
        summaries, daily_rows = [], 0
        daily_file = artifacts / "daily_diagnostics.parquet"
        def append(daily, defs):
            nonlocal writer, daily_rows
            table = pa.Table.from_pandas(daily, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(daily_file, table.schema)
            else:
                table = table.cast(writer.schema)
            writer.write_table(table)
            daily_rows += len(daily)
            summaries.append(summarize_decay(daily, defs))
        previous_artifacts = Path(previous["artifacts"])
        base_daily = pd.read_parquet(previous_artifacts / "daily_diagnostics.parquet")
        expected_rows = len(native) * len(segments) * len(HORIZONS)
        if len(base_daily) != expected_rows or set(base_daily.factor) != set(native.name):
            raise ValueError("Completed native daily coverage changed")
        old_daily = base_daily
        base_daily = base_daily.assign(factor="Alpha158/" + base_daily.factor)
        append(base_daily, native_q)
        del base_daily
        for item in alpha.loc[alpha.reused_from.notna()].itertuples(index=False):
            part = old_daily.loc[old_daily.factor.eq(item.reused_from)].assign(factor=item.name)
            append(part, alpha.loc[alpha.name.eq(item.name)])
            record["libraries"]["Alpha360"] += 1
        record["alpha360_exact_formula_reuses"] = record["libraries"]["Alpha360"]
        del old_daily
        close = read_field(provider, "close", feed.symbols, calendar)
        volume = read_field(provider, "volume", feed.symbols, calendar)
        for field, field_definitions in alpha.groupby("family", sort=False):
            source_field = read_field(provider, field.lower(), feed.symbols, calendar)
            # Reused definitions still contribute actual anchor values to cross-library redundancy.
            for offset in range(0, len(field_definitions), BATCH):
                part = field_definitions.iloc[offset:offset + BATCH]
                values = {}
                for item in part.itertuples(index=False):
                    score = score360(source_field, close, volume, item)
                    score.index.name, score.columns.name = "date", "symbol"
                    values[item.name] = score.stack(future_stack=True).reindex(index)
                batch = pd.DataFrame(values, index=index)
                anchor_parts.append(batch.loc[anchor_mask])
                for name in batch:
                    structural[name] = bool(batch[name].groupby(level="date").nunique().le(1).all())
                new = part.loc[part.reused_from.isna()]
                if len(new):
                    append(diagnostics_batch(batch[new.name], labels, segments), new)
                    record["libraries"]["Alpha360"] += len(new)
                record["definitions_completed"] = sum(record["libraries"].values())
                write_json(out / "run.json", record)
                print(f"Current library: {record['definitions_completed']}/718; Alpha360 {record['libraries']['Alpha360']}/360", flush=True)
            del source_field, batch, values, score
        del close, volume
        empty = pd.DataFrame(columns=["name", "formula"])
        retvol = features_from_provider(provider, feed.symbols, calendar, empty, list(WINDOWS)).reindex(index)
        features = features.join(retvol, validate="one_to_one")
        del retvol
        gc.collect()
        for batch, defs in candidate_batches(features, list(WINDOWS)):
            qualified = qualify(defs, "Templates")
            batch.columns = qualified.name
            definitions.append(qualified)
            anchor_parts.append(batch.loc[anchor_mask])
            for name in batch:
                structural[name] = bool(batch[name].groupby(level="date").nunique().le(1).all())
            append(diagnostics_batch(batch, labels, segments), qualified)
            record["libraries"]["Templates"] += len(defs)
            record["definitions_completed"] = sum(record["libraries"].values())
            write_json(out / "run.json", record)
            print(f"Current library: {record['definitions_completed']}/718; templates {record['libraries']['Templates']}/200", flush=True)
        writer.close()
        writer = None
        del features, batch, labels
        definitions = pd.concat(definitions, ignore_index=True)
        if len(definitions) != 718 or definitions.name.nunique() != 718 or record["definitions_completed"] != 718:
            raise ValueError("Expected all 718 library-qualified definitions")
        anchors = pd.concat(anchor_parts, axis=1)
        del anchor_parts
        gc.collect()
        print("All718 diagnostics consumed; measuring cross-library training redundancy", flush=True)
        profiles, edges, observed = redundancy(anchors, training_dates, definitions)
        profiles["cross_section_constant"] = profiles.name.map(structural).fillna(False).astype(bool)
        profiles["exact_formula_reuse"] = profiles.reused_from.notna()
        template_names = {(row.window_1, row.window_2): row.name for row in definitions.itertuples(index=False)
                          if row.library == "Templates" and row.family == "relative_trend"}
        profiles["structural_sign_mirror"] = [template_names.get((row.window_2, row.window_1))
                                              if row.library == "Templates" and row.family == "relative_trend"
                                              and row.window_1 != row.window_2 else None
                                              for row in profiles.itertuples(index=False)]
        profiles.to_csv(artifacts / "factor_profiles.csv", index=False)
        edges.to_csv(artifacts / "redundancy_edges.csv", index=False)
        summary = pd.concat(summaries, ignore_index=True)
        summary.to_csv(artifacts / "decay_summary.csv", index=False)
        record.update(status="completed_research_diagnostics", daily_rows=daily_rows, summary_rows=len(summary),
                      completed_horizons=list(HORIZONS), training_anchor_dates=observed,
                      redundancy_edges=len(edges), redundancy_components=int(profiles.redundancy_cluster.nunique()),
                      cross_section_constant_definitions=int(profiles.cross_section_constant.sum()))
        if daily_rows != len(segments) * 718 * len(HORIZONS) or len(summary) != 718 * len(HORIZONS) * 3:
            raise ValueError("Complete daily/segment/horizon coverage required")
        record["diagnostic_counts"] = report(out, record, summary, profiles)
        return out, record
    except Exception as error:
        record.update(status="failed_research_diagnostics", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if writer is not None:
            writer.close()
        if feed is not None:
            feed.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
        print(str(out), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostics", required=True, help="Completed native158 factor-family run ID")
    args = parser.parse_args(argv)
    run(args.diagnostics)
    return 0
