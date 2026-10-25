"""Quarterly provider snapshots with publication-date-lagged research views."""

import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import socket
import uuid

import numpy as np
import pandas as pd

from a_stock_committee.market_data import baostock_session, normalize_symbol
from ..config import StrategyConfig
from ..feed import HistoricalFeed
from ..market_dataset import provider_frame, write_frame, write_json
from ..settings import DATA_ROOT, ROOT
from .factors import research_eligibility
from .fetch_receipts import make_receipt


VALUES = ("roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare")
FIELDS = ("code", "pubDate", "statDate", *VALUES)
PERIODS = [(2022, 4), *[(year, quarter) for year in range(2023, 2026) for quarter in range(1, 5)], (2026, 1), (2026, 2)]


def normalize(frame, code, year, quarter):
    if set(frame.columns) != set(FIELDS):
        raise ValueError("Profitability source schema changed")
    expected = (pd.Period(f"{year}Q{quarter}", freq="Q").end_time.date()).isoformat()
    if (not frame.code.eq(code).all() or not frame.statDate.eq(expected).all()
            or frame.duplicated(["code", "statDate"]).any()):
        raise ValueError("Quarter identity conflict or unresolved revisions")
    result = frame.copy()
    for row in result.to_dict("records"):
        published = date.fromisoformat(row["pubDate"])
        if published.isoformat() != row["pubDate"] or published < date.fromisoformat(expected):
            raise ValueError("Missing/invalid publication date")
    for field in VALUES:
        values = pd.to_numeric(result[field].where(result[field].ne(""), np.nan), errors="raise")
        if np.isinf(values).any():
            raise ValueError("Non-finite profitability values")
        result[field] = values
    result["symbol"] = normalize_symbol(code).vt_symbol
    return result


def create(snapshot, folder=None, universe=None):
    snapshot = Path(snapshot).resolve()
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "ready":
        raise ValueError("Financial collection requires a pinned ready market snapshot")
    instruments = pd.read_csv(snapshot / "instruments.csv", dtype=str, keep_default_na=False)
    if set(instruments.symbol) != set(manifest["symbols"]) or instruments.symbol.duplicated().any():
        raise ValueError("Snapshot instrument scope changed")
    if universe is not None:
        universe = Path(universe).resolve()
        instruments = pd.read_csv(universe, dtype=str, keep_default_na=False)
        if not set(manifest["symbols"]) <= set(instruments.symbol) or instruments.symbol.duplicated().any():
            raise ValueError("Declared broader financial scope conflicts with the pinned snapshot")
    if not instruments.code.map(lambda code: normalize_symbol(code).vt_symbol).equals(instruments.symbol):
        raise ValueError("Financial source code/symbol mismatch")
    folder = Path(folder) if folder else DATA_ROOT / "research/financials" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    plan = {"snapshot": str(snapshot), "snapshot_sha256": sha256((snapshot / "manifest.json").read_bytes()).hexdigest(),
            "instruments_sha256": sha256((snapshot / "instruments.csv").read_bytes()).hexdigest(),
            "source": "BaoStock.query_profit_data", "periods": PERIODS, "fields": list(FIELDS),
            "symbols": instruments[["symbol", "code", "ipoDate", "outDate"]].to_dict("records"),
            "asof_end": manifest["end"], "revision_safe_pit_verified": False,
            "limitations": ["Publication dates have no time-of-day; use only on later exchange sessions",
                            "Retrieved values may include retrospective revisions; date lagging is not revision-safe PIT",
                            "Raw provider field units/definitions are not globally verified",
                            "Snapshot membership is not independently verified historical market membership"]}
    if universe is not None:
        plan.update(universe=str(universe), universe_sha256=sha256(universe.read_bytes()).hexdigest(),
                    scope="All declared provider-retained SH/SZ historical codes, not only acquired snapshot members")
    write_json(folder / "plan.json", plan)
    write_json(folder / "receipts.json", {"status": "collecting", "tasks": {}, "execution_admitted": False})
    return folder


def read_plan(folder):
    folder = Path(folder)
    plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    snapshot = Path(plan["snapshot"])
    for name, field in (("manifest.json", "snapshot_sha256"), ("instruments.csv", "instruments_sha256")):
        if sha256((snapshot / name).read_bytes()).hexdigest() != plan[field]:
            raise ValueError("Financial collection's pinned market input changed")
    if plan["source"] != "BaoStock.query_profit_data" or plan["periods"] != [list(p) for p in PERIODS]:
        raise ValueError("Financial collection definition changed")
    if plan.get("universe") and sha256(Path(plan["universe"]).read_bytes()).hexdigest() != plan["universe_sha256"]:
        raise ValueError("Financial universe source changed")
    return plan


