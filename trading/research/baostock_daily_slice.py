"""Bounded raw dailyplus slices, kept separate from StockDB and trading feeds."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import time

import numpy as np
import pandas as pd

from a_stock_committee.market_data import baostock_session
from ..market_dataset import write_frame, write_json
from ..symbols import normalize_symbol
from .daily_source_probe import WHEEL_SHA256, select_sdk
from .exchange_calendar import next_exchange_session

SCHEMA = "bounded_baostock_daily_slice_v1"
QUALITY_CONTRACT = "provider_inventory_nullable_suspension_execution_coverage_v2"
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,pctChg,isST"
NUMERIC = ["open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"]


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def scope(symbols, days, calendar):
    if not 1 <= len(symbols) <= 20 or len(set(symbols)) != len(symbols):
        raise ValueError("Use 1..20 distinct explicit ordinary A-share symbols")
    for symbol in symbols:
        info = normalize_symbol(symbol)
        if symbol != info.vt_symbol or not (info.code.startswith("6") if info.market == "SH" else info.code.startswith(("0", "3"))):
            raise ValueError("Expected canonical ordinary SH/SZ A-share symbols")
    if not 1 <= len(days) <= 5 or days != sorted(set(days)):
        raise ValueError("Use 1..5 distinct ordered declared exchange sessions")
    if any(date.fromisoformat(day).isoformat() != day or date.fromisoformat(day) > date.today() for day in days):
        raise ValueError("Session dates must be canonical past/current ISO dates")
    calendar = Path(calendar).resolve()
    expected, current = [], (date.fromisoformat(days[0]) - timedelta(days=1)).isoformat()
    while True:
        current, evidence = next_exchange_session(current, calendar)
        if current is None:
            raise ValueError(f"Pinned calendar cannot resolve declared sessions: {evidence}")
        if current > days[-1]:
            break
        expected.append(current)
    if expected != days:
        raise ValueError("Declared days omit or substitute a pinned open session")
    return {"calendar": str(calendar), "calendar_sha256": digest(calendar), "symbols": symbols, "days": days}


def kwargs(symbol, days):
    return {"code": normalize_symbol(symbol).baostock_code, "fields": FIELDS,
            "start_date": days[0], "end_date": days[-1], "frequency": "d", "adjustflag": "3"}


def quality(frame, arguments, days):
    issues = []
    if list(frame.columns) != FIELDS.split(","):
        return {"rows": len(frame), "issues": ["daily_schema_changed"]}
    if frame.date.tolist() != days or frame.code.tolist() != [arguments["code"]] * len(days):
        issues.append("missing_duplicate_or_unexpected_symbol_session")
    if not frame.adjustflag.eq("3").all():
        issues.append("not_raw_prices")
    if not frame.tradestatus.isin(["0", "1"]).all() or not frame.isST.isin(["0", "1"]).all():
        issues.append("unknown_provider_status")
    values = frame[NUMERIC].apply(pd.to_numeric, errors="coerce")
    halted = frame.tradestatus.eq("0")
    # Dated provider suspension rows may carry blank activity/return fields.
    # Preserve those nulls as inventory evidence; they cannot form Engine bars.
    nullable = ["volume", "amount", "pctChg"]
    permitted_blank = pd.DataFrame(False, index=frame.index, columns=NUMERIC)
    for field in nullable:
        permitted_blank[field] = halted & frame[field].eq("")
    finite = pd.DataFrame(np.isfinite(values.to_numpy()), index=frame.index, columns=NUMERIC)
    if not (finite | permitted_blank).all().all():
        issues.append("missing_nonfinite_numeric_field")
    prices = values[["open", "high", "low", "close", "preclose"]]
    if (prices.le(0).any().any() or values[["volume", "amount"]].lt(0).any().any()
            or (values.volume.notna() & values.volume.mod(1).ne(0)).any()):
        issues.append("invalid_prices_or_share_activity")
    if (values.high.lt(values[["open", "close", "low"]].max(axis=1)).any()
            or values.low.gt(values[["open", "close", "high"]].min(axis=1)).any()):
        issues.append("inconsistent_ohlc")
    active = values.volume.gt(0)
    vwap = values.amount.div(values.volume.where(active))
    if ((active & (vwap.lt(values.low - .0011) | vwap.gt(values.high + .0011))).any()
            or (values.volume.eq(0) & values.amount.ne(0)).any()):
        issues.append("amount_volume_price_inconsistent")
    if ((values.loc[halted, nullable].notna() & values.loc[halted, nullable].ne(0)).any().any()
            or values.loc[halted, ["open", "high", "low", "close"]].sub(values.loc[halted, "preclose"], axis=0).abs().gt(.0001).any().any()):
        issues.append("suspension_payload_contradiction")
    unpriceable = [{"date": str(frame.loc[index, "date"]), "code": str(frame.loc[index, "code"]),
                   "reason": "nullable_suspension_activity_not_execution_bar",
                   "missing_fields": [field for field in nullable if permitted_blank.loc[index, field]]}
                  for index in frame.index if permitted_blank.loc[index].any()]
    return {"rows": len(frame), "issues": issues, "observed_days": frame.date.tolist(),
            "quality_contract": QUALITY_CONTRACT, "suspended_rows": int(halted.sum()),
            "inventory_complete": not issues, "raw_ohlc_numeric_complete": bool(np.isfinite(prices.to_numpy()).all()),
            "execution_bar_coverage_complete": not issues and not unpriceable,
            "execution_bar_rows": len(frame) - len(unpriceable) if not issues else 0,
            "unpriceable_source_rows": unpriceable,
            "source_unit_check": "amount/volume within OHLC when present; nulls unchanged; not independent exchange-unit proof"}


def cached(folder, entry, symbol, days):
    arguments = kwargs(symbol, days)
    expected = f"{arguments['code']}.csv"
    if entry.get("raw_file") != expected or entry.get("kwargs") != arguments:
        raise ValueError("Cached daily slice identity/arguments changed")
    path = folder / expected
    if digest(path) != entry["raw_sha256"]:
        raise ValueError("Cached daily slice hash changed")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    if quality(frame, arguments, days)["issues"]:
        raise ValueError("Cached daily slice no longer has complete valid raw evidence")
    return frame


def seed_entry(seed, symbols, days, sdk):
    seed = Path(seed).resolve()
    receipt_path = seed / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    arguments = receipt.get("kwargs", {})
    matched = [symbol for symbol in symbols if kwargs(symbol, days) == arguments]
    if (receipt.get("schema") != "bounded_dailyplus_raw_tail_probe_v1"
            or receipt.get("status") != "completed_raw_probe_not_admitted"
            or receipt.get("provider_error_code") != "0" or len(matched) != 1
            or receipt.get("sdk", {}).get("sha256") != sdk["sha256"]
            or receipt.get("execution_admitted") is not False):
        raise ValueError("Seed must be a completed non-admitted exact-scope dailyplus probe")
    raw = seed / "raw.csv"
    if Path(receipt["raw_csv"]).resolve() != raw or digest(raw) != receipt["raw_sha256"]:
        raise ValueError("Seed raw identity/hash changed")
    frame = pd.read_csv(raw, dtype=str, keep_default_na=False)
    assessment = quality(frame, arguments, days)
    if assessment["issues"] or receipt.get("rows") != len(frame) or receipt.get("observed_days") != days:
        raise ValueError("Seed raw schema/session/row evidence changed")
    return matched[0], raw, {"symbol": matched[0], "kwargs": arguments,
        "raw_file": f"{arguments['code']}.csv", "raw_sha256": digest(raw), "quality": assessment,
        "origin": "verified_explicit_seed", "provider_error_code": "0", "provider_error_msg": receipt.get("provider_error_msg"),
        "seed_receipt": str(receipt_path), "seed_receipt_sha256": digest(receipt_path)}


def reused_entries(package, plan):
    """Reassess preserved raw bytes, including one failed nullable halt response.

    The original terminal failure and original assessment remain untouched.
    No previously attempted symbol is queried by this operation.
    """
    package = Path(package).resolve()
    source_plan = json.loads((package / "plan.json").read_text(encoding="utf-8"))
    receipt_path = package / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for key in ("schema", "calendar", "calendar_sha256", "symbols", "days", "sdk_sha256", "fields", "frequency", "adjustflag", "source_scope"):
        if source_plan.get(key) != plan.get(key):
            raise ValueError(f"Reused source package scope changed: {key}")
    if (receipt.get("schema") != SCHEMA or receipt.get("sdk", {}).get("sha256") != WHEEL_SHA256
            or receipt.get("execution_admitted") is not False or digest(package / "raw_daily.csv") != receipt.get("raw_daily_sha256")):
        raise ValueError("Reused package receipt/aggregate raw integrity changed")
    entries = dict(receipt["completed"])
    failed = receipt.get("in_flight")
    if failed:
        symbol = failed["symbol"]
        raw_receipt = package / f"{kwargs(symbol, plan['days'])['code']}.receipt.json"
        if raw_receipt.exists():
            entry = json.loads(raw_receipt.read_text(encoding="utf-8"))
            if entry.get("kwargs") != failed.get("kwargs"):
                raise ValueError("Preserved failed query arguments changed")
            entries[symbol] = entry
    result = []
    for symbol, original in entries.items():
        if symbol not in plan["symbols"] or original.get("provider_error_code") != "0":
            raise ValueError("Reused response outside scope or unsuccessful provider response")
        frame = cached(package, original, symbol, plan["days"])
        source_entry = package / f"{kwargs(symbol, plan['days'])['code']}.receipt.json"
        if source_entry.exists():
            saved = json.loads(source_entry.read_text(encoding="utf-8"))
            if saved.get("raw_sha256") != original["raw_sha256"] or saved.get("kwargs") != original["kwargs"]:
                raise ValueError("Reused per-code receipt identity/hash changed")
        assessment = quality(frame, original["kwargs"], plan["days"])
        if symbol not in receipt["completed"] and (not assessment["unpriceable_source_rows"] or not frame.tradestatus.eq("0").all()):
            raise ValueError("Only a preserved known nontrading nullable response may reassess a failed query")
        entry = {**original, "quality": assessment, "origin": "explicit_preserved_source_package",
                 "original_quality": original["quality"], "source_package_receipt": str(receipt_path),
                 "source_package_receipt_sha256": digest(receipt_path),
                 "source_package_status": receipt["status"], "reused_failed_response": symbol not in receipt["completed"]}
        if source_entry.exists():
            entry.update(source_entry_receipt=str(source_entry), source_entry_receipt_sha256=digest(source_entry))
        result.append((symbol, package / original["raw_file"], entry))
    aggregate = pd.read_csv(package / "raw_daily.csv", dtype=str, keep_default_na=False)
    completed_frames = [cached(package, receipt["completed"][symbol], symbol, plan["days"])
                        for symbol in plan["symbols"] if symbol in receipt["completed"]]
    expected = pd.concat(completed_frames, ignore_index=True) if completed_frames else pd.DataFrame(columns=FIELDS.split(","))
    if not aggregate.equals(expected):
        raise ValueError("Reused package aggregate differs from preserved completed per-code responses")
    return result


def collect(symbols, days, calendar, sdk, destination, *, seeds=(), max_new_queries=20,
            session_factory=baostock_session, source_scope=None, reuse_package=None):
    plan = {"schema": SCHEMA, **scope(symbols, days, calendar), "sdk_sha256": sdk.get("sha256"),
            "fields": FIELDS, "frequency": "d", "adjustflag": "3", "source_scope": source_scope,
            "quality_contract": QUALITY_CONTRACT,
            "execution_admitted": False, "stockdb_modified": False, "ledger_mutated": False}
    if sdk.get("sha256") != WHEEL_SHA256 or not 1 <= max_new_queries <= 20:
        raise ValueError("Expected pinned SDK and at most 20 new sequential queries")
    verified_seeds = [seed_entry(seed, symbols, days, sdk) for seed in seeds]
    if reuse_package:
        verified_seeds.extend(reused_entries(reuse_package, plan))
    if len({item[0] for item in verified_seeds}) != len(verified_seeds):
        raise ValueError("Duplicate seed symbols")
    folder = Path(destination).resolve()
    plan_path, receipt_path = folder / "plan.json", folder / "receipt.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ValueError("Resumed daily slice scope/calendar/SDK changed")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        for symbol, entry in receipt["completed"].items():
            if symbol not in symbols:
                raise ValueError("Saved daily slice outside explicit symbols")
            cached(folder, entry, symbol, days)
        if receipt["status"] == "failed_stopped" or receipt.get("in_flight"):
            raise ValueError("Prior failed/unfinished source query retained; automatic retry is disabled")
    else:
        folder.mkdir(parents=True, exist_ok=False)
        write_json(plan_path, plan)
        receipt = {"schema": SCHEMA, "status": "running", "created_at": datetime.now().astimezone().isoformat(),
                   "sdk": sdk, "completed": {}, "failures": [], "in_flight": None, "logical_queries": 0,
                   "retries": 0, "execution_admitted": False, "stockdb_modified": False, "ledger_mutated": False,
                   "broker_login_attempted": False, "implementation_sha256": digest(__file__)}
    for symbol, raw, entry in verified_seeds:
        if symbol in receipt["completed"]:
            if receipt["completed"][symbol]["raw_sha256"] != entry["raw_sha256"]:
                raise ValueError("Seed conflicts with saved raw evidence")
            continue
        shutil.copyfile(raw, folder / entry["raw_file"])
        receipt["completed"][symbol] = entry
    write_json(receipt_path, receipt)
    pending = [symbol for symbol in symbols if symbol not in receipt["completed"]]
    old_timeout = socket.getdefaulttimeout()
    try:
        if pending:
            socket.setdefaulttimeout(30)
            with session_factory() as bs:
                for symbol in pending[:max_new_queries]:
                    arguments = kwargs(symbol, days)
                    receipt["in_flight"] = {"symbol": symbol, "kwargs": arguments, "started_at": datetime.now().astimezone().isoformat()}
                    receipt["logical_queries"] += 1
                    write_json(receipt_path, receipt)
                    print(f"dailyplus {symbol} {days[0]}..{days[-1]}", flush=True)
                    start = time.monotonic()
                    result = bs.query_history_k_data_plus(**arguments)
                    frame = result.get_data()
                    if frame.empty and not len(frame.columns):
                        frame = pd.DataFrame(columns=result.fields)
                    raw = folder / f"{arguments['code']}.csv"
                    write_frame(raw, frame)
                    entry = {"symbol": symbol, "kwargs": arguments, "raw_file": raw.name, "raw_sha256": digest(raw),
                             "provider_error_code": result.error_code, "provider_error_msg": result.error_msg,
                             "quality": quality(frame, arguments, days), "origin": "provider_dailyplus_query",
                             "elapsed_seconds": round(time.monotonic() - start, 3)}
                    write_json(folder / f"{arguments['code']}.receipt.json", entry)
                    if result.error_code != "0" or entry["quality"]["issues"]:
                        raise ValueError(f"Provider raw evidence incomplete/invalid: {entry}")
                    receipt["completed"][symbol] = entry
                    receipt["in_flight"] = None
                    write_json(receipt_path, receipt)
                    time.sleep(max(0., .5 - (time.monotonic() - start)))
        receipt["status"] = "complete_raw_slice_not_admitted" if len(receipt["completed"]) == len(symbols) else "partial_bounded_slice"
    except Exception as error:
        failure = {"at": datetime.now().astimezone().isoformat(), "query": receipt["in_flight"], "error": f"{type(error).__name__}: {error}"}
        receipt["failures"].append(failure)
        receipt["status"] = "failed_stopped"
    finally:
        socket.setdefaulttimeout(old_timeout)
        receipt["pending_symbols"] = [symbol for symbol in symbols if symbol not in receipt["completed"]]
        receipt["updated_at"] = datetime.now().astimezone().isoformat()
        receipt["complete_rows"] = len(days) * len(receipt["completed"])
        receipt["execution_bar_rows"] = sum(entry["quality"]["execution_bar_rows"] for entry in receipt["completed"].values())
        receipt["execution_bar_coverage_complete"] = (len(receipt["completed"]) == len(symbols)
            and all(entry["quality"]["execution_bar_coverage_complete"] for entry in receipt["completed"].values()))
        receipt["unpriceable_source_rows"] = [row for entry in receipt["completed"].values() for row in entry["quality"]["unpriceable_source_rows"]]
        frames = [cached(folder, receipt["completed"][symbol], symbol, days) for symbol in symbols if symbol in receipt["completed"]]
        panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=FIELDS.split(","))
        write_frame(folder / "raw_daily.csv", panel)
        receipt["raw_daily_sha256"] = digest(folder / "raw_daily.csv")
        receipt["limitations"] = ["Separate raw source evidence; no StockDB overlap replacement or field reconciliation",
            "Volume and amount strings preserved without rounding or scaling; cross-source differences remain unresolved",
            "Known nontrading nullable activity is complete source inventory but cannot form an Engine execution bar",
            "Provider trade/isST flags are dated observations, not independently verified PIT/exchange qualification or actions"]
        write_json(receipt_path, receipt)
    return folder, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--symbols", help="Comma separated canonical A-share symbols")
    identity.add_argument("--cohort", type=Path)
    parser.add_argument("--pool", default="common_pre_window_symbol20")
    parser.add_argument("--days", required=True, help="Comma separated exact ordered exchange sessions")
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--seed", type=Path, action="append", default=[])
    parser.add_argument("--reuse-package", type=Path, help="Reassess and copy preserved exact-scope raw responses without re-querying them")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-new-queries", type=int, default=20)
    args = parser.parse_args(argv)
    source_scope = None
    if args.cohort:
        payload = json.loads(args.cohort.read_text(encoding="utf-8"))
        symbols = payload["pools"][args.pool]
        source_scope = {"cohort": str(args.cohort.resolve()), "sha256": digest(args.cohort), "pool": args.pool, "anchor": payload.get("anchor")}
    else:
        symbols = args.symbols.split(",")
    days = args.days.split(",")
    scope(symbols, days, args.calendar)
    sdk = select_sdk(args.sdk_wheel)
    folder, receipt = collect(symbols, days, args.calendar, sdk, args.out, seeds=args.seed,
                              max_new_queries=args.max_new_queries, source_scope=source_scope, reuse_package=args.reuse_package)
    print(json.dumps({"out": str(folder), "status": receipt["status"], "complete_symbols": len(receipt["completed"]),
                      "complete_rows": receipt["complete_rows"], "logical_queries": receipt["logical_queries"],
                      "pending_symbols": receipt["pending_symbols"], "execution_admitted": False}, ensure_ascii=False))
    return 0 if receipt["status"] == "complete_raw_slice_not_admitted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
