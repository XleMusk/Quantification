"""Reconcile one preserved daily bulk response with its own dated full roster.

This importer performs no provider calls. It preserves the real bulk call and
nullable suspension payload, and never fabricates per-stock dailyplus receipts.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from ..market_dataset import write_frame, write_json
from . import daily_source_probe
from .baostock_all_stock_probe import load_cached_day
from .baostock_daily_slice import FIELDS, NUMERIC, QUALITY_CONTRACT, digest
from .baostock_full_close import CN, historical_closed_scope, roster_symbols
from .daily_source_probe import WHEEL_SHA256

SCHEMA = "baostock_bulk_exact_dated_close_inventory_v1"
COMPLETE = "complete_full_dated_close_inventory_not_admitted"
FLAGS = {"source_verified": False, "pit_verified": False, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "stockdb_modified": False, "ledger_mutated": False}


def _stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Bulk source clocks require an explicit timezone")
    return result.astimezone(CN)


def _market_quality(frame):
    values = frame[NUMERIC].apply(pd.to_numeric, errors="coerce")
    halted = frame.tradestatus.eq("0")
    nullable = ["volume", "amount", "pctChg"]
    permitted = pd.DataFrame(False, index=frame.index, columns=NUMERIC)
    for field in nullable:
        permitted[field] = halted & frame[field].eq("")
    finite = pd.DataFrame(np.isfinite(values.to_numpy()), index=frame.index, columns=NUMERIC)
    if not (finite | permitted).all().all():
        raise ValueError("Bulk normal/nonnullable market fields are missing or nonfinite")
    prices = values[["open", "high", "low", "close", "preclose"]]
    if (prices.le(0).any().any() or values[["volume", "amount"]].lt(0).any().any()
            or (values.volume.notna() & values.volume.mod(1).ne(0)).any()):
        raise ValueError("Bulk prices or share activity are invalid")
    if (values.high.lt(values[["open", "close", "low"]].max(axis=1)).any()
            or values.low.gt(values[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("Bulk OHLC shape is inconsistent")
    active = values.volume.gt(0)
    vwap = values.amount.div(values.volume.where(active))
    if ((active & (vwap.lt(values.low-.0011) | vwap.gt(values.high+.0011))).any()
            or (values.volume.eq(0) & values.amount.ne(0)).any()):
        raise ValueError("Bulk amount/volume is inconsistent with prices")
    if ((values.loc[halted, nullable].notna() & values.loc[halted, nullable].ne(0)).any().any()
            or values.loc[halted, ["open", "high", "low", "close"]]
                .sub(values.loc[halted, "preclose"], axis=0).abs().gt(.0001).any().any()):
        raise ValueError("Bulk suspension payload contradicts nullable/zero activity")
    unpriceable = permitted.any(axis=1)
    return {"quality_contract": QUALITY_CONTRACT, "inventory_complete": True,
        "ordinary_rows": len(frame), "nullable_suspension_rows": int(unpriceable.sum()),
        "suspended_rows": int(halted.sum()), "normal_trading_rows": int((~halted).sum()),
        "execution_bar_coverage_complete": not bool(unpriceable.any()),
        "execution_bar_rows": int((~unpriceable).sum()),
        "unpriceable_source_codes": frame.loc[unpriceable, "code"].tolist(),
        "unit_semantics": "shares_RMB_amount_over_volume_inside_OHLC_not_independent_exchange_proof"}


def _inputs(bulk_probe, roster, calendar, observed, *, implementation):
    bulk_probe, roster, calendar = (Path(path).resolve() for path in (bulk_probe, roster, calendar))
    receipt_path = bulk_probe / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    entries = receipt.get("entries", [])
    if (receipt.get("status") != "completed_probe" or receipt.get("kinds") != ["stock"]
            or len(entries) != 1 or receipt.get("execution_admitted") is not False
            or receipt.get("full_history_verified") is not False):
        raise ValueError("Expected one preserved successful, unadmitted stock bulk probe")
    entry = entries[0]
    day = entry.get("date")
    if (receipt.get("days") != [day] or entry.get("kind") != "stock"
            or entry.get("method") != "query_daily_history_k_AStock" or entry.get("error_code") != "0"
            or entry.get("file") != f"{day}_stock.csv"):
        raise ValueError("Bulk source call/date/file identity changed")
    request = historical_closed_scope(day, calendar, observed)
    created, finished = _stamp(receipt["created_at"]), _stamp(receipt["finished_at"])
    if created < _stamp(day+"T15:00:00+08:00") or finished < created or finished > observed:
        raise ValueError("Bulk source clocks contradict a recorded completed close")
    sdk = receipt.get("sdk", {})
    if (sdk.get("sha256") != WHEEL_SHA256 or sdk.get("distribution_version") != "0.9.4"
            or digest(sdk["wheel"]) != WHEEL_SHA256):
        raise ValueError("Bulk source SDK differs from the pinned distribution")
    implementation = Path(implementation).resolve()
    if digest(implementation) != receipt.get("implementation_sha256"):
        raise ValueError("Preserved bulk implementation does not match acquisition receipt")
    raw_path = bulk_probe / entry["file"]
    if digest(raw_path) != entry.get("sha256"):
        raise ValueError("Bulk source raw hash changed")
    raw = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
    if (list(raw) != BAOSTOCK_FIELDS.split(",") or raw.empty
            or not raw.date.eq(day).all() or raw.duplicated(["date", "code"]).any()
            or not raw.code.str.fullmatch(r"(?:sh|sz)\.\d{6}").all()
            or not raw.adjustflag.eq("3").all() or not raw.tradestatus.isin(["0", "1"]).all()
            or not raw.isST.isin(["0", "1"]).all()):
        raise ValueError("Bulk full-field schema/date/identity/raw/status validation failed")
    old_quality = entry.get("quality", {})
    if (old_quality.get("rows") != len(raw) or old_quality.get("symbols") != raw.code.nunique()
            or set(old_quality.get("issues", [])) - {"missing_or_nonfinite_market_fields"}):
        raise ValueError("Bulk original row count or unresolved acquisition quality changed")
    roster_receipt, raw_roster, pin = load_cached_day(roster, day)
    roster_completed = _stamp(roster_receipt["completed_at"])
    if roster_completed > observed or roster_completed < _stamp(day+"T15:00:00+08:00"):
        raise ValueError("Dated roster completion clock is unavailable or inconsistent")
    ordinary = roster_symbols(raw_roster)
    # The exact target-day roster defines both membership and status. A later
    # roster and a survivor-filtered bulk subset are never accepted here.
    from .baostock_full_close import ORDINARY
    bulk_ordinary = raw.loc[raw.code.map(lambda code: ORDINARY.fullmatch(code) is not None)]
    if set(bulk_ordinary.code) != set(ordinary.code):
        raise ValueError("Bulk ordinary code set differs from its own exact dated full roster")
    bulk_ordinary = bulk_ordinary.set_index("code").loc[ordinary.code].reset_index()
    if bulk_ordinary.tradestatus.tolist() != ordinary.tradeStatus.tolist():
        raise ValueError("Bulk and same-day roster trading statuses disagree")
    panel = bulk_ordinary.loc[:, FIELDS.split(",")].copy()
    quality = _market_quality(panel)
    return {"day": day, "bulk_probe": bulk_probe, "bulk_receipt": receipt,
        "bulk_receipt_sha256": digest(receipt_path), "bulk_raw_path": raw_path,
        "bulk_raw_sha256": digest(raw_path), "bulk_total_rows": len(raw),
        "excluded_nonordinary_codes": sorted(set(raw.code)-set(ordinary.code)),
        "implementation": implementation, "roster_folder": roster,
        "roster_receipt_sha256": digest(roster/"receipt.json"), "roster_raw_sha256": pin["verified_current_raw_sha256"],
        "roster_completed_at": roster_completed.isoformat(), "roster_total_rows": len(raw_roster),
        "ordinary": ordinary, "panel": panel, "quality": quality, "request": request,
        "calendar": calendar, "calendar_sha256": digest(calendar)}


def import_bulk_close(bulk_probe, roster, calendar, out, *, now=None):
    """Create a fresh typed package from existing bytes; perform no network IO."""
    recorded = _stamp(now or datetime.now(CN))
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Bulk reconciliation requires a new output folder")
    values = _inputs(bulk_probe, roster, calendar, recorded, implementation=daily_source_probe.__file__)
    day, quality = values["day"], values["quality"]
    out.mkdir(parents=True, exist_ok=False)
    preserved = out / "bulk_source"
    preserved.mkdir()
    shutil.copy2(values["bulk_probe"] / "receipt.json", preserved / "receipt.json")
    shutil.copy2(values["bulk_raw_path"], preserved / values["bulk_raw_path"].name)
    shutil.copy2(values["implementation"], out / "bulk_collection_implementation.py")
    write_frame(out / "raw_daily.csv", values["panel"])
    plan = {"schema": SCHEMA, "day": day, "calendar": str(values["calendar"]),
        "calendar_sha256": values["calendar_sha256"], "sdk_sha256": WHEEL_SHA256,
        "source_kind": "preserved_daily_bulk_plus_exact_dated_roster", "bulk_probe_folder": "bulk_source",
        "bulk_probe_origin": str(values["bulk_probe"]), "bulk_receipt_sha256": values["bulk_receipt_sha256"],
        "bulk_raw_sha256": values["bulk_raw_sha256"],
        "implementation_sha256": values["bulk_receipt"]["implementation_sha256"],
        "roster_folder": str(values["roster_folder"]), "roster_receipt_sha256": values["roster_receipt_sha256"],
        "roster_raw_sha256": values["roster_raw_sha256"], "roster_total_rows": values["roster_total_rows"],
        "ordinary_symbols": len(values["ordinary"]), "symbols": values["ordinary"].symbol.tolist(),
        "ordinary_status_counts": values["ordinary"].tradeStatus.value_counts().to_dict(),
        "fields": FIELDS, "original_bulk_fields": BAOSTOCK_FIELDS, "adjustflag": "3",
        "selection": "all_own_exact_dated_roster_ordinary_both_status0_and1_no_survivor_filter",
        "common_projection": "exact_original_strings_selected_by_same_day_ordinary_roster_no_imputation",
        "quality_contract": QUALITY_CONTRACT, "research_only": True, "candidate_panel_only": True, **FLAGS}
    write_json(out / "plan.json", plan)
    finished = _stamp(now or datetime.now(CN))
    receipt = {"schema": SCHEMA, "day": day, "status": COMPLETE,
        "created_at": recorded.isoformat(), "run_started_at": recorded.isoformat(),
        "updated_at": finished.isoformat(), "completed_at": finished.isoformat(),
        "raw_daily_sha256": digest(out/"raw_daily.csv"), "full_dated_roster_inventory_complete": True,
        "ordinary_symbols": len(values["ordinary"]), "completed_symbols": len(values["ordinary"]),
        "completed_rows": len(values["panel"]), "pending_symbols": [], "preserved_failed_batches": [],
        "bulk_original_call": {"method": "query_daily_history_k_AStock", "date": day,
            "argument_reconstruction_basis": "preserved_acquisition_implementation_hash_date_keyword_not_per_stock_receipts",
            "original_call_count": 1, "created_at": values["bulk_receipt"]["created_at"],
            "finished_at": values["bulk_receipt"]["finished_at"], "raw_rows": values["bulk_total_rows"]},
        "original_bulk_quality_issues_retained": values["bulk_receipt"]["entries"][0]["quality"]["issues"],
        "roster_completion_observed_at": values["roster_completed_at"],
        "excluded_nonordinary_codes": values["excluded_nonordinary_codes"],
        "per_stock_price_calls": 0, "new_provider_calls_by_importer": 0,
        "new_price_queries": 0, "raw_prices_imputed": False,
        "actual_signal_publication_verified": False, "research_only": True, "candidate_panel_only": True,
        **quality, **FLAGS}
    write_json(out / "receipt.json", receipt)
    return out, receipt


def load_complete_bulk(folder, *, now=None):
    """Revalidate the typed bulk lineage and common projection, read only."""
    folder = Path(folder).resolve()
    plan = json.loads((folder/"plan.json").read_text(encoding="utf-8"))
    receipt = json.loads((folder/"receipt.json").read_text(encoding="utf-8"))
    if (plan.get("schema") != SCHEMA or receipt.get("schema") != SCHEMA
            or plan.get("day") != receipt.get("day") or receipt.get("status") != COMPLETE
            or receipt.get("full_dated_roster_inventory_complete") is not True
            or receipt.get("pending_symbols") or receipt.get("preserved_failed_batches")):
        raise ValueError("Typed bulk close is not a complete exact dated inventory")
    if any(payload.get(key) is not value for payload in (plan, receipt) for key, value in FLAGS.items()):
        raise ValueError("Typed bulk close boundary flags changed")
    finished = _stamp(receipt["completed_at"])
    if finished > _stamp(now or datetime.now(CN)):
        raise ValueError("Typed bulk completion clock is in the future")
    if _stamp(receipt["created_at"]) > finished or receipt["updated_at"] != receipt["completed_at"]:
        raise ValueError("Typed bulk package completion clock changed")
    if plan.get("bulk_probe_folder") != "bulk_source" or plan.get("source_kind") != "preserved_daily_bulk_plus_exact_dated_roster":
        raise ValueError("Typed bulk source lineage changed")
    values = _inputs(folder/"bulk_source", plan["roster_folder"], plan["calendar"], finished,
        implementation=folder/"bulk_collection_implementation.py")
    expected = {"day": values["day"], "calendar_sha256": values["calendar_sha256"], "sdk_sha256": WHEEL_SHA256,
        "bulk_receipt_sha256": values["bulk_receipt_sha256"], "bulk_raw_sha256": values["bulk_raw_sha256"],
        "implementation_sha256": values["bulk_receipt"]["implementation_sha256"],
        "roster_receipt_sha256": values["roster_receipt_sha256"], "roster_raw_sha256": values["roster_raw_sha256"],
        "roster_total_rows": values["roster_total_rows"], "ordinary_symbols": len(values["ordinary"]),
        "symbols": values["ordinary"].symbol.tolist(), "ordinary_status_counts": values["ordinary"].tradeStatus.value_counts().to_dict(),
        "fields": FIELDS, "original_bulk_fields": BAOSTOCK_FIELDS, "adjustflag": "3", "quality_contract": QUALITY_CONTRACT}
    if any(plan.get(key) != value for key, value in expected.items()):
        raise ValueError("Typed bulk source/roster/schema pinned identity changed")
    panel = pd.read_csv(folder/"raw_daily.csv", dtype=str, keep_default_na=False)
    if digest(folder/"raw_daily.csv") != receipt.get("raw_daily_sha256") or not panel.equals(values["panel"]):
        raise ValueError("Typed bulk common projection differs from preserved original values")
    if any(receipt.get(key) != value for key, value in values["quality"].items()):
        raise ValueError("Typed bulk nullable quality evidence changed")
    if (receipt.get("completed_symbols") != len(values["ordinary"]) or receipt.get("completed_rows") != len(panel)
            or receipt.get("per_stock_price_calls") != 0 or receipt.get("new_provider_calls_by_importer") != 0
            or receipt.get("new_price_queries") != 0):
        raise ValueError("Typed bulk completion/call provenance changed")
    expected_call = {"method": "query_daily_history_k_AStock", "date": values["day"], "original_call_count": 1,
        "created_at": values["bulk_receipt"]["created_at"], "finished_at": values["bulk_receipt"]["finished_at"],
        "raw_rows": values["bulk_total_rows"]}
    if any(receipt.get("bulk_original_call", {}).get(key) != value for key, value in expected_call.items()):
        raise ValueError("Typed bulk original-call lineage differs from preserved acquisition")
    if any(receipt.get(key) is not False for key in ("actual_signal_publication_verified", "raw_prices_imputed")):
        raise ValueError("Typed bulk publication/imputation lineage boundary changed")
    return {"plan": plan, "receipt": receipt, "source_folder": str(folder), "read_only_verified": True,
        "source_kind": plan["source_kind"]}, panel, values["ordinary"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bulk-probe", type=Path, required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    folder, receipt = import_bulk_close(args.bulk_probe, args.roster, args.calendar, args.out)
    print(json.dumps({"status": receipt["status"], "day": receipt["day"], "ordinary_symbols": receipt["ordinary_symbols"],
        "per_stock_price_calls": 0, "new_provider_calls_by_importer": 0,
        "receipt": str(folder/"receipt.json"), "execution_admitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
