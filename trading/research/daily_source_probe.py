"""Bounded BaoStock daily bulk-source feasibility, never a trading feed."""

import argparse
from datetime import date, datetime
from hashlib import sha256
import importlib
import json
from pathlib import Path
import shutil
import socket
import sys
import time
import uuid

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from a_stock_committee.market_data import BAOSTOCK_FIELDS, baostock_session
from ..market_dataset import provider_frame, write_frame, write_json
from ..settings import DATA_ROOT


WHEEL_SHA256 = "0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8"
METHODS = {"etf": "query_daily_history_k_ETF", "stock": "query_daily_history_k_AStock",
           "adjustment": "query_daily_adjust_factor"}
ADJUSTMENT_FIELDS = ("code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor")
DOC_URL = "https://www.baostock.com/helpdocs/api/markdown/DailyUpdates.md"
NUMERIC = ("open", "high", "low", "close", "preclose", "volume", "amount")


def adjustment_quality(frame, day):
    """Validate factor events without inferring prices or shareholder movements."""
    issues = []
    if set(frame.columns) != set(ADJUSTMENT_FIELDS):
        return {"rows": len(frame), "issues": ["adjustment_schema_changed"], "empty": frame.empty}
    if frame.empty:
        return {"rows": 0, "issues": [], "empty": True}
    if not frame.dividOperateDate.eq(day).all():
        issues.append("response_date_mismatch")
    if (frame.duplicated(["dividOperateDate", "code"]).any()
            or not frame.code.str.fullmatch(r"(?:sh|sz)\.[0-9]{6}").all()):
        issues.append("invalid_or_duplicate_identity")
    values = frame[list(ADJUSTMENT_FIELDS[2:])].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(values.to_numpy()).all() or values.le(0).any().any():
        issues.append("missing_nonfinite_or_nonpositive_factor")
    return {"rows": len(frame), "issues": issues, "empty": False,
            "symbols": int(frame.code.nunique()), "factor_semantics_verified": False}


def quality(frame, day, kind="stock"):
    """Preserve raw payloads and report contradictions without imputing fields."""
    if kind == "adjustment":
        return adjustment_quality(frame, day)
    issues = []
    if set(frame.columns) != set(BAOSTOCK_FIELDS.split(",")):
        return {"rows": len(frame), "issues": ["daily_schema_changed"], "empty": frame.empty}
    if frame.empty:
        return {"rows": 0, "issues": [], "empty": True}
    if not frame.date.eq(day).all():
        issues.append("response_date_mismatch")
    if (frame.duplicated(["date", "code"]).any()
            or not frame.code.str.fullmatch(r"(?:sh|sz)\.[0-9]{6}").all()):
        issues.append("invalid_or_duplicate_identity")
    if not frame.adjustflag.eq("3").all():
        issues.append("not_raw_prices")
    if not frame.tradestatus.isin(["0", "1"]).all():
        issues.append("invalid_trade_status")
    values = frame[list(NUMERIC)].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(values.to_numpy()).all():
        issues.append("missing_or_nonfinite_market_fields")
    if (values[["open", "high", "low", "close", "preclose"]].le(0).any().any()
            or values[["volume", "amount"]].lt(0).any().any()):
        issues.append("invalid_price_or_activity")
    if ((values.high < values[["open", "low", "close"]].max(axis=1) - 1e-8).any()
            or (values.low > values[["open", "high", "close"]].min(axis=1) + 1e-8).any()):
        issues.append("inconsistent_ohlc")
    inactive = frame.tradestatus.eq("0")
    activity = values.loc[inactive, ["volume", "amount"]]
    if ((activity.notna() & activity.ne(0)).any().any()
            or values.loc[inactive, ["open", "high", "low", "close"]]
            .sub(values.loc[inactive, "preclose"], axis=0).abs().gt(1e-8).any().any()):
        issues.append("suspension_payload_contradiction")
    active = values.volume.gt(0)
    vwap = values.amount.div(values.volume.where(active))
    if ((active & (vwap.lt(values.low - .0011) | vwap.gt(values.high + .0011))).any()
            or (values.volume.eq(0) & values.amount.ne(0)).any()):
        issues.append("amount_volume_price_inconsistent")
    return {"rows": len(frame), "issues": issues, "empty": False,
            "symbols": int(frame.code.nunique()), "normal_trading_rows": int(frame.tradestatus.eq("1").sum()),
            "suspended_rows": int(inactive.sum()), "nonzero_volume_rows": int(active.sum()),
            "unit_check": "amount/volume lies inside OHLC; not independent exchange-unit verification"}


