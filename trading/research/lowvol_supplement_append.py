"""Append strict complete daily packages to an unchanged raw supplement.

The old Parquet rows, order, strings and fields remain an exact prefix. This
creates only a research derivative; it does not revise StockDB or any account.
"""
from datetime import datetime
import argparse
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete
from .index_observation import CN
from .leader_recent_source_history import source_proof

SCHEMA = "lowvol_append_only_complete_source_supplement_v1"
FLAGS = {"research_only": True, "execution_admitted": False, "selection_enabled": False,
         "strategy_validated": False, "stockdb_modified": False, "ledger_mutated": False,
         "source_verified": False, "pit_verified": False, "corporate_actions_verified": False,
         "provider_contacted": False}


def _key(day, code):
    day = str(day)
    day = day[:4] + "-" + day[4:6] + "-" + day[6:] if len(day) == 8 and day.isdigit() else day
    return day, str(code)[-6:]


def append_sources(base, packages, out):
    base, out = Path(base).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new supplement directory; no source or previous derivative is overwritten")
    original = pq.ParquetFile(base)
    schema = original.schema_arrow
    if any(not pa.types.is_string(field.type) for field in schema) or not {"date", "code"} <= set(schema.names):
        raise ValueError("The original raw supplement must retain its string field contract")
    base_sha = digest(base)
    additions, proofs, seen = [], [], set()
    recorded = datetime.now(CN)
    for path in packages:
        raw, source = load_complete(path)
        day = source["plan"]["day"]
        proof = source_proof(raw, source, source["plan"]["calendar_sha256"])
        available = source["receipt"].get("completed_at") or source["receipt"]["updated_at"]
        if datetime.fromisoformat(available) > recorded or not set(raw.columns) <= set(schema.names):
            raise ValueError("Source availability or raw field contract is incompatible")
        proof.update(source_available_at=available, raw_columns=list(raw),
                     unobserved_supplement_fields=[field for field in schema.names if field not in raw])
        proofs.append(proof)
        for row in raw.to_dict("records"):
            key = _key(row["date"], row["code"])
            if key in seen:
                raise ValueError("Requested complete sources contain a duplicate day/code")
            seen.add(key)
            additions.append(row)
    if not additions:
        raise ValueError("At least one complete source must be supplied")
    by_key = {_key(row["date"], row["code"]): row for row in additions}
    overlaps = set()
    # Only potentially intersecting row groups need row-level comparisons.
    requested_days = sorted({key[0] for key in seen})
    for index in range(original.metadata.num_row_groups):
        stats = original.metadata.row_group(index).column(schema.names.index("date")).statistics
        if stats and stats.has_min_max and (_key(stats.max, "")[0] < requested_days[0] or _key(stats.min, "")[0] > requested_days[-1]):
            continue
        for row in original.read_row_group(index).to_pylist():
            key = _key(row["date"], row["code"])
            if key in by_key:
                candidate = by_key[key]
                if any(row[field] != candidate[field] for field in candidate):
                    raise ValueError(f"Overlapping source key differs; original values retained: {key}")
                overlaps.add(key)
    appended = [row for row in additions if _key(row["date"], row["code"]) not in overlaps]
    out.mkdir(parents=True, exist_ok=False)
    temporary = out / "raw_daily.incomplete.parquet"
    final = out / "raw_daily.parquet"
    with pq.ParquetWriter(temporary, schema) as writer:
        for index in range(original.metadata.num_row_groups):
            table = original.read_row_group(index)
            writer.write_table(table)
        if appended:
            # Missing turnover/valuation columns stay null, never guessed/zero.
            writer.write_table(pa.Table.from_pylist(appended, schema=schema))
    if digest(base) != base_sha:
        raise ValueError("Original supplement changed during derivation")
    temporary.replace(final)
    result = {"schema": SCHEMA, "status": "completed_append_only_research_supplement",
        "created_at": datetime.now(CN).isoformat(), "base_file": str(base), "base_sha256": base_sha,
        "base_rows": original.metadata.num_rows, "raw_daily_file": str(final), "raw_daily_sha256": digest(final),
        "raw_daily_rows": original.metadata.num_rows + len(appended), "appended_rows": len(appended),
        "identical_overlapping_keys_not_appended": len(overlaps), "base_columns": schema.names,
        "original_full_row_prefix_preserved": True, "original_strings_and_nulls_preserved": True,
        "source_proofs": proofs, "missing_source_fields_policy": "null_never_zero",
        "current_source_overwrites_original_keys": False, **FLAGS}
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--package", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = append_sources(args.base, args.package, args.out)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
