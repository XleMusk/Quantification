"""Read one saved prior-dated SH/SZ roster snapshot without contacting a source."""
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..settings import DATA_ROOT
from .baostock_daily_slice import digest
from .close_workbench import process_identity
from .index_observation import CN, trading_session
from .index_observation_change import _slot, _time
from .leader_cohort_quote_view import _read, _row, summarize
from .leader_daily_signals import _calendar_view
from .prior_dated_market_quote_snapshot import (FLAGS, LATEST_SCHEMA, OBSERVATION_SCHEMA, SOURCE_SCOPE,
                                               SUCCESS, TARGETS_SCHEMA)
from .leader_cohort_observation import REGISTRY_RELATIVE, _provider_symbol
from .market_roster_industry_context import industry_context

SCHEMA = "prior_dated_market_roster_intraday_quote_view_v1"


def _boundary(value, schema):
    if (value.get("schema") != schema or value.get("source_scope") != SOURCE_SCOPE
            or any(value.get(key) is not expected for key, expected in FLAGS.items())):
        raise ValueError("Prior-dated roster quote research boundaries changed")


def _range(rows):
    stamps = []
    for row in rows:
        try:
            stamps.append(_time(row["source_quote_time"]))
        except (ValueError, TypeError, KeyError):
            continue
    return {"count": len(stamps), "minimum": min(stamps).isoformat() if stamps else None,
            "maximum": max(stamps).isoformat() if stamps else None,
            "span_seconds": (max(stamps) - min(stamps)).total_seconds() if stamps else None}


def _target_binding(manifest, source, targets, *, data_root, folder, now):
    """Check the frozen local contract; do not reopen 5,224 source price files."""
    for key in ("source_day", "observation_day", "source_available_at", "source_package", "source_proof",
                "calendar_file", "calendar_sha256", "fixed_590_registry", "target_count"):
        if source.get(key) != manifest.get(key):
            raise ValueError("Snapshot and frozen targets disagree")
    if manifest.get("background_labels_are_not_current_eligibility") is not True:
        raise ValueError("Prior-day background cannot become current eligibility")
    if (not targets or len(targets) != manifest["target_count"]
            or len({target["symbol"] for target in targets}) != len(targets)):
        raise ValueError("Every unique frozen prior-roster target is required")
    calendar_file = Path(manifest["calendar_file"]).resolve()
    if calendar_file != folder / "calendar.json" or digest(calendar_file) != manifest["calendar_sha256"]:
        raise ValueError("Frozen snapshot calendar path or hash changed")
    calendar = _read(calendar_file)
    source_day, day = manifest["source_day"], manifest["observation_day"]
    _, previous, status = _calendar_view(calendar, day, day, [])
    if status != "pinned_static_exchange_calendar" or previous.get(day) != source_day:
        raise ValueError("Source is not the exact prior pinned session of the snapshot")
    proof = manifest["source_proof"]
    if (proof.get("day") != source_day or proof.get("calendar_sha256") != manifest["calendar_sha256"]
            or proof.get("source_folder") != manifest["source_package"]
            or proof.get("ordinary_inventory_rows") != len(targets)
            or any(proof.get(key) is not True for key in ("read_only_verified", "source_inventory_complete",
                        "full_dated_roster_inventory_complete"))
            or proof.get("receipt_status") != "complete_full_dated_close_inventory_not_admitted"):
        raise ValueError("Snapshot lacks the pinned strict complete prior-source proof")
    for key in ("raw_daily_sha256", "roster_raw_sha256", "source_receipt_sha256", "source_plan_sha256"):
        value = proof.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Recorded original source hash is missing or invalid")
    completed = proof.get("source_completed_at") or proof.get("source_updated_at")
    if not completed or manifest["source_available_at"] != proof.get("terminal_completion_observed_at") or completed != manifest["source_available_at"]:
        raise ValueError("Only actual full-source completion can establish roster availability")
    available, frozen = _time(manifest["source_available_at"]), _time(manifest["created_at"])
    if not _time(source_day + "T15:00:00+08:00") <= available <= frozen <= _time(source["started_at"]) <= now:
        raise ValueError("Source availability, target freezing or observation is in the future")
    registry = manifest["fixed_590_registry"]
    registry_file = Path(registry["file"]).resolve()
    if registry_file != data_root / REGISTRY_RELATIVE or digest(registry_file) != registry["sha256"]:
        raise ValueError("Fixed-590 relationship refers to a different sealed registry")
    fixed = _read(registry_file)
    members = {member["symbol"]: member for member in fixed["members"]}
    if (len(members) != registry["member_count"] or fixed["registered_at"] != registry["registered_at"]
            or _time(registry["registered_at"]) > frozen
            or registry.get("relationship_only_not_registry_mutation") is not True):
        raise ValueError("Fixed-590 relationship clock or membership changed")
    for target in targets:
        symbol = target["symbol"]
        if (target.get("provider_symbol") != _provider_symbol(symbol) or target.get("source_day") != source_day
                or target.get("roster_code") != target["provider_symbol"][:2] + "." + symbol[:6]
                or target.get("background_isST") not in ("0", "1")
                or target.get("background_trade_status") not in ("0", "1")
                or target.get("in_fixed_590") is not (symbol in members)
                or target.get("fixed_590_labels") != ([label["id"] for label in members[symbol]["labels"]] if symbol in members else [])):
            raise ValueError("Frozen target identity, prior-day labels or fixed-cohort relationship changed")
    if (registry["present_in_prior_roster_count"] != sum(target["in_fixed_590"] for target in targets)
            or registry["absent_from_prior_roster_symbols"] != sorted(set(members) - {target["symbol"] for target in targets})):
        raise ValueError("Fixed-cohort presence/absence denominator changed")
    return calendar, available