def select_sdk(wheel):
    """Load the reviewed official pure-Python wheel only in this CLI process."""
    wheel = Path(wheel).resolve()
    if sha256(wheel.read_bytes()).hexdigest() != WHEEL_SHA256:
        raise ValueError("BaoStock wheel differs from the reviewed official 0.9.4 distribution")
    if any(name == "baostock" or name.startswith("baostock.") for name in sys.modules):
        raise ValueError("Run the isolated SDK probe in a fresh process")
    sys.path.insert(0, str(wheel))
    module = importlib.import_module("baostock")
    if not str(module.__file__).startswith(str(wheel)):
        raise ValueError("Isolated BaoStock module was not loaded from the pinned wheel")
    for method in METHODS.values():
        if not callable(getattr(module, method, None)):
            raise ValueError(f"BaoStock SDK lacks {method}")
    return {"wheel": str(wheel), "sha256": WHEEL_SHA256, "distribution_version": "0.9.4",
            "client_version": module.__version__, "module_file": module.__file__}


def fetch_daily(bs, entry, folder):
    begin = time.monotonic()
    result = getattr(bs, METHODS[entry["kind"]])(date=entry["date"])
    entry.update(error_code=result.error_code, error_msg=result.error_msg)
    frame = provider_frame(result)
    relative = f"{entry['date']}_{entry['kind']}.csv"
    write_frame(folder / relative, frame)
    entry.update(file=relative, sha256=sha256((folder / relative).read_bytes()).hexdigest(),
                 quality=quality(frame, entry["date"], entry["kind"]), elapsed_seconds=round(time.monotonic() - begin, 3))
    time.sleep(max(0., .5 - (time.monotonic() - begin)))
    return entry


def collect(days, kinds, sdk, destination=None, session_factory=baostock_session):
    if (not days or not kinds or len(days) * len(kinds) > 6
            or len(set(days)) != len(days) or len(set(kinds)) != len(kinds)
            or set(kinds) - set(METHODS)):
        raise ValueError("Use at most six distinct daily source queries")
    for day in days:
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day or parsed > date.today():
            raise ValueError("A source date must be a canonical past/current ISO date")
    folder = Path(destination) if destination else DATA_ROOT / "research/daily_source_probes" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    receipt = {"purpose": "Daily bulk-source feasibility, not historical-feed admission or a strategy result",
               "status": "running", "created_at": datetime.now().astimezone().isoformat(), "sdk": sdk,
               "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
               "source_document": DOC_URL, "days": days, "kinds": kinds, "entries": [],
               "execution_admitted": False, "full_history_verified": False,
               "limitations": ["Provider daily schema is not independently exchange-reconciled",
                               "ETF classifications, settlement, corporate actions and account permissions are not verified",
                               "ETF isST and equity valuation fields are preserved, not treated as security metadata",
                               "Adjustment factors are not cash dividends or share movements; their price semantics are unverified",
                               "Empty success is unavailable data, not a zero-return observation"]}
    write_json(folder / "receipt.json", receipt)
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30)
    current = None
    try:
        with session_factory() as bs:
            for day in days:
                for kind in kinds:
                    current = {"date": day, "kind": kind, "method": METHODS[kind]}
                    print(f"{day} {kind}", flush=True)
                    receipt["entries"].append(fetch_daily(bs, current, folder))
                    current = None
                    write_json(folder / "receipt.json", receipt)
        receipt["status"] = "completed_probe"
    except Exception as error:
        receipt.update(status="failed_probe", error=f"{type(error).__name__}: {error}", failed_query=current)
    finally:
        socket.setdefaulttimeout(old_timeout)
        receipt["finished_at"] = datetime.now().astimezone().isoformat()
        write_json(folder / "receipt.json", receipt)
    return folder, receipt


def cached_daily(folder, entry, kind="etf"):
    expected = f"{entry['date']}_{entry['kind']}.csv"
    if kind not in METHODS or entry["file"] != expected or entry["kind"] != kind:
        raise ValueError("Daily source receipt identity changed")
    path = folder / expected
    if sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError("Daily source receipt hash changed")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def write_panel(folder, tasks, kind, path):
    """One verified date per row group; full-market histories need not fit in RAM."""
    fields = list(ADJUSTMENT_FIELDS) if kind == "adjustment" else BAOSTOCK_FIELDS.split(",")
    keys = ["dividOperateDate", "code"] if kind == "adjustment" else ["date", "code"]
    schema = pa.schema([(field, pa.string()) for field in fields])
    temporary = path.with_suffix(".tmp")
    rows, symbols, current_quality = 0, set(), {}
    with pq.ParquetWriter(temporary, schema) as writer:
        for day, entry in sorted(tasks.items()):
            frame = cached_daily(folder, entry, kind)
            current_quality[day] = quality(frame, day, kind)
            if set(frame.columns) != set(fields):
                raise ValueError("Changed daily schema retained in source cache; cannot build the standard panel")
            frame = frame.loc[:, fields].sort_values(keys)
            writer.write_table(pa.Table.from_pandas(frame, schema=schema, preserve_index=False))
            rows += len(frame)
            symbols.update(frame.code)
    temporary.replace(path)
    return rows, len(symbols), current_quality


