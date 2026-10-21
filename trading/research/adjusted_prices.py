"""Source-checked research price conversion, not cash/share or feed admission."""
import argparse
from bisect import bisect_right
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid

import pandas as pd
import pyarrow.parquet as pq

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from ..market_dataset import write_frame, write_json
from ..settings import DATA_ROOT
from .daily_source_probe import ADJUSTMENT_FIELDS, WHEEL_SHA256

PRICE_FIELDS = ("open", "high", "low", "close", "preclose")
TEXT_FIELDS = {"date", "code", "adjustflag", "tradestatus", "isST"}
SOURCE_DOC = "https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf"


def numeric(value):
    if not isinstance(value, str):
        raise ValueError("Price/factor inputs must preserve provider strings")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid provider numeric field") from error
    if not result.is_finite():
        raise ValueError("Nonfinite provider numeric field")
    return result


def reconstruct(raw, factors, cutoff):
    """Use the latest effective forward level; never multiply event levels."""
    if date.fromisoformat(cutoff).isoformat() != cutoff:
        raise ValueError("Canonical cutoff required")
    if not set(BAOSTOCK_FIELDS.split(",")) <= set(raw) or not set(ADJUSTMENT_FIELDS) <= set(factors):
        raise ValueError("Provider price/factor schema incomplete")
    if raw.empty:
        raise ValueError("Empty raw price observations are not a source check")
    if raw.duplicated(["code", "date"]).any() or factors.duplicated(["code", "dividOperateDate"]).any():
        raise ValueError("Duplicate raw/factor identity must be reconciled")
    for column in (raw.date, factors.dividOperateDate):
        if any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or day > cutoff for day in column):
            raise ValueError("Noncanonical dates or source observations after cutoff")
    if not raw.adjustflag.eq("3").all() or not raw.tradestatus.isin(["0", "1"]).all():
        raise ValueError("Require raw prices and explicit trade status")
    levels = {}
    for code, group in factors.groupby("code", sort=False):
        group = group.sort_values("dividOperateDate")
        values = [numeric(value) for value in group.foreAdjustFactor]
        if any(value <= 0 for value in values):
            raise ValueError("Nonpositive forward factor")
        # A later provider anchor cannot be relabelled as a cutoff-date price.
        if values[-1] != 1:
            raise ValueError(f"Terminal forward anchor is not verified at cutoff: {code}")
        levels[code] = (group.dividOperateDate.tolist(), group.foreAdjustFactor.tolist())
    result = raw.copy(deep=True)
    effective, multipliers = [], []
    for row_number, (_, row) in enumerate(raw.iterrows()):
        if row.code not in levels:
            raise ValueError(f"Missing factor history, not assumed one: {row.code}")
        dates, values = levels[row.code]
        position = bisect_right(dates, row.date) - 1
        if position < 0:
            raise ValueError(f"Missing initial factor before first raw observation: {row.code} {row.date}")
        multiplier = numeric(values[position])
        for field in PRICE_FIELDS:
            value = row[field]
            if value == "" and row.tradestatus == "0":
                continue
            price = numeric(value)
            if price <= 0:
                raise ValueError("Nonpositive source price")
            result.iat[row_number, result.columns.get_loc(field)] = str(price * multiplier)
        effective.append(dates[position])
        multipliers.append(values[position])
    result["adjustflag"] = "2"
    result["factor_effective_date"] = effective
    result["source_forward_factor"] = multipliers
    result.attrs["price_basis"] = "reconstructed_research_only_not_order_prices"
    return result


def checked_file(folder, name, expected):
    path = folder / name
    path.resolve().relative_to(folder.resolve())
    if sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"Source hash changed: {name}")
    return path


def equivalent(field, left, right):
    if field in TEXT_FIELDS or left == "" or right == "":
        return left == right
    return numeric(left) == numeric(right)


