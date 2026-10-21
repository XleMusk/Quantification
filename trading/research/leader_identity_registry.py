"""Prospective, immutable proxy identities and a fixed close-change observation.

No retrospective identity selection, provider calls, portfolio, or order path is
used. Three closes mean TWO complete close-to-close intervals. The first close
after registration is an anchor, never a measured entry at the previous close.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path

import pandas as pd

from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .leader_environment import ARCHETYPES
from .leader_environment_run import BOUNDARY, SCHEMA as ANALYSIS_SCHEMA
from .leader_listing_features import METHOD_ID

CN = timezone(timedelta(hours=8))
SCHEMA = "leader_prospective_proxy_identity_registry_v1"
OBSERVATION_SCHEMA = "leader_prospective_proxy_close_observation_v1"
REPORT_SCHEMA = "leader_prospective_proxy_tracking_report_v1"
RULE = {
    "id": "first_close_phase_change_three_closes_two_intervals_v1",
    "cohort": "all_saved_known_matched_proxy_candidates_union_with_all_labels_no_topN",
    "episode": "first_observed_close_phase_change_from_registration_context_or_previous_observed_close",
    "episode_is_intraday_start": False,
    "window": "confirmation_close_E_and_next_two_pinned_sessions_E1_E2",
    "measured_intervals": ["E_to_E1", "E1_to_E2"],
    "confirmation_day_return_used_as_performance": False,
    "provider_relative_condition": "each_of_two_provider_pctChg_ge_same_day_full_active_non_ST_median",
    "raw_price_condition": "unadjusted_close_E2_ge_unadjusted_close_E",
    "reference_check_bound": 0.01,
    "reference_check_proves_absence_of_actions": False,
    "return_conventions_combined": False,
    "missing_member_or_interval": "unknown_never_zero_or_dropped",
    "later_episodes_searched_for_better_result": False,
    "sector_relative_or_verified_leader_claimed": False,
    "corporate_actions_verified": False,
    "economic_return_or_PnL": False,
}
FLAGS = {**BOUNDARY, "signal_admitted": False, "current_signal_admitted": False,
         "account_created": False, "provider_contacted": False, "research_only": True}
PHASES = {"broad_strength_snapshot", "broad_weakness_snapshot", "mixed_breadth_snapshot"}


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _time(value):
    value = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Actual registration/observation clocks require a timezone")
    return value.astimezone(CN)


def _now(clock):
    return _time(clock() if clock else datetime.now(CN))


def _close(day):
    return _time(day + "T15:00:00+08:00")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _new(path, value):
    # Caller reserves a new directory first. Existing artifacts are never replaced.
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _flags(value, flags=FLAGS):
    if any(value.get(key) is not expected for key, expected in flags.items()):
        raise ValueError("Prospective research boundaries changed")


def _calendar(path, expected):
    path = Path(path).resolve()
    if digest(path) != expected:
        raise ValueError("Pinned calendar changed")
    payload = _json(path)
    year = int(payload["year"])
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("A full pinned calendar year is required")
    return sessions, previous


def _analysis(path, observed):
    """Reuse a completed analysis proof; do not re-read thousands of source files."""
    path = Path(path).resolve()
    result = _json(path)
    _flags(result, {**BOUNDARY, "current_signal_admitted": False, "provider_contacted": False,
                    "account_created": False, "research_only": True})
    a = result.get("analysis") or {}
    _flags(a, {**BOUNDARY, "signal_admitted": False})
    day, created = result["day"], _time(result["created_at"])
    if (result.get("schema") != ANALYSIS_SCHEMA or result.get("status") != "completed_research_environment_analysis"
            or a.get("day") != day or a.get("observed_at") != result["created_at"]
            or a.get("status") != "complete_close_descriptive_research_only"
            or not _close(day) <= created <= observed):
        raise ValueError("A completed, actually available close analysis is required")
    sessions, previous = _calendar(result["calendar"], result["calendar_sha256"])
    if day not in previous:
        raise ValueError("Analysis day is outside its pinned sessions")
    previews = result.get("input_previews") or []
    current = []
    for item in previews:
        if (item.get("features_match_current_active_raw") is not True
                or digest(item["preview"]) != item["preview_sha256"]
                or digest(item["feature_file"]) != item["feature_sha256"]):
            raise ValueError("Completed analysis input proof changed")
        p = _json(item["preview"])
        _flags(p, BOUNDARY)
        if (p.get("source_inventory_complete") is not True or _time(p["created_at"]) > created
                or p.get("status") != "complete_source_close_research_preview_only"
                or p["day"] > day or p.get("calendar") != result["calendar"]):
            raise ValueError("Analysis preview scope or clock is inconsistent")
        if p["day"] == day:
            current.append(item)
    if len(current) != 1:
        raise ValueError("Exactly one complete current feature proof is required")
    item = current[0]
    qualified = item.get("feature_qualification") or {}
    if qualified.get("method_id") != METHOD_ID or qualified.get("decision_as_of") != day + "T15:05:00+08:00":
        raise ValueError("Registry requires the explicitly verified production listing-feature method")
    if _time(qualified["decision_as_of"]) > created:
        raise ValueError("Qualified decision cutoff is later than actual analysis creation")
    if qualified.get("verification") != result.get("ipo_qualification"):
        raise ValueError("Analysis and feature qualification proofs disagree")
    frame = pd.read_csv(item["feature_file"], dtype={"symbol": str, "date": str})
    required = {"date", "symbol", "close", "pct_chg", "preclose_for_limits", "listing_qualification_status"}
    if (not required <= set(frame) or frame.empty or frame.symbol.duplicated().any()
            or not frame.date.eq(day).all()):
        raise ValueError("Unique exact-day active feature rows required")
    evidence = a["market_environment"]["evidence"]
    pct = pd.to_numeric(frame.pct_chg, errors="raise")
    breadth = (int(pct.gt(0).sum()) - int(pct.lt(0).sum())) / len(frame)
    spec = a["frozen_rule_spec"]
    median = float(pct.median())
    phase = ("broad_strength_snapshot" if breadth >= spec["breadth_strength_threshold"] and median > 0 else
             "broad_weakness_snapshot" if breadth <= spec["breadth_weakness_threshold"] and median < 0 else "mixed_breadth_snapshot")
    if (len(frame) != evidence["active_non_st_rows"] or not pct.map(math.isfinite).all()
            or not math.isclose(float(pct.median()), evidence["median_return_pct"], abs_tol=1e-10)
            or not math.isclose(breadth, evidence["breadth"], abs_tol=1e-10)
            or a["market_environment"]["phase"] != phase):
        raise ValueError("Saved whole-scope market median disagrees with feature proof")
    binding = {"implementation_revision": a["implementation_revision"],
               "frozen_rule_sha256": _hash(a["frozen_rule_spec"]),
               "feature_method_id": qualified["method_id"],
               "ipo_qualification": result["ipo_qualification"],
               "calendar_sha256": result["calendar_sha256"]}
    source = {"analysis": str(path), "analysis_sha256": digest(path), "day": day,
              "created_at": result["created_at"], "feature_file": item["feature_file"],
              "feature_sha256": item["feature_sha256"], "preview_sha256": item["preview_sha256"],
              "raw_daily_sha256": item["raw_daily_sha256"]}
    return result, frame.set_index("symbol", drop=False), binding, source, sessions, previous


def _cohort(result, frame):
    archetypes = result["analysis"]["archetypes"]
    if [a["id"] for a in archetypes] != [a[0] for a in ARCHETYPES]:
        raise ValueError("All eight saved archetypes in original order required")
    members, counts = {}, {}
    for a in archetypes:
        candidates = a["candidates"]
        if a["candidate_count"] != len(candidates) or len({x["symbol"] for x in candidates}) != len(candidates):
            raise ValueError("Full saved candidate membership required, without a display cap")
        if candidates and a.get("status") != "matched":
            raise ValueError("Only known matched daily proxy identities can be registered")
        counts[a["id"]] = len(candidates)
        for candidate in candidates:
            symbol = candidate["symbol"]
            if (candidate.get("source_day") != result["day"] or symbol not in frame.index
                    or candidate.get("trade_ready") is not False):
                raise ValueError("Identity is not an exact-day research proxy")
            member = members.setdefault(symbol, {"symbol": symbol, "name": candidate.get("name"),
                "source_day": result["day"], "baseline_close_context_only": float(frame.loc[symbol, "close"]),
                "identity_status": "daily_price_volume_proxy_not_verified_market_or_sector_leader", "labels": []})
            member["labels"].append({"id": a["id"], "name": a["name"],
                                     "evidence": candidate["evidence"], "invalidation": candidate["invalidation"]})
    return [members[s] for s in sorted(members)], counts


def register(analysis, out, *, _clock=None):
    actual = _now(_clock)
    result, frame, binding, source, sessions, previous = _analysis(analysis, actual)
    future = [day for day in sessions if _close(day) > actual]
    if not future:
        raise ValueError("Pinned calendar has no future eligible close; no cohort registered")
    members, counts = _cohort(result, frame)
    registry = {"schema": SCHEMA, "registered_at": actual.isoformat(), "source": source,
        "calendar": result["calendar"], "binding": binding, "rule": RULE,
        "first_eligible_close_day": future[0], "baseline_environment": result["analysis"]["market_environment"],
        "baseline_phase_adjacent_to_first_eligible_close": previous[future[0]] == source["day"],
        "members": members, "member_count": len(members), "label_counts": counts,
        "full_saved_membership_registered": True, "historical_registration_claimed": False,
        "baseline_is_entry_price": False, "registered_before_intraday_episode_start_claimed": False,
        **FLAGS}
    folder = Path(out).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    _new(folder / "registry.json", registry)
    _new(folder / "registration_receipt.json", {"schema": "leader_identity_registration_receipt_v1",
         "registered_at": actual.isoformat(), "registry_sha256": digest(folder / "registry.json"), **FLAGS})
    return registry


def _registry(path, actual):
    path = Path(path).resolve()
    r, receipt = _json(path), _json(path.parent / "registration_receipt.json")
    _flags(r)
    _flags(receipt)
    if (r.get("schema") != SCHEMA or r.get("rule") != RULE
            or receipt.get("schema") != "leader_identity_registration_receipt_v1"
            or receipt.get("registry_sha256") != digest(path)
            or receipt.get("registered_at") != r.get("registered_at")
            or not _time(r["source"]["created_at"]) <= _time(r["registered_at"]) <= actual):
        raise ValueError("Registry seal, actual clock or frozen rule changed")
    sessions, previous = _calendar(r["calendar"], r["binding"]["calendar_sha256"])
    future = [day for day in sessions if _close(day) > _time(r["registered_at"])]
    if not future or r["first_eligible_close_day"] != future[0]:
        raise ValueError("First eligible close cannot be backdated")
    symbols = [m["symbol"] for m in r["members"]]
    if symbols != sorted(set(symbols)) or r["member_count"] != len(symbols):
        raise ValueError("Fixed cohort membership changed")
    return r, digest(path), sessions, previous


def _observations(path, registry, registry_sha, sessions, actual):
    expected = sessions[sessions.index(registry["first_eligible_close_day"]):]
    records, prior = [], registry_sha
    prior_clock = _time(registry["registered_at"])
    for index, file in enumerate(sorted((Path(path).parent / "observations").glob("*/observation.json"))):
        record = _json(file)
        receipt = _json(file.parent / "observation_receipt.json")
        _flags(record)
        _flags(receipt)
        day = record["source"]["day"]
        if (index >= len(expected) or record.get("schema") != OBSERVATION_SCHEMA
                or receipt.get("schema") != "leader_identity_close_observation_receipt_v1"
                or receipt.get("observation_sha256") != digest(file)
                or receipt.get("recorded_at") != record.get("recorded_at")
                or day != expected[index] or file.parent.name != day.replace("-", "")
                or record.get("registry_sha256") != registry_sha or record.get("prior_record_sha256") != prior
                or record.get("binding") != registry["binding"]
                or not _close(day) <= _time(record["source"]["created_at"]) <= _time(record["recorded_at"]) <= actual
                or _time(record["recorded_at"]) < prior_clock or record.get("phase") not in PHASES
                or not math.isfinite(record["market_median_provider_pct"])
                or record.get("member_count") != registry["member_count"]
                or [m["symbol"] for m in record["members"]] != [m["symbol"] for m in registry["members"]]):
            raise ValueError("Observation chain, clocks or fixed membership changed")
        records.append(record)
        prior = digest(file)
        prior_clock = _time(record["recorded_at"])
    return records, prior


@contextmanager
def _lock(folder):
    with (folder / ".observe.lock").open("a+b") as handle:
        if not handle.seek(0, 2):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        acquire()
        try:
            yield
        finally:
            handle.seek(0)
            release()


def observe(registry_file, analysis, *, _clock=None):
    actual, path = _now(_clock), Path(registry_file).resolve()
    with _lock(path.parent):
        registry, registry_sha, sessions, previous = _registry(path, actual)
        records, prior_sha = _observations(path, registry, registry_sha, sessions, actual)
        result, frame, binding, source, _, _ = _analysis(analysis, actual)
        day = source["day"]
        existing = next((r for r in records if r["source"]["day"] == day), None)
        if existing:
            if existing["source"]["analysis_sha256"] != source["analysis_sha256"]:
                raise ValueError("A recorded close cannot be replaced by a revised analysis")
            return existing
        expected_index = sessions.index(registry["first_eligible_close_day"]) + len(records)
        if expected_index >= len(sessions) or day != sessions[expected_index]:
            raise ValueError("Only the next exact prospective close can be appended; no skipping or backfill")
        if binding != registry["binding"]:
            raise ValueError("Frozen environment/qualification feature method cannot be mixed")
        members = []
        for member in registry["members"]:
            symbol = member["symbol"]
            if symbol not in frame.index:
                members.append({"symbol": symbol, "status": "unknown_not_in_current_active_non_ST_scope",
                                "close": None, "provider_pct_chg": None, "provider_preclose": None,
                                "listing_qualification_status": None})
                continue
            row = frame.loc[symbol]
            prices = [float(row[key]) for key in ("close", "pct_chg", "preclose_for_limits")]
            valid = all(math.isfinite(v) for v in prices) and prices[0] > 0 and prices[2] > 0
            members.append({"symbol": symbol, "status": "active_price_proxy" if valid else "unknown_invalid_price",
                "close": prices[0] if valid else None, "provider_pct_chg": prices[1] if valid else None,
                "provider_preclose": prices[2] if valid else None,
                "listing_qualification_status": str(row.listing_qualification_status)})
        market = result["analysis"]["market_environment"]
        record = {"schema": OBSERVATION_SCHEMA, "registry_sha256": registry_sha,
            "prior_record_sha256": prior_sha, "recorded_at": actual.isoformat(), "source": source,
            "binding": binding, "phase": market["phase"], "phase_label": market["label"],
            "market_median_provider_pct": market["evidence"]["median_return_pct"], "members": members,
            "member_count": len(members), "missing_member_count": sum(m["status"] != "active_price_proxy" for m in members),
            "registered_before_confirmation_close": _time(registry["registered_at"]) < _close(day),
            "confirmation_day_performance_measured": False, **FLAGS}
        folder = path.parent / "observations" / day.replace("-", "")
        folder.mkdir(parents=True, exist_ok=False)
        _new(folder / "observation.json", record)
        _new(folder / "observation_receipt.json", {"schema": "leader_identity_close_observation_receipt_v1",
             "observation_sha256": digest(folder / "observation.json"), "recorded_at": actual.isoformat(), **FLAGS})
        return record


def report(registry_file, *, _clock=None):
    actual, path = _now(_clock), Path(registry_file).resolve()
    registry, registry_sha, sessions, _ = _registry(path, actual)
    records, _ = _observations(path, registry, registry_sha, sessions, actual)
    prior_phase = (registry["baseline_environment"]["phase"]
                   if registry["baseline_phase_adjacent_to_first_eligible_close"] else None)
    episode = None
    for index, record in enumerate(records):
        if prior_phase is not None and record["phase"] != prior_phase:
            episode = {"from_phase": prior_phase, "to_phase": record["phase"],
                "confirmation_close_day": record["source"]["day"], "confirmation_is_intraday_episode_start": False,
                "pre_confirmation_registered_at": registry["registered_at"], "observation_index": index}
            episode["confirmation_analysis_available_at"] = record["source"]["created_at"]
            episode["confirmation_recorded_at"] = record["recorded_at"]
            break
        prior_phase = record["phase"]
    rows, window = [], []
    if episode is not None:
        anchor_index = sessions.index(episode["confirmation_close_day"])
        window = sessions[anchor_index:anchor_index + 3]
        episode["fixed_window_close_days"] = window
        episode["complete_close_count"] = min(3, len(records) - episode["observation_index"])
    enough = episode is not None and len(window) == 3 and episode["complete_close_count"] == 3
    for member in registry["members"]:
        row = {"symbol": member["symbol"], "name": member["name"], "labels": [x["id"] for x in member["labels"]],
               "status": "waiting_environment_change" if episode is None else "waiting_three_closes",
               "provider_relative_condition": None, "raw_price_condition": None,
               "unadjusted_endpoint_change_pct": None, "intervals": []}
        if enough:
            sliced = records[episode["observation_index"]:episode["observation_index"] + 3]
            observations = [next(m for m in r["members"] if m["symbol"] == member["symbol"]) for r in sliced]
            unknown = any(m["status"] != "active_price_proxy" or m["listing_qualification_status"] != "supported" for m in observations)
            for j in (1, 2):
                prev, current = observations[j - 1], observations[j]
                compatible = (prev["close"] is not None and current["provider_preclose"] is not None
                    and abs(current["provider_preclose"] / prev["close"] - 1) <= RULE["reference_check_bound"])
                unknown |= not compatible
                row["intervals"].append({"from_close_day": window[j - 1], "to_close_day": window[j],
                    "reference_approximately_consistent": bool(compatible),
                    "provider_pct_chg": current["provider_pct_chg"],
                    "market_median_provider_pct": sliced[j]["market_median_provider_pct"],
                    "excess_provider_percentage_points": (current["provider_pct_chg"] - sliced[j]["market_median_provider_pct"])
                        if current["provider_pct_chg"] is not None else None})
            if unknown:
                row["status"] = "unknown_missing_or_unqualified_or_reference_incompatible"
            else:
                relative = all(leg["excess_provider_percentage_points"] >= 0 for leg in row["intervals"])
                change = (observations[-1]["close"] / observations[0]["close"] - 1) * 100
                row.update(status="price_proxy_conditions_matched" if relative and change >= 0 else "price_proxy_conditions_not_matched",
                    provider_relative_condition=relative, raw_price_condition=change >= 0, unadjusted_endpoint_change_pct=change)
        rows.append(row)
    statuses = {status: sum(r["status"] == status for r in rows) for status in sorted({r["status"] for r in rows})}
    next_index = sessions.index(registry["first_eligible_close_day"]) + len(records)
    return {"schema": REPORT_SCHEMA, "registry": str(path), "registry_sha256": registry_sha,
        "read_at": actual.isoformat(), "registered_at": registry["registered_at"], "source": registry["source"],
        "first_eligible_close_day": registry["first_eligible_close_day"], "member_count": registry["member_count"],
        "label_counts": registry["label_counts"], "observed_close_days": [r["source"]["day"] for r in records],
        "next_required_close_day": sessions[next_index] if next_index < len(sessions) else None,
        "episode": episode, "rule": RULE, "status_counts": statuses, "members": rows,
        "cross_regime_leader_verified": False,
        "missing_evidence": ["verified_pre_episode_real_leader_identity", "dated_sector_relative_resilience",
                             "intraday_episode_onset", "verified_corporate_actions", "tradable_entry_exit_and_costs"],
        **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("register")
    create.add_argument("--analysis", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    append = sub.add_parser("observe")
    append.add_argument("--registry", type=Path, required=True)
    append.add_argument("--analysis", type=Path, required=True)
    read = sub.add_parser("report")
    read.add_argument("--registry", type=Path, required=True)
    read.add_argument("--out", type=Path, help="Optional new report file; existing report is never replaced")
    args = parser.parse_args(argv)
    if args.command == "register":
        value = register(args.analysis, args.out)
        answer = {k: value[k] for k in ("schema", "registered_at", "first_eligible_close_day", "member_count", "label_counts")}
        answer["registry"] = str(args.out.resolve() / "registry.json")
    elif args.command == "observe":
        value = observe(args.registry, args.analysis)
        answer = {"schema": value["schema"], "source_day": value["source"]["day"], "recorded_at": value["recorded_at"],
                  "member_count": value["member_count"], "missing_member_count": value["missing_member_count"]}
    else:
        value = report(args.registry)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            _new(args.out, value)
        answer = {k: v for k, v in value.items() if k != "members"}
    print(json.dumps({**answer, "execution_admitted": False}, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
