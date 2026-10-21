"""Same-watch five-minute changes of the complete frozen prior-session roster.

Response clocks choose a baseline before member pairing. The full prior-roster
denominator and its old fixed-cohort label relationships never change with price.
"""
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..settings import DATA_ROOT
from .baostock_daily_slice import digest
from .index_observation import CN
from .index_observation_change import _slot, _time
from .leader_cohort_observation_change import _pair, _summary
from .leader_cohort_quote_view import _read
from .leader_environment import ARCHETYPES
from .market_roster_quote_view import _target_binding
from .prior_dated_market_quote_snapshot import (FLAGS, JOB_SCHEMA, LATEST_SCHEMA, OBSERVATION_SCHEMA,
                                               PREPARED_SCHEMA, SOURCE_SCOPE, SUCCESS, TARGETS_SCHEMA)

SCHEMA = "prior_dated_market_roster_intraday_change_view_v1"
POLICY = "same_watch_prepared_source_response_span_300_to_420_seconds_minimum_excess_then_newest_response_before_member_pairing"


def _boundary(value, schema):
    if (value.get("schema") != schema or value.get("source_scope") != SOURCE_SCOPE
            or any(value.get(key) is not expected for key, expected in FLAGS.items())):
        raise ValueError("Wide-roster change research boundaries changed")


def _round(path, *, root, job, job_file, prepared, prepared_file, prepared_sha, data_root, now):
    path = Path(path).resolve()
    if path.name != "receipt.json" or path.parent.parent != root:
        raise ValueError("Watch receipt escapes its direct snapshot directory")
    record = _read(path)
    _boundary(record, OBSERVATION_SCHEMA)
    if (record.get("status") != SUCCESS or record.get("watch") is not True
            or record.get("observation_id") != path.parent.name
            or record.get("watch_job_id") != job["job_id"] or record.get("watch_job_file") != str(job_file)
            or record.get("prepared_source_file") != str(prepared_file)
            or record.get("prepared_source_sha256") != prepared_sha):
        raise ValueError("Round belongs to a different watch or prepared source")
    targets_file = path.parent / "targets.json"
    if record.get("targets_file") != str(targets_file) or digest(targets_file) != record.get("targets_sha256"):
        raise ValueError("Each round must retain its own targets path and hash")
    manifest = _read(targets_file)
    _boundary(manifest, TARGETS_SCHEMA)
    targets = manifest["targets"]
    for key in ("source_package", "source_day", "source_available_at", "source_proof", "calendar_sha256",
                "target_count", "fixed_590_registry"):
        if manifest.get(key) != prepared.get(key):
            raise ValueError("Round changed the prepared source, calendar or fixed-label relationship")
    if targets != prepared["targets"]:
        raise ValueError("The complete original prepared target order and context must remain fixed")
    _, available = _target_binding(manifest, record, targets, data_root=data_root, folder=path.parent, now=now)
    start, stop = _slot(record)
    if (start, stop) != _slot(job):
        raise ValueError("Round cannot cross the declared watch slot")
    began, received, finished = [_time(record[key]) for key in ("started_at", "response_received_at", "finished_at")]
    if not _time(job["started_at"]) <= began <= received <= finished <= now or not start <= began <= finished < stop:
        raise ValueError("Round response/completion time is future or outside its actual slot")
    planned = (len(targets) + 99) // 100
    if (record.get("all_batches_completed") is not True or record.get("batch_size") != 100
            or record.get("units", {}).get("prices") != "CNY"
            or any(record.get(key) != planned for key in ("planned_batches", "completed_batches", "quote_request_count"))):
        raise ValueError("Only complete successful technical rounds can supply a window")
    rows = record["quotes"]
    if len(rows) != len(targets) or [row.get("symbol") for row in rows] != [target["symbol"] for target in targets]:
        raise ValueError("All prepared targets including unknowns must stay in their original order")
    context = ("symbol", "provider_symbol", "roster_code", "roster_name", "source_day", "background_isST",
               "background_trade_status", "in_fixed_590", "fixed_590_labels")
    if any(any(row.get(key) != target.get(key) for key in context) for row, target in zip(rows, targets)):
        raise ValueError("Saved member context differs from its fixed prepared target")
    return {"record": record, "targets": targets, "available": available, "start": start, "stop": stop,
            "began": began, "received": received, "finished": finished}


def _adapt_quote(row, target):
    return {**row, "registered_name": target["roster_name"], "frozen_labels": target["fixed_590_labels"],
            "name_changed_from_registry": row.get("name_changed_from_dated_roster")}


