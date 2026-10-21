"""Resume full-history source factor queries for every pinned observed code."""
import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import socket
import time
import uuid

import pandas as pd

from a_stock_committee.market_data import baostock_session
from ..market_dataset import provider_frame, write_frame, write_json
from ..settings import DATA_ROOT
from .adjusted_prices import checked_file, numeric
from .daily_source_probe import ADJUSTMENT_FIELDS, select_sdk

START = "1990-01-01"


def validate(frame, code, cutoff):
    if set(frame) != set(ADJUSTMENT_FIELDS):
        raise ValueError("Historical factor schema changed")
    if frame.empty:
        return
    if not frame.code.eq(code).all() or frame.dividOperateDate.duplicated().any():
        raise ValueError("Historical factor code/date identity changed")
    for day in frame.dividOperateDate:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or not START <= day <= cutoff:
            raise ValueError("Historical factor outside the fixed request")
    for field in ADJUSTMENT_FIELDS[2:]:
        if any(numeric(value) <= 0 for value in frame[field]):
            raise ValueError("Invalid historical factor value")


def cached(folder, entry, code, cutoff):
    if entry["code"] != code or entry["end"] != cutoff or entry["start"] != START or entry["error_code"] != "0":
        raise ValueError("Historical factor receipt identity changed")
    frame = pd.read_parquet(checked_file(folder, entry["file"], entry["sha256"]))
    if len(frame) != entry["rows"]:
        raise ValueError("Historical factor receipt count changed")
    validate(frame, code, cutoff)
    return frame


