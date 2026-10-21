"""Consume saved targets/buys/PnL with dated raw style fields, without new accounts."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import shutil
import socket
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from a_stock_committee.market_data import baostock_session, normalize_symbol
from ..market_dataset import provider_frame
from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .factors import LABEL


ID = "saved_alpha158_dated_style_exposure_v1"
FIELDS = ("close", "volume", "turn", "peTTM", "pbMRQ")
STYLE = ("size_proxy_percentile", "turn_percentile", "positive_pe_percentile", "positive_pb_percentile", "nonpositive_pe")
LIMITS = [
    "Inspected2026 development diagnosis; no model fitting, strategy search, replay, cash reset or admission; NO_TRADE",
    "The price*volume/turn denominator index is an explicitly assumed SIZE PROXY, not independently verified float/total market cap",
    "Proxy values have unspecified scale; only within-day ranks are used, not an invented RMB market-cap field",
    "Raw daily PE/PB and requested historical industries are retrieval-time provider fields, not revision-safe financial/industry PIT",
    "Industry update dates later than the requested decision are rejected; missing or unsuccessful dates remain unknown",
    "Target statistics use normalized target stock weights, not observed daily holding exposures; buy statistics describe purchase notional flows",
    "Five-session labels/size-peer differences are arithmetic descriptive diagnostics, not executable net alpha or a causal risk attribution",
    "Peer means omit unknown labels with coverage recorded; membership and style buckets never depend on those labels",
    "Period PnL is allocated ONCE per security to its FIRST executed buy decision's bucket/industry; not dynamic holding-level style attribution",
    "The original saved Ledger security PnL is reused, not recomputed by a new accounting engine",
    "Positive-PE/PB percentile comparisons omit nonpositive/missing ratios with explicit coverage, never treating losses as cheap PE",
    "No current classification is silently propagated to earlier dates; dated responses alone do not prove historical availability",
]


def read_source(identifier):
    path = (ROOT / "outputs/prediction_funnel" / identifier / "run.json").resolve()
    path.relative_to((ROOT / "outputs/prediction_funnel").resolve())
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["status"] != "completed_saved_prediction_funnel" or record["execution_admitted"] is not False or len(record["cases"]) != 5:
        raise ValueError("The completed all-five-case saved funnel is required")
    return path, record


def normalize_industry(frame, requested):
    required = {"updateDate", "code", "industry", "industryClassification"}
    if not required <= set(frame) or frame.code.duplicated().any() or frame.empty:
        raise ValueError("Distinct nonempty dated industry schema required")
    for value in frame.updateDate:
        if not value or date.fromisoformat(value).isoformat() != value or value > requested:
            raise ValueError("Industry date is missing/invalid or later than the requested historical decision")
    result = frame.copy()
    result["date"] = requested
    result["symbol"] = result.code.map(lambda value: normalize_symbol(value).vt_symbol)
    return result[["date", "symbol", "updateDate", "industry", "industryClassification"]]


def collect_industries(days, destination):
    destination.mkdir()
    receipt = {"status": "collecting", "source": "BaoStock.query_stock_industry(date=decision_day)",
               "requested_dates": days, "entries": [], "revision_safe_pit_verified": False,
               "execution_admitted": False}
    write_json(destination / "result.json", receipt)
    old_timeout, current = socket.getdefaulttimeout(), None
    try:
        socket.setdefaulttimeout(20)
        with baostock_session() as bs:
            for current in days:
                frame = provider_frame(bs.query_stock_industry(date=current)).fillna("")
                frame.to_csv(destination / (current + "_raw.csv"), index=False)
                valid = normalize_industry(frame, current)
                valid.to_parquet(destination / (current + ".parquet"), index=False)
                receipt["entries"].append({"date": current, "rows": len(valid), "file": current + ".parquet",
                    "retrieved_at": datetime.now().astimezone().isoformat(), "update_dates": sorted(set(valid.updateDate))})
                write_json(destination / "result.json", receipt)
                print(f"dated industry {current}: {len(valid)} rows", flush=True)
        receipt["status"] = "completed_requested_industry_history"
    except Exception as error:
        receipt.update(status="failed_requested_industry_history", failed_date=current,
                       error=f"{type(error).__name__}: {error}")
        print("Industry acquisition stopped: " + receipt["error"], flush=True)
    finally:
        socket.setdefaulttimeout(old_timeout)
        write_json(destination / "result.json", receipt)
    return receipt


def load_industries(folder, days):
    if folder is None:
        return pd.DataFrame(columns=["date", "symbol", "updateDate", "industry", "industryClassification"]), {"status": "not_acquired", "entries": []}
    folder = Path(folder).resolve()
    receipt = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if receipt["source"] != "BaoStock.query_stock_industry(date=decision_day)" or receipt["execution_admitted"] is not False:
        raise ValueError("Explicit dated unadmitted industry source required")
    frames = []
    for entry in receipt["entries"]:
        if entry["date"] not in days:
            continue
        path = (folder / entry["file"]).resolve()
        path.relative_to(folder)
        frame = pd.read_parquet(path)
        if (len(frame) != entry["rows"] or not frame.date.eq(entry["date"]).all()
                or frame.duplicated(["date", "symbol"]).any()
                or frame.updateDate.eq("").any() or frame.updateDate.gt(entry["date"]).any()):
            raise ValueError("Historical industry identities/dates changed")
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "symbol", "updateDate", "industry", "industryClassification"])
    if result.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate dated industry responses")
    return result, receipt


def percentile(values):
    known = np.isfinite(values)
    return (values.where(known).rank(method="average") - .5) / int(known.sum()) if known.any() else values * np.nan


def style_day(pool):
    result = pool.copy()
    for name in FIELDS:
        result[name] = pd.to_numeric(result[name], errors="coerce").replace([np.inf, -np.inf], np.nan)
    valid = result.close.gt(0) & result.volume.gt(0) & result.turn.gt(0)
    result["turnover_denominator_size_proxy"] = (result.close * result.volume / result.turn).where(valid)
    result["size_proxy_percentile"] = percentile(result.turnover_denominator_size_proxy)
    result["turn_percentile"] = percentile(result.turn.where(result.turn.gt(0)))
    result["positive_pe_percentile"] = percentile(result.peTTM.where(result.peTTM.gt(0)))
    result["positive_pb_percentile"] = percentile(result.pbMRQ.where(result.pbMRQ.gt(0)))
    result["nonpositive_pe"] = result.peTTM.le(0).astype(float).where(result.peTTM.notna())
    rank = result.size_proxy_percentile
    result["size_bucket"] = np.ceil(rank * 5).clip(1, 5).map(lambda n: f"Q{int(n)}" if pd.notna(n) else "unknown")
    known = np.isfinite(result[LABEL])
    observed = result.assign(observed=known)
    observed[LABEL] = observed[LABEL].where(known)
    peers = observed.groupby("size_bucket").agg(
        members=("symbol", "size"), observed=("observed", "sum"), label_mean=(LABEL, "mean"))
    result["size_peer_label_mean"] = result.size_bucket.map(peers.label_mean).where(rank.notna())
    result["size_peer_observed_fraction"] = result.size_bucket.map(peers.observed / peers.members).where(rank.notna())
    result["label_minus_size_peer"] = result[LABEL] - result.size_peer_label_mean
    return result, peers.reset_index()


def weighted_value(rows, field, weights):
    valid = np.isfinite(rows[field])
    total = float(weights.sum())
    observed = float(weights.loc[valid].sum())
    return {"mean": float((rows.loc[valid, field] * weights.loc[valid]).sum() / observed) if observed else None,
            "known_weight_fraction": observed / total if total else None}


def exposure_row(rows, day):
    weights = rows.weight
    if not len(rows) or not np.isfinite(weights).all() or weights.le(0).any():
        raise ValueError("Saved positive target weights required")
    result = {"date": day, "targets": len(rows), "gross_target": float(weights.sum())}
    for field in (*STYLE, LABEL, "label_minus_size_peer", "size_peer_observed_fraction"):
        value = weighted_value(rows, field, weights)
        result[field + "_mean"] = value["mean"]
        result[field + "_coverage"] = value["known_weight_fraction"]
    for bucket in ["Q1", "Q2", "Q3", "Q4", "Q5", "unknown"]:
        result["size_" + bucket + "_fraction"] = float(weights.loc[rows.size_bucket.eq(bucket)].sum() / weights.sum())
    named = rows.industry.fillna("").ne("")
    industry_weights = rows.loc[named].groupby("industry").weight.sum() / weights.sum()
    result["industry_coverage"] = float(weights.loc[named].sum() / weights.sum())
    result["largest_known_industry_fraction"] = float(industry_weights.max()) if len(industry_weights) else None
    return result


def first_entry_pnl(pnl, buys):
    first = buys.sort_values(["signal_date", "order_id"]).drop_duplicates("symbol")
    classified = pnl.merge(first[["symbol", "signal_date", "size_bucket", "industry"]], on="symbol", how="left", validate="one_to_one")
    classified["size_bucket"] = classified.size_bucket.fillna("unknown")
    classified["industry"] = classified.industry.fillna("").replace("", "unknown")
    pieces = []
    for field in ("size_bucket", "industry"):
        groups = classified.groupby(field).agg(symbols=("symbol", "size"), observed_pnl_cny=("observed_pnl_cny", "sum"),
                                               buy_notional_cny=("buy_notional_cny", "sum"), fees_cny=("fees_cny", "sum")).reset_index()
        if round(groups.observed_pnl_cny.sum() * 100) != round(pnl.observed_pnl_cny.sum() * 100):
            raise ValueError("First-entry bucket allocation does not conserve original security PnL")
        groups = groups.rename(columns={field: "bucket"}).assign(classification=field)
        pieces.append(groups)
    return classified, pd.concat(pieces, ignore_index=True)


def peer_controls(targets, pool):
    """Leave every selected target out of that day's descriptive peer benchmark."""
    remaining = pool.loc[~pool.symbol.isin(targets.symbol)].copy()
    remaining["observed_label"] = remaining[LABEL].where(np.isfinite(remaining[LABEL]))
    result = targets.copy()
    for prefix, keys in (("industry_peer", ["industry"]), ("industry_size_peer", ["industry", "size_bucket"])):
        valid = remaining.industry.fillna("").ne("")
        if len(keys) == 2:
            valid &= remaining.size_bucket.ne("unknown")
        values = remaining.loc[valid].groupby(keys, dropna=False).agg(
            peer_label=("observed_label", "mean"), observed_peers=("observed_label", "count"), members=("symbol", "size"))
        values = values.rename(columns={name: prefix + "_" + name for name in values})
        result = result.merge(values.reset_index(), on=keys, how="left", validate="many_to_one")
        result[prefix + "_difference"] = result[LABEL] - result[prefix + "_peer_label"]
    return result