def _pairs(baseline, latest, now):
    rows = []
    for target, first, end in zip(latest["targets"], baseline["record"]["quotes"], latest["record"]["quotes"]):
        member = {"symbol": target["symbol"], "name": target["roster_name"],
                  "labels": [{"id": label} for label in target["fixed_590_labels"]]}
        row = _pair(member, _adapt_quote(first, target), _adapt_quote(end, target), baseline, latest,
                    registered_at=latest["available"], start=latest["start"], stop=latest["stop"], now=now)
        row["roster_name"] = row.pop("registered_name")
        row["fixed_590_labels"] = row.pop("frozen_labels")
        if row["reason"]:
            row["reason"] = row["reason"].replace("source_time_precedes_registration", "source_time_precedes_roster_availability")
        rows.append(row)
    return rows


def _group(id_, name, rows, current):
    summary = _summary(rows)
    spans = [row["window_span_seconds"] for row in rows if row["paired"]]
    return {"id": id_, "name": name, "member_count": len(rows), "summary": summary,
            "current_hint": _summary(rows, current=True) if current else None,
            "status": "unknown_no_prior_label_members" if not rows else
                      "unknown_no_compatible_member_pairs" if not summary["paired_count"] else "observed_paired_price_proxy",
            "paired_source_span_seconds": {"minimum": min(spans) if spans else None, "maximum": max(spans) if spans else None},
            "unknown_reason_counts": dict(Counter(row["reason"] for row in rows if not row["paired"]))}


