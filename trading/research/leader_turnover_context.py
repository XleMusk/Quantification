"""Exact-day observed turnover descriptions beside frozen leader proxies.

The caller supplies a complete-close feature table already verified by
leader_environment_run.read_preview. No candidate rules or identities change.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .baostock_bulk_close import SCHEMA as BULK_SCHEMA, _stamp
from .baostock_bulk_extras import SCHEMA as EXTRAS_SCHEMA, EXTRAS, FLAGS
from .baostock_daily_slice import digest
from .leader_environment import ARCHETYPES

SCHEMA = "leader_exact_dated_observed_turnover_context_v1"
QUANTILES = {"p10": .10, "p25": .25, "p50": .50, "p75": .75, "p90": .90}


def _describe(values):
    known = values.loc[values.notna() & values.ge(0)]
    return {"scope_rows": len(values), "observed_nonnegative_turnover_rows": len(known),
        "missing_or_invalid_turnover_rows": len(values) - len(known),
        "coverage_fraction": len(known) / len(values) if len(values) else None,
        "unit": "percent", "median_percent": float(known.median()) if len(known) else None,
        "quantiles_percent": {label: float(known.quantile(q)) if len(known) else None for label, q in QUANTILES.items()}}


def turnover_context(extras_result, current_features, environment, *, current_source, current_provenance, observed_at):
    """Read one typed extras artifact, bind it to the exact verified close."""
    path = Path(extras_result).resolve()
    path = path / "result.json" if path.is_dir() else path
    record = json.loads(path.read_text(encoding="utf-8"))
    day = environment["day"]
    if (record.get("schema") != EXTRAS_SCHEMA
            or record.get("status") != "completed_dated_bulk_extras_research_only"
            or record.get("package_schema") != BULK_SCHEMA
            or any(record.get(key) is not value for key, value in FLAGS.items())
            or record.get("day") != day or current_features.empty
            or not current_features.date.eq(day).all()
            or current_features.symbol.duplicated().any()):
        raise ValueError("Observed turnover requires a complete same-day typed extras/feature/environment scope")
    if (current_source.get("read_only_verified") is not True
            or current_provenance.get("features_match_current_active_raw") is not True
            or Path(record["package"]).resolve() != Path(current_source["source_folder"]).resolve()
            or Path(record["package"]).resolve() != Path(current_provenance["source_folder"]).resolve()):
        raise ValueError("Turnover source differs from the already-verified current raw source")
    package = Path(record["package"]).resolve()
    plan_path, receipt_path = package / "plan.json", package / "receipt.json"
    plan, receipt = [json.loads(p.read_text(encoding="utf-8")) for p in (plan_path, receipt_path)]
    if (digest(plan_path) != record["package_plan_sha256"] or digest(receipt_path) != record["package_receipt_sha256"]
            or plan.get("schema") != BULK_SCHEMA or receipt.get("schema") != BULK_SCHEMA
            or plan.get("day") != day or receipt.get("day") != day
            or receipt.get("raw_daily_sha256") != current_provenance.get("raw_daily_sha256")
            or receipt.get("raw_daily_sha256") != current_source.get("receipt", {}).get("raw_daily_sha256")
            or plan.get("roster_raw_sha256") != record["roster_raw_sha256"]
            or plan.get("calendar_sha256") != record["calendar_sha256"]
            or plan.get("bulk_raw_sha256") != record["original_bulk_sha256"]):
        raise ValueError("Observed turnover raw/roster/package binding changed")
    derived = _stamp(record["actual_derived_at"])
    if (record.get("available_at") != record["actual_derived_at"]
            or not _stamp(record["source_inputs_available_at"]) <= derived <= _stamp(observed_at)):
        raise ValueError("Observed turnover input or derivation clock is unavailable/future")
    data_path = Path(record["data_file"]).resolve()
    data_path.relative_to(path.parent)
    unit_path = Path(record["unit_evidence_file"]).resolve()
    unit_path.relative_to(path.parent)
    units = json.loads(unit_path.read_text(encoding="utf-8"))
    if (digest(data_path) != record["data_sha256"] or digest(unit_path) != record["unit_evidence_sha256"]
            or units != record["unit_evidence"] or units.get("turn_unit") != "percent"
            or record.get("turn_numeric_unit") != "percent" or record.get("turn_scaled") is not False):
        raise ValueError("Observed turnover data or percentage unit evidence changed")
    rows = pd.read_parquet(data_path)
    expected = set(record["original_fields"]) | {"symbol", *[name + "_numeric" for name in EXTRAS]}
    if (set(rows) != expected or len(rows) != record["ordinary_rows"]
            or len(rows) != plan["ordinary_symbols"] or rows.code.duplicated().any()
            or rows.symbol.duplicated().any() or not rows.date.eq(day).all()
            or set(rows.symbol) != set(plan["symbols"])
            or not rows.tradestatus.isin(["0", "1"]).all() or not rows.isST.isin(["0", "1"]).all()
            or not (rows.code.str[3:] + np.where(rows.code.str.startswith("sh."), ".SSE", ".SZSE")).equals(rows.symbol)):
        raise ValueError("Turnover artifact must retain the entire own-day ordinary roster")
    for name in EXTRAS:
        parsed = pd.to_numeric(rows[name], errors="coerce")
        parsed = parsed.where(np.isfinite(parsed)).to_numpy(dtype=float, na_value=np.nan)
        numeric = pd.to_numeric(rows[name + "_numeric"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        if not np.allclose(parsed, numeric, rtol=0, atol=0, equal_nan=True):
            raise ValueError("Extras numeric values changed original signs, blanks or values")
    active = rows.loc[rows.tradestatus.eq("1") & rows.isST.eq("0")
        & pd.to_numeric(rows.volume, errors="raise").gt(0) & pd.to_numeric(rows.amount, errors="raise").gt(0)].set_index("symbol")
    if set(active.index) != set(current_features.symbol):
        raise ValueError("Turnover needs every current active non-ST feature, not an inner-joined subset")
    features = current_features.set_index("symbol").loc[active.index]
    for name, raw_name in (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"),
                           ("volume", "volume"), ("amount", "amount"), ("pct_chg", "pctChg")):
        if not np.isclose(pd.to_numeric(features[name], errors="raise"),
                pd.to_numeric(active[raw_name], errors="raise"), rtol=1e-10, atol=1e-7).all():
            raise ValueError("Turnover extras disagree with already-verified current raw features")
    archetypes = environment["archetypes"]
    if {item["id"] for item in archetypes} != {key for key, _ in ARCHETYPES} or len(archetypes) != len(ARCHETYPES):
        raise ValueError("Turnover descriptions require the unchanged eight archetypes")
    groups = []
    for item in archetypes:
        candidates = item.get("candidates") or []
        symbols = [row["symbol"] for row in candidates]
        if (len(symbols) != len(set(symbols)) or len(symbols) != item["candidate_count"]
                or not set(symbols) <= set(active.index) or any(row["source_day"] != day for row in candidates)):
            raise ValueError("Existing known proxy candidates do not match the exact current raw scope")
        groups.append({"id": item["id"], "name": item["name"], "existing_status": item["status"],
            "known_proxy_candidate_count": len(symbols), "description": _describe(active.loc[symbols, "turn_numeric"]),
            "membership_basis": "existing_frozen_known_proxy_matches_only_no_new_turnover_selection",
            "unknown_membership_included_as_known": False,
            "unresolved_stage_symbols": item.get("unresolved_stage_symbols", []),
            "unresolved_rebound_symbols": item.get("unresolved_rebound_symbols", []),
            "frozen_archetype_missing_evidence": item.get("missing_evidence", []),
            "selection_enabled": False, "leader_identity_confirmed_by_turnover": False})
    return {"schema": SCHEMA, "status": "observed_same_day_turnover_descriptive_context_only", "day": day,
        "observed_at": observed_at, "source_day_is_current_session_claimed": False,
        "extras_result": str(path), "extras_result_sha256": digest(path), "data_sha256": record["data_sha256"],
        "source_folder": str(package), "raw_daily_sha256": receipt["raw_daily_sha256"],
        "roster_raw_sha256": record["roster_raw_sha256"], "calendar_sha256": record["calendar_sha256"],
        "raw_acquisition_finished_at": record["raw_acquisition_finished_at"],
        "source_inputs_available_at": record["source_inputs_available_at"], "extras_actual_derived_at": record["actual_derived_at"],
        "scope": "complete_own_dated_provider_active_non_ST_positive_activity_not_PIT",
        "ordinary_inventory_rows": len(rows), "active_non_ST_rows": len(active),
        "suspended_inventory_rows": int(rows.tradestatus.eq("0").sum()),
        "active_ST_rows_excluded": int((rows.tradestatus.eq("1") & rows.isST.eq("1")).sum()),
        "all_active_non_ST_features_matched": True, "overall": _describe(active.turn_numeric), "archetypes": groups,
        "unit_evidence": units, "valuation_fields_used_for_classification": False,
        "frozen_rules_modified": False, "existing_archetype_states_modified": False,
        "identified_capital_flow": False, "participant_intent_inferred": False,
        "missing_evidence": ["independent_circulating_share_denominator", "financial_and_universe_PIT",
            "intraday_turnover_path_and_queue_quality", "turnover_leader_rule_and_validation_not_defined"],
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "execution_admitted": False, "orders_generated": False, "ledger_mutated": False,
        "model_refit": False, "future_labels_used_for_score": False}
