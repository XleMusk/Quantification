"""Explicit recovery of a stopped multi-year saved-signal research account.

The original whole horizon and account remain frozen. Inspection reads only the
last committed context and first unprocessed session; execution reads the exact
remaining native owners once and uses the same Engine, config and RunContext.
"""
from __future__ import annotations

import ast
import csv
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO, StringIO
import json
import importlib.util
from pathlib import Path
import sys

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..engine import advance
from ..ledger import Ledger
from ..report import metrics
from ..symbols import normalize_symbol
from ..target_exit_lifecycle import FULL_EXIT_LIFECYCLE
from .bulk_feed import AMOUNT_FIELDS
from . import native_lowvol_replay as base


POLICY_ID = "stopped_multiyear_exact_committed_prefix_future_evidence_only_v1"
INSPECTION_SCHEMA = "native_lowvol_stopped_multiyear_inspection_v1"
FLAGS = {"candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False,
    "returns_reportable": False, "total_return_verified": False,
    "model_refit": False, "signal_features_recomputed": False}


def _read(path, *, parse=True):
    path = Path(path).resolve()
    data = path.read_bytes()
    ref = {"path": str(path), "sha256": sha256(data).hexdigest()}
    return (json.loads(data.decode("utf-8-sig")) if parse else data), ref


def _same_pin(ref):
    if base._reference(ref["path"]) != ref:
        raise ValueError("A pinned stopped-account recovery input changed: " + ref["path"])


def _daily(data):
    frame = pd.read_csv(BytesIO(data), dtype={"date": str})
    if "event_time_frontier" not in frame:
        raise ValueError("Saved daily event frontier is missing")
    for column in frame.columns:
        if column == "event_time_frontier":
            frame[column] = frame[column].astype(object)
        for index, value in frame[column].items():
            if column == "event_time_frontier" and pd.isna(value):
                frame.at[index, column] = None
            elif isinstance(value, str) and value.lstrip().startswith(("[", "{", "(")):
                frame.at[index, column] = ast.literal_eval(value)
    rows = frame.to_dict("records")
    if any(not isinstance(row.get("stale_symbols"), list)
            or not isinstance(row.get("events"), list)
            or row["event_time_frontier"] is not None
                and not isinstance(row["event_time_frontier"], dict) for row in rows):
        raise ValueError("Saved daily containers changed")
    return rows


def _positions(state):
    return {s: sum(lot["qty"] for lot in lots) for s, lots in state["positions"].items()
        if sum(lot["qty"] for lot in lots)}


def _economic_prefix(accounting, state, last):
    fields = ("symbol", "code", "dividOperateDate", "dividRegistDate", "dividPayDate",
        "dividStockMarketDate", *AMOUNT_FIELDS)
    symbols = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    symbols.update(symbol for symbol, weight in (state.get("pending_targets") or {}).items() if weight > 0)
    symbols.update(state.get("full_exit_lifecycle", {}).get("remaining_full_exits", {}))
    for name in ("orders", "fills"):
        symbols.update(item["symbol"] for item in state[name].values())
    for name in ("entitlements", "receivables"):
        for identifier, amount in state[name].items():
            if amount:
                parts = identifier.split(":")
                symbols.add(parts[1] if parts[0] == "rights" else parts[0])
    def relevant(action):
        identifier = action["symbol"] + ":" + action["dividOperateDate"]
        return action["symbol"] in symbols and (identifier in state["entitlements"] or identifier in state["receivables"]
            or any(action.get(field) and action[field] <= last for field in
                ("dividOperateDate", "dividRegistDate", "dividPayDate", "dividStockMarketDate")))
    actions = [{field: action.get(field, "") for field in fields}
        for action in accounting.actions if relevant(action)]
    special = [item for item in accounting.special_references
        if item["symbol"] in symbols and item["date"] <= last]
    codes = {normalize_symbol(symbol).baostock_code for symbol in symbols}
    evidence = [item for item in accounting.evidence
        if item["code"] in codes and item["dividOperateDate"] <= last]
    canonical = lambda rows: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return {"actions": canonical(actions), "special_references": canonical(special),
        "evidence": canonical(evidence), "rename": accounting.rename,
        "identity": {code: symbol for code, symbol in accounting.code_symbols.items() if symbol in symbols},
        "coverage": sorted((code, year, known) for (code, year), known in accounting.coverage.items()
            if code in codes and year <= int(last[:4])), "account_related_symbols": sorted(symbols)}