def baseline_coverage(raw_view, factor_view, destination=None):
    raw_view, factor_view = map(Path, (raw_view, factor_view))
    raw_result = json.loads((raw_view / "result.json").read_text(encoding="utf-8"))
    factor_result = json.loads((factor_view / "result.json").read_text(encoding="utf-8"))
    if raw_result["kind"] != "stock" or factor_result["kind"] != "adjustment":
        raise ValueError("Expected raw-stock and adjustment views")
    plans = [json.loads(checked_file(view, "plan.json", result["plan_sha256"]).read_text(encoding="utf-8"))
             for view, result in ((raw_view, raw_result), (factor_view, factor_result))]
    cutoff = plans[0]["requested_end"]
    if plans[1]["requested_end"] != cutoff or date.fromisoformat(cutoff).isoformat() != cutoff:
        raise ValueError("Raw and factor views must share a canonical cutoff")
    raw_path = checked_file(raw_view, "raw_daily.parquet", raw_result["raw_daily_sha256"])
    factor_path = checked_file(factor_view, factor_result["panel_file"], factor_result["panel_sha256"])
    first = {}
    raw_file = pq.ParquetFile(raw_path)
    for index in range(raw_file.metadata.num_row_groups):
        frame = raw_file.read_row_group(index, columns=["code", "date"]).to_pandas()
        if frame.duplicated(["code", "date"]).any():
            raise ValueError("Duplicate raw identities")
        for code, day in frame.itertuples(index=False, name=None):
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or day > cutoff:
                raise ValueError("Raw observation after cutoff or noncanonical date")
            first[code] = min(first.get(code, day), day)
    if len(first) != raw_result["symbols"] or not first:
        raise ValueError("Raw source code count changed or empty")
    factors = pd.read_parquet(factor_path)
    if not set(ADJUSTMENT_FIELDS) <= set(factors):
        raise ValueError("Provider factor schema incomplete")
    if factors.duplicated(["code", "dividOperateDate"]).any():
        raise ValueError("Duplicate factor identities")
    for day, value in zip(factors.dividOperateDate, factors.foreAdjustFactor):
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or day > cutoff:
            raise ValueError("Factor observation after cutoff or noncanonical date")
        if numeric(value) <= 0:
            raise ValueError("Nonpositive forward factor")
    groups = {code: group.sort_values("dividOperateDate") for code, group in factors.groupby("code")}
    outcomes = []
    for code, day in sorted(first.items()):
        group = groups.get(code)
        initial = group.loc[group.dividOperateDate.le(day)] if group is not None else None
        terminal = group.foreAdjustFactor.iloc[-1] if group is not None else None
        has_start = initial is not None and not initial.empty
        terminal_one = terminal is not None and numeric(terminal) == 1
        outcomes.append({"code": code, "first_observed_raw_date": day,
                         "has_factor_history": group is not None,
                         "has_effective_start_level": has_start, "terminal_forward_level_one": terminal_one,
                         "start_level_date": initial.dividOperateDate.iloc[-1] if has_start else None,
                         "terminal_level_date": group.dividOperateDate.iloc[-1] if group is not None else None,
                         "terminal_forward_level": terminal})
    missing_history = [item["code"] for item in outcomes if not item["has_factor_history"]]
    missing_start = [item["code"] for item in outcomes if not item["has_effective_start_level"]]
    bad_terminal = [item["code"] for item in outcomes if not item["terminal_forward_level_one"]]
    complete = sum(item["has_effective_start_level"] and item["terminal_forward_level_one"] for item in outcomes)
    folder = Path(destination) if destination else DATA_ROOT / "research/adjustment_baseline_coverage" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, folder / "implementation.py")
    result = {"scope": "every_observed_raw_code_start_and_terminal_levels_not_adjusted_feed_admission",
              "status": "baseline_levels_present" if complete == len(first) else "baseline_incomplete",
              "raw_view": str(raw_view), "factor_view": str(factor_view), "raw_codes": len(first),
              "cutoff": cutoff,
              "codes_with_start_and_terminal_levels": complete,
              "missing_history_codes": missing_history, "missing_start_level_codes": missing_start,
              "terminal_not_one_codes": bad_terminal, "outcomes": outcomes,
              "execution_admitted": False, "adjusted_prices_verified": False,
              "source_hashes": {"raw": raw_result["raw_daily_sha256"], "factors": factor_result["panel_sha256"]},
              "limitations": ["Covers all observed provider codes, not independently verified historical all-A membership",
                              "First observed date can move earlier as the fixed raw backfill continues",
                              "Price quality, factor semantics, cash/share actions and source completeness are separate",
                              "No missing factor replaced by one, no codes excluded, no prices/backtest/account generated"]}
    write_json(folder / "result.json", result)
    return folder, result


