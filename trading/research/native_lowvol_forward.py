"""Continue saved low-volatility publications in one isolated research ledger.

The original monthly ranks are never recalculated here.  Each exact opening
uses the shared Engine, whole-row native quotes and explicit action evidence.
This is a REPLAY research account, not a broker or a PAPER worker.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..market_dataset import write_json
from ..target_exit_lifecycle import PREOPEN_FULL_EXIT_LIFECYCLE
from .exchange_calendar import load_calendar
from .ipo_qualification import load_ipo_qualification
from .leader_daily_signals import _calendar_view
from .leader_forward_run import ForwardRawFeed
from .native_lowvol_publication import METHOD_ID, bind_preopen, load_publication
from . import recent_action_extension as recent_actions
from .shortline_native_preopen_forward import _lock, _source
from .stockdb_action_accounting import load_action_accounting
from .stockdb_controlled_signal_comparison import ControlledFeed


SCHEMA = "native_lowvol_preopen_checkpoint_v1"
JOURNAL_SCHEMA = "native_lowvol_preopen_committed_session_v1"
RECENT_ACTION_METHOD = "native_lowvol_preopen_recent_cash_action_extension_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "returns_reportable": False, "total_return_verified": False,
    "model_refit": False, "ranks_recomputed": False, "network_accessed": False,
    "broker_interacted": False, "user_account_mutated": False}


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return json.loads(data.decode("utf-8-sig")), {"file": str(path),
        "sha256": sha256(data).hexdigest()}


def _checked(reference):
    value, actual = _read(reference["file"])
    if actual["sha256"] != reference["sha256"]:
        raise ValueError("A sealed research source or journal changed")
    return value


def _context(directory):
    return RunContext("native-lowvol-" + _hash(str(directory))[:24], TradingMode.REPLAY)


def _action_prefix(actions, day):
    # Freeze every already relevant action, including a record date preceding
    # a later ex-date. A newly discovered past record cannot create holdings.
    return sorted([deepcopy(a) for a in actions if any(a.get(field) and a[field] <= day
        for field in ("dividRegistDate", "dividOperateDate", "dividPayDate"))],
        key=lambda a: (a["symbol"], a["dividOperateDate"]))


def _evidence_ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _extension_binding(extension, event_view, reference_view, prior):
    inception = ((prior or {}).get("recent_action_extension") or {}).get("inception_extension")
    if inception is None:
        inception = _evidence_ref(extension)
    if _evidence_ref(inception["path"]) != inception:
        raise ValueError("The original recent-action inception evidence changed")
    return {"method_id": RECENT_ACTION_METHOD, "loader_method": recent_actions.METHOD,
        "base_event": _evidence_ref(Path(event_view) / "result.json"),
        "base_reference": _evidence_ref(Path(reference_view) / "result.json"),
        "inception_extension": deepcopy(inception),
        "scope_basis": "cumulative_actual_account_exposure_not_all_raw_symbols"}


def _exposure_scope(state, publication, old_scope):
    scope = set(old_scope)
    scope.update(symbol for symbol, lots in state["positions"].items()
        if sum(lot["qty"] for lot in lots) > 0)
    scope.update(symbol for symbol, weight in (state.get("pending_targets") or {}).items() if weight > 0)
    scope.update(symbol for symbol, weight in (publication.get("weights") or {}).items() if weight > 0)
    scope.update((state.get("full_exit_lifecycle") or {}).get("remaining_full_exits", {}))
    for field in ("entitlements", "receivables"):
        for identifier, amount in state.get(field, {}).items():
            if amount:
                parts = identifier.split(":")
                scope.add(parts[1] if parts[0] == "rights" else parts[0])
    return sorted(scope)


class NativeLowvolDailyFeed(ForwardRawFeed):
    """Use the existing raw feed with the existing action/reference guards."""
    def __init__(self, raw, sessions, symbols, qualification, accounting):
        super().__init__(raw, sessions, symbols, ipo_qualification=qualification)
        self.action_accounting = accounting
        self.actions = accounting.actions
        self.manifest.update(replay_only=True,
            historical_price_adjustments=accounting.special_references,
            start=accounting.start, end=max(accounting.replay_days))

    def validate_session(self, state, day):
        ControlledFeed.validate_session(self, state, day)

    def signal_frame(self, *args, **kwargs):
        raise AssertionError("A published preopen low-volatility account must not score E close")


def _restore(head, binding, publications, sources, directory):
    if (head.get("schema") != SCHEMA or head.get("method_id") != METHOD_ID
            or head.get("binding") != binding or head.get("config") != binding["config"]
            or head.get("context") != binding["context"]):
        raise ValueError("The independent low-volatility account method/context/config cannot change")
    count = len(head["consumed_publications"])
    if (not count or count > len(publications)
            or head["consumed_publications"] != [p["publication_ref"] for p in publications[:count]]
            or len(head["journal_files"]) != count):
        raise ValueError("Supply the unchanged complete original publication prefix")
    expected_days = [p["execution_day"] for p in publications[:count]]
    if (head["state"].get("broker_binding") or head["state"]["day"] != expected_days[-1]
            or [r["date"] for r in head["daily"]] != expected_days):
        raise ValueError("The saved research account cursor/daily history differs")
    previous = None
    previous_state_hash = _hash(new_state(StrategyConfig(**binding["config"]).validate().initial_cash))
    for index, reference in enumerate(head["journal_files"]):
        path = Path(reference["file"]).resolve()
        if path != directory / "journal" / (expected_days[index] + ".json"):
            raise ValueError("Journal outside the independent research run")
        journal = _checked(reference)
        if (journal["schema"] != JOURNAL_SCHEMA or journal["binding_sha256"] != _hash(binding)
                or journal["previous_journal_sha256"] != previous
                or journal["before_state_sha256"] != previous_state_hash
                or journal["publication_ref"] != head["consumed_publications"][index]
                or journal["post_state_sha256"] != _hash(journal["post_state"])
                or journal["post_daily_sha256"] != _hash(journal["post_daily"])):
            raise ValueError("Broken immutable low-volatility session journal")
        for evidence in journal["action_evidence_refs"]:
            if sha256(Path(evidence["path"]).read_bytes()).hexdigest() != evidence["sha256"]:
                raise ValueError("Previously consumed action evidence changed")
        previous = reference["sha256"]
        previous_state_hash = journal["post_state_sha256"]
    if (journal["post_state"] != head["state"] or journal["post_daily"] != head["daily"]
            or journal["raw_sources"] != head["raw_sources"]
            or journal["action_prefix"] != head["action_prefix"]
            or journal["action_scope"] != head["action_scope"]):
        raise ValueError("The checkpoint differs from its last committed journal")
    if binding.get("recent_action_extension") is not None and (
            journal.get("recent_action_extension") != head.get("latest_recent_action_extension")):
        raise ValueError("The checkpoint changed its last consumed recent-action extension")
    for old in head["raw_sources"]:
        if old["day"] not in sources or sources[old["day"]]["proof"] != old:
            raise ValueError("A consumed exact raw source is missing or has changed")
    for reference in head["action_evidence_refs"]:
        path = Path(reference["path"])
        if sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
            raise ValueError("Previously consumed action evidence changed")


def _result(status, directory, head, *, committed=0, orders=0, **details):
    state = head["state"] if head else None
    recent = ((head or {}).get("binding") or {}).get("recent_action_extension")
    return {"schema": "native_lowvol_preopen_forward_result_v1", "method_id": METHOD_ID,
        "status": status, "observed_at": datetime.now(MARKET_TZ).isoformat(),
        "run_directory": str(directory), "checkpoint": str(directory / "head.json") if head else None,
        "account_count": 1 if head else 0, "account_mode": "replay",
        "newly_committed_sessions": committed, "new_order_count": orders,
        "orders_present": bool(state and state["orders"]), "metrics": None,
        "cash_fen": state["cash"] if state else None,
        "cash_rmb": state["cash"] / 100 if state else None,
        "known_limitations": ["required_blank_or_unknown_raw_quotes_stop_the_exact_session",
            "a_possible_trade_requires_known_exact_previous_session_volume"],
        "stale_symbols": list(head["daily"][-1].get("stale_symbols", [])) if head else None,
        "native_halt_valuation": deepcopy(head["daily"][-1].get("native_halt_valuation", [])) if head else None,
        "last_execution_day": state["day"] if state else None,
        "positions": {s: sum(l["qty"] for l in lots) for s, lots in state["positions"].items()
            if sum(l["qty"] for l in lots)} if state else None,
        "remaining_full_exits": (state.get("full_exit_lifecycle") or {}).get("remaining_full_exits", {}) if state else None,
        "research_ledger_mutated": committed > 0, "old_engineering_account_migrated": False,
        **({"recent_action_method": recent["method_id"],
            "latest_recent_action_extension": head["latest_recent_action_extension"],
            "action_scope_basis": recent["scope_basis"]} if recent else {}),
        **details, **FLAGS}


def run(publications, raw_closes, ipo_evidence, master_run, action_event_view,
        action_reference_view, run_dir, *, through=None, recent_action_extension=None):
    """Consume a cumulative exact publication prefix; never fabricate a bar."""
    if not publications:
        raise ValueError("At least one saved original low-volatility publication is required")
    now, directory = datetime.now(MARKET_TZ), Path(run_dir).resolve()
    loaded = [load_publication(p, now=now) for p in publications]
    first = loaded[0]
    calendar_ref = first["calendar_ref"]
    calendar_file = calendar_ref["file"]
    calendar = load_calendar(Path(calendar_file))
    if calendar is None or sha256(Path(calendar_file).read_bytes()).hexdigest() != calendar_ref["sha256"]:
        raise ValueError("The published exact calendar changed")
    year = calendar["year"]
    sessions, previous, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    source_days = [p["source_day"] for p in loaded]
    if (status != "pinned_static_exchange_calendar" or source_days != sorted(set(source_days))
            or source_days != [d for d in sessions if source_days[0] <= d <= source_days[-1]]):
        raise ValueError("Publications must retain the original exact chronological source-session prefix")
    config = StrategyConfig(**first["config"]).validate()
    for index, p in enumerate(loaded):
        if (p["config"] != first["config"] or p["calendar_ref"] != calendar_ref
                or p["inception"] != first["inception"] or previous.get(p["execution_day"]) != p["source_day"]
                or p["previous_publication_ref"] != (loaded[index-1]["publication_ref"] if index else None)
                or p["execution_day"] == sessions[-1]):
            raise ValueError("A publication changed policy/config/calendar/parent or exact execution date")
    sources = {}
    for folder in raw_closes:
        source = _source(folder, calendar_ref, sessions, now)
        day = source["proof"]["day"]
        if day in sources:
            raise ValueError("Duplicate dated whole-row native source")
        sources[day] = source
    for p in loaded:
        d = p["source_day"]
        if d in sources and sources[d]["proof"] != p["source_proof"]:
            raise ValueError("Published source D and supplied execution source D differ")
    qualification = load_ipo_qualification(ipo_evidence, calendar_file, master_run=master_run)
    head_file = directory / "head.json"
    head = _read(head_file)[0] if head_file.exists() else None
    binding = {"method_id": METHOD_ID, "run_directory": str(directory),
        "mode": TradingMode.REPLAY.value, "context": _context(directory).to_dict(),
        "config": config.to_dict(), "calendar_ref": calendar_ref, "inception": first["inception"],
        "first_publication": first["publication_ref"], "ipo_qualification": qualification.verification,
        "full_exit_lifecycle": PREOPEN_FULL_EXIT_LIFECYCLE,
        "action_update_policy": "immutable_old_evidence_and_exact_economic_prefix_no_retroactive_entitlements",
        "action_knowledge_basis": "posthoc_reported_components_not_PIT",
        "price_basis": "independent_BaoStock_whole_raw_rows_not_StockDB_field_substitution"}
    extension_readiness = None
    if recent_action_extension is not None:
        parent_binding = head["binding"] if head else (
            _read(directory / "binding.json")[0] if (directory / "binding.json").exists() else None)
        binding["recent_action_extension"] = _extension_binding(recent_action_extension,
            action_event_view, action_reference_view, parent_binding)
        if head is None and parent_binding is not None and (
                binding["recent_action_extension"]["inception_extension"] != _evidence_ref(recent_action_extension)):
            raise ValueError("An interrupted inception must retain its original recent-action extension")
        cutoff_preview = through or loaded[-1]["execution_day"]
        cursor_preview = len(head["consumed_publications"]) if head else 0
        work_preview = [p for p in loaded[cursor_preview:] if p["execution_day"] <= cutoff_preview]
        required_extension_day = work_preview[-1]["execution_day"] if work_preview else (
            head["state"]["day"] if head else first["execution_day"])
        extension_readiness = recent_actions.extension_readiness(recent_action_extension,
            action_event_view, action_reference_view, calendar_file, through=required_extension_day)
        if any(directory == path or directory in path.parents or path in directory.parents
                for path in map(Path, extension_readiness["input_directories"])):
            raise ValueError("Recent-action run must remain outside all direct and indirect source packages")
    if head:
        _restore(head, binding, loaded, sources, directory)
    elif directory.exists():
        marker = directory / "binding.json"
        if not marker.exists() or _read(marker)[0] != binding:
            raise FileExistsError("An existing uncommitted run cannot be silently reset or change inception")
    cutoff = through or loaded[-1]["execution_day"]
    if cutoff not in sessions or cutoff < first["execution_day"] or (head and cutoff < head["state"]["day"]):
        raise ValueError("Through must be an exact execution session without moving the cursor backwards")
    cursor = len(head["consumed_publications"]) if head else 0
    work = [p for p in loaded[cursor:] if p["execution_day"] <= cutoff]
    if not work:
        return _result("already_consumed_original_publication_prefix", directory, head)
    required = [work[0]["source_day"], work[0]["execution_day"]]
    if any(d not in sources for d in required):
        return _result("waiting_for_exact_complete_execution_close", directory, head,
            missing_source_days=[d for d in required if d not in sources],
            required_execution_day=required[-1])
    if extension_readiness is not None and not extension_readiness["ready_for_day"]:
        return _result("waiting_for_exact_recent_action_extension", directory, head,
            missing_action_sessions=extension_readiness["missing_action_sessions"],
            required_execution_day=required_extension_day,
            recent_action_method=RECENT_ACTION_METHOD)
    committed, orders = 0, 0
    for p in work:
        d, e = p["source_day"], p["execution_day"]
        missing = [day for day in (d, e) if day not in sources]
        if missing:
            return _result("waiting_for_exact_complete_execution_close", directory, head,
                committed=committed, orders=orders, missing_source_days=missing, required_execution_day=e)
        state = head["state"] if head else new_state(config.initial_cash)
        if head and state["day"] != d:
            raise ValueError("A persistent low-volatility account cannot skip a source/execution session")
        if p["account_ref"] is None:
            if head:
                raise ValueError("A continuing publication needs its exact immutable D account snapshot")
        else:
            snapshot = _checked(p["account_ref"])
            if (not head or snapshot["state"] != state or snapshot["context"] != binding["context"]
                    or snapshot["config"] != config.to_dict() or snapshot["method_id"] != METHOD_ID):
                raise ValueError("Publication was prepared from a different D account")
        raw = pd.concat([sources[day]["canonical"] for day in sorted(sources) if day <= e], ignore_index=True)
        symbols = sorted(raw.symbol.unique())
        raw_days = sorted(day for day in sources if first["source_day"] <= day <= e)
        replay_days = [q["execution_day"] for q in loaded if q["execution_day"] <= e]
        try:
            accounting = (recent_actions.load_extension(action_event_view, action_reference_view,
                recent_action_extension, symbols=symbols, raw_days=raw_days, replay_days=replay_days,
                price_source="independent_baostock_raw") if recent_action_extension is not None else
                load_action_accounting(action_event_view, action_reference_view,
                symbols=symbols, raw_days=raw_days, replay_days=replay_days,
                price_source="independent_baostock_raw"))
            old_scope = set(head["action_scope"]) if head else set()
            if head and _action_prefix([a for a in accounting.actions if a["symbol"] in old_scope],
                    state["day"]) != head["action_prefix"]:
                raise ValueError("New action evidence changes an already relevant economic action prefix")
            action_scope = (_exposure_scope(state, p, old_scope)
                if recent_action_extension is not None else symbols)
            action_prefix = _action_prefix([a for a in accounting.actions if a["symbol"] in action_scope], e)
            extension_ref = _evidence_ref(recent_action_extension) if recent_action_extension is not None else None
            feed = NativeLowvolDailyFeed(raw, sessions, symbols, qualification, accounting)
            envelope = bind_preopen(p, RunContext.from_dict(binding["context"]))
            before_hash = _hash(state)
            journal_file = directory / "journal" / (e + ".json")
            old_journal = head["journal_files"][-1]["sha256"] if head else None
            raw_proofs = [sources[day]["proof"] for day in sorted(sources) if day <= e]
            if journal_file.exists():
                journal, journal_ref = _read(journal_file)
                if (journal["schema"] != JOURNAL_SCHEMA or journal["binding_sha256"] != _hash(binding)
                        or journal["previous_journal_sha256"] != old_journal
                        or journal["before_state_sha256"] != before_hash
                        or journal["publication_ref"] != p["publication_ref"]
                        or journal["raw_sources"] != raw_proofs
                        or journal["action_evidence_refs"] != accounting.provenance["refs"]
                        or journal["action_prefix"] != action_prefix
                        or journal["action_scope"] != action_scope
                        or (extension_ref is not None and journal.get("recent_action_extension") != extension_ref)
                        or journal["post_state_sha256"] != _hash(journal["post_state"])
                        or journal["post_daily_sha256"] != _hash(journal["post_daily"])
                        or journal["post_state"]["day"] != e
                        or journal["post_daily"][:-1] != (head["daily"] if head else [])
                        or journal["post_daily"][-1]["date"] != e):
                    raise ValueError("An interrupted journal does not match this exact session")
                trial, daily = journal["post_state"], journal["post_daily"]
            else:
                trial = deepcopy(state)
                daily = deepcopy(head["daily"]) if head else []
                row = advance(trial, feed, e, config, len(daily),
                    context=RunContext.from_dict(binding["context"]), preopen_intent=envelope,
                    full_exit_lifecycle=PREOPEN_FULL_EXIT_LIFECYCLE)
                row.update(source_day=d, original_publication=p["publication_ref"],
                    published_at=p["published_at"], rank_day=p["rank_day"],
                    execution_close_used_for_ranking=False,
                    action_accounting_basis=binding["action_knowledge_basis"])
                native_rows = feed.rows.get(e, {})
                row["native_halt_valuation"] = [{"symbol": symbol, "date": e,
                    "source_carried_close_rmb": float(native_rows[symbol]["close"]),
                    "source_volume_missing": bool(pd.isna(native_rows[symbol].get("volume"))),
                    "source_amount_missing": bool(pd.isna(native_rows[symbol].get("amount"))),
                    "tradestatus": 0, "valuation_basis": "exact_native_source_carried_raw_close",
                    "current_traded_price_available": False, "active_return_observation": False,
                    "source_proof": deepcopy(sources[e]["proof"])}
                    for symbol in row["stale_symbols"]
                    if native_rows.get(symbol, {}).get("tradestatus") == 0]
                if extension_ref is not None:
                    row["recent_action_extension"] = extension_ref
                    row["action_scope"] = action_scope
                    row["action_scope_basis"] = binding["recent_action_extension"]["scope_basis"]
                daily.append(row)
                journal = {"schema": JOURNAL_SCHEMA, "binding_sha256": _hash(binding),
                    "previous_journal_sha256": old_journal, "before_state_sha256": before_hash,
                    "publication_ref": p["publication_ref"], "source_day": d, "execution_day": e,
                    "raw_sources": raw_proofs, "action_evidence_refs": accounting.provenance["refs"],
                    "action_prefix": action_prefix,
                    "action_scope": action_scope,
                    "action_checks": accounting.checks,
                    "post_state": trial, "post_state_sha256": _hash(trial),
                    "post_daily": daily, "post_daily_sha256": _hash(daily),
                    "committed_at": datetime.now(MARKET_TZ).isoformat(), **FLAGS}
                if extension_ref is not None:
                    journal["recent_action_extension"] = extension_ref
            directory.mkdir(parents=True, exist_ok=True)
            with _lock(directory / "runner.lock"):
                if (head_file.exists() and _read(head_file)[0] != head) or (head is not None and not head_file.exists()):
                    raise ValueError("The persistent account changed during session validation")
                marker = directory / "binding.json"
                if marker.exists():
                    if _read(marker)[0] != binding:
                        raise ValueError("Interrupted account inception binding changed")
                else:
                    write_json(marker, binding)
                if not journal_file.exists():
                    journal_file.parent.mkdir(exist_ok=True)
                    # Shared atomic writer publishes complete bytes before the
                    # checkpoint. A crash here leaves either no journal or a
                    # complete recoverable session, never a partial JSON seal.
                    write_json(journal_file, journal)
                    journal_ref = _read(journal_file)[1]
                else:
                    actual, journal_ref = _read(journal_file)
                    if actual != journal:
                        raise ValueError("The interrupted journal changed before head recovery")
                new_head = {"schema": SCHEMA, "method_id": METHOD_ID, "binding": binding,
                    "config": config.to_dict(), "context": binding["context"], "state": trial,
                    "daily": daily, "consumed_publications": (head["consumed_publications"] if head else []) + [p["publication_ref"]],
                    "raw_sources": raw_proofs,
                    "action_evidence_refs": journal["action_evidence_refs"],
                    "action_prefix": journal["action_prefix"],
                    "action_scope": journal["action_scope"],
                    "journal_files": (head["journal_files"] if head else []) + [journal_ref], **FLAGS}
                if extension_ref is not None:
                    new_head["latest_recent_action_extension"] = extension_ref
                write_json(head_file, new_head)
                orders += len(trial["orders"]) - len(state["orders"])
                head = new_head
                committed += 1
        except (ValueError, KeyError, TypeError, PermissionError) as error:
            return _result("failed_required_evidence_or_engine_session_no_commit", directory, head,
                committed=committed, orders=orders, execution_day=e,
                error=f"{type(error).__name__}: {error}", session_not_committed=True)
    return _result("advanced_independent_native_lowvol_research_account", directory, head,
        committed=committed, orders=orders)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", action="append", type=Path, required=True)
    parser.add_argument("--raw-close", action="append", type=Path, required=True)
    for name in ("ipo-proof", "master-run", "action-event-view", "action-reference-view", "run-dir", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--through")
    parser.add_argument("--recent-action-extension", type=Path)
    args = parser.parse_args(argv)
    output = args.out.resolve()
    protected = [args.run_dir.resolve(), *[p.resolve().parent for p in args.publication],
        *[p.resolve() for p in args.raw_close], args.action_event_view.resolve(), args.action_reference_view.resolve(),
        args.ipo_proof.resolve().parent, args.master_run.resolve().parent]
    if args.recent_action_extension is not None:
        publication = load_publication(args.publication[0])
        readiness = recent_actions.extension_readiness(args.recent_action_extension,
            args.action_event_view, args.action_reference_view, publication["calendar_ref"]["file"],
            through=args.through)
        protected.extend(map(Path, readiness["input_directories"]))
    if output.exists() or any(output == p or p in output.parents or output in p.parents for p in protected):
        raise ValueError("Result needs a new external path outside accounts and source packages")
    result = run(args.publication, args.raw_close, args.ipo_proof, args.master_run,
        args.action_event_view, args.action_reference_view, args.run_dir, through=args.through,
        recent_action_extension=args.recent_action_extension)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"].startswith("failed") else 0
