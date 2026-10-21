"""Prepare actions-only queries for every pinned raw code, retaining old code history."""
import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid

import pandas as pd
import pyarrow.parquet as pq

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from ..market_dataset import load_receipt, normalize_symbol, summarize, task_complete, write_frame, write_json
from ..settings import DATA_ROOT
from .adjusted_prices import checked_file
from .daily_source_probe import WHEEL_SHA256


def validate_seed(frame, code, year):
    if not {"code", "dividOperateDate"} <= set(frame) or not frame.code.eq(code).all():
        raise ValueError("Action source code/schema mismatch")
    if any(date.fromisoformat(day).year != year for day in frame.dividOperateDate):
        raise ValueError("Action implementation dates differ from the queried year")


def prepare(raw_view, destination=None, *, reuse_root=None, probe=None):
    raw_view = Path(raw_view)
    raw_result = json.loads((raw_view / "result.json").read_text(encoding="utf-8"))
    if (raw_result["kind"] != "stock" or raw_result["status"] != "source_cache_complete"
            or raw_result["missing_days"] or raw_result["empty_days"]
            or raw_result["acquired_days"] != raw_result["expected_days"]):
        raise ValueError("Complete pinned raw stock scope required")
    raw_path = checked_file(raw_view, "raw_daily.parquet", raw_result["raw_daily_sha256"])
    raw_plan = json.loads(checked_file(raw_view, "plan.json", raw_result["plan_sha256"]).read_text(encoding="utf-8"))
    calendar_path = checked_file(raw_view, "input_calendar.csv", raw_plan["calendar_sha256"])
    calendar = pd.read_csv(calendar_path, dtype=str).date.tolist()
    codes, days, rows = set(), [], 0
    source = pq.ParquetFile(raw_path)
    for index in range(source.metadata.num_row_groups):
        frame = source.read_row_group(index, columns=["code", "date"]).to_pandas()
        if frame.empty or frame.date.nunique() != 1 or frame.code.duplicated().any():
            raise ValueError("Distinct-code nonempty raw source day required")
        codes.update(frame.code)
        days.append(frame.date.iloc[0])
        rows += len(frame)
    if (rows != raw_result["rows"] or len(codes) != raw_result["symbols"] or sorted(days) != calendar
            or calendar != sorted(set(calendar))):
        raise ValueError("Complete raw row/code/calendar identities changed")
    folder = Path(destination) if destination else DATA_ROOT / "research/stock_action_bulk" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, folder / "scope_implementation.py")
    shutil.copy2(calendar_path, folder / "calendar.csv")
    universe = pd.DataFrame({"code": sorted(codes)})
    universe["symbol"] = universe.code.map(lambda code: normalize_symbol(code).vt_symbol)
    universe["board"], universe["status"] = "", ""
    write_frame(folder / "universe.csv", universe)
    start, end = raw_plan["requested_start"], raw_plan["requested_end"]
    years = list(range(int(start[:4]), int(end[:4]) + 1))
    plan = {"source": "BaoStock", "start": start, "end": end, "fields": BAOSTOCK_FIELDS,
            "summary_stage": "actions", "symbols": len(codes), "expected_queries": len(codes) * len(years),
            "universe_sha256": sha256((folder / "universe.csv").read_bytes()).hexdigest(),
            "calendar_sha256": sha256((folder / "calendar.csv").read_bytes()).hexdigest(),
            "raw_view": str(raw_view), "raw_daily_sha256": raw_result["raw_daily_sha256"],
            "scope": "every_observed_raw_provider_code_including_old_and_new_codes_not_PIT_membership",
            "scope_implementation_sha256": sha256((folder / "scope_implementation.py").read_bytes()).hexdigest(),
            "limitations": ["Actions-only source cache; complete query coverage is not complete economic-event verification",
                            "Implementation-year responses retained, including dates after the raw cutoff; consumers must enforce their cutoff",
                            "Old and new source codes remain separate; economic identity and duplicate-event reconciliation are consumers",
                            "Closed-year cache reuse is not a fresh query or independently verified vendor revision/PIT history",
                            "No company-action credit, feed admission, default dataset, model or account change"]}
    write_json(folder / "collection_plan.json", plan)
    imported = []

    def seed(frame, code, year, path, retrieved_at, provenance, sdk=None):
        validate_seed(frame, code, year)
        symbol = normalize_symbol(code).vt_symbol
        frame = frame.copy()
        if "symbol" in frame and not frame.symbol.eq(symbol).all():
            raise ValueError("Seed symbol/code identity mismatch")
        frame["symbol"] = symbol
        relative = f"actions/{symbol}/{year}.csv"
        receipt = load_receipt(folder, symbol)
        key = f"actions_{year}"
        if key in receipt["tasks"]:
            prior = pd.read_csv(folder / relative, dtype=str, keep_default_na=False)
            if set(prior) != set(frame) or not prior.sort_values(list(prior)).reset_index(drop=True).equals(
                    frame[list(prior)].sort_values(list(prior)).reset_index(drop=True)):
                raise ValueError(f"Overlapping action seeds conflict; preserve both sources: {code} {year}")
            return
        write_frame(folder / relative, frame)
        receipt["tasks"][key] = {"path": relative, "retrieved_at": retrieved_at, "quality": {"rows": len(frame)},
                                 "sha256": sha256((folder / relative).read_bytes()).hexdigest(), "source_sdk": sdk,
                                 "origin": provenance, "original_file": str(path),
                                 "original_sha256": sha256(path.read_bytes()).hexdigest()}
        write_json(folder / "receipts" / (symbol + ".json"), receipt)
        imported.append({"code": code, "year": year, "origin": provenance, "rows": len(frame),
                         "original_file": str(path), "original_sha256": receipt["tasks"][key]["original_sha256"]})

    if reuse_root:
        reuse_root = Path(reuse_root)
        for item in universe.itertuples(index=False):
            receipt = load_receipt(reuse_root, item.symbol)
            for year in years:
                key, relative = f"actions_{year}", f"actions/{item.symbol}/{year}.csv"
                entry = receipt["tasks"].get(key)
                # Current-year legacy queries cannot establish the extended cutoff.
                if (year >= int(end[:4]) or not entry or not entry.get("retrieved_at")
                        or date.fromisoformat(entry["retrieved_at"][:10]) <= date(year, 12, 31)
                        or not task_complete(reuse_root, receipt, (key, relative, year))):
                    continue
                path = reuse_root / relative
                frame = pd.read_csv(path, dtype=str, keep_default_na=False)
                if len(frame) != entry["quality"]["rows"]:
                    raise ValueError("Legacy action receipt row count changed")
                seed(frame, item.code, year, path, entry["retrieved_at"], "closed_year_legacy_cache", entry.get("source_sdk"))
    if probe:
        probe = Path(probe)
        receipt = json.loads((probe / "receipt.json").read_text(encoding="utf-8"))
        if (receipt["status"] != "completed_probe" or receipt["sdk"]["sha256"] != WHEEL_SHA256
                or receipt["method"] != "query_dividend_data" or receipt["year_type"] != "operate"
                or date.fromisoformat(receipt["finished_at"][:10]) <= date.fromisoformat(end)):
            raise ValueError("A completed fresh reviewed implementation-year probe is required")
        checked_file(probe, "implementation.py", receipt["implementation_sha256"])
        for entry in receipt["entries"]:
            if entry["code"] not in codes or entry["year"] not in years or entry["error_code"] != "0":
                raise ValueError("Probe seed outside full code/year scope or unsuccessful")
            path = checked_file(probe, entry["file"], entry["sha256"])
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            if len(frame) != entry["rows"]:
                raise ValueError("Probe action row count changed")
            seed(frame, entry["code"], entry["year"], path, receipt["finished_at"], "fresh_reviewed_probe", receipt["sdk"])
    write_json(folder / "seed_sources.json", imported)
    result = summarize(folder)
    return folder, result