def _adapt_row(row, target, *, now, start, stop, finished, available, began, received):
    for key in ("symbol", "provider_symbol", "roster_code", "roster_name", "source_day", "background_isST",
                "background_trade_status", "in_fixed_590", "fixed_590_labels"):
        if row.get(key) != target.get(key):
            raise ValueError("Saved quote differs from its frozen target context")
    adapted = {**row, "registered_name": target["roster_name"], "frozen_labels": [],
               "name_changed_from_registry": row.get("name_changed_from_dated_roster")}
    member = {"symbol": target["symbol"], "name": target["roster_name"], "labels": []}
    result = _row(adapted, member, now=now, start=start, stop=stop, finished=finished, registered_at=available)
    try:
        if not began <= _time(row["received_at"]) <= received:
            result.update(current_at_read=False, day_change_percent=None, read_status="member_received_time_outside_attempt")
    except (KeyError, ValueError, TypeError):
        result.update(current_at_read=False, day_change_percent=None, read_status="missing_or_invalid")
    if result["read_status"] == "source_time_precedes_registration":
        result["read_status"] = "source_time_precedes_roster_availability"
    result.pop("registered_name")
    result.pop("frozen_labels")
    result["roster_name"] = target["roster_name"]
    result["name_changed_from_dated_roster"] = result.pop("name_changed_from_registry")
    return result


