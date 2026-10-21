"""Prospective four-book crossing method on the existing qualified daily inputs.

The unchanged historical identity rules derive an append-only sidecar.  Original
32-case publications, source panels and accounts are never rewritten.
"""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

from ..contracts import IntentKind, MARKET_TZ, PortfolioIntent
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from . import leader_eight_type_paper as original
from . import leader_eight_type_forward_run as shared
from . import leader_eight_type_residual_paper as anchored
from .leader_environment import _clean
from .leader_environment_run import read_preview
from .leader_eight_type_inputs import _industry, _max_clock
from .leader_historical_identity_rules import HistoricalIdentityRules, SPEC as IDENTITY_SPEC
from .leader_known_identity_scope import CASE_IDS, METHOD_ID as SCOPE_METHOD, SPEC, TYPE_ID, entry_membership_policy
from .leader_missing_type_candidates import _views
from .leader_listing_features import METHOD_ID as FEATURE_METHOD

METHOD_ID = "known_positive_crossing_prospective_shared_engine_v1"
SCHEMA = "leader_known_identity_close_publication_v1"
REGISTRATION_SCHEMA = "leader_known_identity_prospective_registration_v1"
CHECKPOINT_SCHEMA = "leader_known_identity_shared_engine_checkpoint_v1"
RESULT_SCHEMA = "leader_known_identity_shared_engine_result_v1"
ACCOUNT_PREFIX = "leader-known-identity-forward:"
TARGET_POLICY_ID, RISK_POLICY_ID = shared.TARGET_POLICY_ID, shared.RISK_POLICY_ID
FLAGS = deepcopy(original.FLAGS)
PROTOCOL = {**deepcopy(original.PROTOCOL), "id": METHOD_ID, "case_count": 4,
    "types": [TYPE_ID], "cases": list(CASE_IDS), "entry_information_policy": deepcopy(SPEC),
    "identity_rules": deepcopy(IDENTITY_SPEC), "original_32_method_changed": False,
    "capacity_start": "unchanged_rank_path_from_registered_source_prefix",
    "account_start_differs_from_historical_comparison": True}

_now, _read, _ref, _verify = anchored._now, anchored._read, anchored._ref, anchored._verify


