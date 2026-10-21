"""Resumable recent Shanghai/Shenzhen market facts, separate from admitted trading feeds."""
import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import socket
import time
import uuid

import numpy as np
import pandas as pd

from a_stock_committee.market_data import BAOSTOCK_FIELDS, baostock_session, normalize_symbol
from .prepare_data import checked
from .settings import DATA_ROOT

DEFAULT_ROOT = DATA_ROOT / "datasets" / "ashare_2023_20260924"
START, END = "2023-01-01", "2026-09-24"


def stamp():
    return datetime.now().astimezone().isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def write_frame(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    if path.suffix == ".parquet":
        frame.to_parquet(temporary, index=False)
    else:
        frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def provider_frame(result):
    frame = checked(result)
    # BaoStock get_data() drops its declared fields for successful empty results.
    if frame.empty and not len(frame.columns):
        frame = pd.DataFrame(columns=result.fields)
    return frame


def select_universe(basic, start, end):
    """Retain companies that existed during the interval, including later delistings."""
    frame = basic.fillna("").copy()
    required = {"code", "code_name", "ipoDate", "outDate", "type", "status"}
    if not required <= set(frame) or frame.code.duplicated().any():
        raise ValueError("Invalid provider instrument master")
    ordinary = frame.type.eq("1") & frame.code.str.fullmatch(r"(?:sh\.(?:60\d{4}|688\d{3})|sz\.(?:00\d{4}|30\d{4}))")
    frame["exclusion_reason"] = "outside_supported_sh_sz_ordinary_A"
    frame.loc[ordinary, "exclusion_reason"] = ""
    frame.loc[ordinary & frame.ipoDate.gt(end), "exclusion_reason"] = "listed_after_end"
    frame.loc[ordinary & frame.outDate.ne("") & frame.outDate.lt(start), "exclusion_reason"] = "delisted_before_start"
    selected = frame.loc[frame.exclusion_reason.eq("")].copy()
    selected["symbol"] = selected.code.map(lambda value: normalize_symbol(value).vt_symbol)
    selected["board"] = selected.code.map(lambda c: "star" if c.startswith("sh.688") else "chinext" if c.startswith("sz.30") else "main")
    selected["listing_date_known"] = selected.ipoDate.ne("")
    # Interleave exchange/board/status for acquisition only, never for portfolio selection.
    selected["queue_group"] = selected.code.str[:2] + ":" + selected.board + ":" + selected.status
    selected = selected.sort_values("code")
    selected["queue_rank"] = selected.groupby("queue_group").cumcount()
    selected = selected.sort_values(["queue_rank", "queue_group", "code"]).reset_index(drop=True)
    return selected, frame.loc[frame.exclusion_reason.ne("")].copy()


def discover(root=DEFAULT_ROOT, start=START, end=END):
    root = Path(root)
    start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
    if start > end:
        raise ValueError("Start must not follow end")
    path = root / "collection_plan.json"
    if path.exists():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if (plan["start"], plan["end"], plan["fields"]) != (start, end, BAOSTOCK_FIELDS):
            raise ValueError("Use a new folder for a changed data request")
        return plan
    root.mkdir(parents=True, exist_ok=True)
    socket.setdefaulttimeout(30)
    with baostock_session() as bs:
        print("Discovering the provider instrument master (includes delisted codes)", flush=True)
        basic = provider_frame(bs.query_stock_basic())
        calendar = provider_frame(bs.query_trade_dates(start_date=start, end_date=end))
    universe, excluded = select_universe(basic, start, end)
    sessions = calendar.loc[calendar.is_trading_day.eq("1"), ["calendar_date"]].rename(columns={"calendar_date": "date"})
    if universe.empty or sessions.empty or sessions.date.duplicated().any():
        raise ValueError("Empty or inconsistent discovery response")
    write_frame(root / "provider_instruments.csv", basic)
    write_frame(root / "universe.csv", universe)
    write_frame(root / "excluded_instruments.csv", excluded)
    write_frame(root / "calendar.csv", sessions)
    plan = {"version": 1, "source": "BaoStock", "created_at": stamp(), "start": start, "end": end,
            "last_calendar_session": str(sessions.date.max()), "fields": BAOSTOCK_FIELDS,
            "symbols": len(universe), "delisted_at_retrieval": int(universe.status.eq("0").sum()),
            "board_counts": universe.board.value_counts().to_dict(),
            "scope": "Provider-retained SH/SZ ordinary A codes whose listing interval overlaps the request",
            "limitations": ["Beijing exchange and CDR are outside this provider adapter",
                            "Instrument names/status are retrieval-time metadata, not point-in-time features",
                            "Historical ST and suspension flags come from each daily bar",
                            "No complete market-cap, industry history, financial PIT or delisting settlement data",
                            "Provider coverage is not independently exchange-reconciled",
                            "Research cache only; no automatic promotion to trading or live signals"]}
    write_json(path, plan)
    return plan


def bar_quality(frame, instrument, calendar, flag):
    expected_fields = set(BAOSTOCK_FIELDS.split(","))
    if not expected_fields <= set(frame):
        raise ValueError("Daily schema changed")
    if not frame.empty and (not frame.code.eq(instrument["code"]).all()
                            or not frame.adjustflag.astype(str).eq(flag).all()
                            or frame.date.duplicated().any() or not frame.date.isin(calendar).all()):
        raise ValueError("Mismatched symbol, adjustment, duplicate date or off-calendar bar")
    active = pd.to_numeric(frame.tradestatus, errors="coerce").eq(1)
    prices = frame[["open", "high", "low", "close", "preclose"]].apply(pd.to_numeric, errors="coerce")
    bad_prices = active & (~np.isfinite(prices).all(axis=1) | prices.le(0).any(axis=1)
        | prices.high.lt(prices[["open", "close", "low"]].max(axis=1) - 1e-7)
        | prices.low.gt(prices[["open", "close", "high"]].min(axis=1) + 1e-7))
    bad_status = ~frame.tradestatus.isin(["0", "1"]) | ~frame.isST.isin(["0", "1"])
    amounts = frame[["volume", "amount"]].apply(pd.to_numeric, errors="coerce")
    bad_activity = active & (~np.isfinite(amounts).all(axis=1) | amounts.lt(0).any(axis=1))
    expected = [d for d in calendar if (not instrument["ipoDate"] or d >= instrument["ipoDate"])
                and (not instrument["outDate"] or d <= instrument["outDate"])]
    missing = sorted(set(expected) - set(frame.date))
    return {"rows": len(frame), "first_date": str(frame.date.min()) if len(frame) else None,
            "last_date": str(frame.date.max()) if len(frame) else None,
            "missing_calendar_dates": missing, "invalid_price_dates": frame.loc[bad_prices, "date"].tolist(),
            "invalid_status_dates": frame.loc[bad_status, "date"].tolist(),
            "invalid_activity_dates": frame.loc[bad_activity, "date"].tolist(),
            "missing_listing_date": not bool(instrument["ipoDate"]),
            "coverage_interpretation": "Calendar/listing-interval discrepancy; not imputed or assumed tradable"}


def tasks_for(plan, symbol, stage="all"):
    tasks = []
    if stage in {"all", "bars"}:
        tasks += [(kind, f"{kind}/{symbol}.parquet", None) for kind in ("raw", "adjusted")]
    if stage in {"all", "actions"}:
        tasks += [(f"actions_{year}", f"actions/{symbol}/{year}.csv", year)
                  for year in range(int(plan["start"][:4]), int(plan["end"][:4]) + 1)]
    return tasks


def prepare_hs300_priority(root=DEFAULT_ROOT):
    root = Path(root)
    folder = root / "universes" / "hs300_20231229"
    manifest_path = folder / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    socket.setdefaulttimeout(30)
    with baostock_session() as bs:
        members = provider_frame(bs.query_hs300_stocks(date="2023-12-29"))
    if len(members) != 300 or members.code.duplicated().any() or members.updateDate.eq("").any():
        raise ValueError("Historical CSI300 membership response incomplete")
    if pd.to_datetime(members.updateDate).gt(pd.Timestamp("2023-12-29")).any():
        raise ValueError("Membership contains post-selection information")
    members["symbol"] = members.code.map(lambda code: normalize_symbol(code).vt_symbol)
    main = members.loc[members.code.str.fullmatch(r"(?:sh\.60\d{4}|sz\.00\d{4})")].sort_values("code")
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False)
    missing = sorted(set(main.symbol) - set(universe.symbol))
    if missing:
        raise ValueError(f"Historical main-board members missing from provider master: {missing}")
    write_frame(folder / "all_members.csv", members)
    write_frame(folder / "mainboard_priority.csv", main)
    write_frame(folder / "other_boards.csv", members.loc[~members.symbol.isin(main.symbol)])
    record = {"source": "BaoStock.query_hs300_stocks", "requested_date": "2023-12-29",
              "provider_update_dates": sorted(members.updateDate.unique().tolist()), "retrieved_at": stamp(),
              "members": len(members), "mainboard_members": len(main),
              "priority_file": str((folder / "mainboard_priority.csv").resolve()),
              "scope": "Fixed pre-2024 CSI300 main-board subset, not full CSI300 or all A-shares",
              "selection_uses_current_listing_status_or_returns": False,
              "exchange_independently_verified": False,
              "full_market_backfill_target_unchanged": True}
    write_json(manifest_path, record)
    return record


