"""Publish one dated, source-bound input for the four registered seat arms.

This module does not run an Engine, create an account or fetch data.  The first
registration's single source snapshot is fixed; future disclosure batches need
a separately specified append contract, not a silent change to this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from ..rules import stock_board
from .baostock_full_close import load_complete_close
from .exchange_calendar import load_calendar
from .leader_daily_signals import _calendar_view
from .provider_close_batches import _acquisition_times, _inventory
from . import seat_hypothesis_panel_replay as original
from . import seat_hypothesis_snapshot as snapshot


SCHEMA = "public_seat_single_snapshot_dated_inputs_v1"
PUBLICATION_SCHEMA = "public_seat_single_snapshot_dated_publication_v1"
AUDIT_SCHEMA = "seat_dated_exact_source_bar_audit_v1"
COMPARISON_SCHEMA = "seat_exact_comparison_source_metadata_v1"
REGISTRATION_SCHEMA = "public_seat_two_recipe_two_lag_prospective_registration_v1"
ARMS = {f"{recipe}_lag{lag}": (recipe, lag) for recipe in
        ("legacy_repeated_sum", "stockday_unique") for lag in (1, 2)}
FIELDS = ("open", "high", "low", "close", "pre_close", "amount")
PRICE_TOLERANCE = Decimal("0.0001")
AMOUNT_RELATIVE_TOLERANCE = Decimal("0.001")
FLAGS = {"candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "engine_run": False,
    "account_created": False, "orders_generated": False, "ledger_mutated": False,
    "network_accessed": False, "pit_verified": False, "corporate_actions_verified": False,
    "independent_source_identity_verified": False, "historical_publication_verified": False}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return {"file": str(path), "sha256": sha256(data).hexdigest()}


def _verify(reference):
    actual = _ref(reference["file"])
    if actual["sha256"] != reference.get("sha256"):
        raise ValueError("Registered/source file changed: " + actual["file"])
    return actual


def _stamp(value, now):
    if not isinstance(value, str):
        raise ValueError("Source and registration clocks must be explicit aware ISO strings")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None or result > now:
        raise ValueError("Naive or future source/registration clock")
    return result.astimezone(MARKET_TZ)


def _day(value):
    if not isinstance(value, str) or datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
        raise ValueError("Exact canonical session date is required")
    return value


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _protect(out, files, packages=()):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Dated inputs/audit require a new output path")
    for file in files:
        source = Path(file).resolve()
        if out == source or out in source.parents:
            raise ValueError("Output cannot cover an input or source")
    for package in packages:
        package = Path(package).resolve()
        if out == package or package in out.parents or out in package.parents:
            raise ValueError("Output cannot cover or sit inside an input package")
    return out


def _complete(folder, day, now):
    folder = Path(folder).resolve()
    metadata, raw, roster = load_complete_close(folder)
    metadata["ordinary_roster"] = roster.to_dict("records")
    inventory = _inventory(raw, metadata, "pinned_static_exchange_calendar")
    if metadata["plan"]["day"] != day:
        raise ValueError("Close package is not the exact requested D")
    clocks = _acquisition_times(metadata, folder)
    stamps = [*clocks["acquisition_timestamps"].values(),
        *clocks["roster_acquisition_timestamps"].values()]
    if clocks["terminal_completion_observed_at"] is None or clocks["roster_receipt_completed_at"] is None:
        raise ValueError("Complete raw and dated roster need actual terminal acquisition clocks")
    available = max(_stamp(value, now) for value in stamps)
    if available < datetime.fromisoformat(day + "T15:00:00+08:00"):
        raise ValueError("Complete close cannot be acquired before its actual close")
    roster_receipt = _read(clocks["roster_receipt"])
    proof = {"day": day, "source_folder": str(folder), "source_family": "BaoStock",
        "raw": _ref(folder / "raw_daily.csv"), "plan": _ref(folder / "plan.json"),
        "receipt": _ref(folder / "receipt.json"), "roster": _ref(roster_receipt["raw_csv"]),
        "roster_raw_sha256": metadata["plan"]["roster_raw_sha256"],
        "source_available_at": available.isoformat(), "acquisition": clocks,
        "inventory_rows": len(raw), "full_dated_roster_inventory_complete": True}
    # Both strict loaders preserve roster order; _inventory additionally checks
    # exact raw.code == roster.code order before this positional pairing.
    by_symbol = {symbol: dict(row) for symbol, row in zip(roster.symbol, raw.to_dict("records"))}
    return proof, by_symbol, inventory


def _number(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _audit_payload(close_folder, comparison_csv, comparison_metadata, *, decision_day, symbols, now):
    day = _day(decision_day)
    symbols = sorted(set(symbols))
    for symbol in symbols:
        stock_board(symbol)
    primary, rows, _ = _complete(close_folder, day, now)
    metadata = _read(comparison_metadata)
    if (metadata.get("schema") != COMPARISON_SCHEMA
            or metadata.get("source_family", "").strip().lower() in {"", "baostock"}
            or not metadata.get("source_id") or metadata.get("basis") != "raw_unadjusted"
            or metadata.get("price_unit") != "CNY_per_share" or metadata.get("amount_unit") != "CNY"
            or metadata.get("day") != day or metadata.get("independence_declared") is not True):
        raise ValueError("Independent comparison needs explicit source, raw basis, exact day and units")
    _stamp(metadata.get("source_available_at"), now)
    comparison_ref, metadata_ref = _ref(comparison_csv), _ref(comparison_metadata)
    if comparison_ref["file"] == primary["raw"]["file"] or comparison_ref["sha256"] == primary["raw"]["sha256"]:
        raise ValueError("Primary source cannot be its own independent price comparison")
    comparison = pd.read_csv(comparison_csv, dtype=str, keep_default_na=False)
    if not {"date", "symbol", *FIELDS} <= set(comparison.columns):
        raise ValueError("Comparison CSV requires canonical exact-date raw-price and amount fields")
    # Do not select D from a multi-day file: an unseen E must not enter this package.
    if not comparison.date.eq(day).all() or comparison.duplicated(["date", "symbol"]).any():
        raise ValueError("Comparison file must contain only unique exact D keys; no future raw rows")
    if not set(symbols) <= set(rows) or set(comparison.symbol) != set(symbols):
        raise ValueError("Comparison must cover exactly the required symbol/day keys in the primary inventory")
    comparison = comparison.set_index("symbol")
    records = []
    for symbol in symbols:
        results = {}
        for field in FIELDS:
            source_field = "preclose" if field == "pre_close" else field
            a, b = str(rows[symbol][source_field]), str(comparison.loc[symbol, field])
            first, second = _number(a), _number(b)
            valid = (first is not None and second is not None
                and (first >= 0 and second >= 0 if field == "amount" else first > 0 and second > 0))
            delta = abs(first - second) if valid else None
            tolerance = (max(abs(first), abs(second)) * AMOUNT_RELATIVE_TOLERANCE
                if valid and field == "amount" else PRICE_TOLERANCE)
            results[field] = {"primary": a, "comparison": b,
                "absolute_difference": str(delta) if delta is not None else None,
                "within_tolerance": delta <= tolerance if delta is not None else None}
        records.append({"day": day, "symbol": symbol, "fields": results,
            "all_fields_match": all(result["within_tolerance"] is True for result in results.values())})
    return {"schema": AUDIT_SCHEMA, "decision_day": day, "primary": primary,
        "comparison": {"raw": comparison_ref, "metadata": metadata_ref,
            "declared_source": metadata, "independence_verified": False},
        "scope_keys": [{"day": day, "symbol": symbol} for symbol in symbols],
        "tolerances": {"price_absolute_CNY": str(PRICE_TOLERANCE),
            "amount_relative": str(AMOUNT_RELATIVE_TOLERANCE)},
        "records": records, "all_rows_match": all(row["all_fields_match"] for row in records),
        "scope_empty_is_not_market_validation": not symbols, **FLAGS}


def build_exact_source_audit(close_folder, comparison_csv, comparison_metadata, out_file,
                             *, decision_day, symbols):
    """Save real per-row comparisons; the declared source identity is not certified."""
    out = _protect(out_file, [comparison_csv, comparison_metadata],
        [close_folder, Path(comparison_csv).resolve().parent, Path(comparison_metadata).resolve().parent])
    result = _audit_payload(close_folder, comparison_csv, comparison_metadata,
        decision_day=decision_day, symbols=symbols, now=_now())
    result["checked_at"] = _now().isoformat()
    out.parent.mkdir(parents=True, exist_ok=True)
    _write(out, result)
    return result


def validate_exact_source_audit(audit, close_folder, *, decision_day, symbols):
    """Re-read both original files; a renamed legacy summary cannot pass."""
    saved = _read(audit)
    if saved.get("schema") != AUDIT_SCHEMA:
        raise ValueError("Exact source-file/day/key audit required; old summary-only audits are unsupported")
    _stamp(saved.get("checked_at"), _now())
    comparison = saved["comparison"]
    expected = _audit_payload(close_folder, comparison["raw"]["file"], comparison["metadata"]["file"],
        decision_day=decision_day, symbols=symbols, now=_now())
    if saved != {**expected, "checked_at": saved["checked_at"]} or saved["all_rows_match"] is not True:
        raise ValueError("Exact source audit changed, has unknown fields, or its actual rows disagree")
    return {"reference": _ref(audit), "verification": expected}


def _registered(registration, now):
    record = _read(registration)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("candidate_panel_only") is not True
            or any(record.get(key) is not False for key in
                ("execution_admitted", "selection_enabled", "strategy_validated", "historical_publication_verified"))
            or record.get("config") != original._config(100000, 5).to_dict()
            or record.get("rule_spec") != snapshot.RULE_SPEC
            or record.get("selection_function") != "trading.engine.choose_targets"):
        raise ValueError("Original four-arm registration rules/config/boundaries changed")
    registered = _stamp(record.get("registered_at"), now)
    event_day = _day(record["source_event_date"])
    code = {_ref(module.__file__)["file"]: _ref(module.__file__) for module in (snapshot, original)}
    if len(record.get("code_refs", [])) != 2 or {_verify(r)["file"]: _verify(r) for r in record["code_refs"]} != code:
        raise ValueError("Both original frozen source implementations must match their registered exact SHA")
    source_ref = _verify(record["source_snapshot"])
    source = _read(source_ref["file"])
    available = _stamp(record["source_available_at"], now)
    if (source.get("schema") != snapshot.SCHEMA or source.get("status") != "completed_research_snapshot_only"
            or source.get("rule_spec") != record["rule_spec"]
            or source.get("source_available_at") != record["source_available_at"]
            or not available <= _stamp(source["created_at"], now) <= registered
            or source.get("source_query", {}).get("start") != event_day
            or source.get("source_query", {}).get("end") != event_day):
        raise ValueError("Registered single-event-date snapshot/source clock differs")
    archive = _verify(record["source_archive"])
    if _verify(source["source_archive"]) != archive:
        raise ValueError("Registered source cache archive differs")
    references = [_ref(registration), source_ref, archive, *code.values()]
    for reference in source.get("source_files", []):
        references.append(_verify(reference))
    references.append(_verify({"file": source["normalized_events_file"], "sha256": source["normalized_events_sha256"]}))
    arms = record.get("arms", [])
    if [arm.get("arm_id") for arm in arms] != list(ARMS):
        raise ValueError("Exactly the four registered recipes/lags in original order are required")
    recipes = {}
    for arm in arms:
        recipe, lag = ARMS[arm["arm_id"]]
        method = source["methods"][recipe]
        evidence_ref = _verify(arm["availability_evidence_ref"])
        evidence = _read(evidence_ref["file"])
        base, consumer = original._availability_method(evidence)
        if (arm["lag_sessions"] != lag or arm["base_hypothesis_id"] != base or arm["consumer_method_id"] != consumer
                or method["hypothesis_id"] != base or method["snapshot_method_id"] != base
                or arm["source_files"] != {key: method[key] for key in
                    ("candidates_file", "candidates_sha256", "panel_file", "panel_sha256", "availability_evidence")}
                or evidence_ref["file"] != str(Path(method["availability_evidence"]).resolve())):
            raise ValueError("Original arm method/source identity changed")
        panel_ref = _verify({"file": method["panel_file"], "sha256": method["panel_sha256"]})
        candidate_ref = _verify({"file": method["candidates_file"], "sha256": method["candidates_sha256"]})
        if (str(Path(evidence["candidates_file"]).resolve()) != candidate_ref["file"]
                or evidence.get("candidates_sha256") != candidate_ref["sha256"]
                or str(Path(evidence["panel_file"]).resolve()) != panel_ref["file"]
                or evidence.get("panel_sha256") != panel_ref["sha256"]
                or _verify(evidence["source_archive"]) != archive
                or evidence.get("source_available_at") != record["source_available_at"]):
            raise ValueError("Original sidecar must bind exact candidate/panel/archive/source clock")
        panel = pd.read_csv(panel_ref["file"], dtype=str, keep_default_na=False)
        candidates = pd.read_csv(candidate_ref["file"], dtype=str, keep_default_na=False)
        if (not {"trade_date", "symbol", "net_buy_to_reported_turnover", "hypothesis_h1", "hypothesis_status"} <= set(panel)
                or list(candidates.columns) != list(panel.columns)
                or candidates.to_dict("records") != panel.loc[panel.hypothesis_status.eq("matched")].to_dict("records")
                or not panel.trade_date.eq(event_day).all()
                or candidates.duplicated(["trade_date", "symbol"]).any()
                or not candidates.hypothesis_h1.map(original._flag).all()
                or original._candidate_method(candidates, empty_method=base) != base):
            raise ValueError("Original complete panel and matched candidate CSV differ")
        scores = []
        for row in candidates.to_dict("records"):
            score = float(row["net_buy_to_reported_turnover"])
            if not isfinite(score) or score < .01:
                raise ValueError("Registered candidate score is invalid")
            scores.append({"symbol": row["symbol"], "score": score, "event_date": event_day})
        supported = []
        for row in scores:
            try:
                stock_board(row["symbol"])
            except ValueError:
                if base == original.STOCKDAY_HYPOTHESIS_ID:
                    raise ValueError("Stock-day v2 cannot contain nonordinary candidates")
                continue
            supported.append(row)
        supported.sort(key=lambda row: (-row["score"], row["symbol"]))
        if (supported != arm["ranked_research_candidates"] or arm["all_source_candidate_rows"] != len(scores)
                or arm["supported_candidate_rows"] != len(supported)):
            raise ValueError("Registered frozen source candidate inventory/ranking changed")
        expected = {(event_day, row["symbol"]) for row in scores}
        clocks = {}
        for item in evidence["records"]:
            key = (_day(item["event_date"]), item["symbol"])
            if (key not in expected or key in clocks
                    or str(Path(item["source_file"]).resolve()) != archive["file"]
                    or item["source_sha256"] != archive["sha256"]):
                raise ValueError("Original event availability has foreign, duplicate or changed source evidence")
            times = [_stamp(item[key], now) for key in ("retrieved_at", "published_at") if item.get(key) is not None]
            if times and max(times) > available:
                raise ValueError("Event availability cannot exceed the registered complete-source clock")
            clocks[key] = {"retrieved_at": item.get("retrieved_at"), "published_at": item.get("published_at"),
                "available_at": max(times).isoformat() if item.get("retrieved_at") is not None else None}
        references += [panel_ref, candidate_ref, evidence_ref]
        recipes[recipe] = {"candidates": scores, "supported": supported, "clocks": clocks,
            "panel_observations": panel.to_dict("records")}
    return record, recipes, references


def build_dated_inputs(registration, calendar, close_folder, source_audit, out, *, decision_day):
    """Publish a same-D input or return a source wait without creating anything."""
    now, day = _now(), _day(decision_day)
    registered, recipes, references = _registered(registration, now)
    payload = load_calendar(Path(calendar))
    if payload is None:
        raise ValueError("Pinned exchange calendar is required")
    year = int(payload["year"])
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or day not in sessions or registered["source_event_date"] not in sessions:
        raise ValueError("D and the event must lie on the fixed exchange calendar")
    index = sessions.index(day)
    if index + 1 >= len(sessions):
        raise ValueError("Exact next calendar session is required; no weekday fallback")
    execution = sessions[index + 1]
    calendar_ref = _ref(calendar)
    packages = [Path(registration).resolve().parent,
        Path(registered["source_snapshot"]["file"]).resolve().parent, Path(close_folder).resolve()]
    out = _protect(out, [r["file"] for r in references] + [calendar, source_audit], packages)
    folder = Path(close_folder).resolve()
    required = [folder / name for name in ("plan.json", "receipt.json", "raw_daily.csv")]
    if not all(file.is_file() for file in required):
        return {"schema": SCHEMA, "status": "waiting_for_exact_complete_D_close", "decision_day": day,
            "next_execution_day": execution, "output_created": False, "publication": None, **FLAGS}
    receipt = _read(folder / "receipt.json")
    if receipt.get("status") != "complete_full_dated_close_inventory_not_admitted":
        return {"schema": SCHEMA, "status": "waiting_for_exact_complete_D_close", "decision_day": day,
            "observed_source_status": receipt.get("status"), "output_created": False, "publication": None, **FLAGS}
    if now.date().isoformat() != day or now < datetime.fromisoformat(day + "T15:05:00+08:00"):
        raise ValueError("Dated publication must actually occur on D after 15:05; no cross-day backfill")
    primary, raw_rows, inventory = _complete(folder, day, now)
    if _read(folder / "plan.json")["calendar_sha256"] != calendar_ref["sha256"]:
        raise ValueError("Complete D source and publisher calendar differ")
    symbols = sorted({row["symbol"] for recipe in recipes.values() for row in recipe["supported"]})
    if not Path(source_audit).is_file():
        return {"schema": SCHEMA, "status": "waiting_for_exact_bound_independent_source_audit",
            "decision_day": day, "required_symbols": symbols, "output_created": False, "publication": None, **FLAGS}
    audit = validate_exact_source_audit(source_audit, folder, decision_day=day, symbols=symbols)
    references += [calendar_ref, audit["reference"], *[primary[key] for key in ("raw", "plan", "receipt", "roster")],
        audit["verification"]["comparison"]["raw"], audit["verification"]["comparison"]["metadata"]]
    _protect(out, [r["file"] for r in references], packages + [Path(r["file"]).parent for r in references])
    if _stamp(registered["registered_at"], now).date().isoformat() > day:
        raise ValueError("D cannot precede the original prospective registration")
    event_index = sessions.index(registered["source_event_date"])
    arms = []
    for arm in registered["arms"]:
        recipe, lag = ARMS[arm["arm_id"]]
        original_recipe = recipes[recipe]
        earliest = sessions[event_index + lag] if event_index + lag < len(sessions) else None
        schedule, selected = [], []
        supported_symbols = {row["symbol"] for row in original_recipe["supported"]}
        for row in original_recipe["candidates"]:
            clock = original_recipe["clocks"].get((row["event_date"], row["symbol"]))
            available = clock["available_at"] if clock else None
            first = None
            if available is not None and earliest is not None:
                effective = datetime.fromisoformat(available)
                first = next((session for session in sessions[event_index + lag:]
                    if effective <= datetime.fromisoformat(session + "T15:00:00+08:00")), None)
            state = ("outside_scope" if row["symbol"] not in supported_symbols else
                "unknown" if available is None else "not_matured" if first is None or day < first else
                "matured_on_D" if day == first else "matured_before_D")
            observed = {**row, "source_clock": clock, "earliest_lag_calendar_day": earliest,
                "first_eligible_decision_day": first, "status": state,
                "source_available_by_D_1500": (datetime.fromisoformat(available) <=
                    datetime.fromisoformat(day + "T15:00:00+08:00")) if available else None}
            schedule.append(observed)
            if state == "matured_on_D":
                selected.append(row)
        selected.sort(key=lambda row: (-row["score"], row["symbol"]))
        arms.append({"arm_id": arm["arm_id"], "base_hypothesis_id": arm["base_hypothesis_id"],
            "consumer_method_id": arm["consumer_method_id"], "lag_sessions": lag,
            "source_ranked_candidates": original_recipe["supported"], "ranked_candidates": selected,
            "event_schedule": schedule, "panel_observations": original_recipe["panel_observations"],
            "decision_kind": "RANKED_CANDIDATES" if selected else "NO_CHANGE",
            "reason": "known_available_events_only" if selected else "no_matured_known_supported_event_on_D",
            "weights": None, "unknown_event_is_not_a_veto_on_other_known_events": True})
    result = {"schema": SCHEMA, "status": "completed_dated_research_inputs_only", "decision_day": day,
        "next_execution_day": execution, "registration": _ref(registration), "calendar": calendar_ref,
        "config": registered["config"], "input_references": references, "D_primary": primary,
        "D_inventory": inventory, "source_audit": audit, "arms": arms,
        "next_execution_raw_supplied": False, "next_execution_quality": "unknown_not_an_observed_raw_bar",
        "target_weights_generated": False, "metrics": None,
        "selection_rule": "Engine.choose_targets_after_D_opening_with_actual_held_retain_rank_20",
        "source_availability_cutoff": day + "T15:00:00+08:00",
        "source_scope": "original_registered_single_snapshot_only_no_new_disclosures_appended",
        "limitations": ["Dated provider inventory is not verified exchange/PIT eligibility.",
            "IPO, corporate actions and E opening quality remain unresolved; no account advance is admitted.",
            "Comparison identity is declared, not independently certified; numerical agreement is not PIT proof.",
            "Empty or unknown events preserve holdings; no FULL_TARGET or forced liquidation is generated."], **FLAGS}
    out.mkdir(parents=True)
    _write(out / "bundle.json", result)
    # Obtain the publication clock only after the immutable payload has closed.
    for reference in references:
        _verify(reference)
    published = _now()
    if published.date().isoformat() != day or published < now:
        raise ValueError("Publication crossed D or its actual clock moved backwards; payload remains unpublished")
    publication = {"schema": PUBLICATION_SCHEMA, "published_at": published.isoformat(),
        "decision_day": day, "execution_day": execution, "bundle": _ref(out / "bundle.json"),
        "registration": result["registration"], "same_D_actual_publication": True, **FLAGS}
    _write(out / "publication.json", publication)
    return {**result, "publication": {**_ref(out / "publication.json"), "published_at": published.isoformat()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dated = commands.add_parser("dated-inputs", help="Publish the original four arms' exact D input, without an account")
    dated.add_argument("--registration", required=True, type=Path)
    dated.add_argument("--calendar", required=True, type=Path)
    dated.add_argument("--close", required=True, type=Path, dest="close_folder")
    dated.add_argument("--source-audit", required=True, type=Path)
    dated.add_argument("--decision-day", required=True)
    dated.add_argument("--out", required=True, type=Path, help="New dated output directory")
    audit = commands.add_parser("exact-source-audit", help="Compare exact D source files and preserve all field results")
    audit.add_argument("--close", required=True, type=Path, dest="close_folder")
    audit.add_argument("--comparison-csv", required=True, type=Path)
    audit.add_argument("--comparison-metadata", required=True, type=Path)
    audit.add_argument("--decision-day", required=True)
    audit.add_argument("--out", required=True, type=Path, help="New audit JSON file")
    scope = audit.add_mutually_exclusive_group(required=True)
    scope.add_argument("--symbol", action="append", dest="symbols", help="Repeat for every exact required ordinary-share symbol")
    scope.add_argument("--empty-scope", action="store_true", help="Explicitly audit an observed empty candidate batch")
    args = parser.parse_args(argv)
    if args.command == "dated-inputs":
        result = build_dated_inputs(args.registration, args.calendar, args.close_folder,
            args.source_audit, args.out, decision_day=args.decision_day)
        summary = {"schema": result["schema"], "status": result["status"],
            "decision_day": result["decision_day"], "next_execution_day": result.get("next_execution_day"),
            "publication": result.get("publication"), "output_created": args.out.exists(), **FLAGS}
    else:
        result = build_exact_source_audit(args.close_folder, args.comparison_csv,
            args.comparison_metadata, args.out, decision_day=args.decision_day,
            symbols=[] if args.empty_scope else args.symbols)
        summary = {"schema": result["schema"], "status": ("completed_exact_comparison_rows" if
            result["all_rows_match"] else "incomplete_or_mismatched_exact_comparison_rows"),
            "decision_day": result["decision_day"], "scope_rows": len(result["records"]),
            "matched_rows": sum(row["all_fields_match"] for row in result["records"]),
            "audit": _ref(args.out), **FLAGS}
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