def check(probe_folder, prior, raw_view, factor_view, destination=None):
    probe_folder, prior, raw_view, factor_view = map(Path, (probe_folder, prior, raw_view, factor_view))
    receipt = json.loads((probe_folder / "receipt.json").read_text(encoding="utf-8"))
    if receipt["status"] != "completed_probe" or receipt["sdk"]["sha256"] != WHEEL_SHA256:
        raise ValueError("Require completed source probe with the reviewed SDK")
    checked_file(probe_folder, "collection_implementation.py", receipt["implementation_sha256"])
    codes, cutoff = receipt["codes"], receipt["end"]
    if not codes or len(set(codes)) != len(codes):
        raise ValueError("Distinct declared probe codes required")
    entries = {}
    for entry in receipt["entries"]:
        identity = entry["code"], entry["kind"]
        expected_name = entry["code"].replace(".", "_") + "_" + entry["kind"] + ".parquet"
        if (identity in entries or entry["code"] not in codes or entry["kind"] not in {"factors", "adjusted"}
                or entry["end"] != cutoff or entry["error_code"] != "0" or entry["file"] != expected_name):
            raise ValueError("Invalid probe source identity")
        payload = pd.read_parquet(checked_file(probe_folder, entry["file"], entry["sha256"]))
        if payload.empty or len(payload) != entry["rows"] or not payload.code.eq(entry["code"]).all():
            raise ValueError("Probe source code/count changed")
        entries[identity] = payload
    if set(entries) != {(code, kind) for code in codes for kind in ("factors", "adjusted")}:
        raise ValueError("Probe pairs incomplete")
    raw_result = json.loads((raw_view / "result.json").read_text(encoding="utf-8"))
    factor_result = json.loads((factor_view / "result.json").read_text(encoding="utf-8"))
    if raw_result["kind"] != "stock" or factor_result["kind"] != "adjustment":
        raise ValueError("Expected actual raw-stock and factor views")
    raw_path = checked_file(raw_view, "raw_daily.parquet", raw_result["raw_daily_sha256"])
    factor_path = checked_file(factor_view, factor_result["panel_file"], factor_result["panel_sha256"])
    fields = BAOSTOCK_FIELDS.split(",")
    prior_raw = pd.read_parquet(prior / "raw.parquet", columns=fields, filters=[("code", "in", codes)])
    bulk_raw = pd.read_parquet(raw_path, columns=fields, filters=[("code", "in", codes)])
    daily_factors = pd.read_parquet(factor_path, filters=[("code", "in", codes)])
    raw_rows, overlap = {}, 0
    for payload in (prior_raw, bulk_raw):
        if payload.duplicated(["code", "date"]).any():
            raise ValueError("Duplicate source raw prices")
        for row in payload.to_dict("records"):
            identity = row["code"], row["date"]
            if identity in raw_rows:
                if any(not equivalent(field, raw_rows[identity][field], row[field]) for field in fields):
                    raise ValueError(f"Raw sources disagree: {identity}")
                overlap += 1
            raw_rows[identity] = row
    merged_raw = pd.DataFrame(raw_rows.values()).sort_values(["code", "date"])
    outcomes, prices = [], []
    for code in codes:
        factors, observed = entries[(code, "factors")], entries[(code, "adjusted")]
        raw = merged_raw.loc[merged_raw.code.eq(code) & merged_raw.date.between("2023-01-01", cutoff)]
        if set(raw.date) != set(observed.date) or observed.date.duplicated().any() or not observed.adjustflag.eq("2").all():
            raise ValueError("Raw/observed adjusted prices are not aligned")
        converted = reconstruct(raw, factors, cutoff).set_index("date")
        observed = observed.set_index("date")
        mismatches, compared, blank_pairs = [], 0, 0
        for day, row in converted.iterrows():
            for field in PRICE_FIELDS:
                actual, expected = observed.at[day, field], row[field]
                if actual == "" and expected == "" and row.tradestatus == "0":
                    blank_pairs += 1
                    continue
                actual_number, expected_number = numeric(actual), numeric(expected)
                rounding_bound = Decimal(10) ** actual_number.as_tuple().exponent / 2
                compared += 1
                if abs(actual_number - expected_number) > rounding_bound:
                    mismatches.append({"date": day, "field": field, "observed": actual,
                                       "converted": expected, "rounding_bound": str(rounding_bound)})
            for field in ("volume", "amount", "turn", "tradestatus", "isST"):
                if not equivalent(field, row[field], observed.at[day, field]):
                    mismatches.append({"date": day, "field": field, "observed": observed.at[day, field], "converted": row[field]})
        dated = factors.loc[factors.dividOperateDate.between("2023-01-01", cutoff)].set_index("dividOperateDate")
        bulk = daily_factors.loc[daily_factors.code.eq(code)].set_index("dividOperateDate")
        factor_differences = []
        for day in sorted(set(dated.index) | set(bulk.index)):
            if day not in dated.index or day not in bulk.index:
                factor_differences.append({"date": day, "field": "missing_source_date"})
            else:
                for field in ADJUSTMENT_FIELDS[2:]:
                    if not equivalent(field, dated.at[day, field], bulk.at[day, field]):
                        factor_differences.append({"date": day, "field": field,
                                                   "per_code": dated.at[day, field], "daily": bulk.at[day, field]})
        outcomes.append({"code": code, "raw_rows": len(raw), "price_fields_compared": compared,
                         "preserved_blank_price_pairs": blank_pairs, "price_or_activity_mismatches": mismatches,
                         "factor_api_differences": factor_differences})
        prices.append(converted.reset_index())
    folder = Path(destination) if destination else DATA_ROOT / "research/adjusted_price_checks" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, folder / "implementation.py")
    write_frame(folder / "reconstructed_adjusted.parquet", pd.concat(prices, ignore_index=True))
    mismatches = sum(len(item["price_or_activity_mismatches"]) for item in outcomes)
    differences = sum(len(item["factor_api_differences"]) for item in outcomes)
    result = {"status": "source_check_passed" if not (mismatches or differences) else "source_check_failed",
              "scope": "explicit_source_semantic_cases_not_a_strategy_universe_or_full_market_price_feed",
              "source_document": SOURCE_DOC, "probe_folder": str(probe_folder), "codes": codes, "cutoff": cutoff,
              "raw_overlap_rows_verified": overlap, "rows": sum(item["raw_rows"] for item in outcomes),
              "price_fields_compared": sum(item["price_fields_compared"] for item in outcomes),
              "price_or_activity_mismatch_count": mismatches, "factor_api_difference_count": differences,
              "source_hashes": {"probe_receipt": sha256((probe_folder / "receipt.json").read_bytes()).hexdigest(),
                                "prior_raw": sha256((prior / "raw.parquet").read_bytes()).hexdigest(),
                                "raw_view": raw_result["raw_daily_sha256"], "factor_view": factor_result["panel_sha256"]},
              "outcomes": outcomes, "execution_admitted": False, "cash_share_actions_verified": False,
              "limitations": ["Same provider, not independent source verification",
                              "Only four prior anchor discrepancy cases and one control, not all-market adjusted coverage",
                              "Source terminal anchor and starting factor required; missing levels/prices not imputed",
                              "Derived prices are research only, not shareholder total returns or order prices",
                              "No ready manifest, default feed/account change, backtest, model fit or order"]}
    write_json(folder / "result.json", result)
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw-view", "factor-view"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("probe", "prior"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--coverage-only", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.coverage_only:
        folder, result = baseline_coverage(args.raw_view, args.factor_view, args.out)
        print(json.dumps({"folder": str(folder), **{k: v for k, v in result.items() if k not in
                          {"outcomes", "missing_history_codes", "missing_start_level_codes", "terminal_not_one_codes"}},
                          "missing_history_count": len(result["missing_history_codes"]),
                          "missing_start_level_count": len(result["missing_start_level_codes"]),
                          "terminal_not_one_count": len(result["terminal_not_one_codes"])}, indent=2))
        return 0 if result["status"] == "baseline_levels_present" else 1
    if args.probe is None or args.prior is None:
        parser.error("Source price check requires --probe and --prior")
    folder, result = check(args.probe, args.prior, args.raw_view, args.factor_view, args.out)
    print(json.dumps({"folder": str(folder), **{k: v for k, v in result.items() if k != "outcomes"}}, indent=2))
    return 0 if result["status"] == "source_check_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
