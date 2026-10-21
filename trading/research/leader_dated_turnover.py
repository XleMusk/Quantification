"""Offline dated turnover/lagged-industry sidecar, without a new source call.

The 12 October input remains a task in its ORIGINAL multi-day cache receipt.
It is not rewritten as a single-call SDK probe or a new complete-close package.
"""
from datetime import datetime
from hashlib import sha256
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from .baostock_daily_slice import FIELDS
from .baostock_bulk_extras import FLAGS as EXTRAS_FLAGS, SCHEMA as EXTRAS_SCHEMA, UNIT_EVIDENCE

SCHEMA = "leader_dated_turnover_sector_research_manifest_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "provider_contacted": False,
         "sdk_called": False, "source_snapshots_modified": False, "single_call_receipt_fabricated": False,
         "turnover_denominator_independently_verified": False, "industry_PIT_verified": False,
         "theme_membership_verified": False, "strategy_validated": False, "selection_enabled": False,
         "execution_admitted": False, "orders_generated": False, "ledger_mutated": False,
         "model_refit": False, "current_signal_admitted": False}
DAY12, DAY13 = "2026-10-12", "2026-10-13"


def _stamp(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Dated turnover evidence requires timezone-aware actual clocks")
    return parsed


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf8")


def _numeric_turn(rows):
    values = pd.to_numeric(rows.turn, errors="coerce")
    return values.where(np.isfinite(values) & values.ge(0)).astype("Float64")


def _stats(rows):
    active = (rows.tradestatus.eq("1") & rows.isST.eq("0")
              & pd.to_numeric(rows.volume, errors="coerce").gt(0)
              & pd.to_numeric(rows.amount, errors="coerce").gt(0))
    return {"ordinary_rows": len(rows), "known_turnover_rows": int(rows.turn_percent.notna().sum()),
            "unknown_turnover_rows": int(rows.turn_percent.isna().sum()),
            "halt_rows": int(rows.tradestatus.eq("0").sum()), "active_non_ST_rows": int(active.sum()),
            "active_non_ST_known_turnover_rows": int(rows.loc[active, "turn_percent"].notna().sum())}


def _industry(path, now):
    path = Path(path).resolve()
    receipt_path = path.parent / "result.json"
    receipt = _read(receipt_path)
    entries = [item for item in receipt["entries"] if (path.parent / item["file"]).resolve() == path]
    if (receipt.get("source") != "BaoStock.query_stock_industry(date=decision_day)"
            or receipt.get("execution_admitted") is not False or len(entries) != 1):
        raise ValueError("One exact saved, unadmitted dated industry receipt is required")
    entry = entries[0]
    if entry["date"] > DAY12 or _stamp(entry["retrieved_at"]) > now:
        raise ValueError("Lagged industry snapshot or its actual receipt is future/unavailable")
    frame = pd.read_parquet(path)
    if (len(frame) != entry["rows"] or not frame.date.eq(entry["date"]).all()
            or frame.symbol.duplicated().any() or frame.updateDate.eq("").any()
            or frame.updateDate.gt(entry["date"]).any()):
        raise ValueError("Industry snapshot identities, dates or saved row count changed")
    return {"file": str(path), "sha256": _digest(path), "receipt_file": str(receipt_path),
            "receipt_sha256": _digest(receipt_path), "snapshot_day": entry["date"],
            "known_at": entry["retrieved_at"], "rows": len(frame),
            "classification_semantics": "lagged_broad_industry_not_current_theme_or_revision_safe_PIT"}


def _derive12(cache, close, now):
    cache, close = Path(cache).resolve(), Path(close).resolve()
    plan_file, cache_file = cache / "plan.json", cache / "receipts.json"
    plan, receipt = _read(plan_file), _read(cache_file)
    task = receipt["tasks"].get(DAY12)
    if (plan.get("kind") != "stock" or DAY12 not in plan.get("days", [])
            or plan.get("execution_admitted") is not False
            or receipt.get("status") != "source_cache_complete" or receipt.get("execution_admitted") is not False
            or set(receipt["tasks"]) != set(plan["days"]) or not task
            or task.get("date") != DAY12 or task.get("kind") != "stock"
            or task.get("method") != "query_daily_history_k_AStock" or task.get("error_code") != "0"
            or task.get("file") != DAY12 + "_stock.csv"):
        raise ValueError("12 October needs its exact successful original multi-day task, never a fabricated single probe")
    source_available = _stamp(receipt["updated_at"])
    if not _stamp(DAY12 + "T15:00:00+08:00") <= source_available <= now:
        raise ValueError("Original multi-day cache completion bound is unavailable")
    raw_file = cache / task["file"]
    if _digest(raw_file) != task["sha256"]:
        raise ValueError("Original 12 October date-entry CSV hash changed")
    raw = pd.read_csv(raw_file, dtype=str, keep_default_na=False)
    if (list(raw) != BAOSTOCK_FIELDS.split(",") or raw.empty or not raw.date.eq(DAY12).all()
            or raw.code.duplicated().any() or not raw.code.str.fullmatch(r"(?:sh|sz)\.\d{6}").all()
            or not raw.adjustflag.eq("3").all()
            or not raw.tradestatus.isin(["0", "1"]).all() or not raw.isST.isin(["0", "1"]).all()
            or task.get("quality", {}).get("rows") != len(raw)):
        raise ValueError("Original 18-field date/schema/status inventory changed")
    close_plan_file, close_receipt_file = close / "plan.json", close / "receipt.json"
    close_plan, close_receipt = _read(close_plan_file), _read(close_receipt_file)
    if (close_plan.get("schema") != "baostock_full_dated_close_inventory_v1"
            or close_plan.get("day") != DAY12 or close_receipt.get("day") != DAY12
            or close_receipt.get("status") != "complete_full_dated_close_inventory_not_admitted"
            or close_receipt.get("full_dated_roster_inventory_complete") is not True
            or close_receipt.get("roster_pinned_date") != DAY12
            or close_receipt.get("execution_admitted") is not False or close_receipt.get("pending_symbols")
            or _stamp(close_receipt["updated_at"]) > now):
        raise ValueError("One completed own-day 12 October close inventory is required")
    close_raw_file = close / "raw_daily.csv"
    if _digest(close_raw_file) != close_receipt["raw_daily_sha256"]:
        raise ValueError("Own-day close raw projection changed")
    current = pd.read_csv(close_raw_file, dtype=str, keep_default_na=False)
    roster_file = close / "ordinary_roster.csv"
    roster = pd.read_csv(roster_file, dtype=str, keep_default_na=False)
    symbols = current.code.str[3:] + current.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})
    if (list(current) != FIELDS.split(",") or not current.date.eq(DAY12).all()
            or current.code.duplicated().any() or set(symbols) != set(close_plan["symbols"])
            or len(current) != close_plan["ordinary_symbols"] or len(current) != close_receipt["completed_rows"]
            or roster.code.duplicated().any() or set(roster.code) != set(current.code)):
        raise ValueError("Full own-roster 12 October identities were reduced or changed")
    ordinary_mask = raw.code.str.fullmatch(r"(?:sh\.(?:60\d{4}|688\d{3})|sz\.(?:00\d{4}|30\d{4}))")
    if set(raw.loc[ordinary_mask, "code"]) != set(current.code):
        raise ValueError("Original bulk ordinary scope disagrees with its own full dated roster")
    if set(raw.code) - set(current.code) - {"sh.689009"}:
        raise ValueError("Only the recorded nonordinary 689009 exclusion is supported")
    aligned = raw.set_index("code").loc[current.code].reset_index()
    # Every common original STRING is identical, including blanks and precision.
    if not aligned[FIELDS.split(",")].equals(current.reset_index(drop=True)):
        raise ValueError("One of the 13 common original values differs from the independent own-day close")
    if (roster.set_index("code").loc[current.code, "tradeStatus"].tolist() != aligned.tradestatus.tolist()
            or aligned.loc[aligned.tradestatus.eq("0"), "turn"].ne("").any()):
        raise ValueError("Own-roster status or original nullable halted turnover changed")
    aligned["symbol"] = symbols.to_numpy()
    aligned["turn_percent"] = _numeric_turn(aligned)
    source = {"source_kind": "original_multiday_bulk_task_aligned_to_independent_own_close",
              "source_cache_plan": str(plan_file), "source_cache_plan_sha256": _digest(plan_file),
              "source_cache_receipts": str(cache_file), "source_cache_receipts_sha256": _digest(cache_file),
              "original_cache_task_count": len(receipt["tasks"]), "original_date_entry": task,
              "original_raw_file": str(raw_file), "original_raw_sha256": task["sha256"],
              "raw_source_available_at": source_available.isoformat(),
              "raw_clock_basis": "original_multi_day_receipts_updated_at_conservative_completion_bound_not_invented_per_call_clock",
              "own_close_folder": str(close), "own_close_plan_sha256": _digest(close_plan_file),
              "own_close_receipt_sha256": _digest(close_receipt_file), "own_close_raw_sha256": close_receipt["raw_daily_sha256"],
              "own_roster_file": str(roster_file), "own_roster_sha256": _digest(roster_file),
              "own_close_available_at": close_receipt["updated_at"],
              "all_13_common_original_values_exact": True,
              "excluded_nonordinary_codes": sorted(set(raw.code) - set(current.code)),
              "original_fields": list(raw), "original_strings_and_halt_null_preserved": True}
    return aligned, source


