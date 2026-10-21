"""Derive dated turnover/valuation research fields from preserved bulk bytes.

Only complete typed bulk packages are accepted. No source values, data feeds,
strategy inputs or trading permissions are changed, and no network is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..market_dataset import write_json
from . import baostock_bulk_close as bulk
from .baostock_daily_slice import digest
from .daily_source_probe import cached_daily

SCHEMA = "baostock_dated_bulk_extras_research_v1"
EXTRAS = ("turn", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM")
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "pit_verified": False, "source_verified": False, "company_actions_verified": False,
    "historical_publication_verified": False, "provider_contacted": False,
    "model_refit": False, "future_labels_used_for_score": False,
    "raw_values_imputed": False, "numeric_missing_filled_with_zero": False,
    "stockdb_modified": False, "native_features_modified": False, "ledger_mutated": False,
    "orders_generated": False, "ths_payload_generated": False}
UNIT_EVIDENCE = Path(__file__).with_name("baostock_bulk_extras_units.json")


def _statistics(rows):
    fields = {}
    for name in EXTRAS:
        raw, values = rows[name], rows[name + "_numeric"]
        finite = values.notna()
        fields[name] = {"raw_blank_rows": int(raw.eq("").sum()),
            "nonblank_unparseable_or_nonfinite_rows": int((raw.ne("") & ~finite).sum()),
            "numeric_finite_rows": int(finite.sum()), "numeric_null_rows": int((~finite).sum()),
            "numeric_negative_rows": int(values.lt(0).fillna(False).sum()),
            "numeric_zero_rows": int(values.eq(0).fillna(False).sum()),
            "numeric_min": float(values.min()) if finite.any() else None,
            "numeric_max": float(values.max()) if finite.any() else None}
    return {"rows": len(rows), "fields": fields}


def _derive(raw, ordinary):
    # The strict bulk loader has already reconciled this exact day's roster;
    # do not use a current/surviving-universe filter or drop nullable halts.
    rows = raw.set_index("code").loc[ordinary.code].reset_index()
    original = rows.copy()
    rows["symbol"] = ordinary.symbol.to_numpy()
    for name in EXTRAS:
        values = pd.to_numeric(rows[name], errors="coerce")
        rows[name + "_numeric"] = values.where(np.isfinite(values)).astype("Float64")
    if not rows[list(original)].equals(original):
        raise ValueError("Derived extras changed original bulk strings")
    return rows, {"ordinary": _statistics(rows),
        "active": _statistics(rows.loc[rows.tradestatus.eq("1")]),
        "halt": _statistics(rows.loc[rows.tradestatus.eq("0")])}


def build(package, out):
    package, out = Path(package).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Bulk extras require a new independent output directory")
    started = datetime.now().astimezone()
    # Refuse per-stock/common 13-field packages explicitly, before reserving out.
    plan = json.loads((package / "plan.json").read_text(encoding="utf-8"))
    receipt = json.loads((package / "receipt.json").read_text(encoding="utf-8"))
    if plan.get("schema") != bulk.SCHEMA or receipt.get("schema") != bulk.SCHEMA:
        raise ValueError("Extras require a strict typed 18-field bulk source; per-stock projection unsupported")
    metadata, panel, ordinary = bulk.load_complete_bulk(package, now=started)
    probe = package / "bulk_source"
    acquisition = json.loads((probe / "receipt.json").read_text(encoding="utf-8"))
    raw = cached_daily(probe, acquisition["entries"][0], kind="stock")
    if (digest(probe / "receipt.json") != plan["bulk_receipt_sha256"]
            or acquisition["entries"][0]["sha256"] != plan["bulk_raw_sha256"]):
        raise ValueError("Preserved acquisition changed during extras derivation")
    derived, statistics = _derive(raw, ordinary)
    if not derived[panel.columns].equals(panel):
        raise ValueError("Extras ordinary projection differs from strict common raw source")
    units = json.loads(UNIT_EVIDENCE.read_text(encoding="utf-8"))
    roster = json.loads((Path(plan["roster_folder"]) / "receipt.json").read_text(encoding="utf-8"))
    source_ready = max(bulk._stamp(acquisition["finished_at"]), bulk._stamp(roster["completed_at"]),
                       bulk._stamp(receipt["completed_at"]))
    if source_ready > started:
        raise ValueError("Source proof was not available at the actual derivation start")
    record = {"schema": SCHEMA, "status": "building_bulk_extras", "day": plan["day"],
        "started_at": started.isoformat(), "package": str(package),
        "package_schema": metadata["plan"]["schema"], "package_plan_sha256": digest(package / "plan.json"),
        "package_receipt_sha256": digest(package / "receipt.json"),
        "original_bulk_file": str(probe / acquisition["entries"][0]["file"]),
        "original_bulk_sha256": plan["bulk_raw_sha256"],
        "original_bulk_receipt_sha256": plan["bulk_receipt_sha256"],
        "raw_acquisition_started_at": acquisition["created_at"],
        "raw_acquisition_finished_at": acquisition["finished_at"],
        "roster_available_at": roster["completed_at"], "roster_raw_sha256": plan["roster_raw_sha256"],
        "typed_package_completed_at": receipt["completed_at"], "source_inputs_available_at": source_ready.isoformat(),
        "calendar": plan["calendar"], "calendar_sha256": plan["calendar_sha256"],
        "ordinary_roster_complete": True, "ordinary_rows": len(ordinary), "original_bulk_rows": len(raw),
        "excluded_nonordinary_codes": sorted(set(raw.code) - set(ordinary.code)),
        "scope": "own_exact_dated_provider_ordinary_SH_SZ_roster_all_status0_and1_not_PIT",
        "original_fields": list(raw), "raw_strings_preserved": True,
        "numeric_columns": {name: name + "_numeric" for name in EXTRAS},
        "numeric_semantics": "finite_parse_only_blank_invalid_nonfinite_to_null_negative_valuation_values_retained",
        "turn_numeric_unit": "percent", "turn_scaled": False,
        "turn_halt_missing_semantics": "empty_original_string_and_numeric_null_never_zero",
        "unit_evidence": units, "statistics": statistics,
        "financial_report_publication_times_verified": False, "circulating_share_denominator_independently_verified": False,
        "usable_for_other_days": False, "environment_or_strategy_updated": False, **FLAGS,
        "limitations": ["This dated provider snapshot is not historical PIT financial or circulating-share evidence",
            "Turnover is the provider's share-volume/circulating-shares percentage, never amount or identified capital flow",
            "Negative valuation multiples are source values, not zero, missing values or positive-value comparables",
            "The source date cannot be relabeled as a newer market observation; actual acquisition and derivation clocks are retained",
            "No changes to existing raw prices, native features, strategy rules, accounts or execution admission"]}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", record)
    try:
        path = out / "dated_extras.parquet"
        derived.to_parquet(path, index=False)
        write_json(out / "unit_evidence.json", units)
        finished = datetime.now().astimezone().isoformat()
        record.update(status="completed_dated_bulk_extras_research_only", actual_derived_at=finished,
            finished_at=finished, available_at=finished, data_file=str(path), data_sha256=digest(path),
            unit_evidence_file=str(out / "unit_evidence.json"), unit_evidence_sha256=digest(out / "unit_evidence.json"))
        write_json(out / "result.json", record)
        return out, record
    except Exception as error:
        record.update(status="failed_dated_bulk_extras", finished_at=datetime.now().astimezone().isoformat(),
                      error=f"{type(error).__name__}: {error}")
        write_json(out / "result.json", record)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    folder, result = build(args.package, args.out)
    print(json.dumps({key: result[key] for key in ("status", "day", "ordinary_rows", "actual_derived_at", "execution_admitted")}
        | {"result": str(folder / "result.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
