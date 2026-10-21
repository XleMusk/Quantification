"""Prepare scoped current-operate-year action facts from saved research inputs.

Preparation is local and preserves cached bytes and acquisition clocks.
Collection is an explicit separate call, with at most one SDK session and no
automatic retry. Neither call creates a trading account or interprets terms.
"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..market_dataset import collect, summarize, write_frame
from ..symbols import normalize_symbol
from .daily_source_probe import WHEEL_SHA256, select_sdk
from .leader_daily_signals import _calendar_view
from .native_lowvol_publication import METHOD_ID, load_publication
from .shortline_native_preopen_forward import _source


SCHEMA = "recent_action_sources_prepared_v1"
RESULT_SCHEMA = "recent_action_sources_collection_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "pit_verified": False,
    "coverage_verified": False, "returns_reportable": False,
    "broker_interacted": False, "account_mutated": False, "automatic_retry": False}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _checked(ref):
    if _ref(ref["path"]) != ref:
        raise ValueError("Prepared recent action input changed")
    return Path(ref["path"])


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _clock(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Actual action acquisition clock needs an offset")
    return result.astimezone(MARKET_TZ)


def _symbol(value):
    if normalize_symbol(value).vt_symbol != value:
        raise ValueError("Saved research exposure must use exact canonical symbols")
    return value


def _positive(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        return result > 0
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Known nonnegative saved research exposure is required") from error


def _head_scope(path, day):
    value = _read(path)
    if (value.get("schema") != "native_lowvol_preopen_checkpoint_v1"
            or value.get("method_id") != METHOD_ID
            or value.get("execution_admitted") is not False
            or RunContext.from_dict(value["context"]).mode != TradingMode.REPLAY):
        raise ValueError("An explicit saved native low-volatility REPLAY head is required")
    state = value["state"]
    if state.get("broker_binding") or not state.get("day") or state["day"] > day:
        raise ValueError("Saved action scope cannot use a broker or future account")
    found = set(value.get("action_scope", []))
    found.update(s for s, lots in state["positions"].items()
        if _positive(sum(lot["qty"] for lot in lots)))
    found.update(s for s, weight in (state.get("pending_targets") or {}).items() if _positive(weight))
    found.update((state.get("full_exit_lifecycle") or {}).get("remaining_full_exits", {}))
    for field in ("entitlements", "receivables"):
        for identifier, amount in state.get(field, {}).items():
            if _positive(amount):
                parts = identifier.split(":")
                found.add(parts[1] if parts[0] == "rights" else parts[0])
    return {_symbol(s) for s in found}, {"kind": "native_lowvol_research_head",
        **_ref(path), "account_day": state["day"], "scope_count": len(found)}


def _overlap(out, inputs):
    for folder in inputs:
        folder = Path(folder).resolve()
        if out == folder or out in folder.parents or folder in out.parents:
            raise ValueError("New scoped action output must remain separate from all saved inputs")


def _cached(root, symbol, year, close, now):
    receipt_path = root / "receipts" / f"{symbol}.json"
    if not receipt_path.exists():
        return None
    receipt = _read(receipt_path)
    task = receipt.get("tasks", {}).get(f"actions_{year}")
    if task is None:
        return None
    if receipt.get("symbol") != symbol:
        raise ValueError("Cached current-year action receipt symbol changed")
    if task.get("source_error_code") != "0":
        return None
    acquired = _clock(task["retrieved_at"])
    if acquired > now:
        raise ValueError("Cached action receipt has a future acquisition clock")
    if acquired < close:
        return None
    relative = f"actions/{symbol}/{year}.csv"
    payload = root / relative
    if (task.get("path") != relative or not payload.exists()
            or _ref(payload)["sha256"] != task.get("sha256")):
        raise ValueError("Completed cached action payload changed; do not retry it")
    sdk = task.get("source_sdk") or {}
    if sdk.get("distribution_version") != "0.9.4" or sdk.get("sha256") != WHEEL_SHA256:
        raise ValueError("Cached action task does not identify the reviewed SDK 0.9.4")
    rows = pd.read_csv(payload, dtype=str, keep_default_na=False)
    if (not {"symbol", "code"} <= set(rows) or len(rows) != task.get("quality", {}).get("rows")
            or not rows.symbol.eq(symbol).all()
            or not rows.code.eq(normalize_symbol(symbol).baostock_code).all()):
        raise ValueError("Cached action response count or identity changed")
    return {"symbol": symbol, "year": year, "origin_root": str(root),
        "receipt": _ref(receipt_path), "payload": _ref(payload),
        "retrieved_at": task["retrieved_at"], "task": deepcopy(task)}


def _prior_failed(root, day, year, pending):
    """Only explicit reuse roots are inspected; no global history scan."""
    plan_path = root / "collection_plan.json"
    if not plan_path.exists() or _read(plan_path).get("end") != day:
        return []
    blocked = []
    for path in sorted((root / "collection_runs").glob("*.json")):
        attempt = _read(path)
        query = attempt.get("failed_query") or {}
        if attempt.get("status") == "failed" and query.get("task") == f"actions_{year}":
            symbols = {query.get("symbol")} & pending
        elif attempt.get("status") == "failed" and attempt.get("stage") == "actions" and not query:
            # A login/socket failure before the first query is still a saved
            # failed attempt, not permission to retry under another root.
            symbols = pending
        elif attempt.get("status") in {"running", "started"} and attempt.get("stage") == "actions":
            symbols = pending
        else:
            continue
        blocked.extend({"symbol": s, "year": year, "reason": "saved_same_day_failed_or_unresolved_query_no_retry",
            "attempt": _ref(path), "origin_root": str(root)} for s in sorted(symbols))
    for symbol in sorted(pending):
        receipt_path = root / "receipts" / f"{symbol}.json"
        if receipt_path.exists():
            task = _read(receipt_path).get("tasks", {}).get(f"actions_{year}")
            if task and task.get("source_error_code") != "0":
                blocked.append({"symbol": symbol, "year": year, "reason": "saved_same_day_failed_query_no_retry",
                    "receipt": _ref(receipt_path), "origin_root": str(root)})
    status_path = root / "status.json"
    if status_path.exists() and _read(status_path).get("error") is not None and not blocked:
        blocked.extend({"symbol": symbol, "year": year, "reason": "saved_same_day_unattributed_source_failure_no_retry",
            "status": _ref(status_path), "origin_root": str(root)} for symbol in sorted(pending))
    return blocked


def prepare(day, package, calendar_file, out, *, publications=(), account_heads=(), reuse_roots=()):
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("An exact completed source day is required")
    out, package, calendar_file = [Path(p).resolve() for p in (out, package, calendar_file)]
    if out.exists():
        raise FileExistsError("Keep the original prepared scoped source; use its saved collection result")
    now = _now()
    calendar = _read(calendar_file)
    year = int(day[:4])
    calendar_ref = _ref(calendar_file)
    sessions, _, mode = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    if mode != "pinned_static_exchange_calendar" or day not in sessions:
        raise ValueError("Source day must be an exact session in the fixed current-year calendar")
    source = _source(package, {"file": str(calendar_file), "sha256": calendar_ref["sha256"]}, sessions, now)
    if source["proof"]["day"] != day:
        raise ValueError("Complete raw package is not the requested exact source day")
    scope, scope_sources = set(), []
    input_dirs = [package, calendar_file.parent]
    for publication in publications:
        publication = Path(publication).resolve()
        publication = publication / "publication.json" if publication.is_dir() else publication
        saved = load_publication(publication, now=now)
        if (saved["source_day"] > day or saved["calendar_ref"]["sha256"] != calendar_ref["sha256"]
                or Path(saved["calendar_ref"]["file"]).resolve() != calendar_file):
            raise ValueError("Action scope publication has a future source or different calendar")
        selected = {_symbol(s) for s, weight in (saved.get("weights") or {}).items() if _positive(weight)}
        scope.update(selected)
        scope_sources.append({"kind": "saved_native_lowvol_publication", **_ref(publication),
            "source_day": saved["source_day"], "published_at": saved["published_at"], "scope_count": len(selected)})
        input_dirs.append(publication.parent)
    for head in account_heads:
        head = Path(head).resolve()
        head = head / "head.json" if head.is_dir() else head
        selected, evidence = _head_scope(head, day)
        scope.update(selected)
        scope_sources.append(evidence)
        input_dirs.append(head.parent)
    scope = sorted(scope)
    if not 1 <= len(scope) <= 1000:
        raise ValueError("An explicit nonempty bounded saved research exposure scope is required")
    roots = [Path(r).resolve() for r in reuse_roots]
    if len(roots) != len(set(roots)):
        raise ValueError("Duplicate explicit reuse roots are not permitted")
    input_dirs.extend(roots)
    _overlap(out, input_dirs)
    close = _clock(day + "T15:00:00+08:00")
    chosen = {}
    for symbol in scope:
        candidates = [entry for root in roots if (entry := _cached(root, symbol, year, close, now)) is not None]
        if candidates:
            candidates.sort(key=lambda c: _clock(c["retrieved_at"]), reverse=True)
            latest = candidates[0]
            if any(c["payload"]["sha256"] != latest["payload"]["sha256"]
                   for c in candidates if c["retrieved_at"] == latest["retrieved_at"]):
                raise ValueError("Equal-clock completed cached action responses conflict")
            chosen[symbol] = latest
    pending = set(scope) - set(chosen)
    blocked = [item for root in roots for item in _prior_failed(root, day, year, pending)]
    provider = out / "provider"
    provider.mkdir(parents=True)
    write_frame(provider / "universe.csv", pd.DataFrame([{"code": normalize_symbol(s).baostock_code,
        "symbol": s, "board": "", "status": ""} for s in scope]))
    write_frame(provider / "calendar.csv", pd.DataFrame({"date": [d for d in sessions if d <= day]}))
    plan = {"source": "BaoStock", "start": f"{year}-01-01", "end": day, "fields": BAOSTOCK_FIELDS,
        "summary_stage": "actions", "symbols": len(scope), "expected_queries": len(scope),
        "query_method": "query_dividend_data", "year_type": "operate", "query_year": year,
        "universe_sha256": _ref(provider / "universe.csv")["sha256"],
        "calendar_sha256": _ref(provider / "calendar.csv")["sha256"],
        "limitations": ["Saved research exposure scope, not all-market coverage",
            "Current operate-year responses are post-close facts, not historical PIT or complete economic terms",
            "No closed-year, price, broker, account or automatic-retry operation"]}
    _write(provider / "collection_plan.json", plan)
    reused = []
    for symbol in scope:
        if symbol not in chosen:
            continue
        entry = chosen[symbol]
        _checked(entry["receipt"])
        payload = _checked(entry["payload"])
        target = provider / entry["task"]["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(payload.read_bytes())
        _write(provider / "receipts" / f"{symbol}.json", {"symbol": symbol,
            "tasks": {f"actions_{year}": deepcopy(entry["task"])},
            "reused_from": {k: deepcopy(entry[k]) for k in ("origin_root", "receipt", "payload")}})
        reused.append({k: v for k, v in entry.items() if k != "task"})
    status = summarize(provider)
    manifest = {"schema": SCHEMA, "day": day, "prepared_at": now.isoformat(),
        "scope_symbols": scope, "scope_count": len(scope), "scope_sources": scope_sources,
        "scope_basis": "saved_lowvol_targets_held_pending_full_exit_positive_rights_and_cumulative_action_scope",
        "source_close_proof": deepcopy(source["proof"]), "calendar_ref": calendar_ref,
        "provider_root": str(provider), "provider_files": {n: _ref(provider / n)
            for n in ("collection_plan.json", "universe.csv", "calendar.csv")},
        "reused_queries": reused, "pending_symbols": sorted(pending), "blocked_failed_queries": blocked,
        "reuse_roots": [str(r) for r in roots], "network_accessed": False,
        "prepared_status": "blocked_existing_failed_queries" if blocked else (
            "all_queries_reused" if not pending else "prepared_pending_explicit_collection"),
        "status": "scoped_action_sources_prepared_blocked" if blocked else (
            "scoped_action_sources_prepared_from_cache" if not pending else "scoped_action_sources_prepared_pending_queries"),
        "source_complete": not pending and not blocked,
        "new_query_count": 0, "pending_query_count": len(pending), "reused_query_count": len(reused),
        "provider_status_at_prepare": status["status"], **FLAGS}
    _write(out / "manifest.json", manifest)
    return {**manifest, "manifest_file": str(out / "manifest.json"), "manifest_ref": _ref(out / "manifest.json")}


def collect_prepared(run_dir, wheel=None):
    directory = Path(run_dir).resolve()
    manifest_path = directory / "manifest.json"
    manifest = _read(manifest_path)
    if manifest.get("schema") != SCHEMA or manifest.get("execution_admitted") is not False:
        raise ValueError("An explicit prepared research action source is required")
    for ref in manifest["provider_files"].values():
        _checked(ref)
    provider = Path(manifest["provider_root"]).resolve()
    if provider != directory / "provider":
        raise ValueError("Prepared action provider must remain inside its original run")
    result_path = directory / "collection_result.json"
    if result_path.exists():
        saved = _read(result_path)
        if saved["manifest_ref"] != _ref(manifest_path):
            raise ValueError("Saved scoped collection binding changed")
        for ref in saved.get("output_refs", []):
            _checked(ref)
        return saved
    started = directory / "collection_started.json"
    if started.exists():
        return {"schema": RESULT_SCHEMA, "status": "scoped_action_collection_progress_unknown_no_retry",
            "provider_root": str(provider), "manifest_ref": _ref(manifest_path),
            "scope_symbols": manifest["scope_symbols"], "scope_count": manifest["scope_count"],
            "source_complete": False, "new_query_count": None,
            "pending_query_count": None, "reused_query_count": manifest["reused_query_count"],
            "started_ref": _ref(started), "network_accessed": False, **FLAGS}
    if manifest["blocked_failed_queries"]:
        result = {"schema": RESULT_SCHEMA, "status": "scoped_action_sources_blocked_existing_failed_query",
            "provider_root": str(provider), "manifest_ref": _ref(manifest_path),
            "scope_symbols": manifest["scope_symbols"], "scope_count": manifest["scope_count"],
            "source_complete": False, "new_query_count": 0,
            "pending_query_count": manifest["pending_query_count"], "reused_query_count": manifest["reused_query_count"],
            "blocked_failed_queries": manifest["blocked_failed_queries"], "network_accessed": False, **FLAGS}
        _write(result_path, result)
        return result
    _write(started, {"schema": "scoped_action_collection_started_v1", "started_at": _now().isoformat(),
        "manifest_ref": _ref(manifest_path), "pending_symbols": manifest["pending_symbols"], **FLAGS})
    accessed = bool(manifest["pending_symbols"])
    try:
        if accessed:
            sdk = select_sdk(wheel)
            status = collect(provider, max_symbols=len(manifest["scope_symbols"]), stage="actions",
                timeout_seconds=30, summary_every=8, source_sdk=sdk)
        else:
            status = summarize(provider)
        complete = status["status"] == "source_cache_complete" and status.get("error") is None
        refs = [_ref(provider / "status.json")]
        for symbol in manifest["scope_symbols"]:
            receipt = provider / "receipts" / f"{symbol}.json"
            if receipt.exists():
                refs.append(_ref(receipt))
                task = _read(receipt).get("tasks", {}).get(f"actions_{manifest['day'][:4]}")
                if task and (provider / task["path"]).exists():
                    refs.append(_ref(provider / task["path"]))
        result = {"schema": RESULT_SCHEMA, "status": "scoped_action_sources_complete" if complete else "scoped_action_sources_incomplete",
            "day": manifest["day"], "provider_root": str(provider), "provider": status,
            "manifest_ref": _ref(manifest_path), "scope_symbols": manifest["scope_symbols"],
            "scope_count": manifest["scope_count"], "source_complete": complete,
            "reused_query_count": manifest["reused_query_count"],
            "new_query_count": max(0, status["action_queries"] - manifest["reused_query_count"]),
            "pending_query_count": len(manifest["scope_symbols"]) - status["action_queries"],
            "output_refs": refs, "network_accessed": accessed, "completed_at": _now().isoformat(), **FLAGS}
    except Exception as error:
        result = {"schema": RESULT_SCHEMA, "status": "scoped_action_collection_failed_no_retry",
            "day": manifest["day"], "provider_root": str(provider), "manifest_ref": _ref(manifest_path),
            "error": f"{type(error).__name__}: {error}", "network_accessed": accessed,
            "scope_symbols": manifest["scope_symbols"], "scope_count": manifest["scope_count"],
            "source_complete": False, "new_query_count": None, "pending_query_count": None,
            "reused_query_count": manifest["reused_query_count"],
            "completed_at": _now().isoformat(), **FLAGS}
    _write(result_path, result)
    return result