def cached(folder, receipt):
    root = Path(folder).resolve()
    path = (root / receipt["file"]).resolve()
    path.relative_to(root)
    if sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
        raise ValueError("Quarterly provider receipt changed")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def import_probe(folder, probe):
    folder, probe = Path(folder), Path(probe)
    plan = read_plan(folder)
    state = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    source = json.loads((probe / "receipt.json").read_text(encoding="utf-8"))
    if source["status"] != "completed_probe":
        raise ValueError("Only completed financial feasibility receipts can be imported")
    by_code = {row["code"]: row["symbol"] for row in plan["symbols"]}
    for entry in source["entries"]:
        if "year" not in entry or "quarter" not in entry:
            continue
        code, year, quarter = entry["code"], entry["year"], entry["quarter"]
        if (code not in by_code or [year, quarter] not in plan["periods"]
                or entry["file"] != f"{code}_{year}Q{quarter}_profit.csv"):
            raise ValueError("Probe query is outside the pinned financial scope")
        frame = cached(probe, entry)
        if len(frame) != entry["rows"]:
            raise ValueError("Financial probe row count changed")
        normalize(frame, code, year, quarter)
        key = f"{by_code[code]}:{year}Q{quarter}"
        if key in state["tasks"]:
            if not cached(folder, state["tasks"][key]).equals(frame):
                raise ValueError("Probe conflicts with an existing quarterly source")
            continue
        relative = f"quarterly/{by_code[code]}_{year}Q{quarter}.csv"
        write_frame(folder / relative, frame)
        state["tasks"][key] = {"file": relative, "sha256": sha256((folder / relative).read_bytes()).hexdigest(),
                              "code": code, "year": year, "quarter": quarter, "rows": len(frame),
                              "status": "acquired" if len(frame) else "empty_provider_result",
                              "imported_from": {"folder": str(probe), **entry}}
    write_json(folder / "receipts.json", state)
    return state


def collection_counts(state, plan):
    complete = sum(all(f"{row['symbol']}:{year}Q{quarter}" in state["tasks"] for year, quarter in plan["periods"])
                   for row in plan["symbols"])
    return {"complete_symbols": complete, "expected_symbols": len(plan["symbols"]),
            "report_rows": sum(row["rows"] for row in state["tasks"].values()),
            "revision_safe_pit_verified": False}


def audit_collection(folder):
    """Audit an existing collection without resuming or contacting its source."""
    folder = Path(folder).resolve()
    plan = read_plan(folder)
    state = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    expected = {f"{row['symbol']}:{year}Q{quarter}"
                for row in plan["symbols"] for year, quarter in plan["periods"]}
    observed = set(state.get("tasks", {}))
    if not observed <= expected:
        raise ValueError("Financial collection contains a task outside its plan")
    invalid, status_counts = [], {}
    for key, receipt in state.get("tasks", {}).items():
        path = (folder / receipt["file"]).resolve()
        path.relative_to(folder)
        actual = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if actual != receipt.get("sha256"):
            invalid.append(key)
        status = receipt.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    complete = {row["symbol"] for row in plan["symbols"]
                if all(f"{row['symbol']}:{year}Q{quarter}" in observed
                       for year, quarter in plan["periods"])}
    missing = sorted(expected - observed)
    return {
        "schema": "financial_collection_audit_v1",
        "status": "COMPLETE_COLLECTION" if not missing and not invalid and state.get("status") == "complete_acquisition" else "PARTIAL_COLLECTION",
        "source_folder": str(folder), "source_status": state.get("status"),
        "expected_tasks": len(expected), "observed_tasks": len(observed), "missing_tasks": len(missing),
        "missing_task_examples": missing[:20], "complete_symbols": len(complete),
        "expected_symbols": len(plan["symbols"]), "invalid_files": invalid,
        "task_status_counts": status_counts, "network_accessed": False,
        "execution_admitted": False, "revision_safe_pit_verified": False,
        "limitations": ["This audit reads existing files only and does not repair or resume collection.",
                        "Provider retrieval versions are not historical disclosure versions."]}