def market_roster_quote_view(data_root=DATA_ROOT, *, now=None):
    """Read aggregate price proxies; latest failure never falls back to success."""
    now = _time(now or datetime.now(CN))
    data_root = Path(data_root).resolve()
    root = data_root / "research/market_roster_observation"
    result = {"schema": SCHEMA, "status": "waiting_for_first_market_roster_snapshot", "read_at": now.isoformat(),
              "source_scope": SOURCE_SCOPE, "provider_contacted": False, "source_file": None, "targets_file": None,
              "target_count": None, "source_day": None, "observation_day": None, "source_available_at": None,
              "session": None, "snapshot": None, "collector": None, "current_summary": None,
              "observation_summary": None, "groups": [], "source_quote_clocks": None, "industry_context": None,
              "recent_change": None,
              "activity_context": None,
              "background_labels_are_not_current_eligibility": True, "economic_return_or_PnL": False,
              "no_common_instant_claimed": True, **FLAGS}
    try:
        pointer_path = root / "latest.json"
        if not pointer_path.is_file():
            return result
        pointer_sha = digest(pointer_path)
        pointer = _read(pointer_path)
        _boundary(pointer, LATEST_SCHEMA)
        folder = (root / pointer["observation_id"]).resolve()
        source_path, targets_path = [Path(pointer[key]).resolve() for key in ("receipt_file", "targets_file")]
        if (folder.parent != root or source_path != folder / "receipt.json" or targets_path != folder / "targets.json"):
            raise ValueError("Latest snapshot paths escape their single frozen observation directory")
        manifest, source = _read(targets_path), _read(source_path)
        _boundary(manifest, TARGETS_SCHEMA)
        _boundary(source, OBSERVATION_SCHEMA)
        if (digest(targets_path) != pointer["targets_sha256"] or source.get("targets_sha256") != pointer["targets_sha256"]
                or source.get("targets_file") != str(targets_path) or source.get("observation_id") != folder.name):
            raise ValueError("Frozen target path, hash or observation identity changed")
        targets = manifest["targets"]
        calendar, available = _target_binding(manifest, source, targets, data_root=data_root, folder=folder, now=now)
        result.update(source_file=str(source_path), targets_file=str(targets_path), targets_sha256=pointer["targets_sha256"],
                      target_count=len(targets), source_day=manifest["source_day"], observation_day=manifest["observation_day"],
                      source_available_at=manifest["source_available_at"], targets_created_at=manifest["created_at"],
                      source_proof=manifest["source_proof"], fixed_590_registry=manifest["fixed_590_registry"],
                      collector={"recorded_status": source["status"], "process": process_identity(source.get("pid"), source.get("process_started_at"))})
        start, stop = _slot(source)
        began = _time(source["started_at"])
        session = trading_session(calendar, began)
        if (session["session_day"] != manifest["observation_day"] or session["session_name"] != source["session_name"]
                or began.date().isoformat() != manifest["observation_day"]):
            raise ValueError("Actual snapshot start does not match its declared observation day and slot")
        result["session"] = {key: source[key] for key in ("session_day", "session_name", "session_start", "stop_at")}
        rows = source["quotes"]
        if len(rows) != len(targets) or [row.get("symbol") for row in rows] != [target["symbol"] for target in targets]:
            raise ValueError("Saved snapshot must retain all frozen targets in original order")
        result["snapshot"] = {key: source.get(key) for key in ("started_at", "response_received_at", "finished_at",
                              "planned_batches", "completed_batches", "quote_request_count", "status")}
        if source["status"] == "started" or pointer.get("snapshot_status") == "started":
            return {**result, "status": "collecting_snapshot"}
        if source["status"] != SUCCESS:
            return {**result, "status": "unavailable_latest_market_roster_snapshot", "error": source.get("error"),
                    "unavailable_summary": {"member_count": len(targets), "known_count": 0,
                        "unknown_count": len(targets), "breadth_bounds": [-1, 1]}}
        if pointer.get("snapshot_status") != SUCCESS:
            raise ValueError("Latest pointer and successful snapshot disagree")
        planned = (len(targets) + 99) // 100
        if (source.get("all_batches_completed") is not True or source.get("units", {}).get("prices") != "CNY"
                or source.get("batch_size") != 100 or source.get("planned_batches") != planned
                or source.get("completed_batches") != planned or source.get("quote_request_count") != planned):
            raise ValueError("Success requires every planned serial technical batch")
        received, finished = _time(source["response_received_at"]), _time(source["finished_at"])
        if not start <= began <= received <= finished <= now or finished >= stop:
            raise ValueError("Snapshot completion or actual response clock is invalid")
        read_rows = [_adapt_row(row, target, now=now, start=start, stop=stop, finished=finished,
                               available=available, began=began, received=received) for row, target in zip(rows, targets)]
        saved_rows = [_adapt_row(row, target, now=received, start=start, stop=stop, finished=finished,
                                available=available, began=began, received=received) for row, target in zip(rows, targets)]
        current, saved = summarize(read_rows), summarize(saved_rows)
        is_current = current["known_count"] > 0
        groups = []
        predicates = [("all", f"{manifest['source_day']}完整沪深普通A股名册", lambda target: True),
                      ("fixed_590", "原固定590集合在本名册内的成员", lambda target: target["in_fixed_590"]),
                      ("rest", "本名册其余成员", lambda target: not target["in_fixed_590"]),
                      ("prior_day_nonST", "前交易日非ST背景", lambda target: target["background_isST"] == "0"),
                      ("prior_day_ST", "前交易日ST背景", lambda target: target["background_isST"] == "1"),
                      ("prior_day_active_nonST", "前交易日正常交易且非ST背景", lambda target: target["background_isST"] == "0" and target["background_trade_status"] == "1")]
        for id_, name, predicate in predicates:
            indices = [index for index, target in enumerate(targets) if predicate(target)]
            groups.append({"id": id_, "name": name, "member_count": len(indices),
                           "current_summary": summarize([read_rows[index] for index in indices]) if is_current else None,
                           "observation_summary": summarize([saved_rows[index] for index in indices])})
        industry = industry_context(read_rows=read_rows, saved_rows=saved_rows, targets=targets,
                                    source_day=manifest["source_day"], source_available_at=manifest["source_available_at"],
                                    observation_began_at=source["started_at"], read_at=now.isoformat(),
                                    current_valid=is_current, data_root=data_root)
        from .market_roster_observation_change import market_roster_observation_change
        recent_change = market_roster_observation_change(data_root, now=now.isoformat(), expected_latest_receipt=source_path)
        try:
            from .market_roster_activity_context import cumulative_activity_context
            activity = cumulative_activity_context(read_rows=read_rows, saved_rows=saved_rows, quotes=rows, targets=targets,
                          source_clocks={"snapshot_started_at": source["started_at"],
                              "snapshot_response_received_at": source["response_received_at"],
                              "source_day": manifest["source_day"], "source_available_at": manifest["source_available_at"]},
                          read_at=now.isoformat(), data_root=data_root)
        except Exception as error:
            activity = {"schema": "market_roster_research_activity_context_v1", "status": "unavailable_activity_context",
                        "error": f"{type(error).__name__}: {error}", "research_only": True,
                        "selection_enabled": False, "execution_admitted": False, "production_volume_or_orderbook_admitted": False}
        if digest(pointer_path) != pointer_sha:
            return {**result, "status": "unavailable_locator_changed_during_read"}
        return {**result, "status": "current_prior_dated_roster_snapshot" if is_current else "saved_prior_dated_roster_snapshot_not_current",
                "current_summary": current if is_current else None, "observation_summary": saved, "groups": groups,
                "industry_context": industry,
                "recent_change": recent_change,
                "activity_context": activity,
                "read_status_counts": dict(Counter(row["read_status"] for row in read_rows)),
                "observation_status_counts": dict(Counter(row["read_status"] for row in saved_rows)),
                "observation_summary_as_of": received.isoformat(),
                "source_quote_clocks": {"all_parseable_including_stale_or_nonregular": _range(rows),
                    "regular_known_at_final_response": _range([row for row in saved_rows if row["current_at_read"]]),
                    "regular_current_at_read": _range([row for row in read_rows if row["current_at_read"]])}}
    except (OSError, UnicodeError, KeyError, ValueError, TypeError, ArithmeticError) as error:
        return {**result, "status": "unavailable_invalid_saved_market_roster_snapshot", "error": str(error),
                "current_summary": None, "observation_summary": None, "groups": []}