def _raw(registration, views, symbols, wanted):
    old = registration["execution_source"]
    if [str(Path(path).resolve()) for path in views] != [r["source_view"] for r in old["source_views"]]:
        raise ValueError("Original native source ownership cannot change")
    raw, proof = base.load_native_execution_quotes(views, symbols, wanted)
    expected = {row["day"]: row for row in old["day_sources"]}
    if len(expected) != len(old["day_sources"]):
        raise ValueError("Original native source contains duplicate day owners")
    if proof["requested_days"] != wanted or proof["requested_symbols"] != symbols:
        raise ValueError("Exact remaining full-scope native quotes required")
    for current in proof["day_sources"]:
        prior = expected.get(current["day"])
        if prior is None or base._stable_native_proof(current) != base._stable_native_proof(prior):
            raise ValueError("Original exact native day ownership/hash changed: " + current["day"])
    if base._stable_native_proof(proof["source_views"]) != base._stable_native_proof(old["source_views"]):
        raise ValueError("Original native source metadata/hash changed")
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Recovery raw sources overlap")
    return raw, proof


def _instruments(master, symbols):
    rows = pd.read_csv(master, dtype=str, keep_default_na=False)
    instruments = {}
    for row in rows.to_dict("records"):
        if row.get("type") != "1":
            continue
        symbol = normalize_symbol(row["code"]).vt_symbol
        if symbol in symbols:
            value = {key: row.get(key, "") for key in ("ipoDate", "outDate")}
            if symbol in instruments and instruments[symbol] != value:
                raise ValueError("Conflicting original listing metadata")
            instruments[symbol] = value
    if set(instruments) != set(symbols):
        raise ValueError("Original listing metadata no longer covers the whole scope")
    return instruments


@dataclass
class Prepared:
    old_registration: dict
    old_result: dict
    state: dict
    daily: list
    old_csv: bytes
    refs: list
    saved: object
    feed: object
    days: list
    fixed: list
    next_index: int
    inspection: dict
    source_directory: Path
    checks_bytes: bytes | None
    quote_proof: dict
    input_signatures: dict
    engine_source_ref: dict | None