def backfill(coverage, sdk, destination=None, max_codes=64, session_factory=baostock_session):
    if not 1 <= max_codes <= 256:
        raise ValueError("Historical factor batch must be bounded to 1..256 codes")
    coverage = Path(coverage).resolve()
    source = json.loads(coverage.read_text(encoding="utf-8"))
    cutoff = source["cutoff"]
    if date.fromisoformat(cutoff).isoformat() != cutoff or not START <= cutoff <= date.today().isoformat():
        raise ValueError("Historical factor cutoff must be canonical and not future")
    codes = sorted(item["code"] for item in source["outcomes"])
    if (not codes or len(codes) != source["raw_codes"] or len(set(codes)) != len(codes)
            or any(not re.fullmatch(r"(?:sh|sz)\.[0-9]{6}", code) for code in codes)):
        raise ValueError("Distinct complete observed-code coverage required")
    coverage_hash = sha256(coverage.read_bytes()).hexdigest()
    folder = Path(destination) if destination else DATA_ROOT / "research/factor_history_bulk" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    plan = {"purpose": "All pinned observed provider codes, including later delistings, not strategy selection",
            "kind": "adjustment", "method": "query_adjust_factor", "codes": codes,
            "requested_start": START, "requested_end": cutoff, "sdk": sdk,
            "coverage_source": str(coverage), "coverage_sha256": coverage_hash,
            "execution_admitted": False}
    if (folder / "plan.json").exists():
        saved = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
        if any(saved[key] != plan[key] for key in ("codes", "requested_start", "requested_end", "coverage_sha256")):
            raise ValueError("Resumed historical factor scope changed")
        if saved["sdk"]["sha256"] != sdk["sha256"]:
            raise ValueError("Resumed historical factor SDK changed")
        checked_file(folder, "input_coverage.json", coverage_hash)
        state = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    else:
        folder.mkdir(parents=True, exist_ok=False)
        shutil.copy2(coverage, folder / "input_coverage.json")
        write_json(folder / "plan.json", plan)
        state = {"tasks": {}, "failures": [], "execution_admitted": False}
    for code, entry in state["tasks"].items():
        if code not in codes:
            raise ValueError("Historical factor receipt outside the pinned scope")
        cached(folder, entry, code, cutoff)
    pending = [code for code in codes if code not in state["tasks"]]
    view = folder / "views" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    view.mkdir(parents=True, exist_ok=False)
    # Copy before queries so an edited source file cannot relabel an old run.
    shutil.copy2(__file__, view / "collection_implementation.py")
    state.update(status="collecting", error=None)
    write_json(folder / "receipts.json", state)
    previous_timeout, current = socket.getdefaulttimeout(), None
    try:
        if pending:
            socket.setdefaulttimeout(30)
            with session_factory() as bs:
                for count, code in enumerate(pending[:max_codes], 1):
                    current = {"code": code, "start": START, "end": cutoff, "method": "query_adjust_factor"}
                    print(f"{count}/{min(max_codes, len(pending))} {code} historical factors", flush=True)
                    begin = time.monotonic()
                    response = bs.query_adjust_factor(code=code, start_date=START, end_date=cutoff)
                    current.update(error_code=response.error_code, error_msg=response.error_msg)
                    frame = provider_frame(response)
                    path = view / (code.replace(".", "_") + ".parquet")
                    write_frame(path, frame)
                    current.update(file=path.relative_to(folder).as_posix(), rows=len(frame),
                                   fields=frame.columns.tolist(), sha256=sha256(path.read_bytes()).hexdigest(),
                                   elapsed_seconds=round(time.monotonic() - begin, 3))
                    validate(frame, code, cutoff)
                    state["tasks"][code] = current
                    current = None
                    write_json(folder / "receipts.json", state)
                    time.sleep(max(0., .5 - (time.monotonic() - begin)))
        state["status"] = "source_cache_complete" if len(state["tasks"]) == len(codes) else "partial_backfill"
    except Exception as error:
        failure = {"at": datetime.now().astimezone().isoformat(), "query": current,
                   "error": f"{type(error).__name__}: {error}"}
        state["failures"].append(failure)
        state.update(status="failed_backfill", error=failure["error"])
    finally:
        socket.setdefaulttimeout(previous_timeout)
        state["updated_at"] = datetime.now().astimezone().isoformat()
        write_json(folder / "receipts.json", state)
    for name in ("plan.json", "receipts.json", "input_coverage.json"):
        shutil.copy2(folder / name, view / name)
    frames, empty_codes = [], []
    for code, entry in sorted(state["tasks"].items()):
        frame = cached(folder, entry, code, cutoff)
        if frame.empty:
            empty_codes.append(code)
        frames.append(frame)
    panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ADJUSTMENT_FIELDS)
    panel_name = "adjustment_events.parquet"
    write_frame(view / panel_name, panel.sort_values(["code", "dividOperateDate"]))
    result = {"status": state["status"], "kind": "adjustment", "scope": "full_history_pinned_observed_codes",
              "requested_codes": len(codes), "acquired_codes": len(state["tasks"]),
              "missing_query_codes": [code for code in codes if code not in state["tasks"]],
              "empty_success_codes": empty_codes, "rows": len(panel), "symbols": int(panel.code.nunique()),
              "panel_file": panel_name, "panel_sha256": sha256((view / panel_name).read_bytes()).hexdigest(),
              "plan_sha256": sha256((view / "plan.json").read_bytes()).hexdigest(),
              "receipts_sha256": sha256((view / "receipts.json").read_bytes()).hexdigest(),
              "implementation_sha256": sha256((view / "collection_implementation.py").read_bytes()).hexdigest(),
              "preserved_failure_count": len(state["failures"]), "execution_admitted": False,
              "adjusted_prices_verified": False, "cash_share_actions_verified": False,
              "limitations": ["Every pinned observed code is requested; provider membership is not independently full-A verified",
                              "1990-01-01 request start, not proof that provider history is complete from listing",
                              "Empty success is unavailable baseline data, never a factor of one",
                              "Full raw history may reveal additional codes requiring a new explicit scope",
                              "Provider factors are not actual cash/share entitlements or price-feed admission"]}
    write_json(view / "result.json", result)
    return folder, view, result


