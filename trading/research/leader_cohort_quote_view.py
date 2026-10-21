"""Read fixed-cohort public quotes without collecting, selecting or changing identities."""
from datetime import datetime
from decimal import Decimal
import json
import math
from pathlib import Path
from statistics import median

from .baostock_daily_slice import digest
from .close_workbench import process_identity
from .index_observation import CN, trading_session
from .index_observation_change import _slot, _time
from .leader_environment import ARCHETYPES
from .leader_identity_registry import _registry
from .leader_identity_view import COHORT_ID


SCHEMA = "fixed_leader_cohort_intraday_quote_view_v1"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _boundary(value, schema):
    from .leader_cohort_observation import FLAGS, SOURCE_SCOPE
    if (value.get("schema") != schema or value.get("source_scope") != SOURCE_SCOPE
            or any(value.get(key) is not expected for key, expected in FLAGS.items())):
        raise ValueError("Fixed-cohort quote research boundary changed")


def _positive(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("Positive finite original RMB price required")
    return result


def summarize(rows):
    """Keep missing members in the full denominator, with conditional statistics named."""
    values = [row["day_change_percent"] for row in rows if row["current_at_read"]]
    n, k = len(rows), len(values)
    up, down = sum(x > 0 for x in values), sum(x < 0 for x in values)
    unknown = n - k
    return {"member_count": n, "known_count": k, "unknown_count": unknown,
        "advance_count": up, "decline_count": down, "flat_count": k - up - down,
        "conditional_median_change_percent": median(values) if values else None,
        "full_cohort_breadth": (up - down) / n if n and not unknown else None,
        "conditional_known_breadth": (up - down) / k if k else None,
        "breadth_bounds": [(up - down - unknown) / n, (up - down + unknown) / n] if n else None}


def _row(row, member, *, now, start, stop, finished, registered_at, failed=False):
    labels = [label["id"] for label in member["labels"]]
    if (row.get("symbol") != member["symbol"] or row.get("registered_name") != member["name"]
            or row.get("frozen_labels") != labels):
        raise ValueError("Saved quote identity or frozen labels differ from the full registered cohort")
    result = {"symbol": member["symbol"], "registered_name": member["name"],
        "source_name": row.get("source_name"), "frozen_labels": labels,
        "status": row.get("status"), "source_quote_time": row.get("source_quote_time"),
        "source_status": row.get("source_status"), "received_at": row.get("received_at"),
        "age_seconds_at_read": None, "current_at_read": False, "day_change_percent": None,
        "last_price_rmb": row.get("last_price_rmb"), "name_changed_from_registry": row.get("name_changed_from_registry"),
        "read_status": "latest_attempt_failed" if failed else "missing_or_invalid"}
    if failed:
        return result
    if row.get("source_status") not in (None, "00"):
        result["read_status"] = "suspended_or_nonregular"
        return result
    try:
        if row.get("source_status") != "00":
            return result
        when, received = _time(row["source_quote_time"]), _time(row["received_at"])
        if when < registered_at:
            result["read_status"] = "source_time_precedes_registration"
            return result
        opening, previous, last, high, low = [_positive(row[key]) for key in
            ("open_price_rmb", "previous_close_price_rmb", "last_price_rmb", "high_price_rmb", "low_price_rmb")]
        if high < max(opening, last) or low > min(opening, last) or low > high:
            return result
        if not start <= received <= finished < stop or not start <= when < stop:
            result["read_status"] = "outside_current_slot"
            return result
        age = (now - when).total_seconds()
        result["age_seconds_at_read"] = age
        if when > received or age < 0:
            result["read_status"] = "future_source_time"
            return result
        change = float((last / previous - 1) * 100)
        if not math.isfinite(change):
            return result
        result.update(age_seconds_at_read=age, day_change_percent=change)
        if (received - when).total_seconds() > 120 or age > 120:
            result["read_status"] = "stale"
        elif not start <= now < stop:
            result["read_status"] = "outside_current_slot"
        else:
            result.update(current_at_read=True, read_status="current")
    except (ValueError, TypeError, KeyError, ArithmeticError):
        pass
    return result


def leader_cohort_intraday_view(data_root, *, now=None):
    from .leader_cohort_observation import FLAGS, JOB_SCHEMA, LATEST_SCHEMA, OBSERVATION_SCHEMA, SUCCESS, SOURCE_SCOPE
    now = _time(now or datetime.now(CN))
    root = Path(data_root).resolve() / "research/leader_cohort_observation"
    registry_path = Path(data_root).resolve() / "research/leader_identity_registry" / COHORT_ID / "registry.json"
    registry, registry_sha, _, _ = _registry(registry_path, now)
    result = {"schema": SCHEMA, "status": "waiting_for_first_cohort_observation", "read_at": now.isoformat(),
        "source_file": None, "provider_contacted": False, "source_scope": SOURCE_SCOPE, "registry": {"file": str(registry_path),
            "sha256": registry_sha, "registered_at": registry["registered_at"],
            "member_count": registry["member_count"], "source_day": registry["source"]["day"]},
        "session": None, "snapshot": None, "collector": None, "current_summary": None,
        "observation_summary": None, "groups": [], "quotes": [], "recent_change": None,
        "full_cohort_retained": True, "display_is_not_portfolio_selection": True,
        "economic_return_or_PnL": False, "cross_regime_leader_verified": False, **FLAGS}
    pointer_path = root / "latest.json"
    if not pointer_path.is_file():
        return result
    pointer = _read(pointer_path)
    _boundary(pointer, LATEST_SCHEMA)
    job_dir = (root / pointer["job_id"]).resolve()
    job_path, path = (Path(pointer[key]).resolve() for key in ("job_receipt", "attempt_receipt"))
    if (job_dir.parent != root or job_path != job_dir / "job_receipt.json"
            or path.name != "receipt.json" or path.parent.parent != job_dir / "observations"
            or pointer.get("registry_sha256") != registry_sha):
        raise ValueError("Saved cohort locator differs from the fixed registry or job paths")
    job, source = _read(job_path), _read(path)
    _boundary(job, JOB_SCHEMA)
    _boundary(source, OBSERVATION_SCHEMA)
    for record in (job, source):
        if (record.get("job_id") != pointer["job_id"] or record.get("registry_file") != str(registry_path)
                or record.get("registry_sha256") != registry_sha
                or record.get("registry_registered_at") != registry["registered_at"]
                or record.get("member_count") != registry["member_count"]):
            raise ValueError("Observation job, registration clock or full cohort changed")
    start, stop = _slot(job)
    for key in ("session_day", "session_name", "session_start", "stop_at"):
        if source.get(key) != job[key]:
            raise ValueError("Quote attempt is not in its declared job session")
    calendar = _read(Path(job["pinned_calendar_path"]))
    if digest(job["pinned_calendar_path"]) != registry["binding"]["calendar_sha256"]:
        raise ValueError("Quote job calendar differs from the registered calendar")
    if trading_session(calendar, _time(job["started_at"]))["session_name"] != job["session_name"]:
        raise ValueError("Quote job was not started in its actual trading slot")
    began, finished = _time(source["started_at"]), _time(source["finished_at"])
    if not _time(registry["registered_at"]) <= _time(job["started_at"]) <= began <= finished <= now:
        raise ValueError("Quote observation cannot predate registration or be available in the future")
    members, rows = registry["members"], source["quotes"]
    if len(rows) != len(members) or [row.get("symbol") for row in rows] != [m["symbol"] for m in members]:
        raise ValueError("All fixed members including missing quotes must remain in canonical order")
    failed = source.get("status") != SUCCESS
    if not failed and (source.get("all_batches_completed") is not True
            or source.get("units", {}).get("prices") != "CNY"
            or source.get("planned_batches") != job.get("planned_batches")
            or source.get("completed_batches") != job["planned_batches"]
            or source.get("quote_request_count") != job["planned_batches"]):
        raise ValueError("Successful snapshot requires every planned technical batch and its RMB contract")
    registered_at = _time(registry["registered_at"])
    read_rows = [_row(row, member, now=now, start=start, stop=stop, finished=finished,
                      registered_at=registered_at, failed=failed)
                 for row, member in zip(rows, members)]
    snapshot_time = _time(source["response_received_at"]) if source.get("response_received_at") else finished
    if not began <= snapshot_time <= finished:
        raise ValueError("Quote response time is outside its actual attempt")
    saved_rows = [_row(row, member, now=snapshot_time, start=start, stop=stop, finished=finished,
                       registered_at=registered_at, failed=failed)
                  for row, member in zip(rows, members)]
    result.update(source_file=str(path), session={key: job[key] for key in
        ("session_day", "session_name", "session_start", "stop_at")},
        snapshot={key: source.get(key) for key in ("started_at", "finished_at", "response_received_at",
            "planned_batches", "completed_batches", "quote_request_count")} | {"attempt_status": source.get("status")},
        collector={"recorded_status": job.get("status"), "attempts": job.get("attempts"),
            "successes": job.get("successes"), "stop_at": job["stop_at"],
            "process": process_identity(job.get("pid"), job.get("process_started_at"))}, quotes=read_rows)
    from .leader_cohort_observation_change import leader_cohort_observation_change
    result["recent_change"] = leader_cohort_observation_change(data_root, now=now, expected_latest_receipt=path)
    if failed:
        return {**result, "status": "unavailable_latest_cohort_attempt", "error": source.get("error"),
                "unavailable_summary": summarize(read_rows)}
    current, saved = summarize(read_rows), summarize(saved_rows)
    is_current = current["known_count"] > 0
    result.update(status="current_fixed_cohort_observation" if is_current else "saved_cohort_observation_not_current",
        current_summary=current if is_current else None, observation_summary=saved)
    for label, name in ARCHETYPES:
        group = [row for row in read_rows if label in row["frozen_labels"]]
        saved_group = [row for row in saved_rows if label in row["frozen_labels"]]
        result["groups"].append({"id": label, "name": name, "member_count": len(group),
            "current_summary": summarize(group) if is_current else None,
            "observation_summary": summarize(saved_group)})
    return result
