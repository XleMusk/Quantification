"""Read the fixed saved forward panel summary, never advance or load raw quotes."""
from datetime import date, datetime
from hashlib import sha256
import json
import math
from pathlib import Path

from .leader_intraday_path_panel import (
    ANCHOR_SCHEMA, FLAGS, OUTCOME_SCHEMA, PROTOCOL_SCHEMA, REPORT_SCHEMA, RULE,
)
from .leader_environment import ARCHETYPES

RUN = "20261015_fixed_forward_v1"
SCHEMA = "leader_intraday_forward_path_panel_read_only_view_v1"
GROUP_IDS = ("all", "fixed_590", "rest", "prior_day_nonST", "prior_day_ST", "prior_day_active_nonST")
OUTCOME_STATUSES = ("observed", "observed_unknown", "terminal_unknown_no_future_counterpart")


def _stamp(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Forward-panel evidence clocks must include a timezone")
    return value


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Forward-panel counts must be nonnegative integers")
    return value


def _boundary(value, schema):
    if not isinstance(value, dict) or value.get("schema") != schema or any(value.get(key) is not expected for key, expected in FLAGS.items()):
        raise ValueError("Forward-panel schema or research boundaries changed")


def _summary(value, count, *, outcome=False):
    known = "paired_count" if outcome else "known_count"
    moves = ("up_count", "down_count", "flat_count") if outcome else ("advance_count", "decline_count", "flat_count")
    median = "conditional_median_window_return_percent" if outcome else "conditional_median_change_percent"
    if (_count(value["member_count"]) != count
            or _count(value[known]) + _count(value["unknown_count"]) != count
            or sum(_count(value[key]) for key in moves) != value[known]):
        raise ValueError("Forward-panel summary lost its complete denominator")
    number = value.get(median)
    if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)):
        raise ValueError("Forward-panel conditional median must be finite or unknown")
    if value[known] == 0 and number is not None:
        raise ValueError("Unknown or empty groups cannot acquire a conditional median")
    return value


def _groups(value, *, outcome=False):
    for key, ids in (("groups", GROUP_IDS), ("archetype_groups", tuple(item[0] for item in ARCHETYPES))):
        rows = value[key]
        if [row["id"] for row in rows] != list(ids):
            raise ValueError("Forward-panel groups must retain their fixed original order")
        for row in rows:
            _summary(row["summary"], _count(row["member_count"]), outcome=outcome)
            if outcome:
                difference = row.get("conditional_median_difference_vs_rest_pct")
                if difference is not None and (isinstance(difference, bool) or not isinstance(difference, (int, float)) or not math.isfinite(difference)):
                    raise ValueError("Saved relative conditional median must be finite or unknown")
                if row["summary"]["paired_count"] == 0 and difference is not None:
                    raise ValueError("An empty or unknown type has no relative conditional median")


