"""Explicit small action/reference amendments beside unchanged quality prices.

Only saved, independently reviewed curations are consumed. This producer never
opens a price history or research account, infers cash from a reference gap, or
changes the ordinary extension validator. All changes are full-row audited.
"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import argparse
import json
import math
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from ..contracts import MARKET_TZ
from ..symbols import normalize_symbol


SCHEMA = "annual_quality_action_input_amendment_v1"
CURATION_SCHEMA = "annual_quality_action_input_curation_v1"
FLAGS = {"execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "pit_verified": False, "corporate_actions_verified": False,
    "engine_advanced": False, "account_modified": False, "raw_prices_rescanned": False}
TERMS = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
RESERVE_EVENTS = {("603995.SSE", "2026-10-14"), ("600612.SSE", "2026-10-14")}
FORMULA_ID = "differential_cash_reference_v1"
FORMULA_FIELDS = {"reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_clean(item) for item in value]
    if isinstance(value, np.generic):
        return _clean(value.item())
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _save(path, value):
    Path(path).write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _stamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Actual evidence/review clocks require explicit timezone")
    return result


def _unknown(value):
    return value is None or value is pd.NA or (isinstance(value, str) and value == "") or (
        isinstance(value, float) and math.isnan(value))


def _equal(left, right):
    return _clean(left) == _clean(right)


def _checked(path, expected):
    reference = _ref(path)
    if reference["sha256"] != expected:
        raise ValueError("Pinned action/reference source changed: " + str(path))
    return reference


def _expected(row, expected):
    if not isinstance(expected, dict) or not expected:
        raise ValueError("An exact existing source-row expectation is required")
    if any(key not in row or not _equal(row[key], value) for key, value in expected.items()):
        raise ValueError("Curated old action/reference fields differ from the saved source")


def _evidence(item, now):
    reviewed = _stamp(item["reviewed_at"])
    if reviewed > now or not item.get("terms_basis") or not item.get("evidence"):
        raise ValueError("Actual completed review and explicit evidence basis required")
    result = []
    for reference in item["evidence"]:
        if not isinstance(reference.get("role"), str) or not reference["role"]:
            raise ValueError("Explicit source evidence role required")
        actual = _checked(reference["file"], reference["sha256"])
        acquired = _stamp(reference["available_at"])
        if acquired > reviewed or acquired > now:
            raise ValueError("Review cannot precede actual evidence acquisition")
        result.append({**actual, "role": reference["role"], "available_at": acquired.isoformat()})
    return {"reviewed_at": reviewed.isoformat(), "terms_basis": item["terms_basis"],
        "evidence": result, "historical_availability_backfilled": False}


def _index(frame, columns, values):
    mask = pd.Series(True, index=frame.index)
    for field, value in zip(columns, values):
        mask &= frame[field].eq(value)
    indices = list(frame.index[mask])
    if len(indices) != 1:
        raise ValueError("Exactly one saved economic identity is required: " + str(values))
    return indices[0]


def _date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Explicit canonical economic dates required")
    return value


def _number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Known decimal action term required") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Known nonnegative action term required")
    return result


def _change(old, new, identity, evidence, operation):
    return {**identity, "old": _clean(old), "new": _clean(new),
        "changed_fields": [key for key in new if old is None or not _equal(old.get(key), new[key])],
        "operation": operation, **evidence}


def _amend_tables(actions, gaps, curations, scope, bounds, now):
    actions, gaps = actions.copy(deep=True), gaps.copy(deep=True)
    event_changes, reference_changes, seen_events, seen_refs = [], [], set(), set()
    for curation in curations:
        for item in curation.get("event_patches", []):
            key = (item["symbol"], _date(item["dividOperateDate"]))
            if key not in RESERVE_EVENTS or key in seen_events:
                raise ValueError("Only the two explicitly requested reserve completions are supported once")
            seen_events.add(key)
            if item.get("updates") != {"dividReserveToStockPs": "0"}:
                raise ValueError("Known fields/other ordinary terms must remain unchanged")
            index = _index(actions, ("symbol", "dividOperateDate"), key)
            old = actions.loc[index].to_dict()
            _expected(old, item["expected_provider_terms"])
            if not _unknown(old["dividReserveToStockPs"]):
                raise ValueError("Reserve completion may only fill an unknown term")
            evidence = _evidence(item, now)
            actions.at[index, "dividReserveToStockPs"] = "0"
            event_changes.append(_change(old, actions.loc[index].to_dict(),
                dict(symbol=key[0], dividOperateDate=key[1]), evidence, "unknown_reserve_to_explicit_zero"))
        for item in curation.get("event_appends", []):
            row = item["row"]
            if set(row) != set(actions.columns):
                raise ValueError("A new event must explicitly retain the complete original source schema")
            key = (row["symbol"], _date(row["dividOperateDate"]))
            if (key in seen_events or row["symbol"] not in scope
                    or normalize_symbol(row["symbol"]).baostock_code != row["code"]
                    or not bounds[0] <= key[1] <= bounds[1]
                    or ((actions.symbol == key[0]) & (actions.dividOperateDate == key[1])).any()):
                raise ValueError("New action identity conflicts with the original scope/window or existing source")
            seen_events.add(key)
            if not _date(row["dividRegistDate"]) < key[1] <= _date(row["dividPayDate"]):
                raise ValueError("Explicit record/ex/pay chronology required")
            values = [_number(row[field]) for field in TERMS]
            if row["dividStockMarketDate"] and _date(row["dividStockMarketDate"]) < key[1]:
                raise ValueError("New-share listing cannot precede ex date")
            if sum(values[1:]) > 0 and not row["dividStockMarketDate"]:
                raise ValueError("Positive new shares require their actual listing date")
            evidence = _evidence(item, now)
            if not any(r["role"] == "official_implementation_notice" for r in evidence["evidence"]):
                raise ValueError("A new event needs the actual official implementation notice")
            actions = pd.concat([actions, pd.DataFrame([row], columns=actions.columns)], ignore_index=True)
            event_changes.append(_change(None, row, dict(symbol=key[0], dividOperateDate=key[1]),
                evidence, "explicit_official_new_event"))
        for item in curation.get("reference_patches", []):
            key = (item["code"], _date(item["date"]))
            if key in seen_refs:
                raise ValueError("Each reference identity may be explicitly amended only once")
            seen_refs.add(key)
            index = _index(gaps, ("code", "date"), key)
            old = gaps.loc[index].to_dict()
            _expected(old, item["expected"])
            if item.get("formula_id") != FORMULA_ID or item.get("updates"):
                raise ValueError("Only the explicit forward ordinary-action formula is supported; no raw/formula override")
            evidence = _evidence(item, now)
            roles = {r["role"] for r in evidence["evidence"]}
            if not {"official_reference_formula", "official_implementation_notice"} <= roles:
                raise ValueError("Independent official formula and issuer notice are both required")
            symbol = normalize_symbol(key[0]).vt_symbol
            event = actions.loc[_index(actions, ("symbol", "dividOperateDate"), (symbol, key[1]))].to_dict()
            cash, bonus, reserve = [_number(event[field]) for field in TERMS]
            reference_cash = _number(event["reference_adjustment_cash_ps"])
            if bonus or reserve:
                raise ValueError("This explicit differential-cash formula does not support new shares")
            prior, actual = _number(old["previous_observed_close"]), _number(old["preclose"])
            expected = (prior - reference_cash).quantize(Decimal("0.0001"))
            if expected <= 0 or actual <= 0:
                raise ValueError("Positive raw and forward-calculated reference required")
            # Action facts and formula output may change; raw observations never do.
            for field in actions.columns:
                if field in gaps.columns and field not in {"code", "symbol"}:
                    if not _unknown(old[field]) and not _equal(old[field], event[field]):
                        raise ValueError("Known reference action facts conflict with reviewed event")
                    gaps.at[index, field] = event[field]
            error = expected - actual
            gaps.at[index, "has_reported_distribution"] = True
            gaps.at[index, "reference_formula_status"] = "reference_match" if abs(error) <= Decimal("0.011") else "reference_mismatch"
            gaps.at[index, "reference_formula_basis"] = "reviewed_differential_cash_reference"
            gaps.at[index, "expected_reference_rmb"] = str(expected)
            gaps.at[index, "reference_error_rmb"] = float(error)
            reference_changes.append(_change(old, gaps.loc[index].to_dict(), dict(code=key[0], date=key[1]),
                {**evidence, "formula_id": FORMULA_ID, "cash_entitlement_ps": str(cash),
                    "reference_adjustment_cash_ps": str(reference_cash),
                    "cash_inferred_from_reference_difference": False},
                "independently_evidenced_forward_formula"))
    return actions, gaps, event_changes, reference_changes


def build(inputs_file, curations, out):
    """Publish one new source pair; never consume it into an account."""
    inputs_file, out = Path(inputs_file).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("An action amendment needs a new output directory")
    inputs = _read(inputs_file)
    events, references = Path(inputs["event_view"]).resolve(), Path(inputs["reference_view"]).resolve()
    paths = [inputs_file, *map(Path, curations), events, references,
        Path(inputs["price_view"]), Path(inputs["listing_master"])]
    if any(out == p.resolve() or p.resolve() in out.parents or out in p.resolve().parents for p in paths):
        raise ValueError("Amendment output must be separate from original inputs and sources")
    event_result, ref_result = _read(events / "result.json"), _read(references / "result.json")
    if (event_result.get("status") != "research_action_events_projected"
            or ref_result.get("status") != "research_reference_presence_diagnostic"
            or Path(ref_result["event_view"]).resolve() != events
            or Path(ref_result["price_view"]).resolve() != Path(inputs["price_view"]).resolve()
            or event_result.get("execution_admitted") is not False or ref_result.get("execution_admitted") is not False):
        raise ValueError("An aligned unadmitted original price/action/reference pair is required")
    before_refs = {"events": _checked(events / "canonical_actions.parquet", event_result["files"]["canonical_actions.parquet"]),
        "references": _checked(references / "raw_reference_changes.parquet", ref_result["files"]["raw_reference_changes.parquet"])}
    original_actions = pd.read_parquet(before_refs["events"]["file"])
    original_gaps = pd.read_parquet(before_refs["references"]["file"])
    scope = set(pd.read_csv(events / "source_input_universe.csv", dtype=str, keep_default_na=False).symbol)
    now, records, curation_refs = datetime.now(MARKET_TZ), [], []
    for path in curations:
        value = _read(path)
        if value.get("schema") != CURATION_SCHEMA or value.get("execution_admitted") is not False:
            raise ValueError("Explicit unadmitted action-input curation required")
        records.append(value)
        curation_refs.append(_ref(path))
    if not records:
        raise ValueError("At least one explicit action/reference curation is required")
    actions, gaps, event_changes, reference_changes = _amend_tables(original_actions, original_gaps,
        records, scope, (event_result["start"], event_result["end"]), now)
    if not event_changes and not reference_changes:
        raise ValueError("An empty curation is not a new action input")
    for field in original_actions:
        actions[field] = actions[field].astype(original_actions[field].dtype)
    for field in original_gaps:
        gaps[field] = gaps[field].astype(original_gaps[field].dtype)
    changed_indices = {tuple(row[k] for k in ("symbol", "dividOperateDate")) for row in event_changes}
    untouched = ~pd.Series([(s, d) in changed_indices for s, d in zip(original_actions.symbol, original_actions.dividOperateDate)])
    if not actions.iloc[:len(original_actions)].loc[untouched].equals(original_actions.loc[untouched]):
        raise ValueError("Unselected canonical event rows changed")
    ref_keys = {(r["code"], r["date"]) for r in reference_changes}
    untouched_ref = ~pd.Series([(c, d) in ref_keys for c, d in zip(original_gaps.code, original_gaps.date)])
    if not gaps.loc[untouched_ref].equals(original_gaps.loc[untouched_ref]):
        raise ValueError("Unselected reference rows changed")
    out.mkdir(parents=True)
    for source, target in ((events, out / "events"), (references, out / "references")):
        shutil.copytree(source, target, ignore=lambda directory, names:
            ["result.json"] if Path(directory).resolve() == source else [])
    actions.to_parquet(out / "events/canonical_actions.parquet", index=False)
    if reference_changes:
        gaps.to_parquet(out / "references/raw_reference_changes.parquet", index=False)
    after_refs = {"events": _ref(out / "events/canonical_actions.parquet"),
        "references": _ref(out / "references/raw_reference_changes.parquet")}
    after_inputs = {**inputs, "event_view": str(out / "events"), "reference_view": str(out / "references")}
    audit = {"schema": SCHEMA, "created_at": now.isoformat(), "parent_inputs": _ref(inputs_file),
        "inputs_before": deepcopy(inputs),
        "inputs_after": after_inputs, "parent_sources": before_refs, "new_sources": after_refs,
        "curations": curation_refs, "event_changes": event_changes, "reference_changes": reference_changes,
        "price_view_rebuilt": False, "raw_view_rebuilt": False, "listing_master_changed": False,
        "reference_match_inferred_from_zero_reserve": False, **FLAGS}
    _save(out / "audit.json", audit)
    new_event = {**event_result, "canonical_events": len(actions), "files": dict(event_result["files"]),
        "annual_quality_action_amendment": _ref(out / "audit.json")}
    new_event["files"]["canonical_actions.parquet"] = after_refs["events"]["sha256"]
    _save(out / "events/result.json", new_event)
    shutil.copy2(out / "events/result.json", out / "references/input_events.json")
    new_ref = {**ref_result, "event_view": str(out / "events"), "files": dict(ref_result["files"]),
        "reference_formula_status_counts": gaps.reference_formula_status.value_counts().to_dict(),
        "reference_formula_basis_counts": gaps.reference_formula_basis.value_counts(dropna=False).to_dict(),
        "annual_quality_action_amendment": _ref(out / "audit.json")}
    for name in ("raw_reference_changes.parquet", "input_events.json"):
        new_ref["files"][name] = _ref(out / "references" / name)["sha256"]
    _save(out / "references/result.json", new_ref)
    _save(out / "inputs.json", after_inputs)
    result = {"schema": SCHEMA, "status": "completed_explicit_action_reference_source_amendment",
        "created_at": now.isoformat(), "inputs": _ref(out / "inputs.json"), "audit": _ref(out / "audit.json"),
        "event_patch_count": sum(row["old"] is not None for row in event_changes),
        "event_append_count": sum(row["old"] is None for row in event_changes),
        "reference_patch_count": len(reference_changes), **FLAGS}
    _save(out / "result.json", result)
    return result


def validate_audit(inputs_file, source_audit):
    """Verify the complete small source-table delta against its exact curations.

    This does not read prices, accounts or source history. Reconstructing only
    the two small tables prevents a forged audit from omitting other changes.
    """
    inputs, audit = _read(inputs_file), _read(source_audit)
    if (audit.get("schema") != SCHEMA or audit.get("inputs_after") != inputs
            or any(audit.get(key) is not value for key, value in FLAGS.items())
            or _stamp(audit["created_at"]) > datetime.now(MARKET_TZ)):
        raise ValueError("Exact unadmitted source amendment audit and inputs required")
    parent = audit["parent_inputs"]
    _checked(parent["file"], parent["sha256"])
    if _read(parent["file"]) != audit["inputs_before"]:
        raise ValueError("Audit parent input paths changed")
    for key in ("price_view", "listing_master"):
        if inputs[key] != audit["inputs_before"][key]:
            raise ValueError("Action-only amendment changed a price/listing source")
    for side in ("parent_sources", "new_sources"):
        for reference in audit[side].values():
            _checked(reference["file"], reference["sha256"])
    old_inputs = audit["inputs_before"]
    for category, key, filename in (("events", "event_view", "canonical_actions.parquet"),
            ("references", "reference_view", "raw_reference_changes.parquet")):
        if (Path(audit["parent_sources"][category]["file"]).resolve() != Path(old_inputs[key]).resolve() / filename
                or Path(audit["new_sources"][category]["file"]).resolve() != Path(inputs[key]).resolve() / filename):
            raise ValueError("Audit table references differ from its explicit input pair")
    records = []
    for reference in audit["curations"]:
        _checked(reference["file"], reference["sha256"])
        record = _read(reference["file"])
        if record.get("schema") != CURATION_SCHEMA or record.get("execution_admitted") is not False:
            raise ValueError("Audit curation schema changed")
        records.append(record)
    old_actions = pd.read_parquet(audit["parent_sources"]["events"]["file"])
    old_gaps = pd.read_parquet(audit["parent_sources"]["references"]["file"])
    new_actions = pd.read_parquet(audit["new_sources"]["events"]["file"])
    new_gaps = pd.read_parquet(audit["new_sources"]["references"]["file"])
    event_result = _read(Path(old_inputs["event_view"]) / "result.json")
    scope = set(pd.read_csv(Path(old_inputs["event_view"]) / "source_input_universe.csv",
        dtype=str, keep_default_na=False).symbol)
    actions, gaps, events, references = _amend_tables(old_actions, old_gaps, records, scope,
        (event_result["start"], event_result["end"]), _stamp(audit["created_at"]))
    for column in old_actions:
        actions[column] = actions[column].astype(old_actions[column].dtype)
    for column in old_gaps:
        gaps[column] = gaps[column].astype(old_gaps[column].dtype)
    if (not actions.equals(new_actions) or not gaps.equals(new_gaps)
            or _clean(events) != audit["event_changes"] or _clean(references) != audit["reference_changes"]):
        raise ValueError("Actual complete source-table differences do not equal the reviewed audit")
    changes = [*events, *references]
    if not changes:
        raise ValueError("No actual reviewed source change exists")
    return {"event_changes": _clean(events), "reference_changes": _clean(references),
        "reviewed_at": max(_stamp(row["reviewed_at"]) for row in changes).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--curation", required=True, action="append", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = build(args.inputs, args.curation, args.out)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
