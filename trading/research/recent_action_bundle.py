"""Project completed scoped action queries into the existing cash-only bundle.

Official reviews are explicit saved inputs. Missing terms stay pending; this
producer neither fetches a notice nor infers a reference from price agreement.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import tempfile

import pandas as pd

from ..contracts import MARKET_TZ
from ..symbols import normalize_symbol
from . import recent_action_extension as contract
from .leader_daily_signals import _calendar_view


SCHEMA = "recent_action_bundle_build_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "pit_verified": False, "returns_reportable": False, "network_accessed": False,
    "engine_called": False, "ledger_mutated": False, "targets_generated": False,
    "old_sources_modified": False, "full_accounting_validation_performed": False}
FIELDS = ("code", "symbol", *contract.DATES, *contract.TERMS)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _related(row, start, end):
    return any(start <= row.get(key, "") <= end for key in contract.DATES)


def _counter(rows):
    return Counter((row["symbol"], contract._same_economics(row)) for row in rows)


def _provider(root, day, start, sessions, now, refs):
    names = ("collection_plan.json", "universe.csv", "calendar.csv", "status.json")
    files = {name: _ref(root / name) for name in names}
    refs.extend(files.values())
    plan, status = _read(root / names[0]), _read(root / names[-1])
    if status.get("status") != "source_cache_complete" or status.get("error") is not None:
        return None, {"provider_status": status.get("status"), "provider_error": status.get("error")}
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False)
    calendar = pd.read_csv(root / "calendar.csv", dtype=str, keep_default_na=False).date.tolist()
    if not {"code", "symbol"} <= set(universe) or universe.empty:
        raise ValueError("Completed action provider scope is missing")
    scope = universe.symbol.tolist()
    if (len(scope) != len(set(scope)) or universe.code.duplicated().any()
            or not universe.symbol.eq(universe.code.map(lambda value: normalize_symbol(value).vt_symbol)).all()
            or plan.get("source") != "BaoStock" or plan.get("summary_stage") != "actions"
            or status.get("summary_stage") != "actions" or plan.get("end") != day or status.get("end") != day
            or any(int(item.get(key, -1)) != len(scope) for item, key in
                ((plan, "symbols"), (plan, "expected_queries"), (status, "complete_symbols"), (status, "action_queries")))
            or plan.get("universe_sha256") != files["universe.csv"]["sha256"]
            or plan.get("calendar_sha256") != files["calendar.csv"]["sha256"]
            or [value for value in calendar if start <= value <= day] != sessions):
        raise ValueError("Completed scoped action provider plan, calendar or counts changed")
    queries, rows = [], []
    for symbol in scope:
        receipt_path = root / "receipts" / (symbol + ".json")
        receipt = _read(receipt_path)
        year = int(day[:4])
        task = receipt.get("tasks", {}).get("actions_" + str(year), {})
        payload = (root / task.get("path", "")).resolve()
        retrieved = contract._clock(task["retrieved_at"])
        if (receipt.get("symbol") != symbol or payload != root / "actions" / symbol / (str(year) + ".csv")
                or task.get("source_error_code") != "0"
                or not contract._clock(day + "T15:00:00+08:00") <= retrieved <= now):
            raise ValueError("A successful exact operate-year post-close query is required for every scoped symbol")
        payload_ref, receipt_ref = _ref(payload), _ref(receipt_path)
        if payload_ref["sha256"] != task.get("sha256"):
            raise ValueError("Completed provider action bytes changed")
        frame = pd.read_csv(payload, dtype=str, keep_default_na=False)
        if (not set(FIELDS) <= set(frame) or len(frame) != task.get("quality", {}).get("rows")
                or not frame.symbol.eq(symbol).all()
                or not frame.code.eq(normalize_symbol(symbol).baostock_code).all()):
            raise ValueError("Completed provider action identity/count/schema changed")
        for field in contract.DATES:
            for value in frame[field]:
                if value:
                    contract._day(value)
        rows.extend(frame.to_dict("records"))
        refs.extend((payload_ref, receipt_ref))
        queries.append({"symbol": symbol, "year": year, "receipt": receipt_ref, "payload": payload_ref})
    if len(rows) != status.get("action_rows"):
        raise ValueError("Completed provider action row count changed")
    return {"scope_symbols": scope, "provider_files": files, "queries": queries, "rows": rows}, None


def _reviews(prior, reviews, now, refs):
    pairs = {}
    evidence = deepcopy(prior.get("review_evidence", [])) if prior else []
    for reference in evidence:
        contract._pin(reference, refs)
    if prior:
        references = {(normalize_symbol(row["code"]).vt_symbol, row["dividOperateDate"]): row
            for row in prior["reference_evidence"]}
        if len(references) != len(prior["reference_evidence"]):
            raise ValueError("Prior reference evidence contains duplicate action identities")
        for action in prior["actions"]:
            key = (action["symbol"], action["dividOperateDate"])
            if key in pairs or key not in references:
                raise ValueError("Prior canonical action/review identities changed")
            pairs[key] = (deepcopy(action), deepcopy(references[key]))
    for item in reviews:
        if isinstance(item, (str, Path)):
            path = Path(item).resolve()
            value = _read(path)
            reference = _ref(path)
            refs.append(reference)
            if reference not in evidence:
                evidence.append(reference)
            for reference in value.get("rendered_pages", []):
                contract._pin(reference, refs)
                if reference not in evidence:
                    evidence.append(deepcopy(reference))
        elif isinstance(item, dict):
            value = deepcopy(item)
        else:
            raise ValueError("An explicit saved action/reference review is required")
        action, reference = value.get("action"), value.get("reference")
        if not isinstance(action, dict) or not action.get("symbol") or not action.get("dividOperateDate"):
            continue  # Its corresponding provider candidate remains pending.
        reference = reference if isinstance(reference, dict) else {}
        key = (action["symbol"], contract._day(action["dividOperateDate"]))
        if key in pairs:
            old_action, old_reference = pairs[key]
            if not all(action.get(field) not in (None, "") for field in contract.TERMS) or not all(
                    reference.get(field) not in (None, "") for field in (*contract.TERMS,
                        "reference_basis", "reference_adjustment_cash_ps", "reference_adjustment_share_ratio")):
                continue
            if (contract._same_economics(old_action) != contract._same_economics(action)
                    or contract._same_economics(old_reference) != contract._same_economics(reference)
                    or old_reference.get("reference_basis") != reference.get("reference_basis")
                    or any(contract._number(old_reference[key]) != contract._number(reference[key])
                        for key in ("reference_adjustment_cash_ps", "reference_adjustment_share_ratio"))):
                raise ValueError("A supplied review revises an already saved economic item")
            # Keep the original actual review clock and PDF, rather than
            # restamping identical facts when a later bundle is assembled.
            continue
        pairs[key] = (deepcopy(action), deepcopy(reference))
    return pairs, evidence


def _reviewed(row, pair, now, refs):
    action, reference = deepcopy(pair[0]), deepcopy(pair[1])
    official = ("source_file", "source_sha256", "source_url", "source_date", "reviewed_at", "review_note")
    if (any(action.get(key) in (None, "") for key in ("code", "symbol", "terms_basis", *contract.TERMS,
                "dividRegistDate", "dividOperateDate", "dividPayDate", *official))
            or any(reference.get(key) in (None, "") for key in ("code", "dividOperateDate", *contract.TERMS,
                "reference_basis", "reference_adjustment_cash_ps", "reference_adjustment_share_ratio", *official))
            or action.get("review_status") != "manual_fulltext_and_pages_reviewed"
            or reference.get("review_status") != "manual_fulltext_and_pages_reviewed"):
        return None, "official_complete_terms_or_independent_reference_review_incomplete"
    if (action.get("terms_basis") != "issuer_confirmed_cash_only_terms"
            or contract._number(action["dividStocksPs"]) != 0
            or contract._number(action["dividReserveToStockPs"]) != 0):
        return None, "outside_supported_reviewed_cash_only_scope"
    contract._number(action["dividCashPsBeforeTax"])
    if (action["symbol"] != row["symbol"] or action["code"] != row["code"]
            or normalize_symbol(reference["code"]).vt_symbol != row["symbol"]
            or reference["dividOperateDate"] != action["dividOperateDate"]):
        raise ValueError("Official review changed the provider action identity")
    for key in contract.DATES:
        if not row.get(key) and key != "dividStockMarketDate":
            return None, "provider_date_unknown_" + key
        if action.get(key) != "" or key != "dividStockMarketDate":
            contract._day(action.get(key))
        if row.get(key, "") != action.get(key, ""):
            raise ValueError("Reviewed action dates conflict with original provider facts")
    if action["dividRegistDate"] > action["dividOperateDate"] or action["dividPayDate"] < action["dividOperateDate"]:
        raise ValueError("Reviewed cash registration/ex/payment chronology conflicts")
    for key in contract.TERMS:
        if row.get(key, "") == "" and key == "dividReserveToStockPs":
            continue  # An explicit complete cash-only review can supply this exact zero.
        if row.get(key, "") == "":
            return None, "provider_term_unknown_" + key
        if contract._number(row[key]) != contract._number(action[key]):
            raise ValueError("Reviewed action terms conflict with original provider facts")
    if (reference.get("reference_basis") != "issuer_confirmed_standard_cash_only_reference"
            or contract._number(reference["reference_adjustment_cash_ps"]) != contract._number(action["dividCashPsBeforeTax"])
            or contract._number(reference["reference_adjustment_share_ratio"]) != 0
            or any(contract._number(reference[key]) != contract._number(action[key]) for key in contract.TERMS)):
        return None, "independent_stated_cash_only_reference_missing"
    contract._official(action, now, refs)
    contract._official(reference, now, refs)
    return (action, reference), None


def build(provider_root, base_event_view, base_reference_view, calendar_file, out,
          *, day, prior_bundle=None, reviews=()):
    """Build one immutable bundle, or save explicit pending/failure diagnostics.

    ``reviews`` contains saved JSON paths or explicit ``{action, reference}``
    records. Only existing complete cash-only terms are supported. This does
    not call ``load_extension``; the account consumer still validates it once.
    """
    root, event, reference, calendar, out = [Path(value).resolve() for value in
        (provider_root, base_event_view, base_reference_view, calendar_file, out)]
    if out.exists():
        raise FileExistsError("A bundle build requires an explicitly new output directory")
    protected = {root, event, reference, calendar.parent}
    refs, prior = [], None
    if prior_bundle is not None:
        prior_bundle = Path(prior_bundle).resolve()
        prior = _read(prior_bundle)
        inspected = contract.extension_readiness(prior_bundle, event, reference, calendar,
            through=prior["window_end"])
        protected.update(map(Path, inspected["input_directories"]))
        refs.extend(inspected["refs"])
    for item in reviews:
        if isinstance(item, (str, Path)):
            protected.add(Path(item).resolve().parent)
            value = _read(item)
        else:
            value = item
        for record in (value.get("action", {}), value.get("reference", {})):
            if record.get("source_file"):
                protected.add(Path(record["source_file"]).resolve().parent)
    if any(out == path or out in path.parents or path in out.parents for path in protected):
        raise ValueError("Bundle output must remain outside all provider, historical, calendar and review packages")
    now = datetime.now(MARKET_TZ)
    result = {"schema": SCHEMA, "day": day, "created_at": now.isoformat(), "status": "failed",
        "bundle": None, "pending_review": [], "failures": [], "input_refs": [], **FLAGS}
    bundle = None
    try:
        day = contract._day(day)
        if now < contract._clock(day + "T15:00:00+08:00"):
            raise ValueError("A current scoped action bundle needs an actually completed source day")
        events, references, fixed = _read(event / "result.json"), _read(reference / "result.json"), _read(calendar)
        refs.extend(_ref(path) for path in (event / "result.json", reference / "result.json", calendar))
        if (events.get("status") != "research_action_events_projected"
                or references.get("status") != "research_reference_presence_diagnostic"
                or Path(references["event_view"]).resolve() != event
                or events.get("execution_admitted") is not False or references.get("execution_admitted") is not False):
            raise ValueError("The exact frozen historical action/reference pair is required")
        sessions, previous, mode = _calendar_view(fixed, events["end"], day, [])
        sessions = [value for value in sessions if events["end"] < value <= day]
        if (mode != "pinned_static_exchange_calendar" or not sessions or sessions[-1] != day
                or previous[sessions[0]] != events["end"] or sessions[0][:4] != day[:4]):
            raise ValueError("One exact adjacent current-year action window is required")
        start = sessions[0]
        base_calendar = event / "source_input_calendar.csv"
        if _ref(base_calendar)["sha256"] != events["files"][base_calendar.name]:
            raise ValueError("Frozen preceding action calendar changed")
        old_days = pd.read_csv(base_calendar, dtype=str, keep_default_na=False).date.tolist()
        if not old_days or old_days[-1] != events["end"]:
            raise ValueError("Frozen action source does not end at its exact cutoff")
        refs.append(_ref(base_calendar))
        provider, pending_source = _provider(root, day, start, sessions, now, refs)
        if pending_source is not None:
            result.update(status="pending_source", pending_source=pending_source)
        else:
            scope, rows = provider["scope_symbols"], provider["rows"]
            result.update(scope_symbols=scope, scope_count=len(scope), window_start=start, window_end=day)
            reported_file = event / "input_reported_actions.parquet"
            if _ref(reported_file)["sha256"] != events["files"][reported_file.name]:
                raise ValueError("Frozen provider action prefix changed")
            refs.append(_ref(reported_file))
            old_reported = pd.read_parquet(reported_file, columns=list(FIELDS)).fillna("")
            old_reported = old_reported.loc[old_reported.symbol.isin(scope)].to_dict("records")
            def historical(values):
                return [row for row in values if row.get("dividOperateDate")
                    and row["dividOperateDate"] <= events["end"] and row["dividOperateDate"][:4] == day[:4]]
            if _counter(historical(rows)) != _counter(historical(old_reported)):
                raise ValueError("Fresh provider revised or dropped the frozen economic prefix")
            official_entries, official_by_key = [], {}
            if prior and (prior.get("official_event_supplements") or "official_event_supplement_mode" in prior):
                from .official_action_supplement import supplement_entries, match_provider
                official_entries = supplement_entries(prior, created=now, calendar=fixed, refs=refs,
                    reported_keys=[(a["symbol"], a["dividOperateDate"]) for a in prior["actions"]])
                official_by_key = {(entry["action"]["symbol"], entry["action"]["dividOperateDate"]): entry
                    for entry in official_entries}
                for row in rows:
                    key = row["symbol"], row.get("dividOperateDate", "")
                    if key in official_by_key:
                        match_provider(official_by_key[key]["action"], row)
            if prior:
                if prior["window_start"] != start or prior["window_end"] > day:
                    raise ValueError("Prior bundle cannot change the frozen start or move backwards")
                by_symbol = {query["symbol"]: query for query in prior["queries"]}
                for symbol in set(scope) & set(by_symbol):
                    file = contract._pin(by_symbol[symbol]["payload"], refs)
                    old = pd.read_csv(file, dtype=str, keep_default_na=False).to_dict("records")
                    before = lambda values: [row for row in values if row["symbol"] == symbol
                        and _related(row, start, prior["window_end"])
                        and (row["symbol"], row.get("dividOperateDate", "")) not in official_by_key]
                    if _counter(before(old)) != _counter(before(rows)):
                        raise ValueError("Fresh provider revised or added an already relevant prior-bundle economic item")
            pairs, review_evidence = _reviews(prior, reviews, now, refs)
            candidates, unknown = {}, []
            for row in rows:
                if not _related(row, start, day):
                    continue
                if not row.get("dividOperateDate"):
                    unknown.append({"symbol": row["symbol"], "reason": "relevant_provider_event_has_unknown_ex_date", "provider_row": row})
                    continue
                if row["dividOperateDate"] <= events["end"]:
                    continue
                key = (row["symbol"], row["dividOperateDate"])
                if key in candidates:
                    raise ValueError("Duplicate recent provider action needs explicit reconciliation")
                candidates[key] = row
            actions, evidence = [], []
            for key, row in candidates.items():
                if key in official_by_key:
                    continue  # One official event, not a second provider cash payment.
                if key not in pairs:
                    unknown.append({"symbol": key[0], "ex_date": key[1],
                        "reason": "official_full_terms_and_independent_reference_review_required", "provider_row": row})
                    continue
                checked, reason = _reviewed(row, pairs[key], now, refs)
                if checked is None or checked[0]["dividRegistDate"] < start:
                    unknown.append({"symbol": key[0], "ex_date": key[1],
                        "reason": reason or "registration_precedes_supported_extension_window", "provider_row": row})
                    continue
                actions.append(checked[0])
                evidence.append(checked[1])
            if unknown:
                result.update(status="pending_review", pending_review=unknown)
            else:
                bundle = {"schema": contract.SCHEMA, "created_at": now.isoformat(),
                    "base_event": _ref(event / "result.json"), "base_reference": _ref(reference / "result.json"),
                    "calendar": _ref(calendar), "window_start": start, "window_end": day,
                    "scope_symbols": scope, "provider_root": str(root), "provider_files": provider["provider_files"],
                    "queries": provider["queries"], "actions": actions, "reference_evidence": evidence,
                    "review_evidence": review_evidence, "knowledge_mode": "posthoc_component_diagnostic",
                    "known_at": now.isoformat(), "no_full_market_coverage_claim": True, **FLAGS}
                if official_entries:
                    bundle.update(official_event_supplements=deepcopy(prior["official_event_supplements"]),
                        official_event_supplement_mode=prior["official_event_supplement_mode"],
                        official_event_supplement_parent=_ref(prior_bundle))
                    supplement_entries(bundle, created=now, calendar=fixed, refs=refs,
                        reported_keys=candidates, reported_rows=candidates)
                result.update(status="ready", bundle=str(out / "bundle.json"), action_count=len(actions),
                    requires_load_extension_before_account_use=True,
                    reused_prior_reviews=sum((item["symbol"], item["dividOperateDate"]) in candidates
                        for item in prior["actions"]) if prior else 0)
                if official_entries:
                    result.update(official_event_count=len(official_entries),
                        official_event_supplement_mode=prior["official_event_supplement_mode"],
                        official_reviews_reused_without_new_clock=True)
    except (ValueError, KeyError, TypeError, OSError) as error:
        result.update(status="failed", failures=[f"{type(error).__name__}: {error}"])
        bundle = None
    result["input_refs"] = list({(ref["path"], ref["sha256"]): ref for ref in refs}.values())
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + out.name + ".bundle-", dir=out.parent) as temporary:
        stage = Path(temporary)
        if bundle is not None:
            _write(stage / "bundle.json", bundle)
        _write(stage / "result.json", result)
        stage.rename(out)
    return result