def _existing_typed_extras(path, now):
    path = Path(path).resolve()
    record = _read(path)
    now = _stamp(now)
    day = record.get("day")
    try:
        source_close = _stamp(datetime.strptime(day, "%Y-%m-%d").date().isoformat() + "T15:00:00+08:00")
    except (TypeError, ValueError) as error:
        raise ValueError("Typed extras require one exact ISO source date") from error
    if source_close.date().isoformat() != day:
        raise ValueError("Typed extras require one exact ISO source date")
    if (record.get("schema") != EXTRAS_SCHEMA
            or record.get("status") != "completed_dated_bulk_extras_research_only"
            or any(record.get(key) is not value for key, value in EXTRAS_FLAGS.items())
            or record.get("turn_numeric_unit") != "percent" or record.get("turn_scaled") is not False
            or not source_close <= _stamp(record["source_inputs_available_at"])
            <= _stamp(record["actual_derived_at"]) <= _stamp(record["available_at"]) <= now):
        raise ValueError("Dated turnover must reuse exact completed, available percentage-unit extras")
    data_file = Path(record["data_file"]).resolve()
    if _digest(data_file) != record["data_sha256"]:
        raise ValueError("Existing dated extras parquet changed")
    rows = pd.read_parquet(data_file)
    price_receipt_file = Path(record["package"]).resolve() / "receipt.json"
    price_receipt = _read(price_receipt_file)
    units = _read(record["unit_evidence_file"])
    if (_digest(price_receipt_file) != record["package_receipt_sha256"]
            or price_receipt.get("day") != day
            or price_receipt.get("status") != "complete_full_dated_close_inventory_not_admitted"
            or _digest(record["unit_evidence_file"]) != record["unit_evidence_sha256"]
            or units != record["unit_evidence"] or units.get("turn_unit") != "percent"
            or units.get("status") != "official_provider_document_read"
            or _stamp(units["retrieved_at"]) > now):
        raise ValueError("Existing dated price or provider-unit evidence binding changed")
    if (len(rows) != record["ordinary_rows"] or not rows.date.eq(day).all()
            or rows.symbol.duplicated().any() or not rows.tradestatus.isin(["0", "1"]).all()
            or not rows.isST.isin(["0", "1"]).all()):
        raise ValueError("Existing dated full own-roster scope changed")
    parsed = _numeric_turn(rows)
    if not np.allclose(parsed.to_numpy(dtype=float, na_value=np.nan), rows.turn_numeric.to_numpy(dtype=float, na_value=np.nan), rtol=0, atol=0, equal_nan=True):
        raise ValueError("Dated parsed percentage values differ from original strings")
    rows = rows.assign(turn_percent=rows.turn_numeric)
    return rows, {"day": day, "kind": "existing_typed_dated_bulk_extras",
                  "data_file": str(data_file), "data_sha256": record["data_sha256"],
                  "extras_result": str(path), "extras_result_sha256": _digest(path),
                  "available_at": record["available_at"], "derived_at": record["actual_derived_at"],
                  "raw_source_available_at": record["source_inputs_available_at"],
                  "price_source_raw_sha256": price_receipt["raw_daily_sha256"],
                  "price_source_receipt_file": str(price_receipt_file),
                  "data_rederived": False, **_stats(rows)}