def merge(daily_view, history_view, destination=None):
    views = [Path(daily_view), Path(history_view)]
    plans, receipts, payloads = [], [], []
    for view in views:
        result = json.loads((view / "result.json").read_text(encoding="utf-8"))
        if result["kind"] != "adjustment":
            raise ValueError("Expected source factor views")
        plan = json.loads(checked_file(view, "plan.json", result["plan_sha256"]).read_text(encoding="utf-8"))
        frame = pd.read_parquet(checked_file(view, result["panel_file"], result["panel_sha256"]))
        if frame.duplicated(["code", "dividOperateDate"]).any() or len(frame) != result["rows"]:
            raise ValueError("Source factor identities/count changed")
        if set(frame) != set(ADJUSTMENT_FIELDS):
            raise ValueError("Source factor schema changed")
        for code, group in frame.groupby("code"):
            validate(group, code, plan["requested_end"])
        plans.append(plan)
        receipts.append(result)
        payloads.append(frame)
    if plans[0]["requested_end"] != plans[1]["requested_end"]:
        raise ValueError("Cannot merge factors with different terminal cutoffs")
    rows, overlaps = {}, 0
    for frame in payloads:
        for row in frame.to_dict("records"):
            key = row["code"], row["dividOperateDate"]
            if key in rows:
                if any(numeric(rows[key][field]) != numeric(row[field]) for field in ADJUSTMENT_FIELDS[2:]):
                    raise ValueError(f"Conflicting factor sources, not overwritten: {key}")
                overlaps += 1
            else:
                rows[key] = row
    panel = pd.DataFrame(rows.values(), columns=ADJUSTMENT_FIELDS).sort_values(["code", "dividOperateDate"])
    folder = Path(destination) if destination else DATA_ROOT / "research/merged_factor_views" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, folder / "merge_implementation.py")
    plan = {"kind": "adjustment", "requested_start": START, "requested_end": plans[0]["requested_end"],
            "sources": [{"view": str(view), "panel_sha256": receipt["panel_sha256"],
                         "plan_sha256": receipt["plan_sha256"]} for view, receipt in zip(views, receipts)],
            "purpose": "Exact overlap-checked factor union, not cash/share or adjusted-feed admission"}
    write_json(folder / "plan.json", plan)
    write_frame(folder / "adjustment_events.parquet", panel)
    result = {"kind": "adjustment", "status": "source_union_built", "rows": len(panel),
              "symbols": int(panel.code.nunique()), "equal_overlap_rows_verified": overlaps,
              "additional_unique_history_rows": len(panel) - len(payloads[0]),
              "panel_file": "adjustment_events.parquet",
              "panel_sha256": sha256((folder / "adjustment_events.parquet").read_bytes()).hexdigest(),
              "plan_sha256": sha256((folder / "plan.json").read_bytes()).hexdigest(),
              "implementation_sha256": sha256((folder / "merge_implementation.py").read_bytes()).hexdigest(),
              "sources": plan["sources"], "execution_admitted": False, "adjusted_prices_verified": False,
              "cash_share_actions_verified": False,
              "limitations": ["Original daily events kept for codes still awaiting historical queries",
                              "Equal duplicate records reconciled by code/date and all three numeric values",
                              "Same provider sources, not independent completeness or price-anchor verification",
                              "No missing factor defaults, code exclusions, account/feed change or backtest"]}
    write_json(folder / "result.json", result)
    return folder, result