def run_peer_controls(identifier):
    out = (ROOT / "outputs/style_exposure" / identifier).resolve()
    out.relative_to((ROOT / "outputs/style_exposure").resolve())
    record = json.loads((out / "run.json").read_text(encoding="utf-8"))
    if record["status"] != "completed_dated_style_diagnostics" or record["execution_admitted"] is not False:
        raise ValueError("Completed all-date style/industry consumer required")
    artifacts = Path(record["artifacts"])
    destination = artifacts / "peer_controls"
    destination.mkdir(exist_ok=False)
    shutil.copy2(__file__, destination / "implementation.py")
    pool = pd.read_parquet(artifacts / "dated_style_pool.parquet")
    common = pool.loc[pool.date.isin(record["common_dates"])]
    context = {"eligible_anchor_rows": len(pool), "industry_response_rows": sum(row["rows"] for row in record["industry_receipt"]["entries"]),
               "industry_classifications": sorted(pool.industryClassification.dropna().unique()),
               "pool_nonpositive_pe_mean": float(common.groupby("date").nonpositive_pe.mean().mean()),
               "pool_pe_coverage": float(common.groupby("date").nonpositive_pe.count().div(common.groupby("date").size()).mean())}
    results = {"status": "running", "cases": [], "pool_context": context, "artifacts": str(destination),
               "method": "All targets excluded from same-date peer pool; industry and industry+original proxy-size quintile",
               "min_observed_peers": 1, "admitted": False,
               "limits": ["Observed peer labels, not executable accounts/causal alpha/significance",
                          "Unknown/no-peer rows preserved; primary full32 means require all targets matched on all32 anchors",
                          "Some observed peer cells have only one member; their descriptive means are noisy, not formal risk regression",
                          "Proxy cap and retrieval-time industries remain unverified PIT; no strategy or model changes"]}
    write_json(destination / "run.json", results)
    try:
        by_day = {day: frame for day, frame in pool.groupby("date", sort=False)}
        for case in record["cases"]:
            signal = case["signal"]
            targets = pd.read_csv(artifacts / signal / "target_styles.csv")
            targets["industry"] = targets.industry.fillna("")
            pieces, dated = [], []
            for day, frame in targets.groupby("date", sort=True):
                joined = peer_controls(frame, by_day[day])
                pieces.append(joined)
                row = {"date": day, "targets": len(frame)}
                for prefix in ("industry_peer", "industry_size_peer"):
                    weighted = weighted_value(joined, prefix + "_difference", joined.weight)
                    row[prefix + "_observed_difference"] = weighted["mean"]
                    row[prefix + "_coverage"] = weighted["known_weight_fraction"]
                    row[prefix + "_complete_difference"] = weighted["mean"] if weighted["known_weight_fraction"] == 1 else None
                    row[prefix + "_min_observed_peers"] = joined[prefix + "_observed_peers"].min()
                dated.append(row)
            pd.concat(pieces, ignore_index=True).to_csv(destination / (signal + "_targets.csv"), index=False)
            dated = pd.DataFrame(dated)
            dated.to_csv(destination / (signal + "_dates.csv"), index=False)
            common_dates = dated.loc[dated.date.isin(record["common_dates"])]
            result = {"signal": signal, "common_anchors": len(common_dates)}
            for prefix in ("industry_peer", "industry_size_peer"):
                complete = common_dates[prefix + "_complete_difference"].notna().all()
                result[prefix + "_full32_difference"] = float(common_dates[prefix + "_complete_difference"].mean()) if complete else None
                result[prefix + "_complete_anchors"] = int(common_dates[prefix + "_complete_difference"].notna().sum())
                result[prefix + "_mean_coverage"] = float(common_dates[prefix + "_coverage"].mean())
                result[prefix + "_min_observed_peers"] = int(common_dates[prefix + "_min_observed_peers"].min())
            results["cases"].append(result)
            write_json(destination / "run.json", results)
        results["status"] = "completed_leave_target_out_peer_diagnostics"
    except Exception as error:
        results.update(status="failed_leave_target_out_peer_diagnostics", error=f"{type(error).__name__}: {error}")
    finally:
        write_json(destination / "run.json", results)
        record["peer_controls"] = results
        write_json(out / "run.json", record)
        report(out, record)
    print(json.dumps(results["cases"], ensure_ascii=False), flush=True)
    return out, results