def market_roster_observation_change(data_root=DATA_ROOT, *, now=None, expected_latest_receipt=None):
    """Read one exact same-watch window, without source calls or side effects."""
    now = _time(now or datetime.now(CN))
    data_root = Path(data_root).resolve()
    root = data_root / "research/market_roster_observation"
    result = {"schema": SCHEMA, "status": "missing", "read_at": now.isoformat(), "source_scope": SOURCE_SCOPE,
              "latest_receipt": None, "baseline_receipt": None, "latest_response_received_at": None,
              "baseline_response_received_at": None, "window_response_span_seconds": None,
              "summary": None, "current_hint": None, "groups": [], "archetype_groups": [],
              "activity_context": None,
              "baseline_policy": POLICY, "scanned_prior_success_count": 0,
              "archetype_label_scope": "fixed_590_prior_close_labels_relationship_only_not_current_leader_identity",
              "all_prior_roster_members_retained": True, "economic_return_or_PnL": False,
              "provider_contacted": False, "used_for_close_analysis": False, **FLAGS}
    try:
        pointer_file = root / "latest.json"
        if not pointer_file.is_file():
            return result
        pointer_sha = digest(pointer_file)
        pointer = _read(pointer_file)
        _boundary(pointer, LATEST_SCHEMA)
        latest_path = Path(pointer["receipt_file"]).resolve()
        if latest_path != root / pointer["observation_id"] / "receipt.json" or latest_path.parent.parent != root:
            raise ValueError("Latest snapshot locator escapes its fixed root")
        result["latest_receipt"] = str(latest_path)
        if expected_latest_receipt is not None and Path(expected_latest_receipt).resolve() != latest_path:
            return {**result, "status": "unavailable_locator_changed_during_read"}
        latest_record = _read(latest_path)
        _boundary(latest_record, OBSERVATION_SCHEMA)
        result["latest_attempt_status"] = latest_record.get("status")
        if latest_record.get("status") != SUCCESS or pointer.get("snapshot_status") != SUCCESS:
            return {**result, "status": "unavailable_latest_attempt"}
        if latest_record.get("watch") is not True:
            return {**result, "status": "no_same_watch_window"}
        job_id = latest_record["watch_job_id"]
        job_dir = root / "jobs" / job_id
        job_file, prepared_file = [Path(latest_record[key]).resolve() for key in ("watch_job_file", "prepared_source_file")]
        if (job_dir.resolve().parent != root / "jobs" or job_file != job_dir / "job_receipt.json"
                or prepared_file != job_dir / "prepared_source.json"):
            raise ValueError("Watch or prepared-source path escapes its declared job")
        job, prepared = _read(job_file), _read(prepared_file)
        _boundary(job, JOB_SCHEMA)
        _boundary(prepared, PREPARED_SCHEMA)
        prepared_sha = digest(prepared_file)
        if (job.get("job_id") != job_id or job.get("prepared_source_file") != str(prepared_file)
                or job.get("prepared_source_sha256") != prepared_sha
                or latest_record.get("prepared_source_sha256") != prepared_sha
                or not _time(prepared["created_at"]) <= _time(job["started_at"]) <= now):
            raise ValueError("Watch prepared-source hash or actual availability changed")
        for key in ("source_package", "source_day", "source_available_at", "target_count"):
            if job.get(key) != prepared.get(key):
                raise ValueError("Watch job and prepared source disagree")
        result.update(watch_job_id=job_id, watch_job_file=str(job_file), prepared_source_file=str(prepared_file),
                      prepared_source_sha256=prepared_sha, source_day=prepared["source_day"], member_count=prepared["target_count"])
        latest = _round(latest_path, root=root, job=job, job_file=job_file, prepared=prepared, prepared_file=prepared_file,
                        prepared_sha=prepared_sha, data_root=data_root, now=now)
        if pointer.get("targets_file") != latest_record["targets_file"] or pointer.get("targets_sha256") != latest_record["targets_sha256"]:
            raise ValueError("Latest snapshot and its target pointer disagree")
        result.update(latest_response_received_at=latest["received"].isoformat(),
                      session_day=job["session_day"], session_name=job["session_name"])
        choices = []
        for value in job.get("attempt_receipts", []):
            candidate = Path(value).resolve()
            if candidate == latest_path:
                continue
            if candidate.name != "receipt.json" or candidate.parent.parent != root:
                raise ValueError("Saved watch candidate escapes the snapshot root")
            prior = _read(candidate)
            if prior.get("status") != SUCCESS:
                continue
            received = _time(prior["response_received_at"])
            span = (latest["received"] - received).total_seconds()
            if not 300 <= span <= 420:
                continue
            baseline = _round(candidate, root=root, job=job, job_file=job_file, prepared=prepared, prepared_file=prepared_file,
                              prepared_sha=prepared_sha, data_root=data_root, now=now)
            actual_span = (latest["received"] - baseline["received"]).total_seconds()
            if received != baseline["received"] or not 300 <= actual_span <= 420:
                raise ValueError("Baseline response clock changed during this read")
            result["scanned_prior_success_count"] += 1
            choices.append((actual_span - 300, -baseline["received"].timestamp(), str(candidate), baseline))
        if not choices:
            return {**result, "status": "unknown_no_common_300_to_420_second_baseline"}
        _, _, baseline_path, baseline = min(choices, key=lambda value: value[:3])
        rows = _pairs(baseline, latest, now)
        summary, current = _summary(rows), _summary(rows, current=True)
        is_current = current["paired_count"] > 0
        predicates = [("all", f"{prepared['source_day']}完整沪深普通A股名册", lambda target: True),
                      ("fixed_590", "原固定590集合在本名册内成员", lambda target: target["in_fixed_590"]),
                      ("rest", "本名册其余成员", lambda target: not target["in_fixed_590"]),
                      ("prior_day_nonST", "前交易日非ST背景", lambda target: target["background_isST"] == "0"),
                      ("prior_day_ST", "前交易日ST背景", lambda target: target["background_isST"] == "1"),
                      ("prior_day_active_nonST", "前交易日正常交易且非ST背景", lambda target: target["background_isST"] == "0" and target["background_trade_status"] == "1")]
        groups = [_group(id_, name, [row for row, target in zip(rows, latest["targets"]) if predicate(target)], is_current)
                  for id_, name, predicate in predicates]
        archetypes = [_group(id_, name, [row for row in rows if id_ in row["fixed_590_labels"]], is_current) for id_, name in ARCHETYPES]
        try:
            from .market_roster_activity_context import window_activity_context
            activity = window_activity_context(pairs=rows, baseline_quotes=baseline["record"]["quotes"],
                           latest_quotes=latest["record"]["quotes"], targets=latest["targets"],
                           source_clocks={"baseline_response_received_at": baseline["received"].isoformat(),
                               "latest_response_received_at": latest["received"].isoformat(),
                               "source_day": prepared["source_day"], "source_available_at": prepared["source_available_at"]},
                           read_at=now.isoformat(), data_root=data_root)
        except Exception as error:
            activity = {"schema": "market_roster_research_activity_context_v1", "status": "unavailable_activity_context",
                        "error": f"{type(error).__name__}: {error}", "research_only": True,
                        "selection_enabled": False, "execution_admitted": False, "production_volume_or_orderbook_admitted": False}
        if digest(pointer_file) != pointer_sha:
            return {**result, "status": "unavailable_locator_changed_during_read"}
        status = ("unknown_no_compatible_member_pairs" if not summary["paired_count"] else
                  "current_same_watch_roster_change" if is_current else "historical_same_watch_roster_change_not_current")
        spans = [row["window_span_seconds"] for row in rows if row["paired"]]
        return {**result, "status": status, "baseline_receipt": baseline_path,
                "baseline_response_received_at": baseline["received"].isoformat(),
                "window_response_span_seconds": (latest["received"] - baseline["received"]).total_seconds(),
                "summary": summary, "current_hint": current if is_current else None, "groups": groups,
                "archetype_groups": archetypes,
                "activity_context": activity,
                "paired_source_span_seconds": {"minimum": min(spans) if spans else None, "maximum": max(spans) if spans else None},
                "unknown_reason_counts": dict(Counter(row["reason"] for row in rows if not row["paired"]))}
    except (OSError, UnicodeError, KeyError, ValueError, TypeError, ArithmeticError) as error:
        return {**result, "status": "unavailable_invalid_saved_market_roster_change", "error": str(error),
                "summary": None, "current_hint": None, "groups": [], "archetype_groups": []}