def register(base_publication, calendar, first_day, out):
    """Register before firstD close; no source producer, account or publication."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use one new explicit crossing-method registration directory")
    now = _now()
    from .leader_eight_type_paper_view import read_saved_plan
    base, view, cal = _read(base_publication), read_saved_plan(base_publication, now=now), _ref(calendar)
    sessions = anchored._sessions(cal)
    anchor = view["source_close_day"]
    if (view["status"] != "saved_paper_plan" or anchor not in sessions[:-2]
            or first_day != sessions[sessions.index(anchor) + 1]
            or now >= original._stamp(first_day + "T15:00:00+08:00")):
        raise ValueError("Register before the exact first new decision close; never roll a missed date")
    panel = _ref(base["source"]["input_manifest"])
    if panel["sha256"] != base["source"]["input_manifest_sha256"]:
        raise ValueError("Registered original input changed")
    record = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": now.isoformat(),
        "calendar": cal, "anchor_source_day": anchor, "anchor_base_publication": _ref(base_publication),
        "anchor_input": panel, "first_decision_day": first_day,
        "first_execution_day": sessions[sessions.index(first_day) + 1], "case_ids": list(CASE_IDS),
        "config": shared._config(100000, 5).to_dict(), "protocol": deepcopy(PROTOCOL),
        "policy": deepcopy(SPEC), "new_shared_runner_accounts_only": True,
        "original_accounts_migrated": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "registration.json", record)
    return record


def _registered(path, now):
    value = _read(path)
    days = anchored._sessions(value["calendar"])
    _verify(value["anchor_base_publication"]); _verify(value["anchor_input"])
    first, anchor = value["first_decision_day"], value["anchor_source_day"]
    clock = original._stamp(value["registered_at"])
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("protocol") != PROTOCOL or value.get("policy") != SPEC
            or value.get("case_ids") != list(CASE_IDS) or value.get("config") != shared._config(100000, 5).to_dict()
            or anchor not in days[:-2] or first != days[days.index(anchor) + 1]
            or value["first_execution_day"] != days[days.index(first) + 1]
            or not clock <= now or clock >= original._stamp(first + "T15:00:00+08:00")
            or value.get("new_shared_runner_accounts_only") is not True
            or value.get("original_accounts_migrated") is not False
            or any(value.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Original explicit crossing method registration changed")
    return value, _ref(path)


def _own_loaded(panel, ipo, master, cache):
    key = str(Path(panel).resolve())
    if key not in cache:
        cache[key] = shared.load_inputs(key, ipo, master)
    return cache[key]


def _industry_provider(loaded, stamp):
    saved = loaded["provenance"]["input_record"]["industry_source"]
    actual = _industry(saved["source_file"], stamp.isoformat())
    if {k: v for k, v in actual.items() if k != "membership"} != saved:
        raise ValueError("Exact original industry source and acquisition clock required")
    frame = actual["membership"]
    membership = {r["symbol"]: str(r["industry"]).strip() for r in frame.to_dict("records")
        if isinstance(r.get("industry"), str) and r["industry"].strip()}
    evidence = {**{k: deepcopy(v) for k, v in actual.items() if k != "membership"},
        "membership": membership, "posthoc_only": True, "actual_available_at": actual["available_at"]}
    return lambda day: deepcopy(evidence) if evidence["snapshot_date"] <= day else None


def _day_view(loaded, day, stamp):
    """Use exact saved feature preview; never synthesize missing feature columns."""
    record = loaded["provenance"]["input_record"]
    references = [r for r in record["preview_sources"] if _read(r["preview"])["day"] == day]
    if len(references) != 1:
        raise ValueError("Exactly one original qualified preview per identity day required")
    reference = references[0]
    envelope, features, current = read_preview(Path(reference["preview"]).parent,
        ipo_qualification=loaded["qualification"])
    if current != reference or envelope["day"] != day:
        raise ValueError("Original qualified feature preview changed")
    proof = loaded["panel_by_day"][day]["source_proof"]
    value = {"day": day, "features": features,
        "ordinary_rows": loaded["raw"].loc[loaded["raw"].date.eq(day)],
        "available_at": _max_clock(envelope["created_at"], proof.get("source_completed_at"), proof.get("source_updated_at")),
        "source_proof": proof, "feature_method_id": FEATURE_METHOD, "feature_qualification_verified": True}
    indexed = _views([value], day, stamp)[day]
    # The strict reader's ordinary_rows is a transient DataFrame. The identity
    # evaluator needs only its canonical indexed dictionaries and actual clock.
    return {key: indexed[key] for key in ("features", "ordinary", "available_at")}


def _derive(loaded, reg, stamp, prior):
    sessions, _ = shared._calendar(loaded)
    tracker = HistoricalIdentityRules(loaded["scope_members"], sessions, _industry_provider(loaded, stamp), stamp.isoformat())
    overlays = {}
    if prior is not None:
        state = _verify(prior["identity_state"])
        tracker.frames, tracker.episode, tracker.last_day = deepcopy(state["frames"]), deepcopy(state["episode"]), state["last_day"]
        overlays = _verify(prior["derived_panels"])
    days = [d for d in loaded["source_days"] if tracker.last_day is None or d > tracker.last_day]
    if prior is not None and days != [loaded["source_days"][-1]]:
        raise ValueError("Crossing identity source must append exactly one own close")
    observations = {}
    for day in days:
        panel = loaded["panel_by_day"][day]
        observed = tracker.observe(day, panel, _day_view(loaded, day, stamp))
        target = next(t for t in observed["types"] if t["id"] == TYPE_ID)
        derived = deepcopy(panel)
        derived["types"] = [deepcopy(target) if t["id"] == TYPE_ID else deepcopy(t) for t in panel["types"]]
        # Only these named derived availability locations are new; saved nested
        # identity/qualification/source clocks remain untouched.
        derived["available_at"] = stamp.isoformat()
        target_day = next(t for t in derived["types"] if t["id"] == TYPE_ID)
        target_day["available_at"] = stamp.isoformat()
        overlays[day] = _clean(derived)
        observations[day] = _clean(observed["identity_evidence"])
    if sorted(overlays) != loaded["source_days"]:
        raise ValueError("Exact original source prefix must survive crossing projection")
    state = _clean({"frames": tracker.frames, "episode": tracker.episode, "last_day": tracker.last_day})
    return overlays, state, observations


def _prepared(loaded, overlays, stamp):
    projected = {**loaded, "panel_by_day": overlays}
    day, execution, environment, rows = original._prepare(projected, stamp)
    rows = [r for r in rows if r["case_id"] in CASE_IDS]
    item = next(t for t in overlays[day]["types"] if t["id"] == TYPE_ID)
    sessions, _ = shared._calendar(loaded)
    for row in rows:
        evidence = entry_membership_policy(day=day, identity=TYPE_ID, item=item,
            selected=row["cap_candidates"], members=loaded["scope_members"], sessions=sessions,
            panels=overlays, held_symbols=set(), pending_symbols=set())
        row["entry_membership_evidence"] = evidence
        if row["environment_mode"] == "EXIT_ALL":
            continue
        reasons = [r for r in row["reason"].split(";") if r != "whole_type_membership_unknown_in_fixed_panel"]
        if row["decision"] == "FULL_TARGET":
            reasons = []
        reasons.extend(r for r in evidence["blocking_reasons"] if r not in reasons)
        row.update(reason=";".join(reasons) if reasons else "fixed_known_positive_crossing_rank_and_capacity_target",
            prepared_weights=None if reasons else row["cap_weights_before_guard_and_qualification"],
            decision="NO_CHANGE" if reasons else "FULL_TARGET")
    return projected, day, execution, environment, rows


def _previous_record(path, loaded, reg_ref, expected_day, stamp):
    """Restore saved identity state without recursively recalculating old days.

    The forward consumer independently verifies every publication in its
    complete original prefix before creating or advancing any account.
    """
    record = _read(path)
    if (record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("protocol") != PROTOCOL or record.get("entry_information_policy") != SPEC
            or record.get("source_close_day") != expected_day or record.get("paper_registration") != reg_ref
            or [row.get("case_id") for row in record.get("cases", [])] != list(CASE_IDS)
            or original._stamp(record["recorded_at"]) > stamp):
        raise ValueError("Actual crossing publications must retain their adjacent original method prefix")
    for field in ("paper_registration", "base_publication", "identity_state", "derived_panels", "identity_observations"):
        _verify(record[field])
    source = record["source"]
    if digest(source["input_manifest"]) != source["input_manifest_sha256"]:
        raise ValueError("Previous crossing source bytes changed")
    previous = _read(source["input_manifest"])
    latest = loaded["provenance"]["input_record"]
    for field in ("source_days", "source_proofs", "panels", "preview_sources"):
        if field in previous and latest.get(field, [])[:len(previous[field])] != previous[field]:
            raise ValueError("Crossing daily inputs cannot replace an original source prefix")
    if previous["source_days"][-1] != expected_day:
        raise ValueError("Previous crossing input must end on its own exact close")
    return record


def _prepare_publication(base_publication, panel, registration_file, ipo, master, stamp, previous_publication, cache):
    reg, reg_ref = _registered(registration_file, stamp)
    base = shared._publication(base_publication, ipo, master, stamp, cache)
    if base.get("unbindable"):
        raise ValueError("A late original close cannot become a crossing publication")
    day, execution, loaded = base["day"], base["execution"], base["loaded"]
    if (day < reg["first_decision_day"] or stamp.date().isoformat() != day
            or stamp < original._stamp(day + "T15:05:00+08:00")
            or stamp <= original._stamp(base["record"]["recorded_at"])
            or Path(panel).resolve() != Path(loaded["provenance"]["panel"]).resolve()):
        raise ValueError("Same-day exact original publication after the method registration required")
    anchor = _own_loaded(reg["anchor_input"]["file"], ipo, master, cache)
    anchored._anchor_prefix(anchor, loaded)
    prior = None
    if previous_publication is not None:
        sessions, previous_days = shared._calendar(loaded)
        prior = _previous_record(previous_publication, loaded, reg_ref, previous_days[day], stamp)
    elif day != reg["first_decision_day"]:
        raise ValueError("First crossing publication may not skip or roll the registered day")
    overlays, state, observations = _derive(loaded, reg, stamp, prior)
    projected, d, e, env, rows = _prepared(loaded, overlays, stamp)
    value = original._finalize(d, e, env, rows, original._source_bindings(loaded, stamp), stamp)
    value.update(schema=SCHEMA, method_id=METHOD_ID, protocol=deepcopy(PROTOCOL),
        entry_information_policy=deepcopy(SPEC), identity_rules=deepcopy(IDENTITY_SPEC),
        paper_registration=reg_ref, base_publication=_ref(base_publication),
        previous_publication=_ref(previous_publication) if previous_publication is not None else None,
        risk_policy_id=RISK_POLICY_ID, risk_policy_spec=deepcopy(shared.FORWARD_RISK_SPEC),
        target_policy_id=TARGET_POLICY_ID, identity_derivation_at=stamp.isoformat(),
        original_32_inputs_changed=False, original_accounts_migrated=False,
        source_method_unchanged=SCOPE_METHOD, historical_backfill_for_trading=False,
        limitations=["Four fixed crossing cases retain both environment guards and both capacities; no case selection.",
            "Identity and relative-leg definitions are unchanged; source clocks and unknown identities remain explicit.",
            "Prior holdings and pending identity are checked by the same information policy in the shared consumer.",
            "Whole-lot orders, T+1, fees, FIFO and five-session risk exits belong to independent shared research accounts.",
            "Historical PIT, economic action completeness, true theme identity and real-account readiness remain unverified."])
    return value, projected, overlays, state, observations


def publish(base_publication, panel, registration_file, ipo, master, out, *, previous_publication=None):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("A new crossing publication directory is required")
    stamp = _now()
    record, _, overlays, state, observations = _prepare_publication(base_publication, panel,
        registration_file, ipo, master, stamp, previous_publication, {})
    out.mkdir(parents=True, exist_ok=False)
    for name, value in (("derived_panels", overlays), ("identity_state", state), ("identity_observations", observations)):
        write_json(out / (name + ".json"), _clean(value))
        record[name] = _ref(out / (name + ".json"))
    # Payloads close before the actual publication clock. Never backdate the
    # completed files to a source-day 15:05 or to the calculation start.
    finished = _now()
    if finished < stamp or finished.date() != stamp.date():
        raise ValueError("Crossing publication clock crossed its own close day")
    record.update(recorded_at=finished.isoformat(), publication_at=finished.isoformat())
    for row in record["cases"]:
        intent = PortfolioIntent.from_dict(row["portfolio_intent"])
        row["portfolio_intent"] = replace(intent, decided_at=finished.isoformat(),
            source="research:known_positive_crossing:" + row["case_id"]).to_dict()
    write_json(out / "paper_plan.json", _clean(record))
    return record


def load_publication(path, ipo, master, now, cache):
    key = "known-positive-publication:" + str(Path(path).resolve())
    proof = _ref(path)
    if key in cache:
        saved = cache[key]
        if saved["reference"]["sha256"] != proof["sha256"]:
            raise ValueError("Crossing publication changed during load")
        return saved
    record = _read(path)
    if record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID:
        raise ValueError("An explicit known-positive crossing publication required")
    stamp = original._stamp(record["identity_derivation_at"])
    final = original._stamp(record["recorded_at"])
    if not stamp <= final <= now or final.date() != stamp.date():
        raise ValueError("Crossing source and actual publication clocks changed")
    for field in ("paper_registration", "base_publication", "derived_panels", "identity_state", "identity_observations"):
        _verify(record[field])
    prior = record["previous_publication"]
    if prior is not None:
        _verify(prior)
    expected, projected, overlays, state, observations = _prepare_publication(record["base_publication"]["file"],
        record["source"]["input_manifest"], record["paper_registration"]["file"], ipo, master, stamp,
        prior["file"] if prior else None, cache)
    expected.update(recorded_at=final.isoformat(), publication_at=final.isoformat())
    for row in expected["cases"]:
        intent = PortfolioIntent.from_dict(row["portfolio_intent"])
        row["portfolio_intent"] = replace(intent, decided_at=final.isoformat(),
            source="research:known_positive_crossing:" + row["case_id"]).to_dict()
    for field, value in (("derived_panels", overlays), ("identity_state", state), ("identity_observations", observations)):
        if _verify(record[field]) != _clean(value):
            raise ValueError("Crossing identity state, episode or source witnesses changed")
        expected[field] = record[field]
    if expected != record:
        raise ValueError("Crossing fixed decision, source or publication changed")
    sessions, _ = shared._calendar(projected)
    paths, _ = shared._rank_path(overlays, set(projected["scope_members"]), projected["source_days"][0], record["source_close_day"])
    day, execution = record["source_close_day"], record["proposed_execution_day"]
    result = {"day": day, "execution": execution, "record": record, "loaded": _own_loaded(record["source"]["input_manifest"], ipo, master, cache),
        "intents": {r["case_id"]: PortfolioIntent.from_dict(r["portfolio_intent"]) for r in record["cases"]},
        "reference": {**proof, "day": day, "recorded_at": record["recorded_at"], "execution_day": execution,
            "input_manifest": record["source"]["input_manifest"], "input_manifest_sha256": record["source"]["input_manifest_sha256"]},
        "paths": paths, "authoritative_panels": overlays}
    cache[key] = result
    return result


def prepare_intent(pub, key, state):
    """The same saved scope rule also checks prior holdings/pending entries."""
    row = next(r for r in pub["record"]["cases"] if r["case_id"] == key)
    intent = pub["intents"][key]
    if row["environment_mode"] == "EXIT_ALL":
        return intent, {"independent_environment_exit_not_suppressed": True}
    held = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
    pending = set(state.get("pending_targets") or {})
    queued = state.get("pending_published_close_intent") or {}
    queued_intent = queued.get("reduced_intent") or queued.get("intent") or {}
    pending.update(s for s, w in (queued_intent.get("weights") or {}).items() if w > 0)
    panels = pub["authoritative_panels"]
    item = next(t for t in panels[pub["day"]]["types"] if t["id"] == TYPE_ID)
    sessions, _ = shared._calendar(pub["loaded"])
    evidence = entry_membership_policy(day=pub["day"], identity=TYPE_ID, item=item,
        selected=row["cap_candidates"], members=pub["loaded"]["scope_members"], sessions=sessions,
        panels=panels, held_symbols=held, pending_symbols=pending)
    if evidence["blocking_reasons"]:
        intent = PortfolioIntent.no_change(";".join(evidence["blocking_reasons"]),
            decided_at=intent.decided_at, context=intent.context,
            execution_not_before=intent.execution_not_before, source=intent.source)
    return intent, evidence


def risk_policy(type_id, *, state=None, **kwargs):
    return shared.PublishedCloseLeaderTypeRiskExitPolicy(type_id, **kwargs)


target_policy = shared.PublishedFixedExpiryReduction


def registration(path, first, config, now):
    reg, proof = _registered(path, now)
    if (first["day"] != reg["first_decision_day"] or first["execution"] != reg["first_execution_day"]
            or first["record"]["paper_registration"] != proof or config.to_dict() != reg["config"]):
        raise ValueError("Original first crossing D/E, four-case registration and capital must remain exact")
    return {**proof, "method_id": METHOD_ID, "registered_at": reg["registered_at"],
        "first_decision_day": reg["first_decision_day"], "first_execution_day": reg["first_execution_day"]}


def forward(publications, panel, ipo, master, run_dir, *, registration_file, through=None):
    import sys
    reg, proof = _registered(registration_file, _now())
    value = shared.run(publications, panel, ipo, master, run_dir,
        registration=registration_file, through=through, method=sys.modules[__name__])
    return {**value, "signal_module": METHOD_ID, "paper_registration": proof,
        "first_decision_day": reg["first_decision_day"], "first_execution_day": reg["first_execution_day"],
        "original_accounts_migrated": False, "separate_trading_framework_created": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reg = commands.add_parser("register")
    for name in ("base-publication", "calendar", "out"):
        reg.add_argument("--" + name, type=Path, required=True)
    reg.add_argument("--first-day", required=True)
    pub = commands.add_parser("publish")
    for name in ("base-publication", "panel", "registration", "ipo-evidence", "research-master-run", "out"):
        pub.add_argument("--" + name, type=Path, required=True)
    pub.add_argument("--previous-publication", type=Path)
    run = commands.add_parser("forward")
    run.add_argument("--publication", type=Path, action="append", default=[])
    for name in ("panel", "ipo-evidence", "research-master-run", "registration", "run-dir"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument("--through"); run.add_argument("--result-json", type=Path)
    args = parser.parse_args(argv)
    if args.command == "register":
        value = register(args.base_publication, args.calendar, args.first_day, args.out)
    elif args.command == "publish":
        value = publish(args.base_publication, args.panel, args.registration, args.ipo_evidence,
            args.research_master_run, args.out, previous_publication=args.previous_publication)
    else:
        from .leader_dated_catchup_forward import _result_export_path
        export = _result_export_path(args.result_json, run_dir=args.run_dir, panel=args.panel,
            publications=args.publication, registration=args.registration,
            ipo_evidence=args.ipo_evidence, research_master_run=args.research_master_run)
        value = forward(args.publication, args.panel, args.ipo_evidence, args.research_master_run,
            args.run_dir, registration_file=args.registration, through=args.through)
        if export is not None:
            write_json(export, value)
    print(json.dumps({k: value[k] for k in ("schema", "method_id", "status", "first_decision_day",
        "first_execution_day", "case_count", "newly_committed_case_sessions") if k in value}, ensure_ascii=False))
    return 0
