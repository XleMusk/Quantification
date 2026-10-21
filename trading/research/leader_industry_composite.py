"""Reuse terminal dated-industry batches without carrying across missing weeks.

This is a posthoc research source. It preserves actual retrieval clocks and
failed requests; neither provider dates nor broad industries establish PIT or
historical theme membership. No SDK, price data or accounts are used here.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path

from .leader_daily_signals import _calendar_view
from .leader_posthoc_industry import SOURCE, _day, _digest, _label, _time
from .style_exposure import load_industries


SCHEMA = "saved_dated_industry_composite_v1"
METHOD_ID = "preplanned_week_bucket_partial_industry_posthoc_v1"
SELECTION = "exact_preplanned_week_bucket_missing_blocks_no_backfill"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _pin(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": _digest(path)}


def _inputs(plan_path, roots, observed):
    plan_path = Path(plan_path).resolve()
    plan_pin, plan = _pin(plan_path), _read(plan_path)
    calendar_path = Path(plan["calendar_file"]).resolve()
    calendar_pin = _pin(calendar_path)
    if calendar_pin["sha256"] != plan["calendar_sha256"]:
        raise ValueError("The preplanned exchange calendar changed")
    calendar = _read(calendar_path)
    year = int(calendar["year"])
    start, end = f"{year}-01-01", f"{year}-12-31"
    sessions, _, status = _calendar_view(calendar, start, end, [])
    expected, weeks = [], set()
    for day in sessions:
        week = date.fromisoformat(day).isocalendar()[:2]
        if week not in weeks:
            expected.append(day)
            weeks.add(week)
    if (status != "pinned_static_exchange_calendar" or not expected
            or plan.get("selection") != f"first_actual_{year}_exchange_session_per_ISO_week"
            or plan.get("requested_dates") != expected
            or _time(plan["created_at"]) > observed):
        raise ValueError("The full original calendar-week plan and actual clock are required")
    roots = [Path(root).resolve() for root in roots]
    if not roots or len(set(roots)) != len(roots):
        raise ValueError("Distinct terminal industry source roots are required")
    sources, snapshots, by_day = [], {}, {}
    for root in roots:
        receipt_path = root / "result.json"
        receipt_pin, receipt = _pin(receipt_path), _read(receipt_path)
        state = receipt.get("status")
        requested = receipt.get("requested_dates", [])
        entries = receipt.get("entries", [])
        days = [_day(entry["date"]) for entry in entries]
        if (state not in {"completed_requested_industry_history", "failed_requested_industry_history"}
                or receipt.get("source") != SOURCE
                or receipt.get("revision_safe_pit_verified") is not False
                or receipt.get("execution_admitted") is not False
                or not requested or requested != sorted(set(requested))
                or not set(requested) <= set(expected)
                or days != requested[:len(days)]):
            raise ValueError("Only terminal original-order industry batches may be composed")
        failed = receipt.get("failed_date") if state.startswith("failed_") else None
        if ((state.startswith("failed_") and failed is None)
                or (failed is None and days != requested)
                or (failed is not None and (len(days) >= len(requested)
                    or failed != requested[len(days)] or not receipt.get("error")))):
            raise ValueError("Terminal success/failure must agree with the saved request prefix")
        source = {"root": str(root), "receipt": receipt_pin, "status": state,
                  "requested_dates": requested, "failed_date": failed,
                  "error": receipt.get("error"), "entries": []}
        file_pins = {}
        for entry in entries:
            path = (root / entry["file"]).resolve()
            path.relative_to(root)
            if path.name != entry["date"] + ".parquet":
                raise ValueError("One own-date industry parquet file is required")
            file_pins[entry["date"]] = _pin(path)
        frame, loaded = load_industries(root, days)
        if loaded != receipt or _pin(receipt_path) != receipt_pin:
            raise ValueError("Industry receipt changed while reading")
        for entry in entries:
            day = entry["date"]
            if day in snapshots:
                raise ValueError("Duplicate successful dates cannot be selected between sources")
            path = (root / entry["file"]).resolve()
            path.relative_to(root)
            if path.name != day + ".parquet" or _time(entry["retrieved_at"]) > observed:
                raise ValueError("Exact own-date files and actual retrieval clocks are required")
            file_pin = file_pins[day]
            if _pin(path) != file_pin:
                raise ValueError("Industry parquet changed while reading")
            own = frame.loc[frame.date.eq(day)]
            updates = sorted(own.updateDate.astype(str).unique().tolist())
            classes = sorted(own.industryClassification.dropna().astype(str).unique().tolist())
            if (own.empty or len(own) != entry["rows"] or updates != entry.get("update_dates")
                    or len(classes) != 1 or not classes[0].strip()):
                raise ValueError("Own-date industry scope/classification differs from its receipt")
            for update in updates:
                if _day(update) > day:
                    raise ValueError("Provider update date cannot follow its requested date")
            membership = {row.symbol: _label(row.industry) for row in own.itertuples()}
            evidence = {"snapshot_date": day, "source_file": str(path),
                "source_sha256": file_pin["sha256"], "receipt_file": str(receipt_path),
                "receipt_sha256": receipt_pin["sha256"],
                "actual_available_at": entry["retrieved_at"], "source_update_dates": updates,
                "classification": classes[0], "member_count": len(membership),
                "mapped_member_count": sum(value is not None for value in membership.values()),
                "revision_safe_PIT_verified": False, "theme_membership": False,
                "posthoc_only": True, "historical_publication_verified": False,
                "date_basis": "provider_requested_date_not_historical_publication_clock"}
            raw_path = root / (day + "_raw.csv")
            source["entries"].append({**evidence,
                "raw_file": _pin(raw_path) if raw_path.exists() else None})
            snapshots[day] = {**evidence, "membership": membership}
        for day in requested:
            by_day.setdefault(day, []).append({"source_root": str(root),
                "state": "success" if day in days else "failed" if day == failed else "unqueried",
                "source_status": state, "error": receipt.get("error") if day == failed else None})
        sources.append(source)
    coverage = [{"date": day, "state": "success" if day in snapshots else "unknown",
                 "source_observations": by_day.get(day, [])} for day in expected]
    return {"plan": plan_pin, "calendar": calendar_pin, "year": year,
            "planned_dates": expected, "sources": sources, "coverage": coverage}, snapshots


class _Provider:
    def __init__(self, snapshots, planned, year, provenance):
        self._snapshots, self._planned, self._year = snapshots, planned, year
        self.provenance = provenance

    def __call__(self, anchor_day):
        anchor = _day(anchor_day)
        eligible = [day for day in self._planned if day <= anchor]
        if date.fromisoformat(anchor).year != self._year or not eligible:
            return None
        # A failed or unqueried planned bucket blocks earlier snapshots.
        chosen = self._snapshots.get(eligible[-1])
        if chosen is None:
            return None
        return {**deepcopy(chosen), "anchor_day": anchor,
                "snapshot_staleness_calendar_days":
                    (date.fromisoformat(anchor) - date.fromisoformat(chosen["snapshot_date"])).days}


def build(plan, sources, out):
    folder = Path(out).resolve()
    roots = [Path(root).resolve() for root in sources]
    if folder.exists():
        raise FileExistsError("Composite publication requires a new directory")
    if (Path(plan).resolve().is_relative_to(folder)
            or any(folder.is_relative_to(root) or root.is_relative_to(folder) for root in roots)):
        raise ValueError("Composite output must be separate from original sources")
    actual = datetime.now().astimezone().isoformat()
    inputs, snapshots = _inputs(plan, roots, _time(actual))
    missing = [day for day in inputs["planned_dates"] if day not in snapshots]
    result = {"schema": SCHEMA, "method_id": METHOD_ID,
        "status": "completed_partial_composite" if missing else "completed_composite",
        "created_at": actual, "source": SOURCE, "inputs": inputs,
        "snapshot_count": len(snapshots), "planned_count": len(inputs["planned_dates"]),
        "missing_dates": missing, "coverage_complete": not missing, "selection": SELECTION,
        "revision_safe_PIT_verified": False, "theme_membership": False,
        "posthoc_only": True, "historical_publication_verified": False,
        "execution_admitted": False, "provider_contacted": False}
    folder.mkdir(parents=True)
    with (folder / "result.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return result


def load(root, observed_at):
    root = Path(root).resolve()
    result_file = root / "result.json"
    saved = _read(result_file)
    observed = _time(observed_at)
    if (saved.get("schema") != SCHEMA or saved.get("method_id") != METHOD_ID
            or saved.get("selection") != SELECTION or saved.get("source") != SOURCE
            or saved.get("posthoc_only") is not True
            or any(saved.get(flag) is not False for flag in (
                "execution_admitted", "provider_contacted", "revision_safe_PIT_verified",
                "theme_membership", "historical_publication_verified"))
            or _time(saved["created_at"]) > observed):
        raise ValueError("A saved posthoc composite available by actual observation is required")
    declared = saved["inputs"]
    actual, snapshots = _inputs(declared["plan"]["file"],
        [source["root"] for source in declared["sources"]], observed)
    missing = [day for day in actual["planned_dates"] if day not in snapshots]
    if (actual != declared or saved.get("missing_dates") != missing
            or saved.get("snapshot_count") != len(snapshots)
            or saved.get("planned_count") != len(actual["planned_dates"])
            or saved.get("coverage_complete") is not (not missing)
            or saved.get("status") != ("completed_partial_composite" if missing else "completed_composite")):
        raise ValueError("Composite source pins or coverage changed")
    provenance = {key: deepcopy(value) for key, value in saved.items() if key != "schema"}
    provenance.update(root=str(root), receipt_file=str(result_file),
        receipt_sha256=_digest(result_file), observed_at=observed.isoformat(),
        source_entries=[deepcopy(value) for source in actual["sources"] for value in source["entries"]],
        actual_available_at=max((value["actual_available_at"] for value in snapshots.values()),
                                key=_time, default=None))
    return _Provider(snapshots, actual["planned_dates"], actual["year"], provenance)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = build(args.plan, args.source, args.out)
    print(json.dumps({key: result[key] for key in (
        "status", "snapshot_count", "planned_count", "missing_dates", "execution_admitted")},
        ensure_ascii=False))
    return 0
