"""Reconstruct every pinned raw row for research, never admit a trading feed."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import time
import uuid

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..market_dataset import write_json
from ..settings import DATA_ROOT
from .adjusted_prices import baseline_coverage, checked_file, reconstruct
from .price_boundaries import canonicalize, validate_rename


def build(raw_view, factor_view, destination=None, *, allow_derived=False):
    raw_view, factor_view = map(Path, (raw_view, factor_view))
    raw_result = json.loads((raw_view / "result.json").read_text(encoding="utf-8"))
    factor_result = json.loads((factor_view / "result.json").read_text(encoding="utf-8"))
    folder = Path(destination) if destination else DATA_ROOT / "research/bulk_adjusted_prices" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, folder / "implementation.py")
    conversion_source = Path(__file__).with_name("adjusted_prices.py")
    shutil.copy2(conversion_source, folder / "conversion_implementation.py")
    result = {"status": "building", "raw_view": str(raw_view), "factor_view": str(factor_view),
              "scope": "every_pinned_provider_raw_row_no_strategy_universe_selection",
              "execution_admitted": False, "independent_prices_verified": False,
              "cash_share_actions_verified": False, "rows": 0, "symbols": 0, "dates": 0,
              "started_at": datetime.now().astimezone().isoformat(),
              "implementation_sha256": sha256((folder / "implementation.py").read_bytes()).hexdigest(),
              "conversion_implementation_sha256": sha256((folder / "conversion_implementation.py").read_bytes()).hexdigest(),
              "limitations": ["Source-covered SH/SZ rows, not independently verified historical all-A/BSE/ETF coverage",
                              "Formula reconstruction, not an independent API comparison for every code",
                              "Research prices only; raw order prices and source activity fields unchanged",
                              "Blank halted prices retained; no defaults, row/code exclusions or account/feed/model changes",
                              "No point-in-time revision, actual company-action entitlement or strategy validation"]}
    write_json(folder / "result.json", result)
    start = time.monotonic()
    target = folder / "adjusted_daily.parquet"
    temporary = target.with_suffix(".partial.parquet")
    canonical_target = folder / "canonical_daily.parquet"
    canonical_temporary = canonical_target.with_suffix(".partial.parquet")
    try:
        if (raw_result["kind"] != "stock" or factor_result["kind"] != "adjustment"
                or raw_result["status"] != "source_cache_complete"
                or raw_result["acquired_days"] != raw_result["expected_days"]
                or raw_result["missing_days"] or raw_result["empty_days"]):
            raise ValueError("Complete nonempty pinned raw query calendar required; do not cut to acquired dates")
        raw_path = checked_file(raw_view, "raw_daily.parquet", raw_result["raw_daily_sha256"])
        factor_path = checked_file(factor_view, factor_result["panel_file"], factor_result["panel_sha256"])
        plan = json.loads(checked_file(raw_view, "plan.json", raw_result["plan_sha256"]).read_text(encoding="utf-8"))
        calendar_path = checked_file(raw_view, "input_calendar.csv", plan["calendar_sha256"])
        calendar = pd.read_csv(calendar_path, dtype=str, keep_default_na=False).date
        expected = calendar.loc[calendar.between(plan["requested_start"], plan["requested_end"])].tolist()
        if not expected or expected != sorted(set(expected)) or len(expected) != raw_result["expected_days"]:
            raise ValueError("Pinned calendar identities/count changed")
        derived = factor_result.get("status") == "derived_research_levels"
        if derived != allow_derived:
            raise ValueError("Derived research levels require explicit opt-in and a declared derived view")
        rename = None
        if derived:
            factor_plan = json.loads(checked_file(factor_view, "plan.json", factor_result["plan_sha256"]).read_text(encoding="utf-8"))
            if (Path(factor_plan["raw_view"]).resolve() != raw_view.resolve()
                    or factor_plan["source_hashes"]["raw"] != raw_result["raw_daily_sha256"]
                    or factor_result["rename"] != factor_plan["rename"]
                    or any(factor_result[key] is not False for key in
                           ("execution_admitted", "independent_prices_verified", "cash_share_actions_verified"))):
                raise ValueError("Derived view scope/provenance cannot be relabelled or admitted")
            rename = factor_result["rename"]
            validate_rename(rename)
            checked_file(factor_view, "rename_disclosure.pdf", rename["disclosure_sha256"])
            checked_file(factor_view, "implementation.py", factor_result["implementation_sha256"])
            shutil.copy2(Path(__file__).with_name("price_boundaries.py"), folder / "identity_implementation.py")
            result.update(derived_research_levels=True, rename=rename,
                          identity_implementation_sha256=sha256((folder / "identity_implementation.py").read_bytes()).hexdigest(),
                          derived_initial_price_rows=factor_result["derived_initial_price_rows"])
            result["limitations"].extend([
                "Initial and renamed price levels are explicit derivations, not vendor qfq verification",
                "Canonical research view shadows proven alias rows; the full converted source view retains them",
                "Only this reviewed code lineage is resolved; other identities remain provider-code hypotheses"])
        _, coverage = baseline_coverage(raw_view, factor_view, folder / "baseline_coverage")
        result["baseline_coverage_file"] = "baseline_coverage/result.json"
        if coverage["status"] != "baseline_levels_present":
            raise ValueError("Full raw-code initial/terminal factors required; missing codes not removed")
        factors = pd.read_parquet(factor_path)
        provenance = None
        if derived:
            columns = ["origin", "source_code", "source_factor_date"]
            if (not set(columns) <= set(factors) or not factors.origin.isin(
                    ["provider", "derived_ipo_continuity", "derived_code_change_scale"]).all()
                    or int(factors.origin.eq("provider").sum()) != factor_result["original_provider_rows"]
                    or int(factors.origin.eq("derived_ipo_continuity").sum()) != factor_result["derived_initial_level_count"]
                    or int(factors.origin.eq("derived_code_change_scale").sum()) != factor_result["derived_code_change_level_count"]):
                raise ValueError("Derived factor origins/counts changed")
            provenance = factors.set_index(["code", "dividOperateDate"])[columns]
        source = pq.ParquetFile(raw_path)
        symbols, dates, rows, blank_halt_prices = set(), [], 0, 0
        canonical_symbols, canonical_rows, shadow_rows, duplicate_alias_days, blank_alias_days = set(), 0, 0, 0, 0
        writer, canonical_writer = None, None
        try:
            for index in range(source.metadata.num_row_groups):
                raw = source.read_row_group(index).to_pandas()
                if raw.empty or raw.date.nunique() != 1 or raw.code.duplicated().any():
                    raise ValueError("Each source group must preserve one nonempty distinct-code day")
                day = raw.date.iloc[0]
                if day in dates or day not in expected:
                    raise ValueError("Source date duplicate or outside the complete pinned calendar")
                adjusted = reconstruct(raw, factors.loc[factors.code.isin(raw.code)], plan["requested_end"])
                if not adjusted[["code", "date"]].equals(raw[["code", "date"]]):
                    raise ValueError("Conversion changed source row identities")
                if derived:
                    origins = provenance.reindex(pd.MultiIndex.from_arrays([adjusted.code, adjusted.factor_effective_date]))
                    if origins.isna().any().any():
                        raise ValueError("Converted level lacks its explicit origin")
                    for column in origins:
                        adjusted["factor_" + column] = origins[column].to_numpy()
                    adjusted, canonical = canonicalize(raw, adjusted, rename)
                    shadows = int(adjusted.alias_shadow.sum())
                    shadow_rows += shadows
                    duplicate_alias_days += shadows if day < rename["effective_date"] else 0
                    blank_alias_days += shadows if day == rename["effective_date"] else 0
                    canonical_rows += len(canonical)
                    canonical_symbols.update(canonical.code)
                    canonical_table = pa.Table.from_pandas(canonical, preserve_index=False)
                    if canonical_writer is None:
                        canonical_writer = pq.ParquetWriter(canonical_temporary, canonical_table.schema)
                    canonical_writer.write_table(canonical_table)
                table = pa.Table.from_pandas(adjusted, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(temporary, table.schema)
                writer.write_table(table)
                rows += len(adjusted)
                symbols.update(adjusted.code)
                dates.append(day)
                blank_halt_prices += int((raw.tradestatus.eq("0") & raw.close.eq("")).sum())
                if (index + 1) % 20 == 0:
                    print(f"{index + 1}/{source.metadata.num_row_groups} dates; {rows} exact source rows", flush=True)
        finally:
            if writer is not None:
                writer.close()
            if canonical_writer is not None:
                canonical_writer.close()
        if sorted(dates) != expected or rows != raw_result["rows"] or len(symbols) != raw_result["symbols"]:
            raise ValueError("Full reconstructed row/date/code counts differ from the pinned raw input")
        if derived and (canonical_rows + shadow_rows != rows or canonical_symbols != symbols - {rename["old"]}
                        or not duplicate_alias_days or blank_alias_days != 1):
            raise ValueError("Canonical identity partition lacks the observed duplicate/blank alias proof")
        temporary.replace(target)
        if derived:
            canonical_temporary.replace(canonical_target)
            result.update(canonical_panel_file=canonical_target.name,
                          canonical_panel_sha256=sha256(canonical_target.read_bytes()).hexdigest(),
                          canonical_rows=canonical_rows, canonical_symbols=len(canonical_symbols),
                          retained_alias_shadow_rows=shadow_rows, exact_duplicate_alias_days=duplicate_alias_days,
                          retained_blank_alias_days=blank_alias_days)
        result.update(status="research_reconstruction_completed", rows=rows, symbols=len(symbols), dates=len(dates),
                      preserved_blank_halt_prices=blank_halt_prices, cutoff=plan["requested_end"],
                      panel_file=target.name, panel_sha256=sha256(target.read_bytes()).hexdigest(),
                      source_hashes={"raw": raw_result["raw_daily_sha256"], "factors": factor_result["panel_sha256"]})
    except Exception as error:
        result.update(status="failed_reconstruction", error=f"{type(error).__name__}: {error}",
                      partial_panel_file=temporary.name if temporary.exists() else None,
                      partial_canonical_panel_file=canonical_temporary.name if canonical_temporary.exists() else None)
    result.update(finished_at=datetime.now().astimezone().isoformat(), elapsed_seconds=round(time.monotonic() - start, 3))
    write_json(folder / "result.json", result)
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-view", type=Path, required=True)
    parser.add_argument("--factor-view", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--derived-research-levels", action="store_true",
                        help="Explicitly consume evidence-bound derived levels and publish a separate canonical research view")
    args = parser.parse_args(argv)
    folder, result = build(args.raw_view, args.factor_view, args.out, allow_derived=args.derived_research_levels)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0 if result["status"] == "research_reconstruction_completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