def collect(folder, max_symbols=8):
    if not 1 <= max_symbols <= 64:
        raise ValueError("Invalid bounded financial batch size")
    folder = Path(folder)
    plan = read_plan(folder)
    state = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    pending = []
    for info in plan["symbols"]:
        tasks = [(f"{info['symbol']}:{year}Q{quarter}", year, quarter) for year, quarter in plan["periods"]]
        if any(key not in state["tasks"] for key, _, _ in tasks):
            pending.append((info, tasks))
    socket.setdefaulttimeout(30)
    key = None
    try:
        with baostock_session() as bs:
            for index, (info, tasks) in enumerate(pending[:max_symbols], 1):
                for key, year, quarter in tasks:
                    if key in state["tasks"]:
                        cached(folder, state["tasks"][key])
                        continue
                    frame = provider_frame(bs.query_profit_data(info["code"], year, quarter))
                    normalize(frame, info["code"], year, quarter)
                    relative = f"quarterly/{info['symbol']}_{year}Q{quarter}.csv"
                    write_frame(folder / relative, frame)
                    fetched_at = datetime.now().astimezone().isoformat()
                    completion = "COMPLETE" if len(frame) else "COMPLETE_EMPTY"
                    fetch_receipt = make_receipt(
                        {"code": info["code"], "year": year, "quarter": quarter},
                        source="baostock", upstream_source="baostock.query_profit_data",
                        adapter_version="financials.collect_v1",
                        requested_scope={"code": info["code"], "period": f"{year}Q{quarter}"},
                        fetched_at=fetched_at, raw_content_reference=None,
                        schema_fingerprint=sha256("|".join(FIELDS).encode("utf-8")).hexdigest(),
                        expected_partitions=[f"{year}Q{quarter}"],
                        completed_partitions=[f"{year}Q{quarter}"],
                        page_receipts=[{"partition": f"{year}Q{quarter}", "rows": len(frame)}],
                        completion_state=completion)
                    state["tasks"][key] = {"file": relative, "sha256": sha256((folder / relative).read_bytes()).hexdigest(),
                                          "code": info["code"], "year": year, "quarter": quarter,
                                          "rows": len(frame), "status": "acquired" if len(frame) else "empty_provider_result",
                                          "retrieved_at": fetched_at, "fetch_receipt": fetch_receipt}
                    state.update(status="collecting", last_error=None, failed_task=None)
                    write_json(folder / "receipts.json", state)
                    print(f"{index}/{min(max_symbols, len(pending))} {key} rows={len(frame)}", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        state.update(collection_counts(state, plan))
        state.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "stopped_source_error",
                     failed_task=key, last_error=f"{type(error).__name__}: {error}")
        write_json(folder / "receipts.json", state)
        raise
    state.update(collection_counts(state, plan))
    state.update(status="complete_acquisition" if state["complete_symbols"] == len(plan["symbols"]) else "partial_acquisition",
                 last_error=None, failed_task=None)
    write_json(folder / "receipts.json", state)
    return state


def asof_panel(reports, calendar, symbols, max_age_days=400):
    """Date-lag only; never infer a missing publication time or a revision history."""
    if calendar != sorted(set(calendar)) or not calendar or max_age_days < 1:
        raise ValueError("Invalid as-of calendar/age limit")
    if reports.duplicated(["symbol", "statDate"]).any():
        raise ValueError("Unresolved same-quarter financial revisions")
    blocks = []
    for symbol in symbols:
        source = reports.loc[reports.symbol.eq(symbol)].sort_values(["pubDate", "statDate"])
        rows, latest = [], None
        records = iter(source.to_dict("records"))
        next_report = next(records, None)
        for day in calendar:
            while next_report is not None and next_report["pubDate"] < day:
                if latest is None or next_report["statDate"] > latest["statDate"]:
                    latest = next_report
                next_report = next(records, None)
            available = (latest is not None and (date.fromisoformat(day) - date.fromisoformat(latest["statDate"])).days <= max_age_days)
            row = {"date": day, "symbol": symbol, "source_pub_date": latest["pubDate"] if available else "",
                   "source_stat_date": latest["statDate"] if available else ""}
            row.update({field + "_provider_value": latest[field] if available else np.nan for field in VALUES})
            rows.append(row)
        blocks.append(pd.DataFrame(rows))
    return pd.concat(blocks, ignore_index=True) if blocks else pd.DataFrame()