def leader_intraday_path_panel_view(data_root, *, now=None):
    """Expose the latest anchor and latest terminal counterpart separately, from one fixed run."""
    now = now or datetime.now().astimezone()
    _stamp(now.isoformat())
    data_root = Path(data_root).resolve()
    folder = data_root / "research/leader_intraday_path_panel" / RUN
    protocol_file, report_file = folder / "protocol.json", folder / "report.json"
    base = {"schema": SCHEMA, "read_at": now.isoformat(), "source_file": str(report_file),
            "protocol_file": str(protocol_file), "fixed_run": RUN, "protocol": None,
            "anchor_count": 0, "outcome_count": 0, "pending_count": 0, "outcome_status_counts": {},
            "latest_anchor": None, "latest_outcome": None, "derived_at": None,
            "overlapping_windows_not_independent": True, "group_labels_overlap": True,
            "raw_quotes_loaded": False, "panel_advanced": False, "fallback_to_other_run": False,
            "account_accessed": False, **FLAGS}
    if not protocol_file.is_file():
        if report_file.exists():
            raise ValueError("A saved panel report is missing its fixed protocol")
        return {**base, "status": "waiting_for_fixed_panel_registration"}
    protocol_raw = protocol_file.read_bytes()
    protocol = json.loads(protocol_raw)
    _boundary(protocol, PROTOCOL_SCHEMA)
    registered = _stamp(protocol["registered_at"])
    if (protocol.get("rule") != RULE or registered > now
            or Path(protocol["data_root"]).resolve() != data_root
            or [row["id"] for row in protocol["archetypes"]] != [item[0] for item in ARCHETYPES]):
        raise ValueError("Fixed forward-panel protocol, registration clock or original labels changed")
    date.fromisoformat(protocol["source_day"])
    target_count = _count(protocol["target_count"])
    if target_count == 0:
        raise ValueError("A fixed panel needs its full nonempty member inventory")
    public_protocol = {key: protocol[key] for key in ("registered_at", "job_id", "source_day", "source_available_at", "target_count", "archetypes", "session", "rule")}
    base["protocol"] = public_protocol
    if not report_file.is_file():
        return {**base, "status": "registered_waiting_for_saved_report"}
    report = json.loads(report_file.read_bytes())
    _boundary(report, REPORT_SCHEMA)
    derived = _stamp(report["derived_at"])
    if (report.get("rule") != RULE or report.get("protocol_file") != str(protocol_file)
            or report.get("protocol_sha256") != sha256(protocol_raw).hexdigest()
            or any(report[key] != protocol[key] for key in ("registered_at", "job_id", "source_day", "target_count"))
            or report.get("overlapping_windows_not_independent") is not True
            or report.get("group_labels_overlap") is not True or not registered <= derived <= now
            or report.get("status") not in ("registered_waiting_for_anchor", "observing_prospective_research_panel")):
        raise ValueError("Saved panel summary is not bound to its actual fixed protocol and clocks")
    anchors, outcomes, pending = (report[key] for key in ("anchors", "outcomes", "pending"))
    if any(_count(report[key]) != len(rows) for key, rows in (("anchor_count", anchors), ("outcome_count", outcomes), ("pending_count", pending))):
        raise ValueError("Saved panel counts disagree with its complete summary inventory")
    anchor_by_id = {}
    for anchor in anchors:
        _boundary(anchor, ANCHOR_SCHEMA)
        identity = anchor["anchor_id"]
        if (identity in anchor_by_id or anchor["target_count"] != target_count
                or not registered <= _stamp(anchor["created_at"]) <= _stamp(anchor["persisted_at"]) <= derived
                or _stamp(anchor["response_received_at"]) > _stamp(anchor["created_at"])):
            raise ValueError("Anchor identity, full denominator or actual persistence clock changed")
        _summary(anchor["features"]["summary"], target_count)
        _groups(anchor["features"])
        anchor_by_id[identity] = anchor
    outcome_ids, status_counts = set(), {status: 0 for status in OUTCOME_STATUSES}
    for outcome in outcomes:
        _boundary(outcome, OUTCOME_SCHEMA)
        identity = outcome["anchor_id"]
        anchor = anchor_by_id.get(identity)
        if anchor is None or identity in outcome_ids or outcome.get("status") not in OUTCOME_STATUSES:
            raise ValueError("Each terminal counterpart must bind one existing fixed anchor")
        _summary(outcome["summary"], target_count, outcome=True)
        _groups(outcome, outcome=True)
        if ((outcome["status"] == "observed") != (outcome["summary"]["paired_count"] > 0)):
            raise ValueError("Observed and unknown terminal states disagree with the saved full summary")
        if (outcome["anchor_created_at"] != anchor["created_at"] or outcome["anchor_persisted_at"] != anchor["persisted_at"]
                or outcome["anchor_response_received_at"] != anchor["response_received_at"]
                or not _stamp(anchor["persisted_at"]) <= _stamp(outcome["derived_at"]) <= derived
                or outcome.get("future_outcome_is_prediction_accuracy") is not False):
            raise ValueError("Counterpart clocks cannot rewrite an earlier anchor or become prediction accuracy")
        successor = outcome.get("successor_response_received_at")
        if successor is not None:
            end = _stamp(successor)
            span = (end - _stamp(anchor["response_received_at"])).total_seconds()
            if (not 300 <= span <= 420 or span != outcome.get("response_span_seconds")
                    or end > _stamp(outcome["derived_at"])):
                raise ValueError("Saved successor is outside its fixed actual response horizon")
        elif outcome.get("status") != "terminal_unknown_no_future_counterpart" or outcome["summary"]["paired_count"] != 0:
            raise ValueError("No successor must remain terminal unknown, never a zero return")
        if outcome.get("prospective_claim") is True and (successor is None or not _stamp(anchor["persisted_at"]) < _stamp(successor) or outcome["summary"]["paired_count"] == 0):
            raise ValueError("Recorded prospective claim lacks actual after-persistence evidence")
        outcome_ids.add(identity)
        status_counts[outcome["status"]] += 1
    pending_ids = [row["anchor_id"] for row in pending]
    if (len(set(pending_ids)) != len(pending_ids) or any(row.get("status") != "pending" for row in pending)
            or set(pending_ids) & outcome_ids or set(pending_ids) | outcome_ids != set(anchor_by_id)):
        raise ValueError("Pending and terminal summaries must retain every fixed anchor exactly once")
    latest_anchor = max(anchors, key=lambda row: (_stamp(row["created_at"]), row["anchor_id"])) if anchors else None
    latest_outcome = max(outcomes, key=lambda row: (_stamp(row["derived_at"]), row["anchor_id"])) if outcomes else None
    # The report already excludes per-stock masks/members. Do not load their immutable files.
    if latest_anchor:
        latest_anchor = {key: latest_anchor[key] for key in ("anchor_id", "created_at", "persisted_at", "response_received_at", "source_file", "source_sha256", "features")}
    if latest_outcome:
        latest_outcome = {key: latest_outcome[key] for key in ("anchor_id", "derived_at", "status", "anchor_created_at", "anchor_persisted_at", "anchor_response_received_at", "selected_successor_receipt", "successor_response_received_at", "response_span_seconds", "prospective_claim", "summary", "groups", "archetype_groups")}
    return {**base, "status": report["status"], "derived_at": report["derived_at"],
            "anchor_count": len(anchors), "outcome_count": len(outcomes), "pending_count": len(pending),
            "outcome_status_counts": status_counts, "latest_anchor": latest_anchor, "latest_outcome": latest_outcome}