def pin(root, destination=None):
    root = Path(root)
    plan = json.loads((root / "collection_plan.json").read_text(encoding="utf-8"))
    if plan.get("summary_stage") != "actions":
        raise ValueError("An explicitly pinned actions-only root is required")
    raw_view = Path(plan["raw_view"])
    raw_path = checked_file(raw_view, "raw_daily.parquet", plan["raw_daily_sha256"])
    source = pq.ParquetFile(raw_path)
    codes = set()
    for index in range(source.metadata.num_row_groups):
        codes.update(source.read_row_group(index, columns=["code"]).column("code").unique().to_pylist())
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False)
    if (set(universe.code) != codes or len(universe) != len(codes) or len(codes) != plan["symbols"]
            or not universe.symbol.eq(universe.code.map(lambda value: normalize_symbol(value).vt_symbol)).all()):
        raise ValueError("Action source view cannot publish a reduced or relabelled raw code scope")
    frames, coverage, receipts = [], [], {}
    years = range(int(plan["start"][:4]), int(plan["end"][:4]) + 1)
    for item in universe.itertuples(index=False):
        receipt = load_receipt(root, item.symbol)
        receipts[item.symbol] = receipt
        for year in years:
            key, relative = f"actions_{year}", f"actions/{item.symbol}/{year}.csv"
            queried = task_complete(root, receipt, (key, relative, year))
            entry = receipt["tasks"].get(key) if queried else None
            row = {"code": item.code, "symbol": item.symbol, "year": year, "queried": queried,
                   "rows": None, "empty_response": None, "source_file": None, "source_sha256": None}
            if queried:
                if not entry.get("sha256"):
                    raise ValueError("Source action views require pinned per-query payload hashes")
                frame = pd.read_csv(root / relative, dtype=str, keep_default_na=False)
                validate_seed(frame, item.code, year)
                if len(frame) != entry["quality"]["rows"] or not frame.symbol.eq(item.symbol).all():
                    raise ValueError("Source action count/symbol changed")
                row.update(rows=len(frame), empty_response=frame.empty, source_file=relative, source_sha256=entry["sha256"])
                if not frame.empty:
                    frame["_cache_file"] = relative
                    frames.append(frame)
            coverage.append(row)
    coverage_frame = pd.DataFrame(coverage)
    queried = int(coverage_frame.queried.sum())
    complete = int(coverage_frame.groupby("code").queried.all().sum())
    if len(coverage) != plan["expected_queries"]:
        raise ValueError("Full code/year query matrix changed")
    folder = Path(destination) if destination else root / "views" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    for path, name in ((Path(__file__), "implementation.py"), (root / "collection_plan.json", "plan.json"),
                       (root / "universe.csv", "input_universe.csv"), (root / "calendar.csv", "input_calendar.csv")):
        shutil.copy2(path, folder / name)
    write_json(folder / "input_receipts.json", receipts)
    write_frame(folder / "coverage.csv", coverage_frame)
    panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["code", "symbol", "dividOperateDate", "_cache_file"])
    write_frame(folder / "reported_actions.parquet", panel)
    result = {"status": "source_actions_complete" if queried == len(coverage) else "source_actions_partial",
              "root": str(root), "raw_view": str(raw_view), "raw_daily_sha256": plan["raw_daily_sha256"],
              "expected_codes": len(codes), "complete_codes": complete, "expected_queries": len(coverage),
              "queried": queried, "unqueried": len(coverage) - queried, "rows": len(panel),
              "empty_responses": sum(row["empty_response"] is True for row in coverage),
              "duplicate_source_event_rows": int(panel.duplicated(["symbol", "dividOperateDate"], keep=False).sum()),
              "post_cutoff_reported_rows": int(panel.dividOperateDate.gt(plan["end"]).sum()),
              "panel_file": "reported_actions.parquet", "panel_sha256": sha256((folder / "reported_actions.parquet").read_bytes()).hexdigest(),
              "plan_sha256": sha256((folder / "plan.json").read_bytes()).hexdigest(),
              "coverage_sha256": sha256((folder / "coverage.csv").read_bytes()).hexdigest(),
              "input_receipts_sha256": sha256((folder / "input_receipts.json").read_bytes()).hexdigest(),
              "implementation_sha256": sha256((folder / "implementation.py").read_bytes()).hexdigest(),
              "execution_admitted": False, "cash_share_actions_verified": False,
              "limitations": plan["limitations"] + ["All duplicate source rows preserved; no economic-event reconciliation or account credit"]}
    write_json(folder / "result.json", result)
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-view", type=Path, required=True)
    parser.add_argument("--reuse-root", type=Path)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = prepare(args.raw_view, args.out, reuse_root=args.reuse_root, probe=args.probe)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0


def pin_main(argv=None):
    parser = argparse.ArgumentParser(description="Pin all reported action sources and the complete remaining query matrix")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = pin(args.root, args.out)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0