def report(out, record):
    lines = ["# Dated Target / Purchase Style Diagnostic", "", "Status: " + record["status"] + ". NO_TRADE.", "",
             "No training, account replay, default strategy changes or THS operation.",
             "Ranks below use raw provider fields on the saved decision date in the identical original model pool.", "",
             "Primary common anchors: " + str(len(record["common_dates"])) + ".", "",
             "|Case|Target proxy-size rank|Q1 smallest proxy share|Positive PB rank|Nonpositive PE share|Label minus observed size peers|",
             "|---|---:|---:|---:|---:|---:|"]
    def fmt(value):
        return "unknown" if value is None else f"{value:.2%}"
    for row in record["cases"]:
        mean = row["common_anchor_means"]
        lines.append(f"|{row['signal']}|{fmt(mean['size_proxy_percentile_mean'])}|{fmt(mean['size_Q1_fraction'])}|"
                     f"{fmt(mean['positive_pb_percentile_mean'])}|{fmt(mean['nonpositive_pe_mean'])}|{fmt(mean['label_minus_size_peer_mean'])}|")
    lines += ["", "Target means average the same anchors; coverage is retained in per-date CSV/summary. Ratios use only known positive denominators.",
              "Proxy size is NOT verified market cap; size-peer label differences are NOT net account returns or causal alpha.", "",
              "Industry acquisition: " + record["industry_receipt"]["status"] + "."]
    if record["industry_receipt"].get("error"):
        lines.append("Industry gap: " + record["industry_receipt"]["error"])
    controls = record.get("peer_controls")
    if controls:
        context = controls["pool_context"]
        lines += ["", "## Leave-Target-Out Industry / Size Peers", "",
                  f"Industry response rows: {context['industry_response_rows']}; classification: {context['industry_classifications']}.",
                  f"Common pool nonpositive PE: {context['pool_nonpositive_pe_mean']:.2%}; PE coverage: {context['pool_pe_coverage']:.2%}.",
                  "All of each case's targets are removed from that day's peer pool. No-peer labels remain unknown, not zero.", "",
                  "|Case|Industry-peer label difference / complete anchors|Industry+size-peer difference / complete anchors|Min observed joint peers|",
                  "|---|---:|---:|---:|"]
        for row in controls["cases"]:
            lines.append(f"|{row['signal']}|{fmt(row['industry_peer_full32_difference'])} / {row['industry_peer_complete_anchors']}|"
                         f"{fmt(row['industry_size_peer_full32_difference'])} / {row['industry_size_peer_complete_anchors']}|"
                         f"{row['industry_size_peer_min_observed_peers']}|")
        lines += ["", "These full32 descriptive differences are not a traded neutral portfolio, causal attribution, statistical proof or a new strategy.",
                  "Some peer cells have only one observed stock. Joint industry/size controls are NOT complete on all32 anchors; missing rows are not averaged away.",
                  "Controls status: " + controls["status"] + "."]
    lines += ["", "Full-period security PnL is classified by first executed buy's decision; see first_entry_pnl_groups.csv, not dynamic risk attribution.",
              "", "## Boundaries", *["- " + value for value in LIMITS], "",
              "Official provider interface fields: [BaoStock package documentation](https://pypi.org/project/baostock/).",
              "Size-proxy formula is our declared diagnostic assumption; financial disclosure-time/units/industry revisions are not independently verified.",
              "", "Source funnel: " + record["source_funnel"], "Artifacts: " + record["artifacts"]]
    if record.get("error"):
        lines.append("Failure: " + record["error"])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(identifier, acquire_industry=False, industry_view=None):
    if acquire_industry and industry_view:
        raise ValueError("Acquire or reuse industry data, not both")
    source_path, source = read_source(identifier)
    primary = json.loads(Path(source["source_run"]).read_text(encoding="utf-8"))
    price = primary["protocol"]["stock_manifest"]
    root = Path(source["artifacts"])
    targets = {case["signal"]: pd.read_csv(root / case["signal"] / "actual_targets.csv", dtype={"date": str}) for case in source["cases"]}
    days = sorted(set(next(iter(targets.values())).date))
    if any(sorted(set(frame.date)) != days for frame in targets.values()):
        raise ValueError("All fixed models must have the same saved target calendar")
    pool = pd.read_parquet(source["cases"][0]["source_predictions"], columns=["datetime", "instrument"])
    pool["date"] = pd.to_datetime(pool.datetime).dt.strftime("%Y-%m-%d")
    pool = pool.loc[pool.date.isin(days), ["date", "instrument"]].rename(columns={"instrument": "symbol"})
    if pool.duplicated(["date", "symbol"]).any():
        raise ValueError("Saved model pool identities must be distinct")
    raw = pq.read_table(Path(price["raw_view"]) / "raw_daily.parquet", columns=["date", "code", "adjustflag", *FIELDS],
                        filters=[("date", "in", days)]).to_pandas()
    if not raw.adjustflag.eq("3").all():
        raise ValueError("Style proxy must use unadjusted quote/volume fields")
    raw["symbol"] = raw.code.map(lambda value: normalize_symbol(value).vt_symbol)
    pool = pool.merge(raw.drop(columns=["code", "adjustflag"]), on=["date", "symbol"], how="left", validate="one_to_one", indicator="raw_match")
    labels = pd.read_parquet(source["cached_labels"], filters=[("date", "in", list(pd.to_datetime(days)))])
    labels = labels.reset_index()
    labels["date"] = pd.to_datetime(labels.date).dt.strftime("%Y-%m-%d")
    labels[LABEL] = labels[LABEL].where(labels.exit_date.le(pd.Timestamp(source["end"])))
    pool = pool.merge(labels[["date", "symbol", LABEL]], on=["date", "symbol"], how="left", validate="one_to_one")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/style_exposure" / run_id, DATA_ROOT / "research/style_exposure" / run_id
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    record = {"id": run_id, "purpose": ID, "status": "running", "source_funnel": str(source_path), "artifacts": str(artifacts),
              "common_dates": source["common_anchors"]["dates"], "decision_dates": days, "raw_view": price["raw_view"],
              "cases": [], "industry_receipt": {"status": "not_acquired"}, "models_refit": False, "accounts_replayed": False,
              "execution_admitted": False, "fresh_holdout_claimed": False, "limits": LIMITS}
    write_json(out / "run.json", record)
    try:
        if acquire_industry:
            industry_view = artifacts / "industry_history"
            collect_industries(days, industry_view)
        industries, record["industry_receipt"] = load_industries(industry_view, days)
        styled, peers = [], []
        for day, values in pool.groupby("date", sort=True):
            frame, groups = style_day(values)
            styled.append(frame)
            peers.append(groups.assign(date=day))
        styles = pd.concat(styled, ignore_index=True).merge(industries, on=["date", "symbol"], how="left", validate="one_to_one")
        styles["industry"] = styles.industry.fillna("")
        styles.to_parquet(artifacts / "dated_style_pool.parquet", index=False)
        pd.concat(peers, ignore_index=True).to_csv(artifacts / "size_peer_labels.csv", index=False)
        for case in source["cases"]:
            signal = case["signal"]
            dest = artifacts / signal
            dest.mkdir()
            joined = targets[signal].merge(styles, on=["date", "symbol"], how="left", validate="one_to_one", suffixes=("", "_pool"))
            if not np.allclose(joined[LABEL], joined[LABEL + "_pool"], equal_nan=True) or joined.raw_match.isna().any():
                raise ValueError("Original target membership/labels changed")
            dates = pd.DataFrame([exposure_row(frame, day) for day, frame in joined.groupby("date")])
            common = dates.loc[dates.date.isin(record["common_dates"])]
            if len(common) != len(record["common_dates"]):
                raise ValueError("Missing common comparison anchor")
            buys = pd.read_csv(root / signal / "executed_buys.csv", dtype={"signal_date": str})
            if not buys.target_matched.all():
                raise ValueError("Buy style classification needs the original matched prior-close decision")
            purchases = buys.merge(styles.drop(columns=LABEL), left_on=["signal_date", "symbol"], right_on=["date", "symbol"],
                                   how="left", validate="many_to_one")
            purchases["size_bucket"] = purchases.size_bucket.fillna("unknown")
            purchases["industry"] = purchases.industry.fillna("")
            pnl = pd.read_csv(root / signal / "symbol_pnl.csv")
            if round(pnl.observed_pnl_cny.sum() * 100) != round(case["observed_account_pnl_cny"] * 100):
                raise ValueError("Original Ledger security PnL no longer matches source")
            classified, groups = first_entry_pnl(pnl, purchases)
            joined.to_csv(dest / "target_styles.csv", index=False)
            dates.to_csv(dest / "dated_target_exposure.csv", index=False)
            purchases.to_csv(dest / "executed_buy_styles.csv", index=False)
            classified.to_csv(dest / "first_entry_symbol_pnl.csv", index=False)
            groups.to_csv(dest / "first_entry_pnl_groups.csv", index=False)
            buy_groups = purchases.assign(industry=purchases.industry.replace("", "unknown")).groupby(["size_bucket", "industry"]).agg(
                orders=("order_id", "size"), notional_cny=("notional_cny", "sum")).reset_index()
            buy_groups.to_csv(dest / "buy_flow_groups.csv", index=False)
            summary = {"signal": signal, "common_anchor_means": common.mean(numeric_only=True).to_dict(),
                       "target_rows": len(joined), "raw_matched_target_rows": int(joined.raw_match.eq("both").sum()),
                       "size_known_target_rows": int(joined.size_proxy_percentile.notna().sum()),
                       "industry_known_target_rows": int(joined.industry.ne("").sum()), "buy_orders": len(purchases),
                       "first_entry_pnl_groups": groups.to_dict("records"), "original_net_pnl_cny": case["observed_account_pnl_cny"]}
            record["cases"].append(summary)
            write_json(dest / "summary.json", summary)
            write_json(out / "run.json", record)
            print(f"{signal}: style targets={len(joined)}, industry_known={summary['industry_known_target_rows']}, original PnL={summary['original_net_pnl_cny']}", flush=True)
        complete_industry = len({entry["date"] for entry in record["industry_receipt"]["entries"]} & set(days)) == len(days)
        record["status"] = "completed_dated_style_diagnostics" if complete_industry else "completed_dated_style_diagnostics_with_industry_gap"
    except Exception as error:
        record.update(status="failed_dated_style_diagnostics", error=f"{type(error).__name__}: {error}")
    finally:
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--funnel-run")
    source.add_argument("--peer-run", help="Add leave-target-out peers to an existing completed style run, with no new acquisition")
    parser.add_argument("--collect-industry", action="store_true")
    parser.add_argument("--industry-view", type=Path)
    args = parser.parse_args(argv)
    if args.peer_run:
        if args.collect_industry or args.industry_view:
            parser.error("Peer analysis reuses existing data, not new acquisition")
        _, record = run_peer_controls(args.peer_run)
    else:
        _, record = run(args.funnel_run, args.collect_industry, args.industry_view)
    return 0 if record["status"].startswith("completed_") else 1


if __name__ == "__main__":
    raise SystemExit(main())
