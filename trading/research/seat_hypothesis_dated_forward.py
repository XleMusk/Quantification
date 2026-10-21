"""Consume registered seat disclosures in four isolated, resumable REPLAY books.

Each actual D publication executes at E through the existing explicit preopen
Engine path. E's own publication is not needed to execute D, and is never
invented to advance another session. No new disclosure snapshot is appended.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ, PortfolioIntent, PreopenPortfolioIntent, RunContext, TradingMode
from ..engine import advance, choose_targets
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from . import seat_hypothesis_dated_inputs as inputs
from .exchange_calendar import load_calendar
from .ipo_qualification import load_ipo_qualification
from .leader_daily_signals import _calendar_view
from .leader_forward_run import ForwardRawFeed
from .shortline_decision_v3 import _known_raw
from .shortline_native_preopen_forward import _lock, _source


METHOD_ID = "public_seat_four_arm_dated_candidate_preopen_shared_engine_v1"
REGISTRATION_SCHEMA = "public_seat_single_snapshot_forward_method_registration_v1"
SCHEMA = "public_seat_four_arm_dated_forward_checkpoint_v1"
RESULT_SCHEMA = "public_seat_four_arm_dated_forward_result_v1"
JOURNAL_SCHEMA = "public_seat_four_arm_dated_forward_committed_session_v1"
RULE_SPEC = {"method_id": METHOD_ID, "arm_ids": list(inputs.ARMS),
    "source": "original_registered_single_snapshot_only_no_disclosure_append",
    "selection": "original_choose_targets_at_actual_D_book_after_D_opening_fills_retain_rank_20",
    "execution": "actual_D_publication_PreopenPortfolioIntent_shared_Engine_at_exact_E_open",
    "successor_publication_required_to_execute_D": False,
    "no_publication": "wait_no_invented_NO_CHANGE_or_session_advance",
    "no_event": "explicit_published_NO_CHANGE_preserves_holdings",
    "reference": "exact_D_original_close_equals_E_original_preclose_or_stop_unknown",
    "corporate_action_override_supported": False,
    "commit": "all_four_trials_then_sealed_session_journal_then_atomic_head",
    "metrics": "null_accounting_only_unverified_actions_and_PIT_no_total_return",
    "terminal": "no_forced_liquidation_residual_holdings_right_censored"}
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "user_account_mutated": False,
    "broker_interacted": False, "network_accessed": False, "pit_verified": False,
    "corporate_actions_verified": False, "independent_source_identity_verified": False,
    "historical_publication_verified": False, "new_disclosure_append_supported": False}


def _now():
    return datetime.now(MARKET_TZ)


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _context(directory, arm):
    return RunContext("seat-dated:" + _hash(str(directory))[:20] + ":" + arm, TradingMode.REPLAY)


def _qualification_files(path, master):
    """Enumerate actual fixed IPO proof files/clocks, including legacy base sources."""
    files, times = [], []
    def visit(file):
        file = Path(file).resolve()
        if file in files:
            return
        files.append(file)
        item = inputs._read(file)
        if item.get("schema") == "research_ipo_listing_bundle_v1":
            times.append(item["created_at"])
            visit(item["base_evidence"])
            for supplement in item["supplements"]:
                visit(supplement["path"])
        elif "source_receipt" in item:
            if item.get("available_at") is not None: times.append(item["available_at"])
            visit(item["source_receipt"])
        elif "sources" in item:
            for source in item["sources"]:
                files.append(Path(source["raw_path"]).resolve())
                times.extend(source[key] for key in ("retrieved_at", "available_at") if source.get(key))
        elif "raw_file" in item:
            files.append(Path(item["raw_file"]).resolve())
            times.extend(item[key] for key in ("retrieved_at", "available_at", "created_at") if item.get(key))
    visit(path)
    if master is not None:
        item = inputs._read(master)
        files += [Path(master).resolve(), Path(master).resolve().parent / item["master_file"]]
        times.append(item["created_at"])
    return files, times


def _registration(path, now):
    record = inputs._read(path)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("rule_spec") != RULE_SPEC or record.get("arm_ids") != list(inputs.ARMS)
            or record.get("candidate_panel_only") is not True
            or any(record.get(key) is not False for key in ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Exact independently registered four-book forward method is required")
    actual = inputs._stamp(record["registered_at"], now)
    own = inputs._verify(record["implementation"])
    if own != inputs._ref(__file__):
        raise ValueError("Registered core forward implementation changed")
    original_ref = inputs._verify(record["original_registration"])
    original, recipes, source_files = inputs._registered(original_ref["file"], now)
    method_ref = inputs._verify(record["dated_input_method_registration"])
    method = inputs._read(method_ref["file"])
    if (method.get("schema") != "public_seat_single_snapshot_dated_input_method_registration_v1"
            or method.get("original_registration") != original_ref
            or inputs._verify(method["implementation"]) != inputs._ref(inputs.__file__)
            or method.get("original_frozen_code_refs") != original["code_refs"]
            or method.get("arm_ids") != list(inputs.ARMS)
            or method.get("source_event_day") != original["source_event_date"]
            or method.get("config") != original["config"] or record.get("config") != original["config"]
            or method.get("calendar") != record.get("calendar")
            or not inputs._stamp(original["registered_at"], now) <= inputs._stamp(method["registered_at"], now) <= actual):
        raise ValueError("Original source to dated-input method to forward registration chain changed")
    calendar_ref = inputs._verify(record["calendar"])
    payload = load_calendar(Path(calendar_ref["file"]))
    if payload is None:
        raise ValueError("Exact fixed exchange calendar is required")
    year = int(payload["year"])
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    first = record["first_decision_day"]
    if (status != "pinned_static_exchange_calendar" or first not in sessions
            or first < actual.date().isoformat() or original["source_event_date"] not in sessions):
        raise ValueError("Original prospective first D and exchange calendar cannot roll or backfill")
    ipo_ref = inputs._verify(record["ipo_evidence"])
    master_ref = inputs._verify(record["research_master_run"]) if record.get("research_master_run") else None
    files = [Path(r["file"]) for r in source_files] + [Path(path), Path(method_ref["file"]),
        Path(calendar_ref["file"]), Path(ipo_ref["file"])]
    if master_ref: files.append(Path(master_ref["file"]))
    return {"record": record, "reference": inputs._ref(path), "original": original,
        "original_reference": original_ref, "recipes": recipes, "calendar": calendar_ref,
        "sessions": sessions, "config": inputs.original._config(100000, 5),
        "files": files, "ipo": ipo_ref, "master": master_ref}


def _expected_arms(registered, day):
    """Reproduce saved event scheduling, without touching the frozen assembler."""
    sessions, original = registered["sessions"], registered["original"]
    event_index = sessions.index(original["source_event_date"])
    expected = []
    for arm in original["arms"]:
        recipe, lag = inputs.ARMS[arm["arm_id"]]
        source = registered["recipes"][recipe]
        earliest = sessions[event_index + lag] if event_index + lag < len(sessions) else None
        schedule, candidates = [], []
        supported = {row["symbol"] for row in source["supported"]}
        for row in source["candidates"]:
            clock = source["clocks"].get((row["event_date"], row["symbol"]))
            available = clock["available_at"] if clock else None
            first = None
            if available is not None and earliest is not None:
                stamp = datetime.fromisoformat(available)
                first = next((d for d in sessions[event_index + lag:]
                    if stamp <= datetime.fromisoformat(d + "T15:00:00+08:00")), None)
            status = ("outside_scope" if row["symbol"] not in supported else "unknown" if available is None else
                "not_matured" if first is None or day < first else "matured_on_D" if day == first else "matured_before_D")
            schedule.append({**row, "source_clock": clock, "earliest_lag_calendar_day": earliest,
                "first_eligible_decision_day": first, "status": status,
                "source_available_by_D_1500": datetime.fromisoformat(available) <=
                    datetime.fromisoformat(day + "T15:00:00+08:00") if available else None})
            if status == "matured_on_D": candidates.append(row)
        candidates.sort(key=lambda row: (-row["score"], row["symbol"]))
        expected.append({"arm_id": arm["arm_id"], "base_hypothesis_id": arm["base_hypothesis_id"],
            "consumer_method_id": arm["consumer_method_id"], "lag_sessions": lag,
            "source_ranked_candidates": source["supported"], "ranked_candidates": candidates,
            "event_schedule": schedule, "panel_observations": source["panel_observations"],
            "decision_kind": "RANKED_CANDIDATES" if candidates else "NO_CHANGE",
            "reason": "known_available_events_only" if candidates else "no_matured_known_supported_event_on_D",
            "weights": None, "unknown_event_is_not_a_veto_on_other_known_events": True})
    return expected


def _publication(path, registered, now):
    proof, pub = inputs._ref(path), inputs._read(path)
    day, execution = pub["decision_day"], pub["execution_day"]
    sessions = registered["sessions"]
    stamp = inputs._stamp(pub["published_at"], now)
    if (pub.get("schema") != inputs.PUBLICATION_SCHEMA or day not in sessions
            or sessions.index(day) + 1 >= len(sessions) or execution != sessions[sessions.index(day) + 1]
            or pub.get("registration") != registered["original_reference"]
            or pub.get("same_D_actual_publication") is not True
            or stamp.date().isoformat() != day or stamp < datetime.fromisoformat(day + "T15:05:00+08:00")
            or stamp >= datetime.fromisoformat(execution + "T09:30:00+08:00")
            or stamp < inputs._stamp(registered["record"]["registered_at"], now)
            or any(pub.get(key) is not value for key, value in inputs.FLAGS.items())):
        raise ValueError("Actual same-D publication/calendar/forward registration clock changed")
    bundle_ref = inputs._verify(pub["bundle"])
    bundle = inputs._read(bundle_ref["file"])
    if (bundle.get("schema") != inputs.SCHEMA or bundle.get("status") != "completed_dated_research_inputs_only"
            or bundle.get("decision_day") != day or bundle.get("next_execution_day") != execution
            or bundle.get("calendar") != registered["calendar"]
            or bundle.get("registration") != registered["original_reference"]
            or bundle.get("config") != registered["record"]["config"]
            or bundle.get("source_scope") != "original_registered_single_snapshot_only_no_new_disclosures_appended"
            or bundle.get("next_execution_raw_supplied") is not False
            or bundle.get("target_weights_generated") is not False or bundle.get("metrics") is not None
            or bundle.get("arms") != _expected_arms(registered, day)
            or any(bundle.get(key) is not value for key, value in inputs.FLAGS.items())):
        raise ValueError("Saved dated bundle candidates, exact event maturity or NO_CHANGE semantics changed")
    for reference in bundle["input_references"]:
        inputs._verify(reference)
    primary, _, inventory = inputs._complete(bundle["D_primary"]["source_folder"], day, now)
    if primary != bundle["D_primary"] or inventory != bundle["D_inventory"]:
        raise ValueError("Publication's strict D inventory or actual source proof changed")
    symbols = sorted({row["symbol"] for recipe in registered["recipes"].values() for row in recipe["supported"]})
    audit = inputs.validate_exact_source_audit(bundle["source_audit"]["reference"]["file"],
        primary["source_folder"], decision_day=day, symbols=symbols)
    if audit != bundle["source_audit"]:
        raise ValueError("D source audit does not match the published exact original files")
    audit_record = inputs._read(audit["reference"]["file"])
    times = [primary["source_available_at"], audit_record["checked_at"],
        audit_record["comparison"]["declared_source"]["source_available_at"]]
    if any(inputs._stamp(value, now) > stamp for value in times):
        raise ValueError("D publication precedes its actual close/audit/comparison source availability")
    if inputs._ref(path) != proof:
        raise ValueError("Publication changed while validating its original source")
    return {"reference": {"publication": proof, "bundle": bundle_ref,
        "decision_day": day, "execution_day": execution, "published_at": pub["published_at"]},
        "pub": pub, "bundle": bundle}


def _quality(case, targets, d, e, feed, sources, qualification, knowledge_at):
    held = {s for s, lots in case["state"]["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
    pending = set(case["state"].get("pending_targets") or {})
    required = held | pending | set(targets or {})
    issues = []
    for symbol in sorted(required):
        reasons = []
        before, after = feed.rows.get(d, {}).get(symbol), feed.rows.get(e, {}).get(symbol)
        if not _known_raw(before): reasons.append("exact_D_raw_status_activity_or_price_unknown")
        if not _known_raw(after): reasons.append("exact_E_raw_status_activity_or_price_unknown")
        if not reasons and Decimal(sources[d]["original_rows"][symbol]["close"]) != Decimal(sources[e]["original_rows"][symbol]["preclose"]):
            reasons.append("E_reference_differs_from_D_close_action_evidence_unresolved")
        cutoff = datetime.fromisoformat(e + "T09:30:00+08:00")
        assessment = qualification.assess(symbol, e, decision_as_of=cutoff.isoformat())
        if assessment["engine_block_reason"] is not None:
            reasons.append("IPO_or_listing_qualification_unknown_or_excluded_at_E_open")
        if knowledge_at > cutoff:
            reasons.append("fixed_IPO_or_master_source_actually_acquired_after_E_open")
        if reasons: issues.append({"symbol": symbol, "reasons": reasons, "IPO_evidence": assessment})
    return {"required_symbols": sorted(required), "issues": issues,
        "reference_equality_is_not_action_coverage_proof": True, "PIT_and_action_coverage_verified": False}


def _targets(arm, state, config):
    if arm["decision_kind"] == "NO_CHANGE":
        return None
    if arm["decision_kind"] != "RANKED_CANDIDATES" or not arm["ranked_candidates"]:
        raise ValueError("Only actual nonempty ranked candidates or explicit NO_CHANGE are supported")
    held = [symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0]
    return choose_targets(arm["ranked_candidates"], held, config)


def _summary(arm, case, config):
    state = case["state"]
    book = Ledger(state, config, RunContext.from_dict(case["context"]))
    return {"arm_id": arm, "context": case["context"], "last_processed_execution_day": state["day"],
        "daily": deepcopy(case["daily"]), "cash_cents": state["cash"], "equity_cents": book.equity(),
        "positions": {s: {"quantity": book.quantity(s), "available": book.available(s, state["day"])}
            for s in state["positions"] if book.quantity(s)},
        "orders": deepcopy(state["orders"]), "fills": deepcopy(state["fills"]),
        "order_count": len(state["orders"]), "fill_count": len(state["fills"]),
        "blocked_orders": [deepcopy(event) for event in state["events"] if event.get("kind") == "order_blocked"],
        "pending_targets": deepcopy(state.get("pending_targets")), "metrics": None,
        "performance_unavailable_reason": "unverified_actions_and_PIT_accounting_only_residuals_not_liquidated"}


def _result(status, directory, head, config, *, committed=0, created=False, new_orders=0, **facts):
    return {"schema": RESULT_SCHEMA, "method_id": METHOD_ID, "status": status,
        "read_at": _now().isoformat(), "run_directory": str(directory),
        "checkpoint": str(directory / "head.json") if head else None,
        "research_accounts_created": 4 if created else 0, "account_count": 4 if head else 0,
        "research_ledger_mutated": bool(committed), "newly_committed_execution_sessions": committed,
        "new_order_count": new_orders, "orders_generated": new_orders > 0,
        "cases": [_summary(arm, head["cases"][arm], config) for arm in inputs.ARMS] if head else [],
        "metrics": None, "account_reset": False, **facts, **FLAGS}


def _head(head, binding, entries, sources, directory):
    if (head.get("schema") != SCHEMA or head.get("binding") != binding
            or list(head.get("cases", {})) != list(inputs.ARMS)
            or any(head.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Original four-account method/config/source/context binding changed")
    count = len(head["consumed_publications"])
    if count < 1 or not head.get("journal_files"):
        raise ValueError("A persisted head requires at least one committed publication and sealed journal")
    if head["consumed_publications"] != [entry["reference"] for entry in entries[:count]]:
        raise ValueError("Previously consumed publication prefix changed or disappeared")
    end = entries[count - 1]["reference"]["execution_day"] if count else None
    prefix = [sources[d]["proof"] for d in sorted(sources) if end and d <= end]
    if head["raw_sources"] != prefix or len(head["journal_files"]) != count:
        raise ValueError("Previously consumed exact raw source or session journal prefix changed")
    previous, journal = None, None
    for index, reference in enumerate(head["journal_files"]):
        path = Path(reference["file"]).resolve()
        if path.parent != directory / "journal" or inputs._verify(reference) != inputs._ref(path):
            raise ValueError("Session journal source or bytes changed")
        journal = inputs._read(path)
        ending = entries[index]["reference"]["execution_day"]
        if (journal.get("schema") != JOURNAL_SCHEMA or journal.get("previous_journal_sha256") != previous
                or journal.get("publication") != entries[index]["reference"]
                or journal.get("binding_sha256") != _hash(binding)
                or journal.get("raw_sources_sha256") != _hash([sources[d]["proof"] for d in sorted(sources) if d <= ending])):
            raise ValueError("Exact journal publication/context/raw-source chain changed")
        previous = reference["sha256"]
    for arm in inputs.ARMS:
        case = head["cases"][arm]
        days = [entry["reference"]["execution_day"] for entry in entries[:count]]
        if (case["context"] != _context(directory, arm).to_dict() or case["state"].get("broker_binding")
                or case["state"]["day"] != end or [row["date"] for row in case["daily"]] != days
                or (journal and journal["post_case_sha256"][arm] != _hash(case))):
            raise ValueError("Full four-book state/daily trace differs from the sealed session journal")


def run(publications, raw_closes, registration, run_dir, *, through=None, mode=TradingMode.REPLAY):
    if mode != TradingMode.REPLAY:
        raise ValueError("This consumer is REPLAY only; no broker or PAPER worker")
    now, directory = _now(), Path(run_dir).resolve()
    registered = _registration(registration, now)
    config, sessions = registered["config"], registered["sessions"]
    packages = [path.parent for path in registered["files"]] + [Path(p).resolve() for p in raw_closes]
    packages += [Path(p).resolve().parent for p in publications]
    for package in packages:
        if directory == package or directory in package.parents or package in directory.parents:
            raise ValueError("Research run cannot cover or sit inside a source or registration package")
    head_path = directory / "head.json"
    head = inputs._read(head_path) if head_path.is_file() else None
    if not publications:
        if head:
            raise ValueError("Existing research account requires its original complete publication prefix")
        if directory.exists():
            raise FileExistsError("Existing directory without a checkpoint cannot become a reset account")
        return _result("waiting_for_first_actual_dated_publication", directory, None, config,
            first_decision_day=registered["record"]["first_decision_day"])
    entries = [_publication(path, registered, now) for path in publications]
    # Publication validation reads these indirect sources even when they were
    # not explicit CLI parameters. A run must stay outside their packages.
    indirect_files, indirect_packages = [], []
    for entry in entries:
        bundle = entry["bundle"]
        primary, audit = bundle["D_primary"], bundle["source_audit"]
        indirect_files.append(Path(entry["reference"]["bundle"]["file"]))
        indirect_files.extend(Path(reference["file"]) for reference in bundle["input_references"])
        indirect_files.extend(Path(primary[key]["file"]) for key in ("raw", "plan", "receipt", "roster"))
        indirect_files.append(Path(primary["acquisition"]["roster_receipt"]))
        indirect_files.append(Path(audit["reference"]["file"]))
        indirect_files.extend(Path(audit["verification"]["comparison"][key]["file"])
            for key in ("raw", "metadata"))
        indirect_packages.append(Path(primary["source_folder"]).resolve())
    indirect_packages += [file.resolve().parent for file in indirect_files]
    for package in indirect_packages:
        if directory == package or directory in package.parents or package in directory.parents:
            raise ValueError("Research run cannot cover or sit inside an indirect publication/source package")
    days = [entry["reference"]["decision_day"] for entry in entries]
    first = registered["record"]["first_decision_day"]
    start = sessions.index(first)
    if days != sessions[start:start + len(days)]:
        raise ValueError("Original first D and complete publication prefix cannot be skipped, reordered or rolled")
    sources = {}
    for folder in raw_closes:
        source = _source(folder, registered["calendar"], sessions, now)
        day = source["proof"]["day"]
        if day in sources: raise ValueError("Strict raw day cannot be supplied twice")
        if day >= first: sources[day] = source
    for entry in entries:
        d = entry["reference"]["decision_day"]
        if d in sources:
            primary = entry["bundle"]["D_primary"]
            proof = sources[d]["proof"]
            if (str(Path(proof["raw_file"]).resolve()) != primary["raw"]["file"]
                    or proof["raw_sha256"] != primary["raw"]["sha256"]
                    or proof["plan_sha256"] != primary["plan"]["sha256"]
                    or proof["receipt_sha256"] != primary["receipt"]["sha256"]):
                raise ValueError("D execution source differs from the exact published original D source")
    qualification = load_ipo_qualification(registered["ipo"]["file"], registered["calendar"]["file"],
        master_run=registered["master"]["file"] if registered["master"] else None)
    if list(qualification.calendar) != sessions:
        raise ValueError("IPO qualification calendar differs from the fixed execution calendar")
    qualification_files, qualification_clocks = _qualification_files(registered["ipo"]["file"],
        registered["master"]["file"] if registered["master"] else None)
    knowledge = max(inputs._stamp(t, now) for t in qualification_clocks)
    for file in qualification_files:
        package = file.parent
        if directory == package or package in directory.parents or directory in package.parents:
            raise ValueError("Research output cannot cover or sit inside fixed IPO/master source packages")
    binding = {"method_id": METHOD_ID, "rule_spec": RULE_SPEC, "registration": registered["reference"],
        "calendar": registered["calendar"], "config": config.to_dict(),
        "qualification": qualification.verification, "qualification_files": [inputs._ref(p) for p in qualification_files],
        "run_directory": str(directory), "first_decision_day": first,
        "contexts": {arm: _context(directory, arm).to_dict() for arm in inputs.ARMS}}
    if head:
        _head(head, binding, entries, sources, directory)
    elif directory.exists():
        raise FileExistsError("Existing directory without a valid head cannot be reset")
    cutoff = through or entries[-1]["reference"]["execution_day"]
    if cutoff not in sessions or (head and cutoff < head["cases"][next(iter(inputs.ARMS))]["state"]["day"]):
        raise ValueError("Through must be a fixed session without moving an existing account backwards")
    cursor = len(head["consumed_publications"]) if head else 0
    work = [entry for entry in entries[cursor:] if entry["reference"]["execution_day"] <= cutoff]
    if not work:
        return _result("waiting_for_next_actual_publication_or_execution_session", directory, head, config)
    required = [work[0]["reference"][key] for key in ("decision_day", "execution_day")]
    missing = [day for day in required if day not in sources]
    if missing:
        return _result("waiting_for_exact_D_and_E_complete_raw", directory, head, config, missing_source_days=missing)
    # The lock is outside the run: a failed first trial must not create a fake
    # account or poison an otherwise empty run directory.
    directory.parent.mkdir(parents=True, exist_ok=True)
    with _lock(directory.parent / (directory.name + ".runner.lock")):
        if (head_path.exists() and inputs._read(head_path) != head) or (head and not head_path.exists()):
            raise ValueError("Persistent checkpoint changed during source validation")
        committed, created, new_orders = 0, False, 0
        for entry in work:
            d, e = (entry["reference"][key] for key in ("decision_day", "execution_day"))
            missing = [day for day in (d, e) if day not in sources]
            if missing:
                return _result("waiting_for_exact_D_and_E_complete_raw", directory, head, config,
                    committed=committed, created=created, new_orders=new_orders, missing_source_days=missing)
            cases = head["cases"] if head else {arm: {"context": _context(directory, arm).to_dict(),
                "state": new_state(config.initial_cash), "daily": []} for arm in inputs.ARMS}
            if head and any(case["state"]["day"] != d for case in cases.values()):
                raise ValueError("A research account cannot skip an intervening published source session")
            raw = pd.concat([sources[day]["canonical"] for day in sorted(sources) if day <= e], ignore_index=True)
            feed = ForwardRawFeed(raw, sessions, sorted(raw.symbol.unique()), ipo_qualification=qualification)
            arms = {arm["arm_id"]: arm for arm in entry["bundle"]["arms"]}
            targets = {arm: _targets(arms[arm], cases[arm]["state"], config) for arm in inputs.ARMS}
            quality = {arm: _quality(cases[arm], targets[arm], d, e, feed, sources, qualification, knowledge) for arm in inputs.ARMS}
            if any(row["issues"] for row in quality.values()):
                return _result("blocked_exact_reference_raw_or_actual_IPO_knowledge", directory, head, config,
                    committed=committed, created=created, new_orders=new_orders, blocking_evidence=quality,
                    session_not_committed=True)
            trials = deepcopy(cases)
            try:
                for arm in inputs.ARMS:
                    case, context = trials[arm], RunContext.from_dict(trials[arm]["context"])
                    metadata = {"context": context, "reason": arms[arm]["reason"],
                        "source": "research:seat-dated:" + entry["reference"]["publication"]["sha256"] + ":" + arm}
                    intent = PortfolioIntent.no_change(**metadata) if targets[arm] is None else PortfolioIntent.full_target(targets[arm], **metadata)
                    envelope = PreopenPortfolioIntent(d, e, entry["reference"]["published_at"], intent)
                    row = advance(case["state"], feed, e, config, len(case["daily"]), context=context, preopen_intent=envelope)
                    row.update(source_day=d, publication=entry["reference"], selected_targets=targets[arm],
                        retained_from_actual_D_book=True, E_close_used_for_selection=False,
                        reference_and_IPO_quality=quality[arm], original_published_decision=arms[arm]["decision_kind"])
                    case["daily"].append(row)
            except (ValueError, KeyError, TypeError, PermissionError) as error:
                return _result("failed_four_arm_trial_no_commit", directory, head, config,
                    committed=committed, created=created, new_orders=new_orders,
                    failed_arm=arm, error=f"{type(error).__name__}: {error}",
                    session_not_committed=True, all_four_trials_discarded=True)
            current = deepcopy(head) if head else {"schema": SCHEMA, "binding": binding,
                "consumed_publications": [], "journal_files": [], "cases": {}, "raw_sources": [], **FLAGS}
            current["cases"] = trials
            current["consumed_publications"].append(entry["reference"])
            current["raw_sources"] = [sources[day]["proof"] for day in sorted(sources) if day <= e]
            journal = {"schema": JOURNAL_SCHEMA, "publication": entry["reference"],
                "binding_sha256": _hash(binding), "committed_at": _now().isoformat(),
                "previous_journal_sha256": head["journal_files"][-1]["sha256"] if head else None,
                "post_case_sha256": {arm: _hash(trials[arm]) for arm in inputs.ARMS},
                "raw_sources_sha256": _hash(current["raw_sources"]), **FLAGS}
            journal_file = directory / "journal" / (e + "_" + _hash(journal)[:24] + ".json")
            journal_file.parent.mkdir(parents=True, exist_ok=True)
            inputs._write(journal_file, journal)
            current["journal_files"].append(inputs._ref(journal_file))
            write_json(head_path, current)
            created, head = created or head is None, current
            committed += 1
            new_orders += sum(trials[arm]["daily"][-1]["new_orders"] for arm in inputs.ARMS)
        return _result("advanced_four_isolated_dated_research_books", directory, head, config,
            committed=committed, created=created, new_orders=new_orders,
            successor_publication_required_to_execute_D=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, action="append", default=[])
    parser.add_argument("--raw-close", type=Path, action="append", default=[])
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--through")
    parser.add_argument("--result-json", type=Path)
    args = parser.parse_args(argv)
    export = args.result_json.resolve() if args.result_json else None
    if export:
        registered = _registration(args.registration, _now())
        files = registered["files"] + [Path(p) for p in args.publication]
        qualification_files, _ = _qualification_files(registered["ipo"]["file"],
            registered["master"]["file"] if registered["master"] else None)
        files += qualification_files
        for publication in args.publication:
            pub = inputs._read(publication)
            bundle_path = Path(pub["bundle"]["file"])
            bundle = inputs._read(bundle_path)
            files.append(bundle_path)
            files += [Path(reference["file"]) for reference in bundle["input_references"]]
        packages = [p.resolve().parent for p in files] + [args.run_dir.resolve()] + [p.resolve() for p in args.raw_close]
        # The runner lock is a sibling of the run; protect its exact path,
        # without excluding every legitimate sibling observation directory.
        files.append(args.run_dir.resolve().parent / (args.run_dir.name + ".runner.lock"))
        inputs._protect(export, files, packages)
        export.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run(args.publication, args.raw_close, args.registration, args.run_dir, through=args.through)
    except Exception as error:
        if export:
            inputs._write(export, {"schema": "public_seat_dated_forward_command_failure_v1",
                "failed_at": _now().isoformat(), "error": f"{type(error).__name__}: {error}",
                "execution_progress_unknown": True, **FLAGS})
        raise
    if export:
        export.parent.mkdir(parents=True, exist_ok=True)
        inputs._write(export, result)
    print(json.dumps({"schema": result["schema"], "status": result["status"], "checkpoint": result["checkpoint"],
        "account_count": result["account_count"], "newly_committed_execution_sessions": result["newly_committed_execution_sessions"],
        "new_order_count": result["new_order_count"], "metrics": None, **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