def prepare(saved_run, views, event_view, reference_view, calendar_path, listing_master,
        start, end, *, resume_from, prior_calendars=(), first_session_only=True,
        frozen_engine_source=None):
    """Validate frozen prefix and first readiness without advancing or writing."""
    original = Path(resume_from).resolve()
    original_paths = [original / name for name in ("registration.json", "result.json", "account.json", "daily.csv")]
    original_paths += [original / name for name in ("action_reference_checks.csv", "failed_trial.json")
        if (original / name).exists()]
    input_signatures = {str(path): _signature(path) for path in original_paths}
    loaded = [_read(original / name) for name in ("registration.json", "result.json", "account.json")]
    (registration, result, state), refs = [r[0] for r in loaded], [r[1] for r in loaded]
    old_csv, daily_ref = _read(original / "daily.csv", parse=False)
    refs.append(daily_ref)
    checks_bytes = None
    if (original / "action_reference_checks.csv").exists():
        checks_bytes, ref = _read(original / "action_reference_checks.csv", parse=False)
        refs.append(ref)
    if (original / "failed_trial.json").exists():
        _, ref = _read(original / "failed_trial.json", parse=False)
        refs.append(ref)
    if (registration.get("method_id") != base.MULTIYEAR_METHOD
            or result.get("schema") != base.MULTIYEAR_METHOD or result.get("status") != "incomplete"
            or result.get("registration") != registration or not isinstance(result.get("error"), dict)
            or result.get("failed_trial_committed") is not False or state.get("broker_binding")
            or registration.get("execution_admitted") is not False
            or any(result.get(flag) is not value for flag, value in FLAGS.items())):
        raise ValueError("Only an exact stopped unadmitted multi-year account can be recovered")
    # The CLI dispatcher is explicitly changed by this opt-in recovery method;
    # the registered numerical engine, Ledger and source semantics stay fixed.
    implementation = registration.get("implementation", [])
    numerical_core = {str(Path(base.__file__).resolve().parents[1] / name) for name in ("engine.py", "ledger.py")}
    if not numerical_core <= {ref.get("path") for ref in implementation}:
        raise ValueError("Original Engine and Ledger implementation pins are required")
    engine_path = Path(base.__file__).resolve().parents[1] / "engine.py"
    engine_pin = next(ref for ref in implementation if Path(ref["path"]).resolve() == engine_path)
    engine_source_ref = None
    if frozen_engine_source is not None:
        engine_source_ref = base._reference(frozen_engine_source)
        if engine_source_ref["sha256"] != engine_pin["sha256"]:
            raise ValueError("Frozen Engine bytes must exactly match the original registered Engine SHA256")
        refs.append(engine_source_ref)
    prior_engine_source = registration.get("continuation", {}).get("frozen_engine_source")
    if prior_engine_source is not None and engine_source_ref != prior_engine_source:
        raise ValueError("A continued frozen Engine source cannot change or be disabled")
    for ref in implementation:
        path = Path(ref["path"]).resolve()
        if path != Path(base.__file__).resolve() and not (path == engine_path and engine_source_ref is not None):
            _same_pin(ref)
    for ref in registration.get("continuation", {}).get("implementation", []):
        if Path(ref["path"]).resolve() != Path(base.__file__).resolve():
            _same_pin(ref)
    for ref in registration["signal_source"]["pins"]:
        _same_pin(ref)
    fixed, calendar_proof = base._multiyear_calendar(calendar_path, prior_calendars,
        registration["start"], registration["end"])
    if calendar_proof != registration.get("calendar_method"):
        raise ValueError("Whole multi-year calendar or original year prefix changed")
    calendar_ref, master_ref = base._reference(calendar_path), base._reference(listing_master)
    if calendar_ref != registration["calendar"] or master_ref != registration["listing_master"]:
        raise ValueError("Original current calendar or listing metadata changed")
    refs.extend([calendar_ref, master_ref])
    full_days = [day for day in fixed if registration["start"] <= day <= registration["end"]]
    count, last = result["completed_sessions"], state["day"]
    if (type(count) is not int or not 0 < count < len(full_days)
            or type(result.get("requested_sessions")) is not int or result["requested_sessions"] != len(full_days)
            or last != full_days[count - 1] or start != full_days[count]
            or result["error"].get("day") != start or end != registration["end"]):
        raise ValueError("Recovery must preserve the full horizon and start at the exact next unprocessed session")
    if (registration.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or state.get("full_exit_lifecycle", {}).get("method_id") != FULL_EXIT_LIFECYCLE):
        raise ValueError("Original full-exit lifecycle changed")
    daily = _daily(old_csv)
    context = RunContext(base.MULTIYEAR_METHOD, TradingMode.REPLAY)
    config = base.StrategyConfig(**registration["config"]).validate()
    if ([row["date"] for row in daily] != full_days[:count]
            or round(daily[-1]["equity"] * 100) != Ledger(state, config, context).equity()
            or type(state.get("cash")) is not int
            or Decimal(str(daily[-1]["cash"])) * 100 != state["cash"]
            or any(type(result.get(name)) is not int for name in ("orders", "fills"))
            or result.get("orders") != len(state["orders"]) or result.get("fills") != len(state["fills"])
            or result.get("pending_targets") != state.get("pending_targets")
            or result.get("open_positions") != _positions(state)
            or result.get("remaining_full_exits") != state["full_exit_lifecycle"]["remaining_full_exits"]
            or any(item.get("day", last) > last for name in ("orders", "fills")
                for item in state[name].values())
            or any(item.get("day", last) > last for item in state["events"])):
        raise ValueError("Saved committed account/daily prefix or remaining positions changed")
    days = full_days[count:]
    saved = base._resume_signals(saved_run, registration, days, fixed)
    wanted = [last] + (days[:1] if first_session_only else days)
    raw, quote_proof = _raw(registration, views, saved.symbols, wanted)
    original_raw_days = registration["execution_source"]["requested_days"]
    if original_raw_days != [fixed[fixed.index(full_days[0]) - 1]] + full_days:
        raise ValueError("Original execution source does not match its frozen whole horizon")
    kwargs = dict(symbols=saved.symbols, raw_days=original_raw_days,
        replay_days=full_days, price_source="independent_baostock_raw")
    prior = registration["action_accounting"]
    old_accounting = base.load_action_accounting(prior["event_view"], prior["reference_view"], **kwargs)
    if old_accounting.provenance != prior:
        raise ValueError("Original action/reference evidence changed")
    same_pair = (Path(event_view).resolve() == Path(prior["event_view"]).resolve()
        and Path(reference_view).resolve() == Path(prior["reference_view"]).resolve())
    accounting = old_accounting if same_pair else base.load_action_accounting(event_view, reference_view, **kwargs)
    old_prefix = _economic_prefix(old_accounting, state, last)
    if _economic_prefix(accounting, state, last) != old_prefix:
        raise ValueError("Action/reference economic prefix or registered entitlement changed")
    # Full calendar remains available to all Engine rules. Only the requested
    # predecessor and remaining rows/volumes are loaded into this feed.
    feed = base.NativeLowVolFeed(saved, raw, fixed, _instruments(listing_master, saved.symbols),
        accounting, registration["start"], end, method_id=base.MULTIYEAR_METHOD)
    error = None
    try:
        feed.validate_session(deepcopy(state), start)
    except (ValueError, KeyError, TypeError) as exc:
        error = {"day": start, "type": type(exc).__name__, "message": str(exc)}
    inspection = {"schema": INSPECTION_SCHEMA, "policy_id": POLICY_ID,
        "status": "blocked_stopped_multiyear" if error else "ready_to_resume_stopped_multiyear",
        "inspected_at": datetime.now(MARKET_TZ).isoformat(), "source_directory": str(original),
        "original_error": deepcopy(result["error"]), "error": error,
        "completed_sessions": count, "requested_sessions": len(full_days),
        "remaining_sessions": len(days), "last_committed_session": last, "next_session": start,
        "frozen_end": end, "first_session_readiness_only": True,
        "raw_sessions_read": wanted, "first_session_validation_performed": True,
        "remaining_session_sources_loaded": not first_session_only,
        "saved_full_scope_symbols": len(saved.symbols), "new_state_created": False,
        "orders": len(state["orders"]), "fills": len(state["fills"]),
        "original_numerical_core_pins_verified": True,
        "frozen_engine_source": engine_source_ref,
        "frozen_engine_imported": False,
        "engine_advanced": False, "old_account_mutated": False, "metrics": None,
        "prior_economic_prefix_sha256": sha256(json.dumps(old_prefix, sort_keys=True).encode()).hexdigest(),
        "source_refs": refs, **FLAGS}
    if any(_signature(path) != signature for path, signature in input_signatures.items()):
        raise ValueError("Pinned original stopped-account input changed during preparation")
    return Prepared(registration, result, state, daily, old_csv, refs, saved, feed,
        days, fixed, count, inspection, original, checks_bytes, quote_proof,
        input_signatures, engine_source_ref)


