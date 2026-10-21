"""Dated saved leader labels inside the original immutable identity cohort.

This catalog is a new research source contract, not a replacement registry or a
trade signal.  It reuses completed qualified analyses and their existing labels;
no new ranking, threshold, theme membership, model, or account path is created.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import argparse
import json
from pathlib import Path

from . import leader_missing_type_candidates as patterns
from .baostock_daily_slice import digest
from .leader_environment import ARCHETYPES
from .leader_identity_registry import (
    CN, _analysis, _close, _cohort, _hash, _new, _registry, _time,
)

SCHEMA = "leader_dated_identity_catalog_v1"
RECEIPT_SCHEMA = "leader_dated_identity_catalog_receipt_v1"
RESOLUTION_SCHEMA = "leader_exact_prior_identity_resolution_v1"
METHOD_ID = "original_cohort_exact_prior_close_daily_identity_proxy_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "provider_contacted": False,
    "model_refit": False, "orders_generated": False, "ledger_mutated": False,
    "account_created": False, "theme_membership_verified": False,
    "pit_verified": False, "historical_publication_verified": False,
    "person_identity_verified": False, "original_registry_modified": False}


def _actual(value=None):
    return _time(value if value is not None else datetime.now(CN))


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _leader_types():
    """The original catchup definition owns the leader types, not this file."""
    return [key for key, _ in ARCHETYPES if key in patterns.CATCHUP_LEADER_TYPES]


def _rule():
    return {"id": METHOD_ID, "leader_types": _leader_types(),
        "identity": "unchanged_known_matched_saved_daily_price_volume_labels",
        "scope": "original_registry_members_and_order_never_rolling_universe",
        "lookup": "exact_calendar_previous_session_only_no_fallback",
        "availability": "actual_new_catalog_persistence_not_source_date_backfill",
        "required_before": "actual_decision_asof_and_strictly_before_decision_close",
        "industry": "existing_frozen_lagged_broad_industry_not_theme_or_PIT",
        "missing_member": "unknown_not_known_empty_or_removed",
        "leader_thresholds_added": False, "source_outcomes_used_to_select": False}


def _anchor(registry_file, actual):
    path = Path(registry_file).resolve()
    registry, sha, sessions, previous = _registry(path, actual)
    anchor = {"registry_file": str(path), "registry_sha256": sha,
        "registry_registered_at": registry["registered_at"],
        "registry_source": deepcopy(registry["source"]),
        "binding": deepcopy(registry["binding"]),
        "calendar": registry["calendar"], "member_count": registry["member_count"],
        "scope_members": [row["symbol"] for row in registry["members"]]}
    return registry, anchor, sessions, previous


def _dated_labels(registry, analysis_file, actual):
    """Read already-qualified saved labels, without recomputing a strategy."""
    result, frame, binding, source, _, _ = _analysis(analysis_file, actual)
    if binding != registry["binding"] or result["calendar"] != registry["calendar"]:
        raise ValueError("Dated identity source changed the frozen feature/calendar binding")
    cohort, counts = _cohort(result, frame)
    by_symbol = {row["symbol"]: row for row in cohort}
    archetypes = result["analysis"]["archetypes"]
    uncertain = set()
    for item in archetypes:
        if item["id"] in _leader_types():
            uncertain.update(item.get("unresolved_stage_symbols", []))
            uncertain.update(item.get("unresolved_symbols", []))
    members, leaders, unknown = [], [], []
    for original in registry["members"]:
        symbol = original["symbol"]
        daily = by_symbol.get(symbol, {})
        labels = deepcopy(daily.get("labels", []))
        ids = [label["id"] for label in labels]
        reasons = []
        if symbol not in frame.index:
            reasons.append("fixed_member_missing_from_current_active_features")
        if symbol in uncertain:
            reasons.append("saved_leader_label_membership_unresolved")
        known_leader = bool(set(ids).intersection(_leader_types()))
        status = "known_leader_proxy" if known_leader else "unknown" if reasons else "known_nonleader_proxy"
        if known_leader:
            leaders.append(symbol)
        if status == "unknown":
            unknown.append(symbol)
        members.append({"symbol": symbol, "name": original["name"],
            "source_day": result["day"], "labels": ids, "label_evidence": labels,
            "leader_membership_status": status, "unknown_reasons": reasons,
            "identity_status": "daily_price_volume_proxy_not_verified_market_or_sector_leader",
            "person_verified": False, "trade_ready": False})
    preview = next(item for item in result["input_previews"]
        if _read(item["preview"])["day"] == result["day"])
    source = {**source, "preview_file": preview["preview"],
        "feature_qualification": deepcopy(preview["feature_qualification"])}
    return {"source_day": result["day"], "source": source,
        "members": members, "membership_sha256": _hash(members),
        "member_count": len(members), "leader_symbols": leaders,
        "unknown_leader_symbols": unknown, "scope_complete": not unknown,
        "source_label_counts": counts,
        "label_counts": {key: sum(key in member["labels"] for member in members)
            for key, _ in ARCHETYPES},
        "source_member_outside_fixed_scope_count": len(set(by_symbol) - {m["symbol"] for m in members}),
        "saved_type_missing_evidence": {item["id"]: deepcopy(item.get("missing_evidence", []))
            for item in archetypes}}


def _check_sequence(entries, previous):
    days = [entry["source_day"] for entry in entries]
    if days != sorted(set(days)) or any(day not in previous for day in days):
        raise ValueError("Dated identity source dates must be unique original calendar order")
    if any(previous[day] != prior for prior, day in zip(days, days[1:])):
        raise ValueError("Append requires the exact next source session, never a skipped day")


def _load_catalog(path, actual, seen):
    path = Path(path).resolve()
    if path in seen:
        raise ValueError("Dated identity catalog parent cycle")
    seen = seen | {path}
    record = _read(path)
    receipt_file = path.with_name("receipt.json")
    receipt = _read(receipt_file)
    if (record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("rule") != _rule() or record.get("catalog_file") != str(path)
            or any(record.get(k) is not v for k, v in FLAGS.items())
            or receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("catalog_sha256") != digest(path)
            or not _time(record["created_at"]) <= _time(receipt["persisted_at"]) <= actual):
        raise ValueError("Catalog source contract, seal or actual clock changed")
    registry, anchor, sessions, previous = _anchor(record["anchor"]["registry_file"], actual)
    if record["anchor"] != anchor:
        raise ValueError("Catalog cannot change the original fixed identity cohort")
    entries = record["entries"]
    if not entries or record.get("entry_count") != len(entries):
        raise ValueError("Catalog requires its complete nonempty dated entry inventory")
    _check_sequence(entries, previous)
    parent_info = record.get("parent")
    parent = None
    prefix_count = 0
    effective = {}
    if parent_info:
        parent = _load_catalog(parent_info["file"], actual, seen)
        prefix_count = len(parent["entries"])
        if (parent_info.get("sha256") != parent["catalog_sha256"]
                or parent_info.get("entry_count") != prefix_count
                or parent["anchor"] != anchor or entries[:prefix_count] != parent["entries"]
                or len(entries) <= prefix_count
                or _time(parent["persisted_at"]) > _time(record["created_at"])):
            raise ValueError("Catalog parent entry source/labels/clock/hash prefix changed")
        effective.update(parent["effective_available_at_by_day"])
    for index, entry in enumerate(entries):
        expected = _dated_labels(registry, entry["source"]["analysis"], actual)
        if any(entry.get(key) != value for key, value in expected.items()):
            raise ValueError("Saved dated labels, scope or original source proof changed")
        if (entry.get("entry_sha256") != _hash({k: v for k, v in entry.items() if k != "entry_sha256"})
                or not _time(entry["source"]["created_at"]) <= _time(entry["recorded_at"])
                <= _time(entry["available_at"]) <= _time(record["created_at"])):
            raise ValueError("Dated identity labels or original entry clock changed")
        if index >= prefix_count:
            if (entry["origin_catalog"] != str(path)
                    or entry["recorded_at"] != record["created_at"]
                    or entry["available_at"] != record["created_at"]):
                raise ValueError("New entry cannot claim an older origin or recording clock")
            effective[entry["source_day"]] = max(_time(entry["available_at"]),
                _time(receipt["persisted_at"])).isoformat()
    return {**record, "catalog_sha256": digest(path), "persisted_at": receipt["persisted_at"],
        "effective_available_at_by_day": effective, "sessions": sessions, "previous": previous}


def load_catalog(catalog_file, *, observed_at=None):
    """Validate saved source/label prefixes; expose effective persistence clocks."""
    return _load_catalog(catalog_file, _actual(observed_at), set())


def build_catalog(registry_file, analyses, out, *, parent=None, _clock=None):
    """Explicitly build a new directory from saved qualified analysis JSON files.

    ``analyses`` is supplied in source order.  A parent append accepts only exact
    next dates and copies old entries, including their original clocks, verbatim.
    The post-write receipt supplies the actual effective availability lower bound.
    """
    actual = _actual(_clock() if _clock else None)
    registry, anchor, _, previous = _anchor(registry_file, actual)
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Dated catalog requires an explicitly new output directory")
    registry_dir = Path(registry_file).resolve().parent
    if folder == registry_dir or registry_dir in folder.parents:
        raise ValueError("Dated catalog cannot be written inside the original registry")
    if not analyses:
        raise ValueError("At least one explicit saved dated analysis is required")
    old = load_catalog(parent, observed_at=actual) if parent is not None else None
    if old and old["anchor"] != anchor:
        raise ValueError("Append cannot replace the original fixed identity anchor")
    entries = deepcopy(old["entries"]) if old else []
    new = [_dated_labels(registry, value, actual) for value in analyses]
    _check_sequence(entries + new, previous)
    finished = _actual(_clock() if _clock else None)
    if finished < actual:
        raise ValueError("Actual catalog clock moved backwards")
    catalog_file = folder / "catalog.json"
    for entry in new:
        entry.update(recorded_at=finished.isoformat(), available_at=finished.isoformat(),
            origin_catalog=str(catalog_file))
        entry["entry_sha256"] = _hash(entry)
    entries.extend(new)
    record = {"schema": SCHEMA, "method_id": METHOD_ID,
        "catalog_file": str(catalog_file), "created_at": finished.isoformat(),
        "anchor": anchor, "rule": _rule(), "entries": entries, "entry_count": len(entries),
        "parent": {"file": str(Path(parent).resolve()), "sha256": old["catalog_sha256"],
            "entry_count": len(old["entries"])} if old else None,
        "source_day_is_historical_signal_availability": False, **FLAGS}
    if (digest(registry_file) != anchor["registry_sha256"]
            or digest(anchor["calendar"]) != anchor["binding"]["calendar_sha256"]
            or old and digest(parent) != old["catalog_sha256"]):
        raise ValueError("Original anchor/calendar/parent changed during catalog build")
    for entry in entries:
        source = entry["source"]
        for file_key, sha_key in (("analysis", "analysis_sha256"),
                ("feature_file", "feature_sha256"), ("preview_file", "preview_sha256")):
            if digest(source[file_key]) != source[sha_key]:
                raise ValueError("Saved source bytes changed during catalog build")
    folder.mkdir(parents=True, exist_ok=False)
    _new(catalog_file, record)
    persisted = _actual(_clock() if _clock else None)
    if persisted < finished:
        raise ValueError("Actual persistence clock moved backwards")
    _new(folder / "receipt.json", {"schema": RECEIPT_SCHEMA,
        "catalog_sha256": digest(catalog_file), "persisted_at": persisted.isoformat(),
        "clock_basis": "actual_clock_after_catalog_file_was_written", **FLAGS})
    return record


def resolve_prior_identity(catalog_file, *, decision_day, decision_as_of, observed_at=None):
    """Resolve exact D-1 only; missing/future knowledge is unknown, never fallback."""
    actual, asof = _actual(observed_at), _time(decision_as_of)
    result = {"schema": RESOLUTION_SCHEMA, "method_id": METHOD_ID,
        "decision_day": decision_day, "decision_as_of": asof.isoformat(),
        "read_at": actual.isoformat(), "status": "unknown", "reason": None,
        "members": [], "member_count": None, "available_at": None,
        "required_source_day": None, "required_prior_day": None,
        "entry": None, "unknown_leader_symbols": [], "catalog_reference": None, **FLAGS}
    if asof > actual:
        return {**result, "reason": "decision_asof_is_not_yet_actual"}
    if not Path(catalog_file).is_file():
        return {**result, "reason": "dated_identity_catalog_unavailable"}
    loaded = load_catalog(catalog_file, observed_at=actual)
    prior = loaded["previous"].get(decision_day)
    result.update(required_source_day=prior, required_prior_day=prior,
        catalog_reference={"file": loaded["catalog_file"], "sha256": loaded["catalog_sha256"]},
        catalog_file=loaded["catalog_file"],
        catalog_sha256=loaded["catalog_sha256"], registry=loaded["anchor"]["registry_file"],
        registry_sha256=loaded["anchor"]["registry_sha256"])
    if prior is None:
        return {**result, "reason": "decision_calendar_previous_session_unknown"}
    entry = next((row for row in loaded["entries"] if row["source_day"] == prior), None)
    if entry is None:
        return {**result, "reason": "exact_previous_close_identity_missing"}
    available = _time(loaded["effective_available_at_by_day"][prior])
    if available > asof or available >= _close(decision_day):
        return {**result, "reason": "exact_previous_identity_not_available_before_decision",
            "available_at": available.isoformat()}
    return {**result, "status": "available", "reason": None,
        "entry": deepcopy(entry),
        "source": deepcopy(entry["source"]), "source_day": prior,
        "available_at": available.isoformat(), "registered_at": available.isoformat(),
        "original_registry_registered_at": loaded["anchor"]["registry_registered_at"],
        "entry_sha256": entry["entry_sha256"], "members": deepcopy(entry["members"]),
        "member_count": entry["member_count"], "leader_types": _leader_types(),
        "leader_symbols": list(entry["leader_symbols"]),
        "unknown_leader_symbols": list(entry["unknown_leader_symbols"]),
        "scope_complete": entry["scope_complete"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--analysis", action="append", required=True)
    parser.add_argument("--parent")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = build_catalog(args.registry, args.analysis, args.out, parent=args.parent)
    print(json.dumps({"catalog_file": result["catalog_file"],
        "source_days": [entry["source_day"] for entry in result["entries"]],
        "scope_member_count": result["anchor"]["member_count"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
