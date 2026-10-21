"""Exact dated reported turnover and same-source raw range, without signals.

This independent sidecar never changes an earlier bundle or publication. Its
actual availability must precede the consumer's decision/publication clock.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from .leader_daily_signals import _calendar_view
from .leader_dated_turnover import _existing_typed_extras

SCHEMA = "leader_entry_reported_turnover_confirmation_inputs_v1"
METHOD_ID = "exact_dated_reported_turnover_and_same_source_raw_range_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "provider_contacted": False, "account_created": False, "orders_generated": False,
    "ledger_mutated": False, "old_publications_modified": False, "raw_prices_modified": False,
    "model_refit": False, "future_labels_used_for_score": False,
    "pit_verified": False, "corporate_actions_verified": False,
    "independent_circulating_shares_verified": False, "current_signal_admitted": False}
RULE_SPEC = {"id": METHOD_ID,
    "required_conditions": ["reported_turn_D_known_nonnegative", "reported_turn_exact_previous_known_nonnegative",
                            "reported_turn_D_strictly_greater_than_previous", "same_source_raw_high_D_strictly_greater_than_low_D"],
    "turn_unit": "percent", "turn_scaled": False,
    "previous_day": "exact_pinned_calendar_previous_session_no_next_available_substitution",
    "missing_previous_source": "previous_source_unknown_not_empty_and_never_previous_observed_day",
    "unknown_condition_precedence": "any_required_value_unknown_means_unknown_even_if_another_condition_false",
    "halt_blank_turn": "unknown_not_zero",
    "status_and_ST": "original_observation_only_no_extra_eligibility_filter",
    "price_basis": "same_dated_full_close_bulk_adjustflag3_original_high_low_not_StockDB_replacement",
    "scope": "all_current_source_own_dated_ordinary_SH_SZ_roster_no_candidate_or_outcome_filter",
    "source_binding": "each_supplied_day_price_raw_sha256_equals_own_bundle_source_day_proof",
    "availability": "actual_source_completion_then_extras_derivation_then_new_sidecar_completion_no_backdating",
    "purpose": "research_confirmation_input_not_true_turnover_leader_identity_or_strategy_signal"}
RULE_SPEC["row_statuses"] = ["known_true", "known_false", "unknown"]


def _now():
    return datetime.now(MARKET_TZ)


def _stamp(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("Confirmation input clocks require an explicit timezone")
    return value.astimezone(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _calendar(calendar, day):
    path = Path(calendar).resolve()
    payload = _read(path)
    days, previous, status = _calendar_view(payload, day, day, [])
    if status != "pinned_static_exchange_calendar" or days != [day]:
        raise ValueError("Confirmation day must be an exact session of the pinned calendar")
    return previous[day], {"file": str(path), "sha256": _digest(path)}


def _proofs(source_day_proofs, day, previous):
    if not isinstance(source_day_proofs, list):
        raise ValueError("Use the saved bundle source_packages list as source_day_proofs")
    by_day = {}
    for item in source_day_proofs:
        if not isinstance(item, dict) or item.get("day") in by_day:
            raise ValueError("Saved source day proofs must be unique")
        by_day[item.get("day")] = item
    result = []
    for date in (previous, day):
        if date is None or date not in by_day:
            continue
        raw_hash = by_day[date].get("raw_daily_sha256")
        if not isinstance(raw_hash, str) or len(raw_hash) != 64 or any(c not in "0123456789abcdef" for c in raw_hash):
            raise ValueError("Exact dated original price SHA256 proof required")
        result.append({"day": date, "raw_daily_sha256": raw_hash})
    if not any(item["day"] == day for item in result):
        raise ValueError("Current decision day's saved source proof is missing")
    return result


def _source(path, day, calendar_proof, proof, observed):
    if path is None or not Path(path).is_file():
        raise ValueError("Current source must exist; no collection or output is started")
    path = Path(path).resolve()
    rows, metadata = _existing_typed_extras(path, observed)
    record = _read(path)
    package = Path(record["package"]).resolve()
    plan_file = package / "plan.json"
    plan, receipt = _read(plan_file), _read(package / "receipt.json")
    required = {"date", "code", "symbol", "turn", "turn_percent", "high", "low", "tradestatus", "isST", "adjustflag"}
    if (metadata["day"] != day or plan.get("day") != day
            or record.get("package_schema") != "baostock_bulk_exact_dated_close_inventory_v1"
            or plan.get("schema") != record["package_schema"]
            or _digest(plan_file) != record.get("package_plan_sha256")
            or plan.get("calendar_sha256") != calendar_proof["sha256"]
            or record.get("calendar_sha256") != calendar_proof["sha256"]
            or record.get("ordinary_roster_complete") is not True
            or record.get("raw_strings_preserved") is not True
            or not required <= set(rows) or not rows.adjustflag.eq("3").all()
            or len(rows) != plan.get("ordinary_symbols") or len(rows) != receipt.get("completed_rows")
            or set(rows.symbol) != set(plan.get("symbols", []))
            or not rows.symbol.str.fullmatch(r"(?:60\d{4}|688\d{3})\.SSE|(?:00\d{4}|30\d{4})\.SZSE").all()
            or proof is None or metadata["price_source_raw_sha256"] != proof["raw_daily_sha256"]
            or plan.get("execution_admitted") is not False or receipt.get("execution_admitted") is not False):
        raise ValueError("Extras date, full own-roster, raw basis, calendar or saved price source binding differs")
    if rows.loc[rows.tradestatus.eq("0"), "turn"].ne("").any():
        raise ValueError("Halted original turnover must remain an empty unknown, never zero")
    return rows, {**metadata, "package_plan_file": str(plan_file),
                  "package_plan_sha256": record["package_plan_sha256"],
                  "package_receipt_sha256": record["package_receipt_sha256"],
                  "unit_evidence_sha256": record["unit_evidence_sha256"],
                  "calendar_sha256": calendar_proof["sha256"]}


def _number(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _rows(current, prior):
    previous = {} if prior is None else prior.set_index("symbol").to_dict("index")
    result = []
    for raw in current.to_dict("records"):
        old = previous.get(raw["symbol"])
        turn = _number(raw["turn"])
        prev_turn = _number(old["turn"]) if old else None
        high, low = _number(raw["high"]), _number(raw["low"])
        reasons = []
        if turn is None or turn < 0:
            reasons.append("current_reported_turn_unknown_or_negative")
        if prior is None:
            reasons.append("previous_source_unknown")
        elif old is None:
            reasons.append("missing_symbol_in_exact_previous_own_roster")
        elif prev_turn is None or prev_turn < 0:
            reasons.append("previous_reported_turn_unknown_or_negative")
        if high is None or low is None or high <= 0 or low <= 0 or high < low:
            reasons.append("current_same_source_raw_range_unknown_or_invalid")
        increase = None if turn is None or prev_turn is None or min(turn, prev_turn) < 0 else turn > prev_turn
        span = None if high is None or low is None or high <= 0 or low <= 0 or high < low else high > low
        status = "unknown" if reasons else "known_true" if increase and span else "known_false"
        reason = ";".join(reasons) if reasons else "reported_turn_increased_and_nonzero_raw_range" if status == "known_true" else ";".join(
            name for known, name in [(increase, "reported_turn_not_strictly_increased"), (span, "same_source_raw_high_equals_low")] if not known)
        result.append({"date": raw["date"], "symbol": raw["symbol"], "status": status,
            "reason": reason, "unknown_reasons": reasons,
            "turn_raw": raw["turn"], "turn_percent": None if turn is None or turn < 0 else float(turn),
            "previous_turn_raw": old["turn"] if old else None,
            "previous_turn_percent": None if prev_turn is None or prev_turn < 0 else float(prev_turn),
            "high_raw": raw["high"], "low_raw": raw["low"],
            "high": None if high is None else float(high), "low": None if low is None else float(low),
            "turn_increased": increase, "raw_range_nonzero": span,
            "tradestatus": raw["tradestatus"], "isST": raw["isST"],
            "previous_tradestatus": old["tradestatus"] if old else None,
            "previous_isST": old["isST"] if old else None})
    return result


def _prepare(current_extras, previous_extras, calendar, day, source_day_proofs, observed):
    if current_extras is None or not Path(current_extras).is_file():
        raise ValueError("No current extras source: confirmation output is not created")
    previous, calendar_proof = _calendar(calendar, day)
    proofs = _proofs(source_day_proofs, day, previous)
    lookup = {item["day"]: item for item in proofs}
    current, current_proof = _source(current_extras, day, calendar_proof, lookup[day], observed)
    if previous_extras is None:
        prior, prior_proof = None, None
    else:
        if previous is None:
            raise ValueError("Pinned calendar has no exact prior session; a previous observed date cannot substitute")
        prior, prior_proof = _source(previous_extras, previous, calendar_proof, lookup.get(previous), observed)
    rows = _rows(current, prior)
    counts = {"member_count": len(rows), **{key: sum(r["status"] == key for r in rows) for key in RULE_SPEC["row_statuses"]},
              "unknown_reason_counts": dict(Counter(reason for row in rows for reason in row["unknown_reasons"]))}
    return {"day": day, "previous_day": previous, "calendar": calendar_proof,
            "source_day_proofs": proofs, "sources": {"current": current_proof, "previous": prior_proof},
            "previous_source_status": "previous_source_unknown" if prior is None else "exact_previous_source_available",
            "rows": rows, "counts": counts}


def build_confirmation(current_extras, previous_extras, calendar, *, day, source_day_proofs, out):
    """Write NEW research inputs only after both supplied sources validate."""
    started = _now()
    prepared = _prepare(current_extras, previous_extras, calendar, day, source_day_proofs, started)
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Confirmation inputs require a new independent directory")
    out.mkdir(parents=True, exist_ok=False)
    rows = prepared.pop("rows")
    data_file = out / "confirmation_rows.json"
    write_json(data_file, rows)
    data_hash = _digest(data_file)
    finished = _now()
    if finished < started:
        raise ValueError("Actual confirmation completion clock moved backwards")
    record = {"schema": SCHEMA, "method_id": METHOD_ID, "status": "completed_research_confirmation_inputs",
        "started_at": started.isoformat(), "actual_derived_at": finished.isoformat(), "available_at": finished.isoformat(),
        "rule_spec": RULE_SPEC, **prepared, "data_file": str(data_file), "data_sha256": data_hash,
        "turn_unit": "percent", "turn_scaled": False, **FLAGS}
    manifest = out / "manifest.json"
    write_json(manifest, record)
    return manifest, record


def load_confirmation(manifest, *, day, calendar, source_day_proofs, observed_at):
    """Read exact saved evidence; later-created sidecars cannot serve earlier decisions."""
    observed = _stamp(observed_at)
    if observed > _now():
        raise ValueError("A future observation clock cannot admit confirmation evidence")
    path = Path(manifest).resolve()
    manifest_hash = _digest(path)
    record = _read(path)
    if (record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("status") != "completed_research_confirmation_inputs"
            or record.get("day") != day or record.get("rule_spec") != RULE_SPEC
            or record.get("turn_unit") != "percent" or record.get("turn_scaled") is not False
            or any(record.get(key) is not value for key, value in FLAGS.items())
            or not _stamp(record["started_at"]) <= _stamp(record["actual_derived_at"]) == _stamp(record["available_at"]) <= observed):
        raise ValueError("Confirmation method, boundary, exact date or actual availability differs")
    sources = record["sources"]
    prepared = _prepare(sources["current"]["extras_result"], sources["previous"]["extras_result"] if sources["previous"] else None,
                        calendar, day, source_day_proofs, _stamp(record["started_at"]))
    data_file = Path(record["data_file"]).resolve()
    data_file.relative_to(path.parent)
    if _digest(data_file) != record["data_sha256"]:
        raise ValueError("Saved complete confirmation rows changed")
    rows = _read(data_file)
    if rows != prepared.pop("rows") or any(record.get(key) != value for key, value in prepared.items()):
        raise ValueError("Confirmation original source, full rows, exact previous date or derived facts changed")
    if _digest(path) != manifest_hash:
        raise ValueError("Confirmation manifest changed during read")
    return {**prepared, "available_at": record["available_at"], "rows": rows,
        "by_symbol": {row["symbol"]: row for row in rows},
        "sourceproof": {"manifest": {"file": str(path), "sha256": manifest_hash, "available_at": record["available_at"]},
                        **sources}, "turn_unit": "percent", "turn_scaled": False, "rule_spec": RULE_SPEC, **FLAGS}


def main(argv=None):
    import argparse
    from .leader_entry_comparison_publication import load_registration
    from .leader_published_forward_run import _load

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("input-bundle", "registration", "current-extras", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--previous-extras", type=Path)
    args = parser.parse_args(argv)
    registered = load_registration(args.registration)
    own = _load(args.input_bundle, registered["binding"], registered["calendar"], _now())
    day = own["days"][-1]
    if day not in registered["window"][:40]:
        raise ValueError("Confirmation is only required for a registered decision close")
    path, result = build_confirmation(args.current_extras, args.previous_extras,
        registered["binding"]["calendar"], day=day,
        source_day_proofs=own["bundle"]["source_packages"], out=args.out)
    print(json.dumps({"file": str(path), "day": day, "previous_day": result["previous_day"],
        "available_at": result["available_at"], "counts": result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
