"""Plan or explicitly acquire one exact-D Sina comparison for append candidates.

The default is read-only planning. Execute permits one GET, at most 100 current
matured symbols, no retries, no fallback, and no prices from the next session.
This public endpoint is never a BrokerQuote or an independence certification.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import requests

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT
from . import seat_append_dated_inputs as dated
from . import seat_hypothesis_dated_inputs as original
from . import leader_cohort_observation as parser


SCHEMA = "public_seat_append_dynamic_source_comparison_v1"
CONTRACT_FILE = DATA_ROOT / parser.CONTRACT_RELATIVE
FIELDS = original.FIELDS
FLAGS = {"candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "account_created": False,
    "engine_run": False, "orders_generated": False, "ledger_mutated": False,
    "pit_verified": False, "independent_source_identity_verified": False, "is_BrokerQuote": False,
    "historical_publication_verified": False, "publication_created": False}
MAPPING = {"open": "open_price_rmb", "high": "high_price_rmb", "low": "low_price_rmb",
    "close": "last_price_rmb", "pre_close": "previous_close_price_rmb", "amount": "source_raw_amount"}


def _now(clock):
    value = clock() if clock else datetime.now(MARKET_TZ)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An actual aware request/response clock is required")
    return value.astimezone(MARKET_TZ)


def _contract(now):
    path = Path(CONTRACT_FILE).resolve()
    record = original._read(path)
    if (record.get("schema") != "sina_ordinary_SH_SZ_A_price_time_contract_assessment_v1"
            or record.get("price_currency", {}).get("a_currency_value") != "CNY"
            or record.get("price_currency", {}).get("normal_SH_SZ_A_only") is not True
            or record.get("volume_amount_orderbook", {}).get("normalization_performed") is not False
            or record.get("volume_amount_orderbook", {}).get("amount_display_evidence")
                != "official stock_A formatter labels amount yuan"):
        raise ValueError("Archived ordinary-A CNY / official formatter-yuan contract required")
    original._stamp(record.get("created_at"), now)
    references = [original._ref(path), original._verify({"file": record["probe_receipt"],
        "sha256": record["probe_receipt_sha256"]})]
    if not record.get("official_sources"):
        raise ValueError("Contract needs its actual archived official source bytes")
    for item in record["official_sources"]:
        reference = original._verify({"file": item.get("raw_file") or item["original_file"], "sha256": item["sha256"]})
        if item.get("retrieved_at") is not None:
            original._stamp(item["retrieved_at"], now)
        references.append(reference)
    return dated._refs(references)


def _prepare(registration, catalog, close, decision_day, out, clock):
    now, day = _now(clock), original._day(decision_day)
    registered = dated.load_registration(registration, now=now)
    loaded = dated.load_registered_catalog(catalog, registered, now=now)
    if day not in registered["sessions"] or day < registered["record"]["first_decision_day"]:
        raise ValueError("Exact registered calendar and prospective first D must be respected")
    arms = dated.mature_arms(loaded, registered["sessions"], day)
    symbols = dated.required_audit_symbols(arms)
    if len(symbols) > 100:
        raise ValueError("A single bounded comparison permits at most 100 matured symbols")
    codes = [parser._provider_symbol(symbol) for symbol in symbols]
    references = dated._refs([*registered["references"], *loaded["references"],
        original._ref(parser.__file__), original._ref(__file__)])
    contract_refs = _contract(now) if symbols else []
    references = dated._refs([*references, *contract_refs])
    folder = Path(close).resolve()
    packages = loaded["packages"] + [Path(r["file"]).parent for r in references] + [folder]
    target = original._protect(out, [r["file"] for r in references], packages)
    plan = {"schema": SCHEMA, "status": "planned_no_request", "planned_at": now.isoformat(),
        "decision_day": day, "symbols": symbols, "symbol_count": len(symbols),
        "arm_candidate_counts": {arm["arm_id"]: len(arm["ranked_candidates"]) for arm in arms},
        "scope": "union_of_all_four_arms_first_matured_deduplicated_candidates_before_top5",
        "registration": registered["reference"], "catalog": loaded["reference"], "calendar": registered["calendar"],
        "close_folder": str(folder), "endpoint": parser.ENDPOINT + ",".join(codes) if codes else None,
        "max_symbols": 100, "max_get_requests": 1 if symbols else 0, "auto_retry": False,
        "quote_request_count": 0, "request_count": 0, "network_accessed": False, "quote_response_observed": False,
        "quote_fetch_succeeded": None, "fetch_succeeded": None, "source_contract": contract_refs[0] if contract_refs else None,
        "source_contract_references": contract_refs, "input_references": references,
        "exact_complete_D_available": False, "ready_for_execute": False, "output_created": False,
        "next_execution_raw_read": False, **FLAGS}
    if now.date().isoformat() != day or now < datetime.fromisoformat(day + "T15:05:00+08:00"):
        plan.update(status="waiting_for_same_D_post_close", reason="single_observation_must_occur_on_D_after_1505")
        return plan, registered, None, target, references, packages
    if not all((folder / name).is_file() for name in ("plan.json", "receipt.json", "raw_daily.csv")):
        plan.update(status="waiting_for_exact_complete_D_close", reason="missing_complete_D_package")
        return plan, registered, None, target, references, packages
    receipt = original._read(folder / "receipt.json")
    if receipt.get("status") != "complete_full_dated_close_inventory_not_admitted":
        plan.update(status="waiting_for_exact_complete_D_close", reason="D_close_not_complete",
            observed_source_status=receipt.get("status"))
        return plan, registered, None, target, references, packages
    primary, rows, inventory = original._complete(folder, day, now)
    if original._read(folder / "plan.json")["calendar_sha256"] != registered["calendar"]["sha256"]:
        raise ValueError("Exact complete D and registered calendar differ")
    if not set(symbols) <= set(rows):
        raise ValueError("All matured candidates must exist in the exact full D inventory")
    extra = [primary[key] for key in ("raw", "plan", "receipt", "roster")]
    extra += [original._ref(primary["acquisition"]["roster_receipt"])]
    references = dated._refs([*references, *extra])
    packages += [Path(r["file"]).parent for r in extra]
    original._protect(target, [r["file"] for r in references], packages)
    plan.update(status="planned_ready_no_request" if symbols else "planned_empty_scope_no_quote",
        exact_complete_D_available=True, ready_for_execute=True, D_primary=primary, D_inventory=inventory,
        input_references=references)
    return plan, registered, primary, target, references, packages


def _rows(quotes, symbols, day, received):
    if [quote["symbol"] for quote in quotes] != symbols:
        raise ValueError("Parser did not retain the exact requested complete symbol inventory")
    result = []
    for quote in quotes:
        stamp = original._stamp(quote.get("source_quote_time"), received)
        if (quote.get("source_date") != day or quote.get("source_status") != "00"
                or not datetime.fromisoformat(day + "T15:00:00+08:00") <= stamp <= received):
            raise ValueError("Exact-D regular 00 post-close source date/time is required: " + quote["symbol"])
        row = {"date": day, "symbol": quote["symbol"], **{field: quote.get(key) for field, key in MAPPING.items()}}
        try:
            values = {key: Decimal(str(row[key])) for key in MAPPING}
        except ArithmeticError as error:
            raise ValueError("Missing or invalid original six-field source values") from error
        if (not all(value.is_finite() for value in values.values()) or values["amount"] < 0
                or min(values[key] for key in MAPPING if key != "amount") <= 0
                or values["high"] < max(values["open"], values["close"])
                or values["low"] > min(values["open"], values["close"])):
            raise ValueError("Nonfinite, zero or inconsistent original six-field source values")
        result.append(row)
    return result


def run(registration, catalog, close, decision_day, out, *, execute=False, _get=None, _clock=None):
    """Return a pure plan, or explicitly make one request and retain its evidence."""
    plan, registered, primary, target, references, _ = _prepare(
        registration, catalog, close, decision_day, out, _clock)
    if not execute:
        return plan
    if not plan["ready_for_execute"]:
        raise ValueError("Execute requires the actual same-D post-close clock and exact complete D; " + plan["status"])
    for reference in references:
        original._verify(reference)
    target.mkdir(parents=True, exist_ok=False)
    original._write(target / "plan.json", plan)
    source_folder = target / "comparison_source"
    source_folder.mkdir()
    outcome = {**plan, "status": "execution_attempt_started", "output_created": True,
        "output_directory": str(target), "audit": None, "all_rows_match": None}
    day, symbols = plan["decision_day"], plan["symbols"]
    try:
        response_ref, request_ref, received = None, None, _now(_clock)
        if (received.date().isoformat() != day or received < datetime.fromisoformat(plan["planned_at"])
                or received < datetime.fromisoformat(day + "T15:05:00+08:00")):
            raise ValueError("Actual attempt must remain on D after its preflight clock")
        if symbols:
            request = {"schema": "public_seat_append_single_Sina_request_v1", "method": "GET",
                "endpoint": plan["endpoint"], "symbols": symbols, "requested_at": received.isoformat(),
                "timeout_seconds": [5, 12], "allow_redirects": False, "auto_retry": False,
                "headers": {"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
                "registration": plan["registration"], "catalog": plan["catalog"], **FLAGS}
            original._write(source_folder / "request.json", request)
            request_ref = original._ref(source_folder / "request.json")
            outcome.update(quote_request_count=1, request_count=1, network_accessed=True, request=request_ref)
            response = (_get or requests.get)(request["endpoint"], timeout=(5, 12), allow_redirects=False,
                headers=request["headers"])
            received = _now(_clock)
            raw = response.content
            if not isinstance(raw, bytes):
                raise ValueError("Transport must preserve original response bytes")
            (source_folder / "source_response.raw").write_bytes(raw)
            response_ref = original._ref(source_folder / "source_response.raw")
            response_record = {"schema": "public_seat_append_single_Sina_response_v1", "request": request_ref,
                "response_received_at": received.isoformat(), "http_status": response.status_code,
                "raw_response": response_ref, "response_headers": {key: response.headers.get(key) for key in
                    ("Date", "Content-Type", "Content-Length", "Location")}, **FLAGS}
            original._write(source_folder / "response.json", response_record)
            outcome.update(quote_response_observed=True, http_status=response.status_code,
                response=original._ref(source_folder / "response.json"), raw_response=response_ref,
                response_received_at=received.isoformat())
            if received.date().isoformat() != day or received < datetime.fromisoformat(request["requested_at"]):
                raise ValueError("Response crossed D or precedes its actual request clock")
            if response.status_code != 200:
                raise ValueError("Sina HTTP request failed; no retry or fallback")
            session = {"session_start": datetime.fromisoformat(day + "T09:30:00+08:00"),
                "stop_at": datetime.fromisoformat(day + "T15:00:00+08:00")}
            try:
                quotes, grammar = parser.parse_batch(raw,
                    [{"symbol": symbol, "name": None, "labels": []} for symbol in symbols], received,
                    session=session, registered_at=registered["record"]["registered_at"], batch_id=0)
            except (ValueError, UnicodeError) as error:
                original._write(source_folder / "parsed_source.json", {"status": "failed_parser_no_retry",
                    "error": str(error), "raw_response": response_ref, "parser": original._ref(parser.__file__), **FLAGS})
                raise
            original._write(source_folder / "parsed_source.json", {"quotes": quotes, "grammar": grammar,
                "raw_response": response_ref, "parser": original._ref(parser.__file__),
                "current_quote_admitted": False, **FLAGS})
            rows = _rows(quotes, symbols, day, received)
            outcome["quote_fetch_succeeded"] = outcome["fetch_succeeded"] = True
        else:
            rows = []
            original._write(source_folder / "request.json", {"status": "not_requested_empty_candidate_scope",
                "symbols": [], "quote_request_count": 0, "network_accessed": False, **FLAGS})
            request_ref = original._ref(source_folder / "request.json")
            original._write(source_folder / "parsed_source.json", {"status": "no_quote_input_empty_scope",
                "quotes": [], "grammar": None, "quote_response_observed": False, **FLAGS})
        comparison, metadata = source_folder / "comparison.csv", source_folder / "metadata.json"
        pd.DataFrame(rows, columns=["date", "symbol", *FIELDS]).to_csv(comparison, index=False, encoding="utf-8")
        original._write(metadata, {"schema": original.COMPARISON_SCHEMA,
            "source_family": "Sina" if symbols else "ExplicitNoQuoteEmptyScope",
            "source_id": plan["endpoint"] if symbols else "empty_scope_no_price_source_observed_or_requested",
            "day": day, "source_available_at": received.isoformat(), "basis": "raw_unadjusted",
            "price_unit": "CNY_per_share", "amount_unit": "CNY", "independence_declared": True,
            "independence_declaration_scope": "intended_separate_endpoint_family_only_not_upstream_identity_certification",
            "scope_empty_is_not_market_validation": not symbols, "quote_response_observed": bool(symbols),
            "quote_request_count": 1 if symbols else 0, "request_count": 1 if symbols else 0,
            "fetch_succeeded": True if symbols else None, "network_accessed": bool(symbols),
            "source_available_at_semantics": "actual_response_received_at" if symbols else "empty_scope_record_clock_not_a_market_quote_time",
            "source_contract": plan["source_contract"], "source_contract_references": plan["source_contract_references"],
            "raw_response": response_ref, "request": request_ref,
            "parser": original._ref(parser.__file__), "implementation": original._ref(__file__),
            "semantics": "Original ordinary-A OHLC/previous-close and official-formatter-yuan amount strings; no scaling or adjustment",
            "limitations": ["Separate endpoint family declared; upstream source independence remains uncertified",
                "Exact-D after-close comparison only; no current freshness, BrokerQuote, PIT or exchange-wide validation",
                "Empty candidate scope contains no fetched or fabricated prices and does not validate the market"], **FLAGS})
        for reference in references:
            original._verify(reference)
        audit = original.build_exact_source_audit(close, comparison, metadata, target / "audit.json",
            decision_day=day, symbols=symbols)
        outcome.update(audit=original._ref(target / "audit.json"), comparison=original._ref(comparison),
            comparison_metadata=original._ref(metadata), all_rows_match=audit["all_rows_match"],
            scope_empty_is_not_market_validation=audit["scope_empty_is_not_market_validation"],
            status=("completed_empty_scope_no_quote_no_market_validation" if not symbols else
                "completed_exact_D_comparison_only" if audit["all_rows_match"] is True else "blocked_exact_source_mismatch"))
    except Exception as error:
        outcome.update(status="failed_stopped_no_retry", error=type(error).__name__ + ": " + str(error))
        if outcome["quote_request_count"] and outcome["quote_fetch_succeeded"] is None:
            outcome["quote_fetch_succeeded"] = outcome["fetch_succeeded"] = False
    outcome["completed_at"] = _now(_clock).isoformat()
    original._write(target / "outcome.json", outcome)
    return outcome


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--registration", required=True, type=Path)
    cli.add_argument("--catalog", required=True, type=Path)
    cli.add_argument("--close", required=True, type=Path)
    cli.add_argument("--decision-day", required=True)
    cli.add_argument("--out", required=True, type=Path)
    cli.add_argument("--execute", action="store_true")
    args = cli.parse_args(argv)
    result = run(args.registration, args.catalog, args.close, args.decision_day, args.out, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 2 if result["status"].startswith(("failed_", "blocked_")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
