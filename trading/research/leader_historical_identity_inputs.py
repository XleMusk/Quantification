"""Explicit posthoc identity research, using saved native panels and one Engine.

This overlay does not recreate old publications or replace the prospective
identity registry. Six old types retain their saved observations. Only the two
new identity hypotheses receive new, actually recorded research observations.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import FLAGS, load_inputs
from .leader_eight_type_panel_replay import (
    CAPS, GUARDS, PROTOCOL, TYPE_IDS, _case, _environment, _rank_path,
    _validate_panel_data,
)
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .stockdb_action_accounting import load_action_accounting
from .shortline_panel_replay import _config


METHOD_ID = "saved_native_posthoc_observed_identity_and_dated_industry_overlay_v1"
REGISTRATION_SCHEMA = "leader_historical_identity_research_registration_v1"
TYPES = ("cross_regime_candidate", "sector_catchup_leader")
REUSED_EIGHT_EXECUTION = "new_two_types_x_original_two_guards_x_original_two_capacities"
STANDALONE_EXECUTION = "all_eight_types_x_original_two_guards_x_original_two_capacities_once"


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _save_new(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _components():
    from .leader_historical_identity_rules import SPEC, HistoricalIdentityRules
    from .leader_posthoc_industry import load
    return SPEC, HistoricalIdentityRules, load


def register(inputs, industry_root, baseline, reuse_evidence, out, *, standalone=False):
    """Record fixed rules/inputs before deriving the new historical labels."""
    spec, _, industry_load = _components()
    path = Path(inputs).resolve()
    baseline = Path(baseline).resolve() if baseline is not None else None
    reuse_evidence = Path(reuse_evidence).resolve() if reuse_evidence is not None else None
    if type(standalone) is not bool or (standalone and (baseline is not None or reuse_evidence is not None)):
        raise ValueError("Standalone all32 research cannot attach baseline or reuse evidence")
    if not standalone and baseline is None:
        raise ValueError("Original eight-case reuse mode requires its saved baseline")
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Identity research registration requires a new output")
    _, manifest = load_inputs(path)
    if manifest.get("identity_research") is not None:
        raise ValueError("New research must start from original saved native observations")
    actual = _now()
    industry = industry_load(industry_root, observed_at=actual)
    exact_parent = False
    if not standalone:
        saved = _read(baseline)
        if (saved.get("case_count") != 32 or saved.get("protocol") != PROTOCOL
                or saved.get("config") != _config(100000, 5).to_dict()
                or saved.get("start") != manifest["start"] or saved.get("end") != manifest["end"]
                or saved.get("scope_members") != manifest["scope_members"]):
            raise ValueError("Original same-protocol/scope/window 32-case evidence required")
        exact_parent = (saved.get("source", {}).get("manifest") == str(path)
            and saved.get("source", {}).get("manifest_sha256") == digest(path))
        if not exact_parent:
            proof = _read(reuse_evidence) if reuse_evidence else {}
            required_pins = {"raw_sha256", "context_raw_sha256", "scope_members", "start", "end", "through", "source_days", "calendar_sha256", "fixed_protocol"}
            if (proof.get("v3_engine_run_performed") is not False
                    or proof.get("v2_actual_result") != str(baseline)
                    or str(path) not in proof.get("manifests", [])
                    or not required_pins <= set(proof.get("pins_equal", {}))
                    or not all(proof["pins_equal"].values())):
                raise ValueError("Different saved parent requires its explicit unchanged-input reuse evidence")
    record = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID,
        "registered_at": actual, "research_kind": "posthoc_causal_price_hypothesis_not_prospective_validation",
        "base_manifest": str(path), "base_manifest_sha256": digest(path),
        "industry_root": str(Path(industry_root).resolve()), "industry_provenance": industry.provenance,
        "baseline_result": str(baseline) if baseline is not None else None,
        "baseline_result_sha256": digest(baseline) if baseline is not None else None,
        "baseline_reuse_evidence": str(reuse_evidence) if reuse_evidence else None,
        "baseline_reuse_evidence_sha256": digest(reuse_evidence) if reuse_evidence else None,
        "baseline_input_relation": "standalone_all32_no_baseline_reuse" if standalone else (
            "exact_same_parent" if exact_parent else "explicit_prior_saved_input_reuse"),
        "rules": deepcopy(spec), "changed_types": list(TYPES),
        "unchanged_types": [identity for identity in TYPE_IDS if identity not in TYPES],
        "fixed_protocol": deepcopy(PROTOCOL),
        "case_execution": STANDALONE_EXECUTION if standalone else REUSED_EIGHT_EXECUTION,
        "old_cases": "no_prior_cases_reused_all32_run_once" if standalone else "retain_original_result_identity_never_duplicate_as_new_replay",
        "thresholds_selected_by_this_window_return": False,
        "independent_holdout_claimed": False, "historical_dated_industry_PIT_verified": False,
        **FLAGS}
    if standalone:
        record.update(standalone=True, all32=True, base_input_created_at=manifest["created_at"])
    _save_new(folder / "registration.json", record)
    return record


def load_registration(path):
    path = Path(path).resolve()
    value = _read(path)
    spec, _, industry_load = _components()
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("rules") != spec or value.get("changed_types") != list(TYPES)
            or value.get("fixed_protocol") != PROTOCOL
            or any(value.get(key) is not flag for key, flag in FLAGS.items())
            or value["registered_at"] > _now()):
        raise ValueError("Explicit unchanged identity research registration required")
    _execution_mode(value)
    for key in ("base_manifest", "baseline_result", "baseline_reuse_evidence"):
        if value.get(key) is not None and digest(value[key]) != value[key + "_sha256"]:
            raise ValueError("Registered saved identity research input changed")
    if value.get("all32") is True and _read(value["base_manifest"]).get("created_at") != value["base_input_created_at"]:
        raise ValueError("Standalone research must retain its actual saved input clock")
    # Re-read the same registered acquisition boundary, not a fresh clock that
    # would change the saved provenance on every load.
    industry = industry_load(value["industry_root"], observed_at=value["registered_at"])
    if industry.provenance != value["industry_provenance"]:
        raise ValueError("Registered original industry inventory or acquisition clocks changed")
    return value, industry


def _execution_mode(value):
    if "all32" in value or "standalone" in value:
        if (value.get("all32") is not True or value.get("standalone") is not True
                or value.get("case_execution") != STANDALONE_EXECUTION
                or value.get("baseline_input_relation") != "standalone_all32_no_baseline_reuse"
                or any(value.get(key) is not None for key in ("baseline_result", "baseline_result_sha256",
                    "baseline_reuse_evidence", "baseline_reuse_evidence_sha256"))):
            raise ValueError("Standalone all32 registration cannot reuse or masquerade as an old baseline")
        return "standalone_all32"
    if (value.get("case_execution") != REUSED_EIGHT_EXECUTION
            or not value.get("baseline_result") or not value.get("baseline_result_sha256")):
        raise ValueError("Original eight-case registration requires its exact retained baseline")
    return "reused_eight"


def _view(day, panel, raw, qualification, *, historical_listing_classification=None):
    """Use original raw fields and saved qualified stage; never refit features."""
    if not raw.date.eq(day).all() or raw.symbol.duplicated().any():
        raise ValueError("Only one exact own-date native group can build a saved identity view")
    records = raw.to_dict("records")
    ordinary = {record["symbol"]: record for record in records}
    features = {}
    for symbol, risk in panel["risk_features"].items():
        original = ordinary.get(symbol)
        if original is None:
            raise ValueError("Saved qualified row is absent from its exact own raw session")
        if risk.get("date") != day or float(original["close"]) != float(risk["close"]):
            raise ValueError("Saved price identity row differs from exact source date/close")
        if risk.get("ipo_feature_support") is True:
            listing = "supported"
        else:
            proof = qualification.assess(symbol, day, decision_as_of=day + "T15:05:00+08:00")
            listing = ("known_listing_window_excluded" if proof["engine_block_reason"] in {
                "unsupported_first_five_listing_sessions", "not_yet_listed", "posthoc_reported_outside_listing_period"}
                else "unknown")
        features[symbol] = {**deepcopy(risk), "symbol": symbol,
            "high": float(original["high"]), "low": float(original["low"]),
            "amount": float(original["amount"]), "pct_chg": float(original["pctChg"]),
            "preclose_for_limits": float(original["preclose"]),
            "stage": risk.get("qualified_stage"), "limit_up": risk.get("qualified_limit_up"),
            "listing_qualification_status": listing}
    result = {"features": features, "ordinary": ordinary, "available_at": panel["available_at"]}
    if historical_listing_classification is not None:
        from .leader_historical_listing_classification import VerifiedHistoricalListingDay, verify_day, DESCRIPTOR
        hook = historical_listing_classification
        if (not isinstance(hook, VerifiedHistoricalListingDay)
                or hook.qualification_verification != qualification.verification):
            raise ValueError("Historical identity exclusions require the exact verified listing sidecar")
        result[DESCRIPTOR] = verify_day(hook.classification, day, raw, hook.scope_members, qualification)
    return result


def derive(registration, out):
    record, industry = load_registration(registration)
    _, cls, _ = _components()
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Identity overlay requires a new separate output")
    parent, manifest = load_inputs(record["base_manifest"])
    listing_classification = None
    if manifest.get("historical_listing_classification") is not None:
        from .leader_historical_listing_classification import load_classification
        listing_classification = load_classification(manifest, parent["qualification"])
    actual = _now()
    tracker = cls(manifest["scope_members"], list(parent["qualification"].calendar), industry, actual, spec=record["rules"])
    panels, observations = {}, {}
    by_day = {day: frame for day, frame in parent["raw"].groupby("date", sort=False)}
    for day in manifest["source_days"]:
        original = parent["panel_by_day"][day]
        listing_day = None
        if listing_classification is not None:
            from .leader_historical_listing_classification import verify_day
            listing_day = verify_day(listing_classification["days"][day], day, by_day[day],
                manifest["scope_members"], parent["qualification"])
        observed = tracker.observe(day, original, _view(day, original, by_day[day], parent["qualification"],
            historical_listing_classification=listing_day))
        replacements = {item["id"]: item for item in observed["types"]}
        if set(replacements) != set(TYPES):
            raise ValueError("Only both registered identity hypotheses can replace saved unknown observations")
        panel = deepcopy(original)
        panel["types"] = [replacements[item["id"]] if item["id"] in replacements else deepcopy(item)
            for item in original["types"]]
        panel["available_at"] = actual
        panel["identity_research_method"] = METHOD_ID
        panels[day], observations[day] = panel, observed["identity_evidence"]
    child = deepcopy(manifest)
    child.update(created_at=_now(), panels_file=str(folder / "panels.json"))
    descriptor = {"method_id": METHOD_ID,
        "registration": str(Path(registration).resolve()), "registration_sha256": digest(registration),
        "parent_manifest": record["base_manifest"], "parent_manifest_sha256": record["base_manifest_sha256"],
        "observations_file": str(folder / "identity_observations.json"),
        "changed_types": list(TYPES), "actual_derived_at": actual,
        "historical_publication_backdated": False, "old_six_type_observations_modified": False}
    _save_new(folder / "panels.json", panels)
    _save_new(folder / "identity_observations.json", observations)
    child["panels_sha256"] = digest(child["panels_file"])
    descriptor["observations_sha256"] = digest(descriptor["observations_file"])
    child["identity_research"] = descriptor
    _save_new(folder / "manifest.json", child)
    return child


def validate_overlay(value, panels):
    descriptor = value["identity_research"]
    if (descriptor.get("method_id") != METHOD_ID or descriptor.get("changed_types") != list(TYPES)
            or descriptor.get("historical_publication_backdated") is not False
            or digest(descriptor["registration"]) != descriptor["registration_sha256"]
            or digest(descriptor["parent_manifest"]) != descriptor["parent_manifest_sha256"]
            or digest(descriptor["observations_file"]) != descriptor["observations_sha256"]):
        raise ValueError("Historical identity overlay's explicit original evidence changed")
    registration, _ = load_registration(descriptor["registration"])
    parent = _read(descriptor["parent_manifest"])
    if (parent.get("identity_research") is not None
            or registration["base_manifest"] != descriptor["parent_manifest"]
            or registration["registered_at"] > descriptor["actual_derived_at"]):
        raise ValueError("Research overlay cannot stack identities or predate its actual registration")
    for key in parent:
        if key not in {"created_at", "panels_file", "panels_sha256"} and value.get(key) != parent[key]:
            raise ValueError("Historical identity overlay changed a source/qualification/protocol field")
    if digest(parent["panels_file"]) != parent["panels_sha256"]:
        raise ValueError("Original saved observations changed")
    original = _read(parent["panels_file"])
    if list(panels) != list(original):
        raise ValueError("Identity overlay cannot invent or skip source sessions")
    for day, panel in panels.items():
        before = original[day]
        if (panel["available_at"] != descriptor["actual_derived_at"]
                or panel.get("identity_research_method") != METHOD_ID):
            raise ValueError("Identity observations need their actual new derivation clock")
        for key, prior in before.items():
            if key not in {"available_at", "types"} and panel.get(key) != prior:
                raise ValueError("Identity overlay changed source/risk/environment observations")
        if [item["id"] for item in panel["types"]] != list(TYPE_IDS):
            raise ValueError("All eight original type slots must remain present")
        for first, second in zip(before["types"], panel["types"]):
            if first["id"] not in TYPES and first != second:
                raise ValueError("Unchanged six types must retain original bytes/values")


def run(inputs, out, *, action_event_view, action_reference_view):
    loaded, manifest = load_inputs(inputs)
    if "identity_research" not in manifest:
        raise ValueError("Only the explicit registered identity overlay can run this eight-case comparison")
    registration, _ = load_registration(manifest["identity_research"]["registration"])
    if _execution_mode(registration) != "reused_eight":
        raise ValueError("Standalone all32 registration requires replay-all, not the old eight-case replay")
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("New identity cases require their own result directory")
    members, sessions, previous, now = _validate_panel_data(
        loaded["panel_by_day"], loaded["raw"], loaded["source_days"], loaded["calendar_payload"],
        loaded["scope_members"], loaded["qualification"])
    paths, original = _rank_path(loaded["panel_by_day"], members, manifest["start"], manifest["end"])
    environments = _environment(loaded["panel_by_day"], sessions, now)
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    accounting = load_action_accounting(action_event_view, action_reference_view,
        symbols=manifest["scope_members"], raw_days=sorted(set(context_raw.date) | set(manifest["source_days"])),
        replay_days=manifest["source_days"], price_source="independent_baostock_raw")
    factory = HistoricalExecutionFactory(accounting)
    cases = [_case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
        paths[identity][cap], environments, members, loaded["qualification"], loaded["context_anchor"],
        manifest["start"], manifest["end"], identity, cap, guard,
        f"historical-identity:{folder.name}:{identity}:{guard}:{cap}",
        execution_policy_factory=factory, context_raw=context_raw)
        for identity in TYPES for guard in GUARDS for cap, _ in CAPS]
    result = {"schema": "leader_historical_identity_eight_case_shared_replay_v1", "method_id": METHOD_ID,
        "created_at": _now(), "start": manifest["start"], "end": manifest["end"],
        "source_days": loaded["source_days"], "source": loaded["provenance"],
        "parent_fixed_protocol": deepcopy(PROTOCOL), "executed_type_ids": list(TYPES),
        "case_count": len(cases), "cases": cases, "case_selection_by_performance": False,
        "shared_case_function": "leader_eight_type_panel_replay._case",
        "unchanged_24_cases_reused_from": {"result": registration["baseline_result"],
            "sha256": registration["baseline_result_sha256"], "input_reuse_evidence": registration["baseline_reuse_evidence"]},
        "environment_decisions": environments, "raw_top5_paths": {key: original[key] for key in TYPES},
        "capacity_paths": {key: paths[key] for key in TYPES}, "execution_policy": factory.provenance,
        "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
        "account_created": True, "persistent_account_used": False,
        "broker_interacted": False, "independent_holdout_claimed": False, **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "result.json", result)
    for case in cases:
        case_dir = folder / case["case_id"]
        case_dir.mkdir()
        for key in ("decisions", "daily", "orders", "fills", "account"):
            write_json(case_dir / (key + ".json"), case[key])
    return result


def run_all(inputs, out, *, action_event_view, action_reference_view):
    """Use the original 32-case runner once, without a preceding baseline run."""
    path = Path(inputs).resolve()
    path = path / "manifest.json" if path.is_dir() else path
    manifest = _read(path)
    descriptor = manifest.get("identity_research")
    if not isinstance(descriptor, dict):
        raise ValueError("Standalone all32 replay requires its derived identity overlay")
    registration, _ = load_registration(descriptor["registration"])
    if _execution_mode(registration) != "standalone_all32":
        raise ValueError("Old eight-case reuse registration cannot be expanded into replay-all")
    from .leader_eight_type_historical_native import run as shared_run
    result = shared_run(path, out, action_event_view=action_event_view, action_reference_view=action_reference_view)
    if result.get("case_count") != 32:
        raise ValueError("The shared standalone runner did not save all 32 original protocol cases")
    _save_new(Path(out) / "identity_method.json", {
        "schema": "leader_historical_identity_all32_shared_replay_v1", "method_id": METHOD_ID,
        "created_at": _now(), "case_execution": STANDALONE_EXECUTION, "all32": True,
        "registration": descriptor["registration"], "registration_sha256": digest(descriptor["registration"]),
        "input_manifest": str(path), "input_manifest_sha256": digest(path),
        "shared_result": str(Path(out).resolve() / "result.json"), "shared_result_sha256": digest(Path(out) / "result.json"),
        "shared_runner": "leader_eight_type_historical_native.run", "shared_runner_calls": 1,
        "baseline_replayed": False, "cases_reused_from_prior_replay": 0,
        "changed_identity_types": list(TYPES), "unchanged_types": registration["unchanged_types"],
        "original_32_protocol_unchanged": True, "fixed_protocol": deepcopy(PROTOCOL),
        "case_selection_by_performance": False, **FLAGS})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    registration = commands.add_parser("register")
    for name in ("inputs", "industry-root", "out"):
        registration.add_argument("--" + name, type=Path, required=True)
    registration.add_argument("--baseline", type=Path)
    registration.add_argument("--reuse-evidence", type=Path)
    registration.add_argument("--standalone", action="store_true")
    build = commands.add_parser("derive")
    build.add_argument("--registration", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    for command in ("replay", "replay-all"):
        replay = commands.add_parser(command)
        for name in ("inputs", "action-event-view", "action-reference-view", "out"):
            replay.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.inputs, args.industry_root, args.baseline, args.reuse_evidence, args.out,
            standalone=args.standalone)
        summary = {"registered_at": result["registered_at"], "method_id": METHOD_ID}
    elif args.command == "derive":
        result = derive(args.registration, args.out)
        summary = {"manifest": str(args.out / "manifest.json"), "source_days": len(result["source_days"])}
    else:
        replay_function = run_all if args.command == "replay-all" else run
        result = replay_function(args.inputs, args.out, action_event_view=args.action_event_view,
            action_reference_view=args.action_reference_view)
        summary = {"cases": result["case_count"], "cases_with_fills": sum(bool(c["fills"]) for c in result["cases"])}
    print(json.dumps({**summary, **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