def extend(base_view, extension_view, destination=None):
    """Append a later factor-event view to a full-history factor view.

    Factor events are sparse by code/date, so the daily row-group extender is
    intentionally not reused here. Equal overlap is checked field by field;
    conflicting events are rejected instead of last-writer-wins.
    """
    views = [Path(base_view), Path(extension_view)]
    plans, results, payloads = [], [], []
    for view in views:
        result = json.loads((view / "result.json").read_text(encoding="utf-8"))
        if result.get("kind") != "adjustment" or result.get("status") not in {
                "source_cache_complete", "source_union_built"}:
            raise ValueError("Expected complete adjustment factor views")
        plan = json.loads(checked_file(view, "plan.json", result["plan_sha256"]).read_text(encoding="utf-8"))
        frame = pd.read_parquet(checked_file(view, result["panel_file"], result["panel_sha256"]))
        if set(frame.columns) != set(ADJUSTMENT_FIELDS) or frame.duplicated(
                ["code", "dividOperateDate"]).any() or len(frame) != result["rows"]:
            raise ValueError("Source factor identities/count/schema changed")
        for code, group in frame.groupby("code"):
            validate(group, code, plan["requested_end"])
        plans.append(plan)
        results.append(result)
        payloads.append(frame)
    if plans[1]["requested_start"] <= plans[0]["requested_end"]:
        raise ValueError("Factor extension must start after the base cutoff")
    if any(day <= plans[0]["requested_end"] for day in payloads[1].dividOperateDate):
        raise ValueError("Factor extension contains an event at or before the base cutoff")

    rows, overlaps = {}, 0
    for frame in payloads:
        for row in frame.to_dict("records"):
            key = row["code"], row["dividOperateDate"]
            if key in rows:
                if any(numeric(rows[key][field]) != numeric(row[field]) for field in ADJUSTMENT_FIELDS[2:]):
                    raise ValueError(f"Conflicting factor sources, not overwritten: {key}")
                overlaps += 1
            else:
                rows[key] = row
    panel = pd.DataFrame(rows.values(), columns=ADJUSTMENT_FIELDS).sort_values(["code", "dividOperateDate"])
    folder = Path(destination) if destination else DATA_ROOT / "research/extended_factor_views" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, folder / "extension_implementation.py")
    plan = {"kind": "adjustment", "requested_start": plans[0]["requested_start"],
            "requested_end": plans[1]["requested_end"],
            "sources": [{"view": str(view), "panel_sha256": result["panel_sha256"],
                         "plan_sha256": result["plan_sha256"]}
                        for view, result in zip(views, results)],
            "purpose": "Exact full-history factor extension; no missing factor defaults or adjusted-feed admission"}
    write_json(folder / "plan.json", plan)
    write_frame(folder / "adjustment_events.parquet", panel)
    result = {"kind": "adjustment", "status": "source_union_built", "rows": len(panel),
              "symbols": int(panel.code.nunique()), "equal_overlap_rows_verified": overlaps,
              "additional_unique_extension_rows": len(panel) - len(payloads[0]),
              "panel_file": "adjustment_events.parquet",
              "panel_sha256": sha256((folder / "adjustment_events.parquet").read_bytes()).hexdigest(),
              "plan_sha256": sha256((folder / "plan.json").read_bytes()).hexdigest(),
              "implementation_sha256": sha256((folder / "extension_implementation.py").read_bytes()).hexdigest(),
              "sources": plan["sources"], "execution_admitted": False,
              "adjusted_prices_verified": False, "cash_share_actions_verified": False,
              "limitations": ["Same-provider factor sources, not independent completeness or price-anchor verification",
                              "Extension appends later factor events only; it does not infer corporate actions",
                              "No missing factor defaults, code exclusions, account/feed change or backtest"]}
    write_json(folder / "result.json", result)
    return folder, result


def merge_main(argv=None):
    parser = argparse.ArgumentParser(description="Merge original daily and historical factor views without overwrites")
    parser.add_argument("--daily-view", type=Path, required=True)
    parser.add_argument("--history-view", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = merge(args.daily_view, args.history_view, args.out)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0


def extend_main(argv=None):
    parser = argparse.ArgumentParser(description="Extend a full-history factor view with later events")
    parser.add_argument("--base-view", type=Path, required=True)
    parser.add_argument("--extension-view", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = extend(args.base_view, args.extension_view, args.out)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, required=True, help="Pinned baseline coverage result.json")
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--max-codes", type=int, default=64)
    args = parser.parse_args(argv)
    sdk = select_sdk(args.sdk_wheel)
    folder, view, result = backfill(args.coverage, sdk, args.folder, args.max_codes)
    print(json.dumps({"folder": str(folder), "view": str(view), **{key: value for key, value in result.items()
                      if key not in {"missing_query_codes", "empty_success_codes", "limitations"}},
                      "missing_query_count": len(result["missing_query_codes"]),
                      "empty_success_count": len(result["empty_success_codes"])}, indent=2))
    return 1 if result["status"] == "failed_backfill" else 0


if __name__ == "__main__":
    raise SystemExit(main())
