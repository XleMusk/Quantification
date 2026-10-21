"""Read a fixed-cohort, same-job five-minute price window from saved receipts.

The batch response clock selects the baseline before any member pairing. Missing
or incompatible members stay in the registered denominator, never at zero.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
from statistics import median

from .baostock_daily_slice import digest
from .index_observation import CN, trading_session
from .index_observation_change import _slot, _time
from .leader_cohort_observation import (FLAGS, JOB_SCHEMA, LATEST_SCHEMA, OBSERVATION_SCHEMA,
                                       REGISTRY_RELATIVE, SOURCE_SCOPE, SUCCESS)
from .leader_cohort_quote_view import _boundary, _positive, _read, _row
from .leader_environment import ARCHETYPES
from .leader_identity_registry import _registry

SCHEMA = "fixed_leader_cohort_intraday_change_view_v1"
POLICY = "same_job_same_slot_response_span_300_to_420_seconds_minimum_excess_then_newest_response_no_pair_or_return_selection"


def _summary(rows, *, current=False):
    values = [row["window_return_percent"] for row in rows
              if row["paired"] and (not current or row["current_at_read"])]
    count, paired = len(rows), len(values)
    up, down = sum(value > 0 for value in values), sum(value < 0 for value in values)
    unknown = count - paired
    return {"member_count": count, "paired_count": paired, "unknown_count": unknown,
            "up_count": up, "down_count": down, "flat_count": paired - up - down,
            "conditional_median_window_return_percent": median(values) if values else None,
            "full_cohort_breadth": (up - down) / count if count and not unknown else None,
            "conditional_paired_breadth": (up - down) / paired if paired else None,
            "breadth_bounds": [(up - down - unknown) / count, (up - down + unknown) / count] if count else None}


def _unknown(member, reason):
    return {"symbol": member["symbol"], "registered_name": member["name"],
            "frozen_labels": [label["id"] for label in member["labels"]], "paired": False,
            "current_at_read": False, "window_span_seconds": None, "window_return_percent": None,
            "reason": reason, "baseline_source_quote_time": None, "latest_source_quote_time": None}


def _record(record, *, job, registry, registry_sha, registry_path, start, stop, now):
    """Validate technical completeness independently of valid-member coverage."""
    _boundary(record, OBSERVATION_SCHEMA)
    if record.get("status") != SUCCESS:
        raise ValueError("A successful complete technical batch round is required")
    for key in ("job_id", "session_day", "session_name", "session_start", "stop_at"):
        if record.get(key) != job[key]:
            raise ValueError("Saved baseline belongs to a different job or trading slot")
    if (record.get("registry_file") != str(registry_path) or record.get("registry_sha256") != registry_sha
            or record.get("registry_registered_at") != registry["registered_at"]
            or record.get("member_count") != registry["member_count"]
            or record.get("calendar_sha256") != registry["binding"]["calendar_sha256"]):
        raise ValueError("Saved baseline registry, full scope or pinned calendar changed")
    planned = job["planned_batches"]
    if (record.get("all_batches_completed") is not True or record.get("units", {}).get("prices") != "CNY"
            or record.get("planned_batches") != planned or record.get("completed_batches") != planned
            or record.get("quote_request_count") != planned):
        raise ValueError("Every planned technical batch must have completed")
    began, received, finished = [_time(record[key]) for key in ("started_at", "response_received_at", "finished_at")]
    if not _time(job["started_at"]) <= began <= received <= finished <= now or not start <= began <= finished < stop:
        raise ValueError("Observation availability or same-slot clock is inconsistent")
    rows, members = record["quotes"], registry["members"]
    if len(rows) != len(members) or [row.get("symbol") for row in rows] != [member["symbol"] for member in members]:
        raise ValueError("Every fixed member including unknowns must remain in canonical order")
    # Identity validation does not require usable prices, source timestamps, or a
    # successful individual quote; those remain member-level unknowns below.
    for row, member in zip(rows, members):
        if (row.get("registered_name") != member["name"]
                or row.get("frozen_labels") != [label["id"] for label in member["labels"]]):
            raise ValueError("Frozen member names or labels changed")
    return {"record": record, "began": began, "received": received, "finished": finished}


def _endpoint(row, member, batch, *, registered_at, start, stop):
    """Judge an endpoint at its own actual reception, not the later read clock."""
    try:
        received = _time(row["received_at"])
        if not batch["began"] <= received <= batch["received"]:
            return None, "member_received_time_outside_attempt"
        checked = _row(row, member, now=received, start=start, stop=stop,
                       finished=batch["finished"], registered_at=registered_at)
        if not checked["current_at_read"]:
            return None, checked["read_status"]
        return {"stamp": _time(row["source_quote_time"]), "last": _positive(row["last_price_rmb"]),
                "previous": _positive(row["previous_close_price_rmb"])}, None
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None, "missing_or_invalid_endpoint"


def _pair(member, baseline_row, latest_row, baseline, latest, *, registered_at, start, stop, now):
    result = _unknown(member, "unpaired")
    first, reason = _endpoint(baseline_row, member, baseline, registered_at=registered_at, start=start, stop=stop)
    if first is None:
        result["reason"] = "baseline_" + reason
        return result
    end, reason = _endpoint(latest_row, member, latest, registered_at=registered_at, start=start, stop=stop)
    if end is None:
        result["reason"] = "latest_" + reason
        return result
    span = (end["stamp"] - first["stamp"]).total_seconds()
    result.update(baseline_source_quote_time=first["stamp"].isoformat(), latest_source_quote_time=end["stamp"].isoformat(),
                  window_span_seconds=span)
    if not 300 <= span <= 420:
        result["reason"] = "member_source_span_outside_300_to_420_seconds"
        return result
    if first["previous"] != end["previous"]:
        result["reason"] = "previous_close_reference_changed"
        return result
    value = float((end["last"] / first["last"] - Decimal(1)) * 100)
    if not math.isfinite(value):
        result["reason"] = "nonfinite_window_return"
        return result
    age = (now - end["stamp"]).total_seconds()
    result.update(paired=True, window_return_percent=value, reason=None,
                  current_at_read=start <= now < stop and 0 <= age <= 120)
    return result


def leader_cohort_observation_change(data_root, *, now=None, expected_latest_receipt=None):
    """Describe saved five-minute cohort changes; no collector or registry writes."""
    now = _time(now or datetime.now(CN))
    result = {"schema": SCHEMA, "status": "missing", "read_at": now.isoformat(), "job_id": None,
              "latest_receipt": None, "baseline_receipt": None, "latest_response_received_at": None,
              "baseline_response_received_at": None, "window_response_span_seconds": None,
              "scanned_prior_success_count": 0, "summary": None, "groups": [], "quotes": [],
              "current_hint": None, "source_scope": SOURCE_SCOPE, "provider_contacted": False,
              "baseline_policy": POLICY, "used_for_close_analysis": False, "economic_return_or_PnL": False,
              "all_fixed_members_retained": True, "cross_regime_leader_verified": False, **FLAGS}
    data_root = Path(data_root).resolve()
    root = data_root / "research/leader_cohort_observation"
    try:
        pointer_path = root / "latest.json"
        if not pointer_path.is_file():
            return result
        pointer_sha = digest(pointer_path)
        pointer = _read(pointer_path)
        _boundary(pointer, LATEST_SCHEMA)
        job_id = pointer["job_id"]
        job_dir = (root / job_id).resolve()
        job_path, latest_path = [Path(pointer[key]).resolve() for key in ("job_receipt", "attempt_receipt")]
        if (job_dir.parent != root or job_path != job_dir / "job_receipt.json"
                or latest_path.name != "receipt.json" or latest_path.parent.parent != job_dir / "observations"):
            raise ValueError("Saved cohort change locator escapes the fixed job")
        result.update(latest_receipt=str(latest_path), job_id=job_id)
        if expected_latest_receipt is not None and Path(expected_latest_receipt).resolve() != latest_path:
            return {**result, "status": "unavailable_locator_changed_during_read"}
        registry_path = data_root / REGISTRY_RELATIVE
        registry, registry_sha, _, _ = _registry(registry_path, now)
        if pointer.get("registry_sha256") != registry_sha:
            raise ValueError("Latest pointer belongs to another registry")
        job = _read(job_path)
        _boundary(job, JOB_SCHEMA)
        if (job.get("job_id") != job_id or job.get("registry_file") != str(registry_path)
                or job.get("registry_sha256") != registry_sha
                or job.get("registry_registered_at") != registry["registered_at"]
                or job.get("member_count") != registry["member_count"]
                or job.get("calendar_sha256") != registry["binding"]["calendar_sha256"]):
            raise ValueError("Job and sealed registry disagree")
        start, stop = _slot(job)
        registered_at = _time(registry["registered_at"])
        if not registered_at <= _time(job["started_at"]) <= now:
            raise ValueError("Job cannot predate registration or be in the future")
        pinned = Path(job["pinned_calendar_path"]).resolve()
        if pinned != job_dir / "calendar.json" or digest(pinned) != registry["binding"]["calendar_sha256"]:
            raise ValueError("Saved job calendar differs from the registry")
        if trading_session(_read(pinned), _time(job["started_at"]))["session_name"] != job["session_name"]:
            raise ValueError("Job was not begun in its claimed trading slot")
        result.update(session_day=job["session_day"], session_name=job["session_name"],
                      registry_sha256=registry_sha, member_count=registry["member_count"],
                      quotes=[_unknown(member, "no_selected_baseline") for member in registry["members"]])
        latest_record = _read(latest_path)
        _boundary(latest_record, OBSERVATION_SCHEMA)
        if latest_record.get("job_id") != job_id:
            raise ValueError("Latest observation belongs to a different job")
        if latest_record.get("status") != SUCCESS:
            return {**result, "status": "unavailable_latest_attempt", "error": latest_record.get("error")}
        latest = _record(latest_record, job=job, registry=registry, registry_sha=registry_sha,
                         registry_path=registry_path, start=start, stop=stop, now=now)
        result["latest_response_received_at"] = latest["received"].isoformat()
        choices = []
        for candidate in (job_dir / "observations").glob("*/receipt.json"):
            candidate = candidate.resolve()
            if candidate == latest_path:
                continue
            if candidate.parent.parent != job_dir / "observations":
                raise ValueError("Saved baseline escapes the declared job")
            record = _read(candidate)
            if record.get("status") != SUCCESS:
                continue
            received = _time(record["response_received_at"])
            if received >= latest["received"]:
                continue
            span = (latest["received"] - received).total_seconds()
            if not 300 <= span <= 420:
                continue
            baseline = _record(record, job=job, registry=registry, registry_sha=registry_sha,
                               registry_path=registry_path, start=start, stop=stop, now=now)
            result["scanned_prior_success_count"] += 1
            choices.append((span - 300, -received.timestamp(), str(candidate), baseline))
        if not choices:
            result.update(status="unknown_no_common_300_to_420_second_baseline", summary=_summary(result["quotes"]))
            return result
        _, _, baseline_path, baseline = min(choices, key=lambda choice: choice[:3])
        quotes = [_pair(member, first, end, baseline, latest, registered_at=registered_at,
                        start=start, stop=stop, now=now) for member, first, end in
                  zip(registry["members"], baseline["record"]["quotes"], latest["record"]["quotes"])]
        summary, current = _summary(quotes), _summary(quotes, current=True)
        is_current = current["paired_count"] > 0
        groups = []
        for id_, name in ARCHETYPES:
            rows = [row for row in quotes if id_ in row["frozen_labels"]]
            groups.append({"id": id_, "name": name, "member_count": len(rows), "summary": _summary(rows),
                           "current_hint": _summary(rows, current=True) if is_current else None})
        if digest(pointer_path) != pointer_sha:
            return {**result, "status": "unavailable_locator_changed_during_read", "summary": None, "current_hint": None}
        status = ("unknown_no_compatible_member_pairs" if not summary["paired_count"] else
                  "current_same_slot_cohort_change" if is_current else "historical_same_slot_cohort_change_not_current")
        return {**result, "status": status,
                "baseline_receipt": baseline_path, "baseline_response_received_at": baseline["received"].isoformat(),
                "window_response_span_seconds": (latest["received"] - baseline["received"]).total_seconds(),
                "summary": summary, "current_hint": current if is_current else None, "groups": groups, "quotes": quotes}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, InvalidOperation) as error:
        return {**result, "status": "unavailable_invalid_saved_cohort_change", "error": str(error),
                "summary": None, "groups": [], "current_hint": None}