def build_panel(folder):
    folder = Path(folder)
    plan = read_plan(folder)
    state = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    frames, symbols = [], []
    for info in plan["symbols"]:
        keys = [f"{info['symbol']}:{year}Q{quarter}" for year, quarter in plan["periods"]]
        if not all(key in state["tasks"] for key in keys):
            continue
        symbols.append(info["symbol"])
        for key in keys:
            receipt = state["tasks"][key]
            frame = cached(folder, receipt)
            if len(frame) != receipt["rows"]:
                raise ValueError("Quarterly receipt row count changed")
            frames.append(normalize(frame, receipt["code"], receipt["year"], receipt["quarter"]))
    if not symbols:
        raise ValueError("No fully acquired financial histories; do not silently make a partial-period view")
    reports = pd.concat(frames, ignore_index=True)
    calendar_path = Path(plan["snapshot"]) / "calendar.csv"
    calendar = pd.read_csv(calendar_path, dtype=str).date.tolist()
    panel = asof_panel(reports, calendar, symbols)
    destination = folder / "views" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, exist_ok=False)
    panel.to_parquet(destination / "publication_lagged.parquet", index=False)
    write_json(destination / "source_receipts.json", state)
    write_json(destination / "source_plan.json", plan)
    summary = {"status": "completed_publication_lagged_provider_view", "symbols": len(symbols), "report_rows": len(reports),
               "declared_symbols": len(plan["symbols"]), "symbols_with_reports": int(reports.symbol.nunique()),
               "empty_quarter_results": sum(state["tasks"][f"{symbol}:{year}Q{quarter}"]["rows"] == 0
                                            for symbol in symbols for year, quarter in plan["periods"]),
               "daily_rows": len(panel), "rows_with_available_report": int(panel.source_pub_date.ne("").sum()),
               "available_by_field": {field: int(panel[field + "_provider_value"].notna().sum()) for field in VALUES},
               "source_receipts_sha256": sha256((folder / "receipts.json").read_bytes()).hexdigest(),
               "calendar_sha256": sha256(calendar_path.read_bytes()).hexdigest(),
               "panel_sha256": sha256((destination / "publication_lagged.parquet").read_bytes()).hexdigest(),
               "publication_policy": "pubDate strictly earlier than decision session; no same-day use",
               "stale_after_stat_date_days": 400, "revision_safe_pit_verified": False,
               "execution_admitted": False, "strategy_validated": False, "limitations": plan["limitations"]}
    write_json(destination / "result.json", summary)
    return destination, summary