def _existing13(path, now):
    """Keep the fixed historical 12/13 sidecar contract unchanged."""
    rows, metadata = _existing_typed_extras(path, now)
    if metadata["day"] != DAY13:
        raise ValueError("The historical 12/13 sidecar requires exact 13 October extras")
    return rows, metadata


def build(cache12, close12, extras13, industry, out, *, now=None, unit_evidence=UNIT_EVIDENCE):
    """Create one NEW sidecar; never create a fake bulk-importer/SDK receipt."""
    started = _stamp(now or datetime.now().astimezone())
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Dated turnover sidecar needs a new independent output directory")
    units = _read(unit_evidence)
    if units.get("turn_unit") != "percent" or units.get("status") != "official_provider_document_read" or _stamp(units["retrieved_at"]) > started:
        raise ValueError("Saved original provider percentage-unit evidence is required")
    mapping = _industry(industry, started)
    rows12, source12 = _derive12(cache12, close12, started)
    _, day13 = _existing13(extras13, started)
    ready = max(_stamp(source12["raw_source_available_at"]), _stamp(source12["own_close_available_at"]),
                _stamp(units["retrieved_at"]), _stamp(mapping["known_at"]))
    out.mkdir(parents=True, exist_ok=False)
    data12 = out / "20261012_turnover.parquet"
    rows12.to_parquet(data12, index=False)
    finished = _stamp(now or datetime.now().astimezone())
    day12 = {"day": DAY12, "kind": "new_offline_own_roster_aligned_multiday_turnover",
             "data_file": str(data12), "data_sha256": _digest(data12), "available_at": finished.isoformat(),
             "derived_at": finished.isoformat(), "source_inputs_available_at": ready.isoformat(),
             **source12, **_stats(rows12)}
    result = {"schema": SCHEMA, "status": "completed_offline_dated_turnover_sector_research",
              "started_at": started.isoformat(), "derived_at": finished.isoformat(), "available_at": finished.isoformat(),
              "dates": {DAY12: day12, DAY13: day13}, "industry": mapping,
              "unit_evidence_file": str(Path(unit_evidence).resolve()), "unit_evidence_sha256": _digest(unit_evidence),
              "turn_unit": "percent", "turn_scaled": False,
              "scope": "each_own_dated_full_SH_SZ_ordinary_roster_all_status_and_ST_retained",
              "independent_circulating_shares": None,
              "limitations": ["Daily provider turnover is not independent circulating-share or intraday absorption evidence",
                              "Lagged broad industries are not current themes or revision-safe historical membership",
                              "Actual derivation clocks are not backfilled as historical signal publication"], **FLAGS}
    _write(out / "manifest.json", result)
    return out / "manifest.json", result


