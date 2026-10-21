"""Register and publish immutable D-only inputs for four daily leader routers.

The publication contains route evidence, not an assumed empty personal book.
Only the separately registered consumer binds it to actual research holdings.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from . import leader_type_router as router
from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import (CAPS, GUARDS, INPUT_SCHEMA, PROTOCOL,
    _config, _environment, _rank_path, load_inputs)
from .leader_eight_type_paper import _source_bindings, _stamp
from .leader_eight_type_forward_run import (_calendar, _facts, _prefix,
    _publication as _base_publication, _same_lineage)


METHOD_ID = "single_active_type_actual_published_close_daily_v1"
REGISTRATION_SCHEMA = METHOD_ID + "_registration"
PUBLICATION_SCHEMA = METHOD_ID + "_publication"
CASE_IDS = tuple("single_active_type__" + guard + "__" + capacity
    for guard in GUARDS for capacity, _ in CAPS)
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "current_signal_admitted": False, "returns_reportable": False,
    "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "broker_interacted": False, "user_account_mutated": False,
    "ths_payload_generated": False, "pit_verified": False, "corporate_actions_verified": False}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path)}


def _checked(reference):
    if _pin(reference["path"]) != reference:
        raise ValueError("Registered router input or code changed: " + reference["path"])
    return Path(reference["path"])


def _runtime():
    root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__), Path(__file__).with_name("leader_type_router_forward.py"),
        Path(router.__file__), root / "published_close_router.py", root / "engine.py",
        root / "contracts.py", root / "ledger.py",
        Path(__file__).with_name("leader_eight_type_panel_replay.py"),
        Path(__file__).with_name("leader_type_risk_exit.py"),
        Path(__file__).with_name("leader_published_risk_exit.py")]
    return [_pin(path) for path in paths]


def _spec():
    return {"method_id": METHOD_ID, "routing_method": router.METHOD_ID,
        "routing_rules": deepcopy(router.SPEC), "original_protocol": deepcopy(PROTOCOL),
        "case_ids": list(CASE_IDS), "config": _config(100000, 5).to_dict(),
        "actual_publication": "own_D_inputs_saved_before_real_clock_never_legacy_1505",
        "first_decision": "registered_before_first_D_close_no_late_start_or_roll",
        "binding": "saved_D_evidence_plus_actual_pre_D_research_state_postopen_owner_decision",
        "candidate_completeness": "D_only_no_successor_raw_requirement",
        "execution": "exact_next_calendar_E_validated_only_when_E_is_consumed",
        "unknown": "NO_CHANGE_preserves_shares_independent_known_D_exit_remains_allowed",
        "source": "original_32_publication_and_same_immutable_input_prefix",
        "accounting": "same_Engine_Ledger_four_independent_100000_books_no_model_or_case_selection",
        "metrics": "unfinished_daily_research_metrics_null", **FLAGS}


def _new_directory(out, references):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new immutable router publication/registration directory")
    for reference in references:
        path = Path(reference["path"]).resolve()
        if out == path or out in path.parents or path in out.parents:
            raise ValueError("Output must not overwrite or nest in an input file")
    return out


def register(anchor_publication, inputs, ipo_evidence, research_master_run, out, *, first_day):
    """Metadata-only inception registration; no model, source or account work."""
    references = {name: _pin(path) for name, path in (
        ("anchor_publication", anchor_publication), ("anchor_inputs", inputs),
        ("ipo_evidence", ipo_evidence), ("research_master_run", research_master_run))}
    anchor, manifest = _read(anchor_publication), _read(inputs)
    calendar_path = (Path(inputs).resolve().parent / manifest["calendar"]).resolve()
    references["calendar"] = _pin(calendar_path)
    protocol_path = (Path(inputs).resolve().parent / manifest["protocol_file"]).resolve()
    references["protocol"] = _pin(protocol_path)
    calendar = _read(calendar_path)
    sessions, _, status = _calendar_view(calendar, f"{calendar['year']}-01-01",
        f"{calendar['year']}-12-31", [])
    now = _now()
    anchor_source = anchor.get("source", {})
    if (manifest.get("schema") != INPUT_SCHEMA
            or anchor.get("schema") != "leader_eight_type_unbound_close_paper_plan_v1"
            or anchor.get("protocol") != PROTOCOL or anchor.get("case_count") != 32
            or anchor.get("execution_admitted") is not False
            or anchor.get("source_close_day") != manifest["source_days"][-1]
            or Path(anchor_source.get("input_manifest", "")).resolve() != Path(inputs).resolve()
            or anchor_source.get("input_manifest_sha256") != references["anchor_inputs"]["sha256"]
            or digest(calendar_path) != manifest["calendar_sha256"]
            or digest(protocol_path) != manifest["protocol_sha256"]
            or status != "pinned_static_exchange_calendar" or first_day not in sessions
            or sessions.index(first_day) + 1 >= len(sessions)
            or sessions[sessions.index(first_day) - 1] != manifest["source_days"][-1]
            or not _stamp(anchor["recorded_at"]) <= now < _stamp(first_day + "T15:00:00+08:00")):
        raise ValueError("Exact original anchor and registration before first D close required")
    if any(manifest.get(key) is not value for key, value in
            {"candidate_panel_only": True, "execution_admitted": False,
             "selection_enabled": False, "strategy_validated": False}.items()):
        raise ValueError("Anchor must retain the original unvalidated research boundary")
    out = _new_directory(out, list(references.values()))
    code = _runtime()
    result = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID,
        "registered_at": now.isoformat(), "first_decision_day": first_day,
        "first_execution_day": sessions[sessions.index(first_day) + 1],
        "rotation_anchor_day": first_day, "scope_members": manifest["scope_members"],
        "scope_frozen_at": manifest["scope_frozen_at"], "references": references,
        "anchor_source_prefix": deepcopy(manifest["source_proofs"]),
        "spec": _spec(), "code": code, "config": _config(100000, 5).to_dict(),
        "case_ids": list(CASE_IDS), "accounts_created": False, "engine_advanced": False,
        "orders_generated": False, "raw_loaded": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "registration.json", result)
    return result


def registration(path, *, now=None):
    value = _read(path)
    now = now or _now()
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("spec") != _spec() or value.get("code") != _runtime()
            or value.get("config") != _config(100000, 5).to_dict()
            or value.get("case_ids") != list(CASE_IDS)
            or not _stamp(value["registered_at"]) < _stamp(value["first_decision_day"] + "T15:00:00+08:00")
            or _stamp(value["registered_at"]) > now
            or any(value.get(key) is not flag for key, flag in FLAGS.items())):
        raise ValueError("Original daily router registration, method/code and actual clock required")
    for reference in value["references"].values():
        _checked(reference)
    return value


def _load_inputs(path, record):
    refs = record["references"]
    return load_inputs(path, refs["ipo_evidence"]["path"], refs["research_master_run"]["path"])


def _own_inputs(inputs, out):
    """Copy the exact saved raw and manifest, keeping all source clocks intact."""
    inputs = Path(inputs).resolve()
    before, manifest = digest(inputs), _read(inputs)
    out.mkdir(parents=True, exist_ok=False)
    raw = (inputs.parent / manifest["raw_file"]).resolve()
    raw_copy = out / "raw_daily.parquet"
    shutil.copyfile(raw, raw_copy)
    if digest(raw_copy) != manifest["raw_sha256"]:
        raise ValueError("Own publication raw bytes changed during copy")
    manifest["raw_file"] = str(raw_copy)
    for key in ("calendar", "protocol_file", "history_snapshot"):
        manifest[key] = str((inputs.parent / manifest[key]).resolve())
    identity = manifest["identity_report"]
    identity_copy = out / "identity_report.json"
    shutil.copyfile(identity["file"], identity_copy)
    if digest(identity_copy) != identity["sha256"] or digest(inputs) != before:
        raise ValueError("Own identity or original input changed during copy")
    identity["file"] = str(identity_copy)
    path = out / "manifest.json"
    write_json(path, manifest)
    return path


def _route_data(loaded, day):
    sessions, _ = _calendar(loaded)
    paths, _ = _rank_path(loaded["panel_by_day"], set(loaded["scope_members"]),
        loaded["source_days"][0], day)
    environment = _environment(loaded["panel_by_day"], sessions,
        max(panel["available_at"] for panel in loaded["panel_by_day"].values()))[day]
    return {"environment": environment,
        "paths": {identity: {cap: paths[identity][cap][day] for cap, _ in CAPS}
                  for identity in router.TYPE_IDS}}


def publish(registration_path, inputs, base_publication, out):
    registered = registration(registration_path)
    out = _new_directory(out, [_pin(inputs), _pin(base_publication), _pin(registration_path)])
    observed, refs, cache = _now(), registered["references"], {}
    base = _base_publication(base_publication, refs["ipo_evidence"]["path"],
        refs["research_master_run"]["path"], observed, cache)
    if base.get("unbindable"):
        raise ValueError("An actual timely original32 D publication is required; never late-publish or roll")
    loaded = _load_inputs(inputs, registered)
    _same_lineage(base["loaded"], loaded)
    day = loaded["source_days"][-1]
    if day != base["day"] or day < registered["first_decision_day"]:
        raise ValueError("Own input and original32 publication must have the same registered D")
    if [p for p in loaded["provenance"]["source_proofs"]
            if p["day"] <= registered["anchor_source_prefix"][-1]["day"]] != registered["anchor_source_prefix"]:
        raise ValueError("Original anchor source prefix changed")
    out.mkdir(parents=True, exist_ok=False)
    own_path = _own_inputs(inputs, out / "own_inputs")
    own = _load_inputs(own_path, registered)
    _same_lineage(loaded, own)
    if any(_facts(loaded["panel_by_day"][d]) != _facts(own["panel_by_day"][d]) for d in loaded["source_days"]):
        raise ValueError("Own publication candidate facts changed during save")
    bindings = _source_bindings(own, _now())
    route_data = _route_data(own, day)
    recorded = _now()  # Deliberately after own files and candidate validation.
    sessions, _ = _calendar(own)
    execution = sessions[sessions.index(day) + 1]
    if (recorded < observed or recorded.date().isoformat() != day
            or recorded < _stamp(day + "T15:05:00+08:00")
            or not _stamp(base["reference"]["recorded_at"]) <= recorded < _stamp(execution + "T09:30:00+08:00")
            or _stamp(bindings["input_recorded_at"]) > recorded):
        raise ValueError("Publication requires its actual D evening; no backfill or shifted execution")
    result = {"schema": PUBLICATION_SCHEMA, "status": "saved_router_D_evidence_pending_account_binding",
        "method_id": METHOD_ID, "registration": _pin(registration_path),
        "base_publication": deepcopy(base["reference"]), "source_close_day": day,
        "proposed_execution_day": execution, "recorded_at": recorded.isoformat(),
        "publication_at": recorded.isoformat(), "publication_time_backfilled": False,
        "publication_clock_basis": "actual_now_after_own_immutable_inputs_saved",
        "source": bindings, "source_prefix": _prefix(own, day), "routing_inputs": route_data,
        "protocol": deepcopy(PROTOCOL), "spec": registered["spec"], "case_count": len(CASE_IDS),
        "cases": [{"case_id": key, "guard": guard, "capacity": cap,
                   "active_type": None, "account_binding_pending": True,
                   "target_weights": None, "metrics": None}
                  for key, (guard, cap) in zip(CASE_IDS, ((g, c) for g in GUARDS for c, _ in CAPS))],
        "successor_raw_required": False, "successor_raw_used": False,
        "accounts_created": False, "engine_advanced": False, "orders_generated": False, **FLAGS}
    write_json(out / "publication.json", result)
    return result


def load_publication(path, registered, registration_ref, *, now=None, cache=None):
    path, cache, now = Path(path).resolve(), {} if cache is None else cache, now or _now()
    before, value = digest(path), _read(path)
    if (value.get("schema") != PUBLICATION_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("registration") != registration_ref or value.get("spec") != registered["spec"]
            or value.get("protocol") != PROTOCOL or value.get("case_count") != 4
            or [row.get("case_id") for row in value.get("cases", [])] != list(CASE_IDS)
            or value.get("publication_time_backfilled") is not False
            or value.get("successor_raw_used") is not False
            or any(value.get(key) is not flag for key, flag in FLAGS.items())):
        raise ValueError("Saved router publication changed its registered rules or research boundary")
    day, execution = value["source_close_day"], value["proposed_execution_day"]
    stamp = _stamp(value["recorded_at"])
    if (not _stamp(day + "T15:05:00+08:00") <= stamp < _stamp(execution + "T09:30:00+08:00")
            or stamp.date().isoformat() != day or stamp > now
            or _stamp(registered["registered_at"]) > stamp
            or value["publication_at"] != value["recorded_at"]):
        raise ValueError("Original actual publication clock cannot be changed or rolled")
    source = value["source"]
    own_path = str(Path(source["input_manifest"]).resolve())
    if digest(own_path) != source["input_manifest_sha256"]:
        raise ValueError("Immutable own input manifest changed")
    if own_path not in cache:
        cache[own_path] = _load_inputs(own_path, registered)
    loaded = cache[own_path]
    sessions, _ = _calendar(loaded)
    if (loaded["source_days"][-1] != day or sessions[sessions.index(day) + 1] != execution
            or loaded["scope_members"] != registered["scope_members"]
            or _source_bindings(loaded, now) != source
            or _prefix(loaded, day) != value["source_prefix"]
            or _route_data(loaded, day) != value["routing_inputs"]):
        raise ValueError("Own D source, calendar, original capacity or environment facts changed")
    base_ref = value["base_publication"]
    if digest(base_ref["file"]) != base_ref["sha256"]:
        raise ValueError("Original32 actual publication bytes changed")
    base = _base_publication(base_ref["file"], registered["references"]["ipo_evidence"]["path"],
        registered["references"]["research_master_run"]["path"], now, cache)
    if (base.get("unbindable") or base["reference"] != base_ref or base["day"] != day
            or _stamp(base_ref["recorded_at"]) > stamp):
        raise ValueError("Saved actual original32 publication binding changed")
    _same_lineage(base["loaded"], loaded)
    if digest(path) != before:
        raise ValueError("Publication changed during read")
    return {"day": day, "execution": execution, "record": value, "loaded": loaded,
        "reference": {"day": day, "file": str(path), "sha256": before,
            "recorded_at": value["recorded_at"], "execution_day": execution,
            "input_manifest": own_path, "input_manifest_sha256": digest(own_path)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    declaration = actions.add_parser("register")
    for name in ("anchor-publication", "inputs", "ipo-evidence", "research-master-run", "out"):
        declaration.add_argument("--" + name, required=True, type=Path)
    declaration.add_argument("--first-day", required=True)
    publication = actions.add_parser("publish")
    for name in ("registration", "inputs", "base-publication", "out"):
        publication.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    value = (register(args.anchor_publication, args.inputs, args.ipo_evidence,
        args.research_master_run, args.out, first_day=args.first_day) if args.action == "register"
        else publish(args.registration, args.inputs, args.base_publication, args.out))
    print(json.dumps({key: value[key] for key in ("schema", "method_id", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