def _no_overlap(out, paths):
    for path in paths:
        path = Path(path).resolve()
        if out == path or out in path.parents or path in out.parents:
            raise ValueError("Recovery output must not overlap an account or input source")


def _signature(path):
    stat = Path(path).stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _unchanged(prepared, refs):
    # Huge original registration/result bytes were hashed when parsed. As in
    # NativeHistorySource, retain those exact-file signatures during this call;
    # changed files stop rather than causing repeated multi-megabyte JSON I/O.
    for ref in refs:
        if ref["path"] in prepared.input_signatures:
            if _signature(ref["path"]) != prepared.input_signatures[ref["path"]]:
                raise ValueError("Pinned original stopped-account input changed during recovery")
        else:
            _same_pin(ref)


def _frozen_advance(ref):
    """Import an exact pinned Engine only after write-free readiness succeeds."""
    _same_pin(ref)
    name = "trading._native_lowvol_frozen_engine_" + ref["sha256"]
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ref["path"])
        if spec is None or spec.loader is None:
            raise ValueError("Frozen Engine source cannot be loaded as a trading package module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            data = Path(ref["path"]).read_bytes()
            if sha256(data).hexdigest() != ref["sha256"]:
                raise ValueError("Frozen Engine source changed before import")
            # Compile the verified bytes directly: no pyc is written beside
            # the immutable research source, even in a non-CLI caller.
            exec(compile(data, ref["path"], "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module.advance


def resume(saved_run, views, event_view, reference_view, calendar_path, listing_master,
        start, end, out, *, resume_from, prior_calendars=(), inspect_only=False,
        frozen_engine_source=None):
    """Recover into a new directory, or return a write-free first-day inspection."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Stopped multi-year recovery requires a new output directory")
    _no_overlap(out, [resume_from, Path(saved_run).resolve().parent, *views,
        event_view, reference_view, calendar_path, listing_master, *prior_calendars,
        *([] if frozen_engine_source is None else [frozen_engine_source])])
    prepared = prepare(saved_run, views, event_view, reference_view, calendar_path, listing_master,
        start, end, resume_from=resume_from, prior_calendars=prior_calendars,
        first_session_only=inspect_only, frozen_engine_source=frozen_engine_source)
    if inspect_only or prepared.inspection["error"] is not None:
        return prepared.inspection
    p, state, new_daily, error = prepared, deepcopy(prepared.state), [], None
    all_input_refs = p.refs + p.old_registration["signal_source"]["pins"]
    all_input_refs += p.feed.action_accounting.provenance["refs"]
    _no_overlap(out, [ref["path"] for ref in all_input_refs])
    _unchanged(p, all_input_refs)
    continuation = {"policy_id": POLICY_ID, "from_directory": str(p.source_directory),
        "input_refs": p.refs, "original_error": p.old_result["error"],
        "new_start": start, "new_end": end, "reused_sessions": p.next_index,
        "global_session_index_start": p.next_index,
        "context": RunContext(base.MULTIYEAR_METHOD, TradingMode.REPLAY).to_dict(),
        "new_state_created": False, "economic_prefix_preserved_through": state["day"],
        "economic_prefix_sha256": p.inspection["prior_economic_prefix_sha256"],
        "original_unprocessed_source_ownership_reused": True,
        "execution_read": {"raw_sessions": p.quote_proof["requested_days"],
            "symbol_count": len(p.saved.symbols), "all_original_owners_matched": True,
            "old_committed_sessions_replayed": False},
        "prior_failed_trial_committed": False,
        "prior_action_reference_checks_preserved": p.checks_bytes is not None,
        "implementation": [base._reference(Path(__file__).resolve()), base._reference(Path(base.__file__).resolve())]}
    if p.engine_source_ref is not None:
        continuation["frozen_engine_source"] = deepcopy(p.engine_source_ref)
    registration = {**deepcopy(p.old_registration), "created_at": datetime.now(MARKET_TZ).isoformat(),
        "action_accounting": p.feed.action_accounting.provenance,
        "continuation_supported": True, "continuation": continuation}
    advance_session = _frozen_advance(p.engine_source_ref) if p.engine_source_ref is not None else advance
    out.mkdir(parents=True, exist_ok=False)
    (out / "registration.json").write_text(json.dumps(registration, ensure_ascii=False, indent=2), encoding="utf-8")
    context = RunContext(base.MULTIYEAR_METHOD, TradingMode.REPLAY)
    for offset, day in enumerate(p.days):
        trial = deepcopy(state)
        try:
            row = advance_session(trial, p.feed, day, p.saved.config, p.next_index + offset,
                context=context, full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
        except Exception as exc:
            error = {"day": day, "type": type(exc).__name__, "message": str(exc)}
            (out / "failed_trial.json").write_text(json.dumps(trial, ensure_ascii=False, indent=2), encoding="utf-8")
            break
        state, new_daily = trial, new_daily + [row]
    _unchanged(p, all_input_refs)
    daily = p.daily + new_daily
    for name, value in (("account.json", state), ("orders.csv", list(state["orders"].values())),
            ("fills.csv", list(state["fills"].values())), ("action_reference_checks.csv", p.feed.action_accounting.checks)):
        if name.endswith(".json"):
            (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            pd.DataFrame(value).to_csv(out / name, index=False)
    if p.checks_bytes is not None:
        (out / "prior_action_reference_checks.csv").write_bytes(p.checks_bytes)
    (out / "daily.csv").write_bytes(p.old_csv)
    if new_daily:
        with (out / "daily.csv").open("ab") as handle:
            if not p.old_csv.endswith(b"\n"):
                handle.write(b"\n")
        columns = next(csv.reader(StringIO(p.old_csv.decode("utf-8-sig"))))
        with (out / "daily.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.DictWriter(handle, fieldnames=columns).writerows(new_daily)
    result = {"schema": base.MULTIYEAR_METHOD, "status": "incomplete" if error else "completed",
        "error": error, "registration": registration, "requested_sessions": p.old_result["requested_sessions"],
        "completed_sessions": len(daily), "reused_sessions": p.next_index,
        "newly_completed_sessions": len(new_daily), "global_session_index_start": p.next_index,
        "resume_from": str(p.source_directory), "orders": len(state["orders"]), "fills": len(state["fills"]),
        "metrics": metrics(daily, p.saved.config.initial_cash) if not error and state["fills"] else None,
        "pending_targets": state.get("pending_targets"), "open_positions": _positions(state),
        "remaining_full_exits": state["full_exit_lifecycle"]["remaining_full_exits"],
        "failed_trial_committed": False, "metric_basis": p.old_result["metric_basis"],
        "old_account_mutated": False, "new_cash_account_created": False, **FLAGS}
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
