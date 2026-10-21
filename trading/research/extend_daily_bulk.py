"""Append completed daily source panels without changing their old prefix."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


KINDS = {
    "stock": ("raw_daily.parquet", "date", "raw_daily_sha256"),
    "adjustment": ("adjustment_events.parquet", "dividOperateDate", None),
}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _calendar(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")["date"].tolist()


def _groups(path, date_field, expected_days=None):
    source = pq.ParquetFile(path)
    if expected_days is not None and source.metadata.num_row_groups != len(expected_days):
        raise ValueError("row-group count differs from the pinned source calendar")
    dates, rows = [], 0
    for index in range(source.metadata.num_row_groups):
        frame = source.read_row_group(index).to_pandas()
        if frame.empty:
            if expected_days is None:
                raise ValueError(f"row group {index} is empty without a pinned calendar")
            day = str(expected_days[index])
        else:
            if frame[date_field].nunique() != 1:
                raise ValueError(f"row group {index} is not one date")
            day = str(frame[date_field].iloc[0])
            if expected_days is not None and day != str(expected_days[index]):
                raise ValueError("row-group date differs from the pinned source calendar")
        if dates and day <= dates[-1]:
            raise ValueError("source panel dates are not strictly increasing")
        dates.append(day)
        rows += len(frame)
    return dates, rows, source


def extend(base_view, extension_view, *, output=None, calendar=None, kind="stock"):
    if kind not in KINDS:
        raise ValueError(f"unsupported daily kind: {kind}")
    base_view, extension_view = Path(base_view).resolve(), Path(extension_view).resolve()
    panel_name, date_field, result_hash_field = KINDS[kind]
    base_result = _json(base_view / "result.json")
    extension_result = _json(extension_view / "result.json")
    if base_result.get("status") != "source_cache_complete" or extension_result.get("status") != "source_cache_complete":
        raise ValueError("both source views must be complete")
    if base_result.get("kind") != kind or extension_result.get("kind") != kind:
        raise ValueError("source kind differs")
    if base_result.get("missing_days") or extension_result.get("missing_days"):
        raise ValueError("missing source days cannot be appended")
    base_path = base_view / panel_name
    extension_path = extension_view / panel_name
    base_plan = _json(base_view / "plan.json")
    extension_plan = _json(extension_view / "plan.json")
    base_days, base_rows, base_file = _groups(base_path, date_field, base_plan.get("days"))
    extension_days, extension_rows, extension_file = _groups(extension_path, date_field, extension_plan.get("days"))
    if set(base_days).intersection(extension_days) or not extension_days or extension_days[0] <= base_days[-1]:
        raise ValueError("extension must contain new dates strictly after the base")
    if base_file.schema_arrow != extension_file.schema_arrow:
        raise ValueError("base and extension Parquet schemas differ")
    if calendar is not None:
        full_calendar = _calendar(calendar)
        expected = base_days + extension_days
        positions = {day: index for index, day in enumerate(full_calendar)}
        if any(day not in positions for day in expected) or expected != sorted(expected, key=positions.get):
            raise ValueError("base plus extension dates are not an ordered prefix of the supplied calendar")
    else:
        full_calendar = base_days + extension_days
    destination = Path(output).resolve() if output else base_view.parent / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / panel_name
    temporary = target.with_suffix(".partial.parquet")
    with pq.ParquetWriter(temporary, base_file.schema_arrow) as writer:
        for source in (base_file, extension_file):
            for index in range(source.metadata.num_row_groups):
                writer.write_table(source.read_row_group(index))
    temporary.replace(target)
    pd.DataFrame({"date": full_calendar[: len(expected)]}).to_csv(destination / "input_calendar.csv", index=False)
    plan = {
        "purpose": f"Research-only extended raw {kind} daily cache; old prefix preserved",
        "kind": kind,
        "days": expected,
        "requested_start": expected[0],
        "requested_end": expected[-1],
        "calendar_source": str(calendar) if calendar else "derived_from_source_panels",
        "base_view": str(base_view),
        "extension_view": str(extension_view),
        "execution_admitted": False,
    }
    (destination / "input_calendar.csv").write_text(
        pd.DataFrame({"date": full_calendar[: len(expected)]}).to_csv(index=False), encoding="utf-8"
    )
    plan["calendar_sha256"] = _sha(destination / "input_calendar.csv")
    (destination / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    receipts = {
        "status": "source_cache_complete",
        "execution_admitted": False,
        "base_result": str(base_view / "result.json"),
        "extension_result": str(extension_view / "result.json"),
        "base_panel_sha256": _sha(base_path),
        "extension_panel_sha256": _sha(extension_path),
        "prefix_days": len(base_days),
        "extension_days": len(extension_days),
    }
    (destination / "receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {
        "status": "source_cache_complete",
        "kind": kind,
        "expected_days": len(expected),
        "acquired_days": len(expected),
        "missing_days": [],
        "empty_days": [],
        "rows": base_rows + extension_rows,
        "symbols": int(pd.unique(pd.concat([
            pd.read_parquet(base_path, columns=["code"] if kind == "stock" else ["code"]),
            pd.read_parquet(extension_path, columns=["code"] if kind == "stock" else ["code"]),
        ], ignore_index=True)["code"]).size),
        "cutoff": expected[-1],
        "panel_file": panel_name,
        "panel_sha256": _sha(target),
        "old_prefix_preserved": True,
        "prefix_days": len(base_days),
        "extension_days": len(extension_days),
        "execution_admitted": False,
        "strategy_validated": False,
        "promotion": False,
    }
    if result_hash_field:
        result[result_hash_field] = result["panel_sha256"]
    result["plan_sha256"] = _sha(destination / "plan.json")
    result["receipts_sha256"] = _sha(destination / "receipts.json")
    result["implementation_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    (destination / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(__file__, destination / "extension_implementation.py")
    return destination, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-view", type=Path, required=True)
    parser.add_argument("--extension-view", type=Path, required=True)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--kind", choices=sorted(KINDS), default="stock")
    args = parser.parse_args(argv)
    folder, result = extend(args.base_view, args.extension_view, output=args.out, calendar=args.calendar, kind=args.kind)
    print(json.dumps({"folder": str(folder), **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