def load_dated_turnover(manifest_path, day, *, observed_at):
    """Return (metadata, full-own-roster frame) for a consumer, without source calls."""
    path = Path(manifest_path).resolve()
    record = _read(path)
    observed = _stamp(observed_at)
    if (record.get("schema") != SCHEMA or record.get("status") != "completed_offline_dated_turnover_sector_research"
            or any(record.get(key) is not value for key, value in FLAGS.items())
            or record.get("turn_unit") != "percent" or record.get("turn_scaled") is not False
            or not _stamp(record["started_at"]) <= _stamp(record["derived_at"]) <= _stamp(record["available_at"]) <= observed):
        raise ValueError("Dated turnover manifest boundaries or actual availability changed")
    entry = record["dates"][day]
    if entry.get("day") != day or _stamp(entry["available_at"]) > observed:
        raise ValueError("Dated turnover sidecar does not support this exact available date")
    if day == DAY13:
        if _digest(entry["extras_result"]) != entry["extras_result_sha256"]:
            raise ValueError("Existing 13 October typed metadata changed")
        rows, existing = _existing13(entry["extras_result"], observed)
        if existing != entry:
            raise ValueError("Existing 13 October data binding changed")
    elif day == DAY12 and entry.get("all_13_common_original_values_exact") is True:
        data_file = Path(entry["data_file"]).resolve()
        data_file.relative_to(path.parent)
        if _digest(data_file) != entry["data_sha256"]:
            raise ValueError("New aligned 12 October sidecar parquet changed")
        rows = pd.read_parquet(data_file)
    else:
        raise ValueError("No date interpolation or unbound turnover source is allowed")
    if (len(rows) != entry["ordinary_rows"] or not rows.date.eq(day).all() or rows.symbol.duplicated().any()
            or _stats(rows) != {key: entry[key] for key in _stats(rows)}):
        raise ValueError("Full own-roster turnover data or unknown counts changed")
    mapping = record["industry"]
    if (_digest(record["unit_evidence_file"]) != record["unit_evidence_sha256"]
            or _digest(mapping["receipt_file"]) != mapping["receipt_sha256"]
            or _digest(mapping["file"]) != mapping["sha256"]
            or _stamp(mapping["known_at"]) > observed or mapping["snapshot_day"] > day):
        raise ValueError("Lagged industry source or availability changed")
    industry = pd.read_parquet(mapping["file"])[["symbol", "industry", "industryClassification", "updateDate"]]
    result = rows[["date", "symbol", "code", "turn", "turn_percent", "tradestatus", "isST", "volume", "amount"]].copy()
    result = result.rename(columns={"turn": "turn_raw"})
    result = result.merge(industry.rename(columns={"updateDate": "industry_update_date"}), on="symbol", how="left", validate="one_to_one")
    result["industry_snapshot_day"], result["industry_known_at"] = mapping["snapshot_day"], mapping["known_at"]
    result["turn_source_known_at"] = entry["available_at"]
    metadata = {**entry, "manifest_file": str(path), "manifest_available_at": record["available_at"],
                "industry": mapping, "turn_unit": "percent", "full_roster_retained": True, **FLAGS}
    return metadata, result