def prioritized(universe, priority_file=None):
    if priority_file is None:
        return universe.to_dict("records")
    priority = pd.read_csv(priority_file, dtype=str, keep_default_na=False)
    if "symbol" not in priority or priority.symbol.duplicated().any() or priority.symbol.eq("").any():
        raise ValueError("Priority file needs distinct nonempty symbol values")
    missing = sorted(set(priority.symbol) - set(universe.symbol))
    if missing:
        raise ValueError(f"Priority symbols outside discovery universe: {missing}")
    ranks = {symbol: index for index, symbol in enumerate(priority.symbol)}
    records = universe.to_dict("records")
    # Stable order retains every other target, including delisted companies.
    return sorted(records, key=lambda item: ranks.get(item["symbol"], len(ranks)))


def load_receipt(root, symbol):
    path = root / "receipts" / f"{symbol}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"symbol": symbol, "tasks": {}}


def task_complete(root, receipt, task):
    key, relative, _ = task
    entry = receipt["tasks"].get(key)
    complete = bool(entry and entry["path"] == relative and (root / relative).exists())
    if complete and entry.get("sha256") and sha256((root / relative).read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError(f"Cached task hash changed; preserve and reconcile before retry: {relative}")
    return complete


def summarize(root=DEFAULT_ROOT, error=None):
    root = Path(root)
    plan = json.loads((root / "collection_plan.json").read_text(encoding="utf-8"))
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False)
    rows = []
    for item in universe.to_dict("records"):
        receipt = load_receipt(root, item["symbol"])
        tasks = tasks_for(plan, item["symbol"], plan.get("summary_stage", "all"))
        present = [task for task in tasks if task_complete(root, receipt, task)]
        raw_task = next((task for task in tasks if task[0] == "raw"), None)
        raw = receipt["tasks"].get("raw", {}) if raw_task and task_complete(root, receipt, raw_task) else {}
        quality = raw.get("quality", {})
        rows.append({"symbol": item["symbol"], "board": item["board"], "status_at_retrieval": item["status"],
                     "completed_tasks": len(present), "expected_tasks": len(tasks),
                     "raw_rows": quality.get("rows", 0), "first_date": quality.get("first_date"),
                     "last_date": quality.get("last_date"),
                     "quality_issue_count": int(quality.get("missing_listing_date", False)) + sum(
                         len(quality.get(k, [])) for k in ("missing_calendar_dates", "invalid_price_dates",
                                                         "invalid_status_dates", "invalid_activity_dates")),
                     "raw_available": any(t[0] == "raw" for t in present),
                     "adjusted_available": any(t[0] == "adjusted" for t in present),
                     "action_queries": sum(t[2] is not None for t in present),
                     "action_rows": sum(receipt["tasks"][t[0]]["quality"]["rows"] for t in present if t[2] is not None)})
    frame = pd.DataFrame(rows)
    complete = frame.completed_tasks.eq(frame.expected_tasks)
    result = {"updated_at": stamp(), "status": "source_cache_complete" if complete.all() else "partial_backfill",
              "source": plan["source"], "start": plan["start"], "end": plan["end"], "expected_symbols": len(frame),
              "complete_symbols": int(complete.sum()), "raw_symbols": int(frame.raw_available.sum()),
              "adjusted_symbols": int(frame.adjusted_available.sum()), "raw_rows": int(frame.raw_rows.sum()),
              "delisted_raw_symbols": int((frame.raw_available & frame.status_at_retrieval.eq("0")).sum()),
              "symbols_with_quality_issues": int(frame.quality_issue_count.gt(0).sum()),
              "summary_stage": plan.get("summary_stage", "all"),
              "action_queries": int(frame.action_queries.sum()), "action_rows": int(frame.action_rows.sum()),
              "execution_admitted": False, "error": error, "limitations": plan["limitations"]}
    write_frame(root / "coverage.csv", frame)
    write_json(root / "status.json", result)
    return result