def align_market(view, snapshot, config=None):
    """Preserve the financial grid but mask features outside the existing pool."""
    view, snapshot = Path(view), Path(snapshot).resolve()
    result = json.loads((view / "result.json").read_text(encoding="utf-8"))
    source = view / "publication_lagged.parquet"
    if (result["status"] != "completed_publication_lagged_provider_view"
            or sha256(source.read_bytes()).hexdigest() != result["panel_sha256"]):
        raise ValueError("Financial view source changed")
    panel = pd.read_parquet(source)
    panel["date"] = pd.to_datetime(panel.date)
    if panel.duplicated(["date", "symbol"]).any() or len(panel) != result["daily_rows"]:
        raise ValueError("Financial grid identity/count conflict")
    available = panel.source_pub_date.ne("")
    if not (pd.to_datetime(panel.loc[available, "source_pub_date"]) < panel.loc[available, "date"]).all():
        raise ValueError("Financial view contains same-day/future publications")
    feed = HistoricalFeed(snapshot)
    if feed.folder.resolve() != snapshot or set(panel.date.dt.strftime("%Y-%m-%d")) != set(feed.calendar):
        raise ValueError("Financial/market snapshot calendar or path mismatch")
    config = (config or StrategyConfig()).validate()
    eligible = research_eligibility(feed, config).rename("research_eligible")
    raw = feed.raw[["date", "symbol", "close", "volume", "amount", "tradestatus", "isST"]].copy()
    raw["date"] = pd.to_datetime(raw.date)
    raw = raw.rename(columns={"close": "raw_close_rmb", "volume": "raw_volume_shares", "amount": "raw_amount_rmb"})
    raw["has_market_observation"] = True
    joined = panel.merge(raw, on=["date", "symbol"], how="left", validate="one_to_one")
    joined = joined.merge(eligible.reset_index(), on=["date", "symbol"], how="left", validate="one_to_one")
    joined["has_market_observation"] = joined.has_market_observation.eq(True)
    joined["research_eligible"] = joined.research_eligible.eq(True)
    joined["ipo_date"] = joined.symbol.map({s: i["ipoDate"] for s, i in feed.instruments.items()})
    joined["out_date"] = joined.symbol.map({s: i.get("outDate", "") for s, i in feed.instruments.items()})
    joined["report_available"] = joined.source_pub_date.ne("")
    joined["usable_for_factor_research"] = joined.research_eligible & joined.report_available
    fields = [name + "_provider_value" for name in VALUES]
    joined.loc[~joined.research_eligible, fields] = np.nan
    destination = view / "market_alignment" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, exist_ok=False)
    joined.to_parquet(destination / "aligned.parquet", index=False)
    summary = {"status": "completed_financial_market_alignment", "financial_view": str(view.resolve()),
               "financial_panel_sha256": result["panel_sha256"], "market_snapshot": str(snapshot),
               "market_input_sha256": {name: sha256((snapshot / name).read_bytes()).hexdigest()
                                       for name in ("manifest.json", "raw.parquet", "adjusted.parquet", "instruments.csv", "calendar.csv", "actions.csv")},
               "daily_rows": len(joined), "symbols": int(joined.symbol.nunique()),
               "rows_without_market_observation": int((~joined.has_market_observation).sum()),
               "research_eligible_rows": int(joined.research_eligible.sum()),
               "available_reports_outside_pool": int((joined.report_available & ~joined.research_eligible).sum()),
               "rows_usable_for_factor_research": int(joined.usable_for_factor_research.sum()),
               "usable_by_field": {field: int(joined[field + "_provider_value"].notna().sum()) for field in VALUES},
               "eligibility": {"consumer": "factors.research_eligibility", "min_listing_days": config.min_listing_days,
                               "min_amount_rmb": config.min_amount, "liquidity_sessions": 20,
                               "selection_date": feed.manifest["selection_date"]},
               "aligned_sha256": sha256((destination / "aligned.parquet").read_bytes()).hexdigest(),
               "revision_safe_pit_verified": False, "execution_admitted": False, "strategy_validated": False,
               "limitations": result["limitations"] + ["Research pool is not next-session order/account eligibility",
                                                        "Missing market/status rows are retained, never imputed",
                                                        "No labels, financial-factor selection or performance comparison"]}
    write_json(destination / "result.json", summary)
    return destination, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--universe", type=Path)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-symbols", type=int, default=8)
    parser.add_argument("--build-view", action="store_true")
    parser.add_argument("--align-market", action="store_true")
    parser.add_argument("--market-snapshot", type=Path)
    parser.add_argument("--import-probe", type=Path)
    parser.add_argument("--audit", type=Path, help="Audit an existing collection without network access")
    args = parser.parse_args(argv)
    if args.audit:
        result = audit_collection(args.audit)
        destination = ROOT / "outputs/financials" / args.audit.name / "collection_audit.json"
        write_json(destination, result)
        print(json.dumps({"out": str(destination), **{key: result[key] for key in ("status", "missing_tasks", "complete_symbols", "network_accessed")}}, ensure_ascii=False))
        return 0 if result["status"] == "COMPLETE_COLLECTION" else 2
    if bool(args.snapshot) == bool(args.folder):
        parser.error("Specify exactly one pinned snapshot or existing collection folder")
    if args.folder and args.universe:
        parser.error("A resumed financial universe cannot be changed")
    if args.align_market and not args.build_view:
        parser.error("Market alignment requires a newly built immutable financial view")
    if args.market_snapshot and not args.align_market:
        parser.error("--market-snapshot is only used by --align-market")
    folder = args.folder if args.folder else create(args.snapshot, universe=args.universe)
    print("COLLECTION " + str(folder), flush=True)
    if args.import_probe:
        import_probe(folder, args.import_probe)
    if args.execute:
        state = collect(folder, args.max_symbols)
        print(json.dumps({key: value for key, value in state.items() if key != "tasks"}), flush=True)
    if args.build_view:
        view, summary = build_panel(folder)
        print(json.dumps({"view": str(view), **summary}), flush=True)
        if args.align_market:
            snapshot = args.market_snapshot or Path(read_plan(folder)["snapshot"])
            aligned, summary = align_market(view, snapshot)
            print(json.dumps({"market_alignment": str(aligned), **summary}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
