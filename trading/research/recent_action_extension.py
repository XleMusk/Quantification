"""Explicit, scoped posthoc action extension; never replaces native quotes.

The frozen historical pair remains the source of its complete prefix.  Fresh
operate-year responses establish only queried scope/time coverage; a new event
also needs reviewed cash-only terms and an independent stated reference basis.
"""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..symbols import normalize_symbol
from .bulk_feed import AMOUNT_FIELDS
from .leader_daily_signals import _calendar_view
from .stockdb_action_accounting import StockDBActionAccounting, load_action_accounting


SCHEMA = "recent_scoped_action_extension_v1"
METHOD = "scoped_recent_cash_only_action_extension_independent_native_raw_v1"
DATES = ("dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate")
TERMS = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Exact recent-action date required")
    return value


def _clock(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Actual timezone-aware action evidence clock required")
    return result


def _number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Explicit finite nonnegative action term required") from error


def _pin(ref, refs):
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise ValueError("Explicit immutable recent-action file reference required")
    path = Path(ref["path"]).resolve()
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != ref["sha256"]:
        raise ValueError(f"Recent-action source hash changed: {path.name}")
    item = {"path": str(path), "sha256": digest}
    if item not in refs:
        refs.append(item)
    return path


def _same_economics(row):
    # Provider number formatting is not an economic revision; unknown stays
    # distinct from an explicit zero and is never filled from the other row.
    return tuple(str(_number(row.get(k)).normalize()) if row.get(k, "") != "" and k in TERMS
                 else str(row.get(k, "")) for k in DATES + TERMS)


def _official(item, created, refs):
    if (item.get("review_status") != "manual_fulltext_and_pages_reviewed"
            or not item.get("review_note") or not str(item.get("source_url", "")).startswith("https://")
            or _clock(item["reviewed_at"]) > created):
        raise ValueError("Explicit actual official full-document review required")
    _day(item["source_date"])
    path = _pin({"path": item["source_file"], "sha256": item["source_sha256"]}, refs)
    if not path.read_bytes().startswith(b"%PDF-"):
        raise ValueError("Reviewed action primary document must retain PDF bytes")


def extension_readiness(path, base_event_view, base_reference_view, calendar_file, *, through=None):
    """Inspect the exact date contract and input locations, without an overlay.

    This pins the bundle's declared input files, without rebuilding the old
    accounting or interpreting any query/action terms.
    ``ready_for_day`` means the saved window claims the exact requested date;
    the consumer must still call :func:`load_extension` before any Engine work.
    """
    path = Path(path).resolve()
    value, verified_refs = _json(path), []
    verified_refs.append({"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()})
    if (value.get("schema") != SCHEMA or any(value.get(k) is not False for k in
            ("execution_admitted", "selection_enabled", "strategy_validated", "pit_verified", "returns_reportable"))):
        raise ValueError("Explicit unadmitted posthoc recent-action extension required")
    event, reference, calendar_path = [Path(p).resolve() for p in
        (base_event_view, base_reference_view, calendar_file)]
    if (_pin(value["base_event"], verified_refs) != event / "result.json"
            or _pin(value["base_reference"], verified_refs) != reference / "result.json"
            or _pin(value["calendar"], verified_refs) != calendar_path):
        raise ValueError("Recent extension readiness changed the exact base pair/calendar")
    events, references, calendar = _json(event / "result.json"), _json(reference / "result.json"), _json(calendar_path)
    if (events.get("status") != "research_action_events_projected"
            or references.get("status") != "research_reference_presence_diagnostic"
            or Path(references["event_view"]).resolve() != event
            or events.get("execution_admitted") is not False or references.get("execution_admitted") is not False):
        raise ValueError("Pinned unadmitted historical pair required for extension readiness")
    start, end = _day(value["window_start"]), _day(value["window_end"])
    requested = _day(through) if through is not None else end
    created = _clock(value["created_at"])
    if start <= events["end"] or start > end or created < _clock(end + "T15:00:00+08:00"):
        raise ValueError("Recent extension readiness requires actual post-close evidence")
    sessions, previous, mode = _calendar_view(calendar, start, max(end, requested), [])
    if (mode != "pinned_static_exchange_calendar" or start not in sessions or end not in sessions
            or requested not in sessions or previous[start] != events["end"]):
        raise ValueError("Recent extension readiness requires exact adjacent fixed-calendar sessions")
    scope = value["scope_symbols"]
    if (not scope or len(scope) != len(set(scope))
            or any(normalize_symbol(s).vt_symbol != s for s in scope)):
        raise ValueError("Explicit unique recent-action extension scope required")
    manifest_refs = list(verified_refs)
    declared_refs = list(verified_refs)
    directories = {path.parent, event, reference, calendar_path.parent, Path(value["provider_root"]).resolve()}

    def declare(ref):
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"} or len(ref["sha256"]) != 64:
            raise ValueError("Complete immutable indirect recent-action reference required")
        normalized = {"path": str(Path(ref["path"]).resolve()), "sha256": ref["sha256"]}
        if normalized not in declared_refs:
            _pin(ref, verified_refs)
            declared_refs.append(normalized)
        directories.add(Path(normalized["path"]).parent)

    for ref in value["provider_files"].values():
        declare(ref)
    for query in value["queries"]:
        declare(query["receipt"])
        declare(query["payload"])
    for item in value["actions"] + value["reference_evidence"]:
        for field in ("source_file", "reference_source_file", "holder_source_file", "issuer_page_file"):
            if item.get(field):
                declare({"path": item[field], "sha256": item[field.replace("_file", "_sha256")]})
    for ref in value.get("review_evidence", []):
        declare(ref)
    if value.get("official_event_supplements") or "official_event_supplement_mode" in value:
        from .official_action_supplement import supplement_entries
        if value.get("official_event_supplement_parent"):
            declare(value["official_event_supplement_parent"])
        official_refs = []
        supplement_entries(value, created=created, calendar=calendar, refs=official_refs,
            reported_keys=[(a["symbol"], a["dividOperateDate"]) for a in value["actions"]])
        for ref in official_refs:
            declare(ref)
    missing = [day for day in sessions if end < day <= requested]
    return {"schema": "recent_scoped_action_extension_readiness_v1",
        "method": METHOD, "window_start": start, "window_end": end,
        "requested_day": requested, "ready_for_day": start <= requested <= end,
        "missing_action_sessions": missing,
        "status": "saved_window_covers_requested_day_pending_full_load" if start <= requested <= end
            else "waiting_for_fresh_scoped_action_extension",
        "scope_symbols": list(scope), "scope_count": len(scope),
        "extension_file": str(path), "extension_sha256": verified_refs[0]["sha256"],
        "refs": declared_refs, "verified_small_manifest_refs": manifest_refs,
        "verified_input_refs": verified_refs,
        "input_directories": sorted(str(folder) for folder in directories),
        "full_accounting_validation_performed": False, "engine_called": False,
        "source_empty_inferred": False, "coverage_verified": False,
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
        "pit_verified": False, "returns_reportable": False}


class RecentActionAccounting(StockDBActionAccounting):
    def __init__(self, *args, extension_scope, extension_start, official_entries=(),
                 official_knowledge_mode=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.extension_scope = set(extension_scope)
        self.extension_start = extension_start
        self.official_entries = deepcopy(official_entries)
        self.official_knowledge_mode = official_knowledge_mode

    def validate(self, state, day, rows):
        if day >= self.extension_start:
            held = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
            pending = {s for s, weight in (state.get("pending_targets") or {}).items() if weight > 0}
            if not (held | pending) <= self.extension_scope:
                raise ValueError("Recent action exposure outside freshly queried scoped extension")
            if self.official_knowledge_mode == "prospective_actual_knowledge":
                for entry in self.official_entries:
                    action = entry["action"]
                    if (action["symbol"] in held | pending
                            and action["dividRegistDate"] <= day < entry["first_knowledge_eligible_session"]):
                        raise ValueError("Official supplement was not actually available before this session opening")
        return super().validate(state, day, rows)


def load_extension(base_event_view, base_reference_view, extension_file, *,
                   symbols, raw_days, replay_days, price_source="independent_baostock_raw"):
    """Load an opt-in immutable extension without scanning any price history.

    ``symbols`` may retain the original full strategy cohort.  Only held or
    positive pending exposure inside the explicitly queried recent scope can
    pass validation after the old pair's cutoff.  Return type and Engine hooks
    are the existing StockDBActionAccounting contract.
    """
    if price_source != "independent_baostock_raw":
        raise ValueError("Recent extension requires the independent whole-row native quote stream")
    event, reference = Path(base_event_view).resolve(), Path(base_reference_view).resolve()
    path = Path(extension_file).resolve()
    value, refs = _json(path), []
    refs.append({"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()})
    if (value.get("schema") != SCHEMA or any(value.get(k) is not False for k in
            ("execution_admitted", "selection_enabled", "strategy_validated", "pit_verified", "returns_reportable"))):
        raise ValueError("Explicit unadmitted posthoc recent-action extension required")
    if _pin(value["base_event"], refs) != event / "result.json" or _pin(value["base_reference"], refs) != reference / "result.json":
        raise ValueError("Recent action extension changed its frozen historical pair")
    events = _json(event / "result.json")
    start, end = _day(value["window_start"]), _day(value["window_end"])
    created = _clock(value["created_at"])
    close_clock = _clock(end + "T15:00:00+08:00")
    if start <= events["end"] or start > end or created < close_clock:
        raise ValueError("Recent action extension must follow the frozen cutoff after actual close")
    symbols, raw_days, replay_days = list(symbols), list(raw_days), list(replay_days)
    scope = value["scope_symbols"]
    if (not scope or len(set(scope)) != len(scope) or not set(scope) <= set(symbols)
            or any(normalize_symbol(s).vt_symbol != s for s in scope)):
        raise ValueError("Explicit unique recent-action scope must remain inside original cohort")
    if (not symbols or len(set(symbols)) != len(symbols)
            or any(normalize_symbol(s).vt_symbol != s for s in symbols)):
        raise ValueError("Exact original strategy symbol scope required")
    for days in (raw_days, replay_days):
        if not days or days != sorted(set(days)) or any(_day(d) != d for d in days):
            raise ValueError("Exact ordered action extension sessions required")
    calendar = _json(_pin(value["calendar"], refs))
    sessions, previous, mode = _calendar_view(calendar, start, end, [])
    if mode != "pinned_static_exchange_calendar" or not sessions or sessions[0] != start or sessions[-1] != end:
        raise ValueError("Complete pinned exchange calendar window required")
    old_calendar_path = event / "source_input_calendar.csv"
    if sha256(old_calendar_path.read_bytes()).hexdigest() != events["files"][old_calendar_path.name]:
        raise ValueError("Historical action source calendar changed")
    old_days = pd.read_csv(old_calendar_path, dtype=str, keep_default_na=False).date.tolist()
    if len(old_days) < 2 or old_days[-1] != events["end"] or previous[start] != old_days[-1]:
        raise ValueError("Recent action extension lacks the exact frozen preceding session")
    combined = old_days + sessions
    if (not set(replay_days) <= set(raw_days) or not set(raw_days) <= set(combined)
            or raw_days != combined[combined.index(raw_days[0]):combined.index(raw_days[-1]) + 1]
            or replay_days != raw_days[1:] or replay_days[-1] > end):
        raise ValueError("Recent action replay requires its exact contiguous raw/session prefix")
    base = load_action_accounting(event, reference, symbols=symbols,
        raw_days=old_days[-2:], replay_days=[old_days[-1]], price_source=price_source)
    refs = deepcopy(base.provenance["refs"]) + refs
    root = Path(value["provider_root"]).resolve()
    provider_files = value["provider_files"]
    for name in ("collection_plan.json", "universe.csv", "calendar.csv", "status.json"):
        if _pin(provider_files[name], refs) != root / name:
            raise ValueError("Recent provider evidence escapes its declared immutable source")
    plan, status = _json(root / "collection_plan.json"), _json(root / "status.json")
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False)
    provider_calendar = pd.read_csv(root / "calendar.csv", dtype=str, keep_default_na=False).date.tolist()
    if (plan.get("summary_stage") != "actions" or plan.get("end") != end
            or plan.get("source") != "BaoStock" or int(plan.get("symbols", -1)) != len(scope)
            or int(plan.get("expected_queries", -1)) != len(scope)
            or status.get("status") != "source_cache_complete" or status.get("error") is not None
            or status.get("summary_stage") != "actions" or status.get("end") != end
            or int(status.get("complete_symbols", -1)) != len(scope)
            or int(status.get("action_queries", -1)) != len(scope)
            or set(universe.symbol) != set(scope) or len(universe) != len(scope)
            or not universe.symbol.eq(universe.code.map(lambda c: normalize_symbol(c).vt_symbol)).all()
            or [d for d in provider_calendar if start <= d <= end] != sessions
            or plan.get("universe_sha256") != provider_files["universe.csv"]["sha256"]
            or plan.get("calendar_sha256") != provider_files["calendar.csv"]["sha256"]):
        raise ValueError("Fresh scoped action provider coverage is incomplete or changed")
    queries = value["queries"]
    if len(queries) != len(scope) or {q["symbol"] for q in queries} != set(scope):
        raise ValueError("One fresh operate-year query per scoped symbol is required")
    fresh, query_times = [], []
    for query in queries:
        symbol, year = query["symbol"], query["year"]
        if type(year) is not int or year != int(end[:4]) or int(start[:4]) != year:
            raise ValueError("Exact single current operate-year extension required")
        receipt_path, payload = _pin(query["receipt"], refs), _pin(query["payload"], refs)
        receipt = _json(receipt_path)
        task = receipt.get("tasks", {}).get(f"actions_{year}", {})
        retrieved = _clock(task["retrieved_at"])
        if (receipt_path != root / "receipts" / f"{symbol}.json" or receipt.get("symbol") != symbol
                or payload != root / "actions" / symbol / f"{year}.csv"
                or (root / task.get("path", "")).resolve() != payload
                or task.get("sha256") != query["payload"]["sha256"]
                or task.get("source_error_code") != "0" or not close_clock <= retrieved <= created):
            raise ValueError("Fresh post-close successful scoped action receipt required")
        rows = pd.read_csv(payload, dtype=str, keep_default_na=False)
        if (len(rows) != task.get("quality", {}).get("rows") or not {"code", "symbol", *DATES, *TERMS} <= set(rows)
                or not rows.symbol.eq(symbol).all()
                or not rows.code.eq(normalize_symbol(symbol).baostock_code).all()):
            raise ValueError("Fresh action raw rows/count/identity changed")
        fresh.extend(rows.to_dict("records"))
        query_times.append(task["retrieved_at"])
    if len(fresh) != status.get("action_rows"):
        raise ValueError("Fresh action status row count differs from immutable responses")
    # Compare old provider terms to old provider terms, not to officially
    # completed canonical values (which legitimately fill original unknowns).
    old_reported_path = event / "input_reported_actions.parquet"
    if sha256(old_reported_path.read_bytes()).hexdigest() != events["files"][old_reported_path.name]:
        raise ValueError("Original provider action prefix changed")
    refs.append({"path": str(old_reported_path), "sha256": events["files"][old_reported_path.name]})
    old_reported = pd.read_parquet(old_reported_path, columns=["symbol", "code", *DATES, *TERMS]).fillna("")
    old_reported = old_reported.loc[old_reported.symbol.isin(scope)]
    def prior(rows):
        return Counter((r["symbol"], _same_economics(r)) for r in rows
            if r.get("dividOperateDate", "") and r["dividOperateDate"] <= events["end"]
            and r["dividOperateDate"][:4] == end[:4])
    if prior(old_reported.to_dict("records")) != prior(fresh):
        raise ValueError("Fresh provider economically revised or dropped an already consumed action prefix")
    reported = {}
    for row in fresh:
        relevant = any(start <= row.get(k, "") <= end for k in DATES)
        if not relevant:
            continue
        if not row.get("dividOperateDate"):
            raise ValueError("Recent registration/payment/listing has unknown ex-date")
        _day(row["dividOperateDate"])
        if row["dividOperateDate"] <= events["end"]:
            continue
        key = (row["symbol"], row["dividOperateDate"])
        if key in reported:
            raise ValueError("New duplicated provider action requires separate explicit reconciliation")
        reported[key] = row
    old_keys = {(a["symbol"], a["dividOperateDate"]) for a in base.actions}
    official_entries = []
    if value.get("official_event_supplements") or "official_event_supplement_mode" in value:
        from .official_action_supplement import supplement_entries
        if value.get("official_event_supplement_parent"):
            _pin(value["official_event_supplement_parent"], refs)
        official_entries = supplement_entries(value, created=created, calendar=calendar, refs=refs,
            reported_keys=reported, reported_rows=reported, old_keys=old_keys)
    official_keys = {(entry["action"]["symbol"], entry["action"]["dividOperateDate"]) for entry in official_entries}
    additions, evidence = value["actions"], value["reference_evidence"]
    keys = [(a["symbol"], a["dividOperateDate"]) for a in additions]
    ekeys = [(normalize_symbol(a["code"]).vt_symbol, a["dividOperateDate"]) for a in evidence]
    if len(set(keys)) != len(keys) or set(keys) != set(reported) - official_keys or len(set(ekeys)) != len(ekeys) or set(ekeys) != set(keys):
        raise ValueError("Every new registration/ex event requires exactly one explicit reviewed action and reference")
    for action in additions:
        key = (action["symbol"], action["dividOperateDate"])
        raw = reported[key]
        if key in old_keys or action["symbol"] not in scope or normalize_symbol(action["code"]).vt_symbol != action["symbol"]:
            raise ValueError("Extension cannot revise or overlap the frozen canonical action prefix")
        if (action.get("terms_basis") != "issuer_confirmed_cash_only_terms"
                or _number(action["dividStocksPs"]) != 0 or _number(action["dividReserveToStockPs"]) != 0):
            raise ValueError("Only explicitly reviewed ordinary cash-only extensions are supported")
        _number(action["dividCashPsBeforeTax"])
        for field in DATES:
            if action.get(field, "") != "" or field != "dividStockMarketDate":
                _day(action.get(field))
        if not start <= action["dividRegistDate"] <= action["dividOperateDate"] or action["dividPayDate"] < action["dividOperateDate"]:
            raise ValueError("New cash-only registration/ex/payment chronology unsupported or unknown")
        for field in DATES + TERMS:
            if raw.get(field, "") == "" and field == "dividReserveToStockPs":
                continue  # Only the explicitly reviewed full cash-only scheme may complete this unknown.
            if raw.get(field, "") != action.get(field, ""):
                if field not in TERMS or raw.get(field, "") == "" or _number(raw[field]) != _number(action[field]):
                    raise ValueError("Reviewed cash-only action differs from explicit provider facts")
        _official(action, created, refs)
        item = evidence[ekeys.index(key)]
        if (item.get("reference_basis") != "issuer_confirmed_standard_cash_only_reference"
                or _number(item["reference_adjustment_cash_ps"]) != _number(action["dividCashPsBeforeTax"])
                or _number(item["reference_adjustment_share_ratio"]) != 0
                or any(_number(item[k]) != _number(action[k]) for k in TERMS)):
            raise ValueError("Independent stated cash-only reference terms are required, not raw-price agreement")
        _official(item, created, refs)
    combined_actions = deepcopy(base.actions) + deepcopy(additions) + [entry["action"] for entry in official_entries]
    combined_evidence = deepcopy(base.evidence) + deepcopy(evidence) + [entry["reference"] for entry in official_entries]
    provenance = {**deepcopy(base.provenance), "method": METHOD,
        "base_method": base.provenance["method"], "base_event_view": str(event), "base_reference_view": str(reference),
        "extension_file": str(path), "refs": refs, "raw_start": raw_days[0],
        "start": replay_days[0], "end": replay_days[-1], "extension_start": start, "extension_end": end,
        "extension_scope_symbols": list(scope), "extension_scope_count": len(scope),
        "query_retrieved_at": query_times, "extension_action_count": len(additions),
        "old_canonical_prefix_preserved": True, "provider_raw_terms_modified": False,
        "new_terms_method": "issuer_confirmed_cash_only_terms", "source_query_complete": False,
        "scoped_recent_query_complete": True, "full_market_recent_coverage": False,
        "independent_whole_row_price_stream": True, "no_stockdb_fields_replaced": True}
    if official_entries:
        from .official_action_supplement import METHOD as OFFICIAL_METHOD
        provenance.update(official_event_supplement_method=OFFICIAL_METHOD,
            official_event_supplement_mode=value["official_event_supplement_mode"],
            official_event_supplement_count=len(official_entries),
            official_event_knowledge=[{k: v for k, v in entry.items() if k not in {"action", "reference"}}
                for entry in official_entries], official_provider_omission_preserved=True)
    base_universe = pd.DataFrame([{"code": c, "symbol": s} for c, s in base.code_symbols.items()])
    return RecentActionAccounting(combined_actions, base.special_references, combined_evidence,
        base.coverage, base_universe, {"rename": base.rename}, symbols, raw_days, replay_days, provenance,
        extension_scope=scope, extension_start=start, official_entries=official_entries,
        official_knowledge_mode=value.get("official_event_supplement_mode"))
