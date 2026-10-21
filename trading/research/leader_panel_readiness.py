"""Necessary entry conditions in saved leader panels, without account replay.

Possible entry means only that the original panel/rank/environment branches
can reach a target.  Raw quotes, actions, IPO qualification, cash, fills and
later holding exits are deliberately not evaluated here.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .leader_eight_type_historical_native import load_inputs
from .leader_eight_type_panel_replay import CAPS, GUARDS, TYPE_IDS, _environment, _rank_path, _stamp


SCHEMA = "saved_leader_panel_entry_readiness_v1"
CONTROL_GUARD = "two_close_entry_filter_only"
FLAGS = {"candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False,
    "returns_reportable": False, "engine_called": False, "account_created": False,
    "provider_contacted": False, "persistent_ledger_mutated": False}


def summarize_panels(*, panel_by_day, source_days, calendar_payload, scope_members,
                     start, end, as_of, environment_exit_ablation=False,
                     _rank_paths=None, _environments=None):
    """Inspect the saved causal branches; never construct an account or feed."""
    sessions, _, status = _calendar_view(calendar_payload,
        f"{calendar_payload['year']}-01-01", f"{calendar_payload['year']}-12-31", [])
    members = set(scope_members)
    if (status != "pinned_static_exchange_calendar" or not members
            or len(members) != len(scope_members) or source_days != sorted(set(source_days))
            or not source_days or sorted(panel_by_day) != source_days
            or [day for day in sessions if source_days[0] <= day <= source_days[-1]] != source_days
            or start > end or start not in source_days or end not in source_days):
        raise ValueError("Exact saved panel dates, fixed scope and pinned calendar required")
    observed = _stamp(as_of)
    for day, panel in panel_by_day.items():
        if (panel.get("day") != day
                or not _stamp(day + "T15:00:00+08:00") <= _stamp(panel["available_at"]) <= observed
                or [item["id"] for item in panel["types"]] != list(TYPE_IDS)):
            raise ValueError("Eight exactly dated types with actual saved availability required")
        for item in panel["types"]:
            if not isinstance(item.get("unresolved_symbols"), list) or not isinstance(item.get("candidates"), list):
                raise ValueError("Unknown membership and observed candidates must remain explicit")
            if item.get("available_at") is not None and _stamp(item["available_at"]) > _stamp(panel["available_at"]):
                raise ValueError("Type evidence is later than its saved panel")
            for candidate in item["candidates"]:
                if (any(candidate.get(key, day) != day for key in ("date", "source_day"))
                        or (candidate.get("available_at") is not None
                            and _stamp(candidate["available_at"]) > _stamp(panel["available_at"]))
                        or {"hit_4", "hit_5", "executable_hit_4", "executable_hit_5"}.intersection(candidate)):
                    raise ValueError("Future or differently dated candidate evidence is forbidden")

    # An existing replay caller may reuse its exact already-computed branches.
    paths = _rank_paths if _rank_paths is not None else _rank_path(panel_by_day, members, start, end)[0]
    environments = _environments if _environments is not None else _environment(panel_by_day, sessions, as_of)
    control = None
    if environment_exit_ablation:
        control = deepcopy(environments)
        for observation in control.values():
            for decision in observation["per_type"].values():
                if decision["mode"] == "EXIT_ALL":
                    decision["mode"] = "NO_CHANGE"
    guards = (*GUARDS, CONTROL_GUARD) if environment_exit_ablation else GUARDS
    days = [day for day in source_days if start <= day <= end]
    successor = {day: sessions[index + 1] if index + 1 < len(sessions) else None
        for index, day in enumerate(sessions)}
    source_set = set(source_days)
    cases = []
    for identity in TYPE_IDS:
        for guard in guards:
            for cap_name, _ in CAPS:
                failures, modes, unresolved = Counter(), Counter(), Counter()
                possible, nonempty, unknown, observation_unknown, suppressed = [], 0, 0, 0, 0
                for day in days:
                    item = next(row for row in panel_by_day[day]["types"] if row["id"] == identity)
                    selected = paths[identity][cap_name][day]["candidates"]
                    missing = set(item["unresolved_symbols"]) & members
                    unresolved.update(missing)
                    nonempty += bool(selected)
                    unknown += bool(missing)
                    known = item.get("status") in {"matched", "not_matched"}
                    observation_unknown += not known
                    original_mode = environments[day]["per_type"][identity]["mode"]
                    mode = ("ALLOW_FIXED_CANDIDATES" if guard == "unguarded" else
                        (control if guard == CONTROL_GUARD else environments)[day]["per_type"][identity]["mode"])
                    modes[mode] += 1
                    suppressed += guard == CONTROL_GUARD and original_mode == "EXIT_ALL"
                    reasons = []
                    if successor[day] not in source_set:
                        reasons.append("withheld_missing_exact_successor_full_source")
                    elif mode == "EXIT_ALL":
                        reasons.append("environment_exit_all_no_new_entry")
                    elif mode == "NO_CHANGE":
                        reasons.append("environment_unknown_or_unconfirmed")
                    elif mode != "ALLOW_FIXED_CANDIDATES":
                        raise ValueError("Unknown saved environment mode")
                    else:
                        if not known:
                            reasons.append("type_observation_unknown")
                        if missing:
                            reasons.append("type_membership_unknown_in_fixed_panel")
                        if not selected:
                            reasons.append("type_empty_or_capacity_filtered_all_no_signal")
                    failures.update(reasons)
                    if not reasons:
                        possible.append(day)
                cases.append({"case_id": f"{identity}__{guard}__{cap_name}", "type": identity,
                    "guard": guard, "capacity": cap_name, "decision_day_count": len(days),
                    "nonempty_candidate_days": nonempty, "membership_unknown_days": unknown,
                    "observation_unknown_days": observation_unknown,
                    "potential_entry_days": len(possible), "potential_entry_dates": possible,
                    "first_potential_entry_day": possible[0] if possible else None,
                    "failure_reason_counts": dict(failures), "environment_mode_counts": dict(modes),
                    "environment_exit_suppressed_days": suppressed,
                    "common_unresolved_symbols": [{"symbol": symbol, "decision_days": count}
                        for symbol, count in sorted(unresolved.items(), key=lambda row: (-row[1], row[0]))[:20]],
                    "status": "necessary_entry_conditions_present" if possible else "no_possible_entry_from_saved_panel",
                    "metrics": None, "execution_feasibility_verified": False})
    return {"schema": SCHEMA, "read_at": as_of, "start": start, "end": end,
        "source_days": list(source_days), "scope_count": len(members),
        "environment_exit_ablation": bool(environment_exit_ablation), "case_count": len(cases),
        "cases_with_potential_entry": sum(row["potential_entry_days"] > 0 for row in cases),
        "cases_without_potential_entry": sum(row["potential_entry_days"] == 0 for row in cases),
        "cases": cases, "metrics": None,
        "semantics": "necessary_saved_panel_entry_conditions_only_not_strategy_validity_or_trade_clearance",
        "unchecked_conditions": ["raw_quote_status_and_prices", "corporate_actions_and_reference",
            "IPO_qualification", "cash_and_position_constraints", "fills_and_later_exit_feasibility"],
        "successor_used_for_ranking_or_refill": False, "future_labels_used": False, **FLAGS}


def run(inputs, out, *, environment_exit_ablation=False):
    inputs, out = Path(inputs).resolve(), Path(out).resolve()
    if inputs.is_dir():
        inputs = inputs / "manifest.json"
    if out.exists():
        raise FileExistsError("Panel readiness requires a new output directory")
    if out.is_relative_to(inputs.parent) or inputs.is_relative_to(out):
        raise ValueError("Readiness output must be separate from saved inputs")
    loaded, manifest = load_inputs(inputs)
    result = summarize_panels(panel_by_day=loaded["panel_by_day"], source_days=loaded["source_days"],
        calendar_payload=loaded["calendar_payload"], scope_members=loaded["scope_members"],
        start=manifest["start"], end=manifest["end"], as_of=datetime.now(MARKET_TZ).isoformat(),
        environment_exit_ablation=environment_exit_ablation)
    result["source"] = {"manifest": str(inputs), "manifest_sha256": digest(inputs),
        "input_created_at": manifest["created_at"],
        "panels_file": manifest["panels_file"], "panels_sha256": manifest["panels_sha256"]}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--environment-exit-ablation", action="store_true")
    args = parser.parse_args(argv)
    result = run(args.inputs, args.out, environment_exit_ablation=args.environment_exit_ablation)
    print(json.dumps({"result": str(args.out.resolve() / "result.json"),
        "case_count": result["case_count"], "cases_with_potential_entry": result["cases_with_potential_entry"],
        "cases_without_potential_entry": result["cases_without_potential_entry"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