def load_turnover_source(source_path, *, observed_at, day=None):
    """Thin fixed-unit batch for the three-pattern consumer; no new derivation."""
    path = Path(source_path).resolve()
    source = _read(path)
    if source.get("schema") == SCHEMA:
        day = day or DAY12
        metadata, rows = load_dated_turnover(path, day, observed_at=observed_at)
        price_sha = metadata.get("price_source_raw_sha256", metadata.get("own_close_raw_sha256"))
        available = max(_stamp(metadata["available_at"]), _stamp(metadata["manifest_available_at"])).isoformat()
    elif source.get("schema") == EXTRAS_SCHEMA:
        if day is not None and day != source.get("day"):
            raise ValueError("Requested turnover date differs from its exact typed source date")
        rows, metadata = _existing_typed_extras(path, _stamp(observed_at))
        day = metadata["day"]
        rows = rows.rename(columns={"turn": "turn_raw"})
        price_sha, available = metadata["price_source_raw_sha256"], metadata["available_at"]
    else:
        raise ValueError("Only the aligned historical sidecar or exact completed dated extras are supported")
    records = []
    for row in rows[["date", "symbol", "turn_raw", "turn_percent", "tradestatus", "isST", "volume", "amount"]].to_dict("records"):
        row["turn_percent"] = None if pd.isna(row["turn_percent"]) else float(row["turn_percent"])
        records.append(row)
    return {"day": day, "available_at": available, "unit": "percent", "units_verified": True,
            "unit_verification_scope": "saved_official_provider_document_not_independent_exchange_denominator",
            "price_source_raw_sha256": price_sha, "rows": records, "sourceproof": metadata, **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cache12", "close12", "extras13", "industry", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    path, result = build(args.cache12, args.close12, args.extras13, args.industry, args.out)
    print(json.dumps({"manifest": str(path), "status": result["status"], "available_at": result["available_at"],
                      "dates": list(result["dates"]), "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