def backfill(calendar, start, end, sdk, destination=None, max_days=64, seeds=(),
             session_factory=baostock_session, *, kind="etf", newest_first=False, timeout_seconds=30):
    """Resume provider-returned securities per pinned day, not today's universe."""
    if not 1 <= timeout_seconds <= 120:
        raise ValueError("Source socket timeout must be bounded to 1..120 seconds")
    if kind not in METHODS or not 1 <= max_days <= 256:
        raise ValueError("Daily backfill needs stock/etf/adjustment and a bound of 1..256 days")
    for day in (start, end):
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day or parsed > date.today():
            raise ValueError("Invalid backfill date")
    if start > end:
        raise ValueError("Backfill start is after end")
    calendar = Path(calendar).resolve()
    calendar_hash = sha256(calendar.read_bytes()).hexdigest()
    sessions = pd.read_csv(calendar, dtype=str, keep_default_na=False).date
    if (sessions.duplicated().any() or not sessions.is_monotonic_increasing
            or any(date.fromisoformat(day).isoformat() != day for day in sessions)):
        raise ValueError("Expected a canonical distinct ordered exchange-session calendar")
    days = sessions.loc[sessions.between(start, end)].tolist()
    if not days:
        raise ValueError("No pinned exchange sessions in the requested period")
    folder = Path(destination) if destination else DATA_ROOT / f"research/{kind}_daily_bulk" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    plan_path = folder / "plan.json"
    plan = {"purpose": f"Research-only raw {kind} daily cache; no feed or strategy admission",
            "source_document": DOC_URL, "calendar_source": str(calendar), "calendar_sha256": calendar_hash,
            "requested_start": start, "requested_end": end, "days": days, "kind": kind, "sdk": sdk,
            "acquisition_order": "newest_first" if newest_first else "oldest_first",
            "execution_admitted": False, "historical_universe_independently_verified": False,
            "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}
    if plan_path.exists():
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        for key in ("calendar_sha256", "requested_start", "requested_end", "days", "kind"):
            if saved[key] != plan[key]:
                raise ValueError("Resumed daily scope changed")
        if saved.get("acquisition_order", "oldest_first") != plan["acquisition_order"]:
            raise ValueError("Resumed daily acquisition order changed")
        if saved["sdk"]["sha256"] != sdk["sha256"]:
            raise ValueError("Resumed daily SDK changed")
        if sha256((folder / "input_calendar.csv").read_bytes()).hexdigest() != calendar_hash:
            raise ValueError("Pinned daily calendar changed")
        state = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    else:
        folder.mkdir(parents=True, exist_ok=False)
        shutil.copy2(calendar, folder / "input_calendar.csv")
        write_json(plan_path, plan)
        state = {"tasks": {}, "failures": [], "execution_admitted": False}
    for day, entry in state["tasks"].items():
        if day not in days or entry["date"] != day:
            raise ValueError("Daily receipt is outside the pinned scope")
        cached_daily(folder, entry, kind)
    for seed in seeds:
        seed = Path(seed)
        receipt_path = seed / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt["status"] != "completed_probe" or receipt["sdk"]["sha256"] != sdk["sha256"]:
            raise ValueError("Expected a completed probe using the pinned SDK")
        for entry in receipt["entries"]:
            day = entry["date"]
            if entry["kind"] != kind or day not in days or day in state["tasks"]:
                continue
            frame = cached_daily(seed, entry, kind)
            shutil.copy2(seed / entry["file"], folder / entry["file"])
            state["tasks"][day] = {**entry, "quality": quality(frame, day, kind),
                                  "seed_receipt": str(receipt_path),
                                  "seed_receipt_sha256": sha256(receipt_path.read_bytes()).hexdigest()}
    pending = [day for day in days if day not in state["tasks"]]
    if newest_first:
        pending.reverse()
    state.update(status="collecting", expected_days=len(days), error=None)
    write_json(folder / "receipts.json", state)
    old_timeout = socket.getdefaulttimeout()
    current = None
    try:
        if pending:
            socket.setdefaulttimeout(timeout_seconds)
            with session_factory() as bs:
                for count, day in enumerate(pending[:max_days], 1):
                    current = {"date": day, "kind": kind, "method": METHODS[kind]}
                    print(f"{count}/{min(max_days, len(pending))} {day} all provider {kind}", flush=True)
                    state["tasks"][day] = fetch_daily(bs, current, folder)
                    current = None
                    write_json(folder / "receipts.json", state)
        state["status"] = "source_cache_complete" if len(state["tasks"]) == len(days) else "partial_backfill"
    except Exception as error:
        failure = {"at": datetime.now().astimezone().isoformat(), "query": current,
                   "error": f"{type(error).__name__}: {error}"}
        state["failures"].append(failure)
        state.update(status="failed_backfill", error=failure["error"])
    finally:
        socket.setdefaulttimeout(old_timeout)
        state["updated_at"] = datetime.now().astimezone().isoformat()
        write_json(folder / "receipts.json", state)
    view = folder / "views" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    view.mkdir(parents=True, exist_ok=False)
    for name in ("plan.json", "receipts.json", "input_calendar.csv"):
        shutil.copy2(folder / name, view / name)
    shutil.copy2(__file__, view / "collection_implementation.py")
    panel_name = "adjustment_events.parquet" if kind == "adjustment" else "raw_daily.parquet"
    rows, symbols, current_quality = write_panel(folder, state["tasks"], kind, view / panel_name)
    panel_hash = sha256((view / panel_name).read_bytes()).hexdigest()
    summary = {"status": state["status"], "kind": kind, "expected_days": len(days), "acquired_days": len(state["tasks"]),
               "request_timeout_seconds": timeout_seconds,
               "missing_days": [day for day in days if day not in state["tasks"]], "rows": rows,
               "symbols": symbols, "acquisition_order": plan["acquisition_order"],
               "empty_days": [day for day, item in current_quality.items() if item["empty"]],
               "quality_issue_days": {day: item["issues"] for day, item in current_quality.items() if item["issues"]},
               "current_quality": current_quality,
               "plan_sha256": sha256(plan_path.read_bytes()).hexdigest(),
               "receipts_sha256": sha256((folder / "receipts.json").read_bytes()).hexdigest(),
               "panel_file": panel_name, "panel_sha256": panel_hash,
               "implementation_sha256": sha256((view / "collection_implementation.py").read_bytes()).hexdigest(),
               "preserved_failure_count": len(state["failures"]),
               "execution_admitted": False, "strategy_validated": False,
               "limitations": ["Observed provider securities, not independently reconciled historical full-market coverage",
                               "Raw source fields only; adjusted-price reconstruction and corporate actions remain unverified",
                               "Dated security classifications/status, suspension completeness, units and account permissions remain unverified",
                               "Adjustment factors do not supply cash/share actions or independently verified adjusted-price semantics",
                               "Empty/missing/contradictory sessions are retained, not filled or excluded"]}
    if kind != "adjustment":
        summary["raw_daily_sha256"] = panel_hash
    else:
        summary["factor_semantics_verified"] = False
    write_json(view / "result.json", summary)
    return folder, view, summary


def backfill_main(argv=None):
    parser = argparse.ArgumentParser(description="Resumable research-only daily bulk stock/ETF/adjustment cache")
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--max-days", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=int, default=30, help="Bounded source socket timeout; default30, maximum120")
    parser.add_argument("--kind", choices=list(METHODS), default="etf")
    parser.add_argument("--newest-first", action="store_true")
    parser.add_argument("--brief", action="store_true", help="Print counts; full dates and quality stay in result.json")
    parser.add_argument("--seed-probe", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    sdk = select_sdk(args.sdk_wheel)
    folder, view, summary = backfill(args.calendar, args.start, args.end, sdk, args.folder,
                                   args.max_days, args.seed_probe, kind=args.kind, newest_first=args.newest_first,
                                   timeout_seconds=args.timeout_seconds)
    if args.brief:
        summary = {**{k: v for k, v in summary.items() if k not in
                     {"missing_days", "quality_issue_days", "current_quality", "empty_days"}},
                   "missing_day_count": len(summary["missing_days"]),
                   "empty_day_count": len(summary["empty_days"]),
                   "quality_issue_day_count": len(summary["quality_issue_days"])}
    print(json.dumps({"folder": str(folder), "view": str(view), **summary}, ensure_ascii=False, indent=2))
    return 1 if summary["status"] == "failed_backfill" else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--date", action="append", required=True, dest="days")
    parser.add_argument("--kinds", nargs="+", choices=list(METHODS), default=["etf"])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    sdk = select_sdk(args.sdk_wheel)
    folder, receipt = collect(args.days, args.kinds, sdk, args.out)
    print(json.dumps({"folder": str(folder), **receipt}, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] == "completed_probe" else 1


if __name__ == "__main__":
    raise SystemExit(main())
