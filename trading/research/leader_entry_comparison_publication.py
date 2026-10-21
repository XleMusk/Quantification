"""Independent three-arm preregistration and account-free dated publications.

Five original common20 targets are published once.  Confirmation is evidence,
not a pre-filter: each isolated consumer applies its arm after D's opening.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ, PortfolioIntent
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_close_publication import _stamp, load_fixed_cohort_registration, prepare_close_case_targets
from .leader_daily_replay import EXIT_POLICIES, FAMILIES, POOLS
from .leader_daily_signals import _calendar_view
from .leader_entry_confirmation_inputs import RULE_SPEC as CONFIRMATION_RULE
from .leader_entry_confirmation_policy import ARM_IDS, RULE_SPEC as ARM_RULE
from .leader_forward_run import _calendar as _window_calendar, _case_key, _ipo_proof, _protocol, _scoped
from .leader_published_forward_run import _load

METHOD_ID = "leader_common20_prospective_entry_confirmation_three_arm_v1"
REGISTRATION_SCHEMA = "leader_entry_comparison_preregistration_v1"
SCHEMA = "leader_entry_comparison_close_publication_v1"
COMMON5KEYS = tuple(_case_key(POOLS[0], family, EXIT_POLICIES[0]) for family in FAMILIES)
CASE_IDS = tuple(family + ":" + arm for family in FAMILIES for arm in ARM_IDS)
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "current_signal_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "broker_interacted": False, "account_created": False,
    "engine_advanced": False, "orders_generated": False, "ledger_mutated": False,
    "ths_payload_generated": False, "old_publications_modified": False,
    "pit_verified": False, "corporate_actions_verified": False}
SPEC = {"id": METHOD_ID, "families": list(FAMILIES), "arms": list(ARM_IDS),
    "base_case_ids": list(COMMON5KEYS), "case_ids": list(CASE_IDS),
    "inherited_scope": "entire_original_14_anchor_cohort_and_exact_common20_no_new_freeze",
    "new_decision_window": "first_unexamined_close_registered_before_first_D_15_00",
    "length_open_sessions": 40, "execution_tail_sessions": 6,
    "auxiliary_successor": "calendar_date_only_no_future_bar_required",
    "initial_state": "independent_cash100000_no_old_account_or_15_decision_reuse",
    "original_top5_and_weights": "same_five_common20_families_no_refill_or_renormalization",
    "confirmation_rule": deepcopy(CONFIRMATION_RULE), "arm_rule": deepcopy(ARM_RULE),
    "no_confirmation_manifest": "explicit_unknown_for_every_candidate_not_empty_or_known_false",
    "tail": "NO_CHANGE_no_new_candidates_confirmation_not_required_independent_expiry_continues",
    "source_clock": "own_D_or_earlier_input_and_confirmation_available_before_actual_D_publication",
    "publication_clock": "same_D_date_at_or_after_15_05_before_exact_E_open_no_backfill_or_roll",
    "metrics": "same_40_and_46_windows_all_families_no_fill_error_or_incomplete_returns_null",
    "blocked_exit": "original_Ledger_residual_reassessment_no_extra_sticky_rule",
    "parameters_tuned": False, "old_checked_window_replayed": False}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return json.loads(data.decode("utf-8-sig")), {"file": str(path), "sha256": sha256(data).hexdigest()}


def _flags(value):
    if any(value.get(key) is not expected for key, expected in FLAGS.items()):
        raise ValueError("Entry-comparison research boundaries changed")


def _source_registration(path, binding, now):
    value, proof = _read(path)
    original, original_proof = _read(value["original_publication"]["file"])
    original_ref = value["original_publication"]
    cohort = value["cohort"]
    if (value.get("schema") != "leader_published_close_forward_registration_v1"
            or value.get("protocol_binding") != binding or value.get("config") != binding["config"]
            or value.get("first_decision_day") != "2026-10-15" or cohort.get("anchor") != "2026-10-14"
            or len(cohort["pools"][POOLS[0]]) != 20
            or len(set(cohort["pools"][POOLS[0]])) != 20
            or original_ref.get("sha256") != original_proof["sha256"]
            or original_ref.get("recorded_at") != original.get("recorded_at")
            or original.get("source_close_day") != value["first_decision_day"]
            or not _stamp(original["recorded_at"]) <= _stamp(value["registered_at"]) <= now
            or any(value.get(key) is not False for key in
                   ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit", "future_labels_used_for_score"))
            or value.get("candidate_panel_only") is not True):
        raise ValueError("Original 15-day formal source registration/cohort/protocol changed")
    proof.update(registered_at=value["registered_at"], first_decision_day=value["first_decision_day"],
                 first_execution_day=value["first_execution_day"])
    return value, proof


def _environment(protocol):
    binding, calendar, config = _protocol(protocol)
    sessions, _, status = _calendar_view(calendar, f"{calendar['year']}-01-01", f"{calendar['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("An exact pinned exchange calendar is required")
    return binding, calendar, config, sessions


def register_method(source_registration, protocol, out, *, first_day="2026-10-16"):
    """Save new rules before first D closes; never create research accounts."""
    now = _now()
    binding, calendar, config, sessions = _environment(protocol)
    if first_day not in sessions or not now < _stamp(first_day + "T15:00:00+08:00") or first_day <= "2026-10-15":
        raise ValueError("New method must precede its first unseen D close; 15 cannot be reused")
    source, source_proof = _source_registration(source_registration, binding, now)
    window = _window_calendar(calendar, first_day)
    _, protocol_proof = _read(protocol)
    finished = _now()
    if finished < now or not finished < _stamp(first_day + "T15:00:00+08:00"):
        raise ValueError("Registration preparation crossed the first unseen D close")
    now = finished
    value = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": now.isoformat(),
        "spec": deepcopy(SPEC), "source_registration": source_proof,
        "source_registration_first_decision_day": source["first_decision_day"],
        "original_cohort": deepcopy(source["cohort"]), "common20": deepcopy(source["cohort"]["pools"][POOLS[0]]),
        "protocol": protocol_proof, "protocol_binding": binding, "config": config.to_dict(),
        "calendar": {"file": str(Path(binding["calendar"]).resolve()), "sha256": digest(binding["calendar"])},
        "first_decision_day": first_day, "first_execution_day": window[1],
        "first_execution_deadline": window[1] + "T09:30:00+08:00",
        "decision_sessions": window[:40], "execution_tail_sessions": window[40:46],
        "window": window, "decision_window_end": window[39], "execution_tail_end": window[45],
        "auxiliary_next_session": window[46], "base_case_ids": list(COMMON5KEYS), "case_ids": list(CASE_IDS),
        "source_registration_is_new_method": False, **FLAGS}
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    target = out / "registration.json"
    write_json(target, value)
    return target, value


def load_registration(registration, *, now=None):
    """Return validated record, proof and the existing protocol runtime objects."""
    now = now or _now()
    value, proof = _read(registration)
    _flags(value)
    binding, calendar, config, sessions = _environment(value["protocol"]["file"])
    _, protocol_proof = _read(value["protocol"]["file"])
    source, source_proof = _source_registration(value["source_registration"]["file"], binding, now)
    day = value["first_decision_day"]
    window = _window_calendar(calendar, day)
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("spec") != SPEC or value.get("protocol") != protocol_proof
            or value.get("protocol_binding") != binding or value.get("config") != config.to_dict()
            or value.get("source_registration") != source_proof
            or value.get("original_cohort") != source["cohort"] or value.get("common20") != source["cohort"]["pools"][POOLS[0]]
            or value.get("calendar") != {"file": str(Path(binding["calendar"]).resolve()), "sha256": digest(binding["calendar"])}
            or not _stamp(source["registered_at"]) <= _stamp(value["registered_at"]) <= now
            or not _stamp(value["registered_at"]) < _stamp(day + "T15:00:00+08:00")
            or day <= "2026-10-15" or value.get("first_execution_day") != window[1]
            or value.get("first_execution_deadline") != window[1] + "T09:30:00+08:00"
            or value.get("window") != window or value.get("decision_sessions") != window[:40]
            or value.get("execution_tail_sessions") != window[40:46]
            or value.get("decision_window_end") != window[39] or value.get("execution_tail_end") != window[45]
            or value.get("auxiliary_next_session") != window[46]
            or value.get("base_case_ids") != list(COMMON5KEYS) or value.get("case_ids") != list(CASE_IDS)
            or value.get("source_registration_first_decision_day") != source["first_decision_day"]
            or value.get("source_registration_is_new_method") is not False):
        raise ValueError("Frozen entry-comparison preregistration changed")
    proof["registered_at"] = value["registered_at"]
    return {"record": value, "proof": proof, "binding": binding, "calendar": calendar,
        "sessions": sessions, "config": config, "window": window}


def _confirmation(manifest, own, registered, day, observed_at, *, tail):
    if tail or manifest is None:
        reason = "registered_tail_confirmation_not_required" if tail else "confirmation_manifest_not_available"
        return {"day": day, "previous_day": own["previous"][day], "available_at": None,
            "by_symbol": {} if tail else {symbol: {"date": day, "symbol": symbol,
                "status": "unknown", "reason": reason} for symbol in registered["record"]["common20"]},
            "sourceproof": None, "status": reason}
    from .leader_entry_confirmation_inputs import load_confirmation
    value = load_confirmation(manifest, day=day, calendar=registered["binding"]["calendar"],
        source_day_proofs=own["bundle"]["source_packages"], observed_at=observed_at)
    # The full dated ordinary roster remains in the separately pinned sidecar.
    # The publication needs the frozen20 only, never another 5k-row copy.
    return {**{key: deepcopy(item) for key, item in value.items() if key not in {"rows", "by_symbol"}},
        "publication_scope": "inherited_common20_only_full_roster_retained_in_sidecar",
        "by_symbol": {symbol: deepcopy(value["by_symbol"].get(symbol, {"date": day, "symbol": symbol,
            "status": "unknown", "reason": "symbol_absent_from_current_confirmation_roster"}))
            for symbol in registered["record"]["common20"]}}


def _prepare(own, registered, stamp, confirmation_manifest):
    record, binding, config = registered["record"], registered["binding"], registered["config"]
    day = own["days"][-1]
    window = registered["window"]
    if day not in window[:-1] or str(own["raw"].date.max()) != day:
        raise ValueError("Publication requires its own exact D input within the fixed new window")
    execution = window[window.index(day) + 1]
    if (stamp.date().isoformat() != day or not _stamp(day + "T15:05:00+08:00") <= stamp
            or stamp >= _stamp(execution + "T09:30:00+08:00")
            or _stamp(own["proof"]["recorded_at"]) > stamp):
        raise ValueError("Publication requires actual same-D after-close time, never late backfill or day roll")
    cohort, source_proof = load_fixed_cohort_registration(record["source_registration"]["file"], binding, own["raw"], stamp)
    if cohort != record["original_cohort"] or source_proof != record["source_registration"]:
        raise ValueError("Own D raw no longer reproduces the inherited 14-anchor cohort")
    source = [p for p in own["bundle"]["source_packages"] if p["day"] == day]
    if len(source) != 1:
        raise ValueError("A complete own-D terminal source is required")
    ready = _stamp(source[0]["terminal_completion_observed_at"])
    history = binding.get("history_source_view")
    required = [ready, _stamp(record["registered_at"]), _stamp(own["proof"]["recorded_at"])]
    if history is not None:
        required.extend((_stamp(history["available_at"]), _stamp(binding["history_source_specified_at"])))
    if ready < _stamp(day + "T15:00:00+08:00") or max(required) > stamp:
        raise ValueError("Publication predates its source or registered method availability")
    tail = window.index(day) >= 40
    confirmation = _confirmation(confirmation_manifest, own, registered, day, stamp.isoformat(), tail=tail)
    if confirmation.get("available_at") is not None:
        required.append(_stamp(confirmation["available_at"]))
    if max(required) > stamp:
        raise ValueError("Confirmation became available after the actual publication")
    qualification = _ipo_proof(binding)
    if qualification is None:
        raise ValueError("Original typed IPO qualification is required")
    prepared = prepare_close_case_targets(own["bundle"], cohort=cohort, head=None, day=day,
        calendar=registered["sessions"], config=config, ipo_qualification=qualification,
        decision_tail=tail, case_ids=COMMON5KEYS)
    if [p["case_id"] for p in prepared] != list(COMMON5KEYS):
        raise ValueError("Exactly the original five common20 pure targets are required")
    scoped_batches = _scoped(own["bundle"], record["common20"], window[39])[0]
    rows = []
    for item in prepared:
        item = deepcopy(item)
        weights, context = item.pop("weights"), item.pop("context")
        if context is not None:
            raise ValueError("Publisher must not bind a research account")
        intent = (PortfolioIntent.no_change(item["no_change_reason"], decided_at=stamp.isoformat()) if weights is None else
            PortfolioIntent.full_target(weights, decided_at=stamp.isoformat(), execution_not_before=execution,
                source="research:leader_entry_comparison_publication:" + item["family"]))
        batch = scoped_batches[item["family"]].get(day)
        rows.append({**item, "portfolio_intent": intent.to_dict(), "target_weights": None if weights is None else dict(intent.weights),
            "target_cash_weight": None if weights is None else 1 - sum(weights.values()),
            "original_day_batch": deepcopy(batch), "original_candidates": deepcopy((batch or {}).get("candidates", [])),
            "batch_status": (batch or {}).get("status", "missing"),
            "holdings_and_arm_application_account_bound": False, **FLAGS})
    return {"schema": SCHEMA, "method_id": METHOD_ID, "status": "dated_base_targets_confirmation_pending_account_application",
        "recorded_at": stamp.isoformat(), "publication_at": stamp.isoformat(),
        "source_close_day": day, "proposed_execution_day": execution, "next_open_deadline": execution + "T09:30:00+08:00",
        "registered_window_tail": tail, "registration": registered["proof"], "protocol_binding": binding,
        "input_bundle": deepcopy(own["proof"]), "source_terminal_completion": deepcopy(source[0]),
        "source_ready_at": max(required).isoformat(), "ipo_qualification": qualification.verification,
        "confirmation": confirmation, "confirmationproof": confirmation.get("sourceproof"),
        "base_case_count": 5, "case_count_after_account_application": 15, "base_cases": rows,
        "publication_time_backfilled": False, "execution_day_rolled_forward": False,
        "successor_raw_used": False, "successor_raw_required_for_target_weights": False, **FLAGS}


def publish(input_bundle, confirmation_manifest, registration, out):
    """Read the saved own-D bundle; publish five unbound original targets only."""
    registered = load_registration(registration)
    own = _load(input_bundle, registered["binding"], registered["calendar"], _now())
    stamp = _now()
    value = _prepare(own, registered, stamp, confirmation_manifest)
    finished = _now()
    if (finished < stamp or finished.date().isoformat() != value["source_close_day"]
            or finished >= _stamp(value["next_open_deadline"])):
        raise ValueError("Publication preparation crossed its actual D/next-open boundary")
    value["recorded_at"] = value["publication_at"] = finished.isoformat()
    for row in value["base_cases"]:
        row["portfolio_intent"] = replace(PortfolioIntent.from_dict(row["portfolio_intent"]),
            decided_at=finished.isoformat()).to_dict()
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    path = out / "publication.json"
    write_json(path, value)
    return path, value


def load_publication(pubpath, own_loaded, registration, now=None):
    """Validate five saved targets and their own evidence, without an account."""
    now = now or _now()
    registered = load_registration(registration, now=now)
    value, proof = _read(pubpath)
    _flags(value)
    stamp = _stamp(value["recorded_at"])
    if stamp > now:
        raise ValueError("Future publication recording is unavailable")
    manifest = ((value.get("confirmationproof") or {}).get("manifest") or {}).get("file")
    expected = _prepare(own_loaded, registered, stamp, manifest)
    if value != expected:
        raise ValueError("Saved base publication/own input/confirmation/clock contract changed")
    proof.update(day=value["source_close_day"], recorded_at=value["recorded_at"])
    return {"record": value, "proof": proof, "day": value["source_close_day"],
        "execution": value["proposed_execution_day"], "tail": value["registered_window_tail"],
        "own": own_loaded, "reference": proof,
        "scoped_batches": {row["family"]: ({value["source_close_day"]: row["original_day_batch"]}
            if row["original_day_batch"] is not None else {}) for row in value["base_cases"]},
        "confirmation": value["confirmation"]["by_symbol"],
        "intents": {row["case_id"]: PortfolioIntent.from_dict(row["portfolio_intent"]) for row in value["base_cases"]}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    for name in ("source-registration", "protocol", "out"):
        register.add_argument("--" + name, type=Path, required=True)
    register.add_argument("--first-day", default="2026-10-16")
    publication = commands.add_parser("publish")
    for name in ("input-bundle", "registration", "out"):
        publication.add_argument("--" + name, type=Path, required=True)
    publication.add_argument("--confirmation-manifest", type=Path)
    args = parser.parse_args(argv)
    path, result = (register_method(args.source_registration, args.protocol, args.out, first_day=args.first_day)
        if args.command == "register" else publish(args.input_bundle, args.confirmation_manifest, args.registration, args.out))
    print(json.dumps({"file": str(path), "schema": result["schema"], "method_id": METHOD_ID, **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
