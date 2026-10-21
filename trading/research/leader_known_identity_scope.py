"""Explicit candidate-information scope contrast on saved crossing identity facts.

The original full-scope accounts remain untouched.  This method changes only
whether unrelated unknown identities prevent entry, never the stored unknowns,
ranking, risk exits, market/industry denominators or execution accounting.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path

import pandas as pd

from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import FLAGS, load_inputs
from .leader_eight_type_panel_replay import CAPS, GUARDS, PROTOCOL, TYPE_IDS, _case, _environment, _rank_path, _validate_panel_data
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .leader_historical_identity_inputs import _now, _read, _save_new
from .leader_historical_identity_rules import METHOD_ID as IDENTITY_METHOD, OLD_TYPES
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting


TYPE_ID = "cross_regime_candidate"
METHOD_ID = "known_positive_crossing_candidate_identity_scope_v1"
REGISTRATION_SCHEMA = "leader_known_identity_scope_registration_v1"
SCHEMA = "leader_known_identity_scope_four_case_replay_v1"
SPEC = {"id": METHOD_ID, "type": TYPE_ID, "case_count": 4,
    "information_policy": "selected_matched_known_positive_candidates_only_unrelated_unknown_retained",
    "owned_or_pending_identity_unknown": "NO_CHANGE_independent_risk_exits_unchanged",
    "selected_identity_unknown_or_incomplete_proof": "whole_case_NO_CHANGE_no_refill",
    "empty_candidates": "NO_CHANGE_not_liquidation", "ranking_capacity_and_guard": "unchanged_original_paths",
    "confirmation": "original_exact_three_consecutive_closes_two_relative_legs_prior_known_positive_identity",
    "market_industry_denominators": "unchanged_original_full_denominators_never_known_subset",
    "signal_thresholds_or_costs_changed": False, "unknown_members_reclassified": False,
    "independent_holdout_claimed": False, "parameters_selected_by_PnL": False}
CASE_IDS = tuple(f"{TYPE_ID}__{guard}__{cap}" for guard in GUARDS for cap, _ in CAPS)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _proof(row, day, sessions, panels):
    """Check the existing positive witness; never recalculate relative ranks."""
    symbol, evidence = row.get("symbol"), row.get("evidence", {})
    days, legs = evidence.get("episode_close_days"), evidence.get("legs")
    if (row.get("source_day") != day or row.get("date") != day
            or row.get("historical_identity_method") != IDENTITY_METHOD
            or evidence.get("status") != "matched" or evidence.get("reason") is not None
            or not isinstance(days, list) or len(days) != 3
            or days != sorted(set(days)) or any(d not in sessions for d in days)
            or evidence.get("confirmation_day") != days[-1] or days[-1] > day
            or not isinstance(legs, list) or len(legs) != 2):
        return "selected_crossing_identity_exact_witness_unknown"
    index = sessions.index(days[0])
    if sessions[index:index + 3] != days or index == 0:
        return "selected_crossing_identity_exact_witness_unknown"
    original = evidence.get("original_proxy_identity", {})
    if (original.get("symbol") != symbol or original.get("identity_source_day") != sessions[index - 1]
            or not set(original.get("labels", [])).intersection(OLD_TYPES)
            or evidence.get("episode_id") != IDENTITY_METHOD + ":" + days[0]):
        return "selected_crossing_prior_identity_unknown"
    anchor = evidence.get("structure_close")
    if not _finite(anchor) or anchor <= 0:
        return "selected_crossing_structure_unknown"
    for d in days + [day]:
        panel = panels.get(d, {})
        feature = panel.get("risk_features", {}).get(symbol, {})
        if (panel.get("environment", {}).get("source_complete") is not True
                or feature.get("date") != d or feature.get("ipo_feature_support") is not True
                or not _finite(feature.get("close")) or feature["close"] <= 0):
            return "selected_crossing_own_episode_qualification_unknown"
    if (panels[days[0]]["risk_features"][symbol]["close"] != anchor
            or panels[days[-1]]["risk_features"][symbol]["close"] < anchor
            or panels[day]["risk_features"][symbol]["close"] < anchor):
        return "selected_crossing_structure_witness_conflict"
    for leg, previous, current in zip(legs, days[:-1], days[1:]):
        if (leg.get("from_day") != previous or leg.get("to_day") != current
                or any(not _finite(leg.get(k)) for k in ("provider_pct", "prior_raw_close", "native_preclose",
                    "market_median_pct", "sector_median_pct", "sector_denominator"))
                or leg["sector_denominator"] <= 0 or int(leg["sector_denominator"]) != leg["sector_denominator"]
                or leg["prior_raw_close"] != panels[previous]["risk_features"][symbol]["close"]
                or leg["native_preclose"] <= 0 or leg["prior_raw_close"] <= 0
                or abs(leg["native_preclose"] / leg["prior_raw_close"] - 1) > .01 + 1e-12
                or leg["provider_pct"] < max(leg["market_median_pct"], leg["sector_median_pct"])
                or leg.get("reference_status") != "original_one_percent_proxy_not_absence_of_actions"):
            return "selected_crossing_relative_leg_witness_unknown"
    return None


def entry_membership_policy(*, day, identity, item, selected, members, sessions, panels,
                            held_symbols, pending_symbols):
    if identity != TYPE_ID:
        raise ValueError("Known-positive identity scope is registered only for crossing candidates")
    unresolved = sorted(set((item or {}).get("unresolved_symbols", [])) & set(members))
    selected_symbols = {row["symbol"] for row in selected}
    owned_unknown = sorted(set(unresolved) & (set(held_symbols) | set(pending_symbols)))
    selected_unknown = sorted(set(unresolved) & selected_symbols)
    reasons, proofs = [], {}
    if owned_unknown:
        reasons.append("owned_or_pending_identity_unknown_NO_CHANGE")
    if selected_unknown:
        reasons.append("selected_identity_unknown_NO_CHANGE")
    for row in selected:
        problem = _proof(row, day, sessions, panels)
        proofs[row["symbol"]] = {"status": "known_positive_witness" if problem is None else "unknown",
            "reason": problem, "original_evidence": deepcopy(row.get("evidence"))}
        if problem is not None and problem not in reasons:
            reasons.append(problem)
    return {"method_id": METHOD_ID, "blocking_reasons": reasons,
        "original_unresolved_symbols": unresolved, "original_scope_complete": (item or {}).get("scope_complete"),
        "selected_symbols": sorted(selected_symbols), "selected_unknown_symbols": selected_unknown,
        "owned_or_pending_unknown_symbols": owned_unknown,
        "unrelated_unknown_symbols": sorted(set(unresolved) - selected_symbols - set(held_symbols) - set(pending_symbols)),
        "unknown_members_reclassified": False, "original_panel_modified": False,
        "full_fixed_scope_complete_claimed": False, "selected_witnesses": proofs}


def _bound(inputs, baseline, action_event_view, action_reference_view):
    path, baseline_path = Path(inputs).resolve(), Path(baseline).resolve()
    manifest, saved = _read(path), _read(baseline_path)
    if (manifest.get("identity_research") is None
            or saved.get("source", {}).get("manifest") != str(path)
            or saved["source"].get("manifest_sha256") != digest(path)
            or saved.get("start") != manifest["start"] or saved.get("end") != manifest["end"]
            or saved.get("source_days") != manifest["source_days"]):
        raise ValueError("Exact saved identity input and original same-protocol baseline required")
    schema = saved.get("schema")
    if schema == "leader_historical_identity_eight_case_shared_replay_v1":
        if saved.get("parent_fixed_protocol") != PROTOCOL:
            raise ValueError("Exact original eight-case identity protocol required")
        accounting = saved.get("execution_policy", {}).get("accounting", {})
    elif schema in ("leader_environment_exit_ablation_shared_replay_v1",
                    "leader_eight_type_candidate_panel_replay_v1"):
        _bound_identity_descriptor(manifest)
        expected = [f"{kind}__{guard}__{cap}" for kind in TYPE_IDS for guard in GUARDS for cap, _ in CAPS]
        if schema == "leader_environment_exit_ablation_shared_replay_v1":
            from .leader_environment_exit_ablation import CASE_IDS as ALL_CASE_IDS, METHOD_ID as ABLATION_METHOD, SPEC as ABLATION_SPEC
            expected = list(ALL_CASE_IDS)
            if (saved.get("method_id") != ABLATION_METHOD or saved.get("spec") != ABLATION_SPEC
                    or saved.get("parent_fixed_protocol") != PROTOCOL):
                raise ValueError("Exact fixed 48-case environment study required; its control guard is not inherited")
        else:
            marker = _read(baseline_path.parent / "identity_method.json")
            if (saved.get("protocol") != PROTOCOL
                    or marker.get("schema") != "leader_historical_identity_all32_shared_replay_v1"
                    or marker.get("method_id") != manifest["identity_research"]["method_id"]
                    or marker.get("all32") is not True or marker.get("fixed_protocol") != PROTOCOL
                    or marker.get("input_manifest") != str(path) or marker.get("input_manifest_sha256") != digest(path)
                    or marker.get("shared_result") != str(baseline_path)
                    or marker.get("shared_result_sha256") != digest(baseline_path)):
                raise ValueError("Exact saved all32 identity result and method marker required")
        if (saved.get("case_count") != len(expected) or [c.get("case_id") for c in saved.get("cases", [])] != expected
                or saved.get("config") != _config(100000, 5).to_dict()
                or saved.get("scope_members") != manifest["scope_members"]
                or any((c.get("type"), c.get("guard"), c.get("capacity")) != tuple(c["case_id"].split("__"))
                    or c.get("config") != saved["config"] for c in saved["cases"])):
            raise ValueError("All original baseline cases, scope and costs must remain exact")
        accounting = saved.get("historical_execution_policy", {}).get("accounting", {})
    else:
        raise ValueError("Unsupported saved historical identity baseline schema")
    # Only the original two guards enter this comparison, including when the
    # saved source contains the separate environment-exit-ablation control.
    cases = [case for case in saved["cases"] if case.get("case_id") in CASE_IDS]
    if (len(cases) != 4 or {case["case_id"] for case in cases} != set(CASE_IDS)
            or any(case.get("config") != _config(100000, 5).to_dict()
                or case.get("type") != TYPE_ID
                or case.get("guard", case["case_id"].split("__")[1]) != case["case_id"].split("__")[1]
                or case.get("capacity", case["case_id"].split("__")[2]) != case["case_id"].split("__")[2]
                or not isinstance(case.get("orders"), list) or not isinstance(case.get("fills"), list)
                for case in cases)):
        raise ValueError("Retain all four original crossing baselines without performance filtering or replay")
    for case in cases:
        if case.get("status") == "skipped_no_possible_entry_from_saved_panel" and (
                case.get("account") is not None or case.get("metrics") is not None
                or case.get("engine_sessions_committed") != 0 or case.get("orders") != []
                or case.get("fills") != [] or case.get("daily") != []
                or case.get("entry_readiness", {}).get("potential_entry_days") != 0):
            raise ValueError("Skipped baseline must remain unavailable with no simulated account")
    for key, folder in (("event_view", action_event_view), ("reference_view", action_reference_view)):
        folder = Path(folder).resolve()
        if Path(accounting.get(key, "")).resolve() != folder:
            raise ValueError("Exact original accounting action/reference pair required")
        pins = {str(Path(p["path"]).resolve()): p["sha256"] for p in accounting.get("refs", [])}
        file = folder / "result.json"
        if pins.get(str(file)) != digest(file):
            raise ValueError("Original accounting pair changed")
    return manifest, saved, cases, accounting


def _bound_identity_descriptor(manifest):
    """Read only the saved identity registration, not panels or price data."""
    from .leader_historical_identity_inputs import METHOD_ID as OVERLAY_METHOD, TYPES
    from .leader_historical_identity_rules import SPEC as IDENTITY_SPEC
    descriptor = manifest.get("identity_research", {})
    if (descriptor.get("method_id") != OVERLAY_METHOD or descriptor.get("changed_types") != list(TYPES)
            or descriptor.get("historical_publication_backdated") is not False
            or descriptor.get("old_six_type_observations_modified") is not False
            or not descriptor.get("registration")
            or digest(descriptor["registration"]) != descriptor.get("registration_sha256")):
        raise ValueError("Exact saved two-type historical identity overlay required")
    original = _read(descriptor["registration"])
    if (original.get("method_id") != OVERLAY_METHOD or original.get("rules") != IDENTITY_SPEC
            or original.get("fixed_protocol") != PROTOCOL):
        raise ValueError("Historical identity rules or original protocol differ")


def register(inputs, baseline, action_event_view, action_reference_view, out):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("New information-scope registration requires a new directory")
    manifest, _, cases, accounting = _bound(inputs, baseline, action_event_view, action_reference_view)
    record = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": _now(),
        "research_kind": "new_information_policy_on_previously_inspected_historical_input_posthoc_diagnostic",
        "inputs": str(Path(inputs).resolve()), "inputs_sha256": digest(inputs),
        "baseline": str(Path(baseline).resolve()), "baseline_sha256": digest(baseline),
        "action_event_view": str(Path(action_event_view).resolve()),
        "action_reference_view": str(Path(action_reference_view).resolve()),
        "accounting": accounting, "policy": deepcopy(SPEC), "parent_fixed_protocol": deepcopy(PROTOCOL),
        "start": manifest["start"], "end": manifest["end"], "case_ids": list(CASE_IDS),
        "retained_baseline_case_ids": [c["case_id"] for c in cases],
        "baseline_replayed": False, "independent_holdout_claimed": False, "returns_reportable": False, **FLAGS}
    _save_new(folder / "registration.json", record)
    return record


def load_registration(path):
    record = _read(path)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("policy") != SPEC or record.get("parent_fixed_protocol") != PROTOCOL
            or record.get("case_ids") != list(CASE_IDS) or record.get("registered_at", "9999") > _now()
            or any(record.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Explicit unchanged known-positive scope registration required")
    for key in ("inputs", "baseline"):
        if digest(record[key]) != record[key + "_sha256"]:
            raise ValueError("Registered information-scope source changed")
    _, _, cases, accounting = _bound(record["inputs"], record["baseline"],
        record["action_event_view"], record["action_reference_view"])
    if record["accounting"] != accounting:
        raise ValueError("Registered accounting boundary changed")
    return record, cases


def run(registration, out):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Independent scope comparison requires a new result directory")
    record, baseline_cases = load_registration(registration)
    loaded, manifest = load_inputs(record["inputs"])
    members, sessions, previous, now = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
        loaded["source_days"], loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
    paths, original = _rank_path(loaded["panel_by_day"], members, manifest["start"], manifest["end"])
    environments = _environment(loaded["panel_by_day"], sessions, now)
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    accounting = load_action_accounting(record["action_event_view"], record["action_reference_view"],
        symbols=manifest["scope_members"], raw_days=sorted(set(context_raw.date) | set(manifest["source_days"])),
        replay_days=manifest["source_days"], price_source="independent_baostock_raw")
    if accounting.provenance != record["accounting"]:
        raise ValueError("New scope cases must use the exact original accounting boundaries")
    factory = HistoricalExecutionFactory(accounting)
    cases = [_case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
        paths[TYPE_ID][cap], environments, members, loaded["qualification"], loaded["context_anchor"],
        manifest["start"], manifest["end"], TYPE_ID, cap, guard,
        f"known-identity-scope:{folder.name}:{TYPE_ID}:{guard}:{cap}",
        execution_policy_factory=factory, context_raw=context_raw,
        entry_membership_policy=entry_membership_policy) for guard in GUARDS for cap, _ in CAPS]
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "created_at": _now(),
        "registration": str(Path(registration).resolve()), "registration_sha256": digest(registration),
        "source": loaded["provenance"], "start": manifest["start"], "end": manifest["end"],
        "source_days": loaded["source_days"], "scope_members": manifest["scope_members"],
        "parent_fixed_protocol": deepcopy(PROTOCOL), "entry_information_policy": deepcopy(SPEC),
        "case_count": 4, "cases": cases, "retained_baseline_cases": baseline_cases,
        "baseline_replayed": False, "new_cases_executed": 4, "execution_policy": factory.provenance,
        "raw_top5_paths": {TYPE_ID: original[TYPE_ID]}, "capacity_paths": {TYPE_ID: paths[TYPE_ID]},
        "environment_decisions": environments, "shared_case_function": "leader_eight_type_panel_replay._case",
        "independent_holdout_claimed": False, "returns_reportable": False, **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "result.json", result)
    for case in cases:
        case_dir = folder / case["case_id"]
        case_dir.mkdir()
        for key in ("decisions", "daily", "orders", "fills", "account"):
            write_json(case_dir / (key + ".json"), case[key])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register")
    for name in ("inputs", "baseline", "action-event-view", "action-reference-view", "out"):
        add.add_argument("--" + name, type=Path, required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--registration", type=Path, required=True)
    replay.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.inputs, args.baseline, args.action_event_view, args.action_reference_view, args.out)
        summary = {"method_id": METHOD_ID, "registered_at": result["registered_at"], "case_count": 4}
    else:
        result = run(args.registration, args.out)
        summary = {"method_id": METHOD_ID, "case_count": result["case_count"],
            "cases": [{k: c[k] for k in ("case_id", "status", "engine_sessions_committed", "complete_PnL_available")}
                for c in result["cases"]]}
    print(json.dumps(summary, ensure_ascii=False))
    return 0