def collect(root=DEFAULT_ROOT, max_symbols=32, stage="all", priority_file=None, *,
            timeout_seconds=30, summary_every=8, source_sdk=None):
    root = Path(root)
    if (not 1 <= max_symbols <= 1000 or stage not in {"bars", "actions", "all"}
            or not 1 <= timeout_seconds <= 120 or not 1 <= summary_every <= 1000):
        raise ValueError("Invalid bounded collection settings")
    plan = json.loads((root / "collection_plan.json").read_text(encoding="utf-8"))
    if plan.get("summary_stage") == "actions" and stage != "actions":
        raise ValueError("Pinned actions-only scope cannot acquire/relabel bars or change its task stage")
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False)
    calendar = pd.read_csv(root / "calendar.csv", dtype=str).date.tolist()
    if plan.get("summary_stage") == "actions":
        if (len(universe) != plan["symbols"] or universe.code.duplicated().any()
                or universe.symbol.duplicated().any()
                or not universe.symbol.eq(universe.code.map(lambda value: normalize_symbol(value).vt_symbol)).all()):
            raise ValueError("Pinned full action-query code/symbol scope changed")
        for filename, key in (("universe.csv", "universe_sha256"), ("calendar.csv", "calendar_sha256")):
            if key in plan and sha256((root / filename).read_bytes()).hexdigest() != plan[key]:
                raise ValueError("Pinned action-query scope hash changed")
    pending = []
    for item in prioritized(universe, priority_file):
        receipt = load_receipt(root, item["symbol"])
        if any(not task_complete(root, receipt, task) for task in tasks_for(plan, item["symbol"], stage)):
            pending.append(item)
    if not pending:
        return summarize(root)
    summarize(root)
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_seconds)
    attempt_path = root / "collection_runs" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8] + ".json")
    attempt = {"status": "running", "stage": stage, "max_symbols": max_symbols, "timeout_seconds": timeout_seconds,
               "started_at": stamp(), "source_sdk": source_sdk, "completed_queries": 0, "failed_query": None}
    write_json(attempt_path, attempt)
    current = None
    try:
        with baostock_session() as bs:
            for count, item in enumerate(pending[:max_symbols], 1):
                symbol = item["symbol"]
                receipt = load_receipt(root, symbol)
                for key, relative, year in tasks_for(plan, symbol, stage):
                    if task_complete(root, receipt, (key, relative, year)):
                        continue
                    begin = time.monotonic()
                    current = {"symbol": symbol, "code": item["code"], "task": key, "year": year}
                    print(f"{count}/{min(max_symbols, len(pending))} {symbol} {key}", flush=True)
                    if year is None:
                        flag = "3" if key == "raw" else "2"
                        response = bs.query_history_k_data_plus(item["code"], plan["fields"],
                            start_date=plan["start"], end_date=plan["end"], frequency="d", adjustflag=flag)
                        frame = provider_frame(response)
                        quality = bar_quality(frame, item, calendar, flag)
                    else:
                        response = bs.query_dividend_data(item["code"], str(year), "operate")
                        frame = provider_frame(response)
                        quality = {"rows": len(frame)}
                        if "code" not in frame or (len(frame) and not frame.code.eq(item["code"]).all()):
                            raise ValueError("Corporate-action schema or symbol mismatch")
                    frame["symbol"] = symbol
                    write_frame(root / relative, frame)
                    receipt["tasks"][key] = {"path": relative, "retrieved_at": stamp(), "quality": quality,
                                             "sha256": sha256((root / relative).read_bytes()).hexdigest(),
                                             "source_sdk": source_sdk, "source_error_code": getattr(response, "error_code", None),
                                             "elapsed_seconds": round(time.monotonic() - begin, 3)}
                    write_json(root / "receipts" / f"{symbol}.json", receipt)
                    attempt["completed_queries"] += 1
                    current = None
                    time.sleep(max(0., .5 - (time.monotonic() - begin)))
                if count % summary_every == 0:
                    summarize(root)
    except Exception as error:
        attempt.update(status="failed", error=f"{type(error).__name__}: {error}", failed_query=current)
        summarize(root, f"{type(error).__name__}: {error}")
        raise
    else:
        attempt["status"] = "completed_batch"
    finally:
        socket.setdefaulttimeout(old_timeout)
        attempt["finished_at"] = stamp()
        write_json(attempt_path, attempt)
    return summarize(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--max-symbols", type=int, default=32)
    parser.add_argument("--stage", choices=["all", "bars", "actions"], default="all")
    parser.add_argument("--priority-file", type=Path)
    parser.add_argument("--prepare-hs300-priority", action="store_true")
    parser.add_argument("--sdk-wheel", type=Path, help="Load the reviewed official SDK only in this CLI process")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--summary-every", type=int, default=8)
    args = parser.parse_args(argv)
    if not args.execute:
        status = args.root / "status.json"
        result = json.loads(status.read_text(encoding="utf-8")) if status.exists() else {"status": "not_collected", "root": str(args.root)}
    else:
        sdk = None
        if args.sdk_wheel:
            from .research.daily_source_probe import select_sdk
            sdk = select_sdk(args.sdk_wheel)
        discover(args.root, args.start, args.end)
        if args.prepare_hs300_priority:
            result = prepare_hs300_priority(args.root)
        else:
            result = summarize(args.root) if args.discover_only else collect(
                args.root, args.max_symbols, args.stage, args.priority_file, timeout_seconds=args.timeout_seconds,
                summary_every=args.summary_every, source_sdk=sdk)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
