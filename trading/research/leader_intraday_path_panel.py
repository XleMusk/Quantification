"""Prospective local anchors and fixed future counterparts for old leader labels.

No source collection, prediction, model, portfolio, order or registry mutation.
Overlapping windows and overlapping labels are descriptive, not independent
trials. The fixed comparator is the prior-roster complement of the old 590 set.
"""
from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from ..market_dataset import write_json
from ..settings import DATA_ROOT
from .baostock_daily_slice import digest
from .close_workbench import _process_start
from .index_observation import CN, _collector_lock
from .index_observation_change import _slot, _time
from .leader_cohort_observation_change import _summary
from .leader_environment import ARCHETYPES
from .market_roster_quote_view import _adapt_row, _range, summarize
from .market_roster_observation_change import _boundary, _pairs, _round
from .prior_dated_market_quote_snapshot import (FLAGS as SOURCE_FLAGS, JOB_SCHEMA, LATEST_SCHEMA,
                                               PREPARED_SCHEMA, SOURCE_SCOPE, SUCCESS)

PROTOCOL_SCHEMA = "leader_intraday_forward_path_panel_protocol_v1"
ANCHOR_SCHEMA = "leader_intraday_forward_path_panel_anchor_v1"
OUTCOME_SCHEMA = "leader_intraday_forward_path_panel_outcome_v1"
REPORT_SCHEMA = "leader_intraday_forward_path_panel_report_v1"
WATCH_SCHEMA = "leader_intraday_forward_path_panel_watch_v1"
FLAGS = {**SOURCE_FLAGS, "provider_contacted": False, "model_refit": False, "model_trained": False,
         "portfolio_created": False, "economic_return_or_PnL": False, "strategy_selector_created": False}
RULE = {"id": "actual_persisted_anchor_first_complete_response_300_420_v1", "response_horizon_seconds": [300, 420],
        "successor_selection": "earliest_complete_response_in_fixed_horizon_before_member_pairing",
        "anchor_current_mask_seconds": 120, "successor_source_after_anchor_persistence": True,
        "comparator": "fixed_prior_roster_non590_rest", "labels": "original_eight_prior_close_labels_no_reclassification",
        "missing_or_incompatible": "unknown_never_zero_drop_or_replace_endpoint", "phase": "prior_close_background_only",
        "overlapping_windows_not_independent": True, "group_labels_overlap": True,
        "prediction_accuracy_computed": False, "winrate_computed": False, "future_labels_used_for_anchor_features": False}


def _now(clock=None):
    return _time(clock() if clock else datetime.now(CN))


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def _new(path, value):
    with Path(path).open("x", encoding="utf8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _job(path, data_root, now):
    root = Path(data_root).resolve() / "research/market_roster_observation"
    path = Path(path).resolve()
    job = _read(path)
    _boundary(job, JOB_SCHEMA)
    folder = root / "jobs" / job["job_id"]
    if path != folder / "job_receipt.json" or folder.resolve().parent != root / "jobs":
        raise ValueError("Panel must pin one exact saved wide-watch job")
    prepared_file = folder / "prepared_source.json"
    if job.get("prepared_source_file") != str(prepared_file) or digest(prepared_file) != job.get("prepared_source_sha256"):
        raise ValueError("Watch prepared source changed")
    prepared = _read(prepared_file)
    _boundary(prepared, PREPARED_SCHEMA)
    if not _time(prepared["created_at"]) <= _time(job["started_at"]) <= now:
        raise ValueError("Watch or prepared evidence is not yet actually available")
    if len(prepared["targets"]) != prepared["target_count"]:
        raise ValueError("Full original target inventory required")
    _slot(job)
    return root, path, job, prepared_file, prepared


def _panel(out, now):
    protocol = _read(out / "protocol.json")
    receipt = _read(out / "registration_receipt.json")
    if (protocol.get("schema") != PROTOCOL_SCHEMA or protocol.get("rule") != RULE
            or any(protocol.get(key) is not value for key, value in FLAGS.items())
            or receipt.get("protocol_sha256") != digest(out / "protocol.json")
            or not _time(protocol["registered_at"]) <= _time(receipt["persisted_at"]) <= now):
        raise ValueError("Immutable panel protocol, registration seal or clock changed")
    data_root = Path(protocol["data_root"])
    root, job_file, job, prepared_file, prepared = _job(protocol["job_file"], data_root, now)
    if (protocol["prepared_source_sha256"] != digest(prepared_file) or protocol["job_id"] != job["job_id"]
            or protocol["target_count"] != prepared["target_count"]
            or protocol["fixed_590_registry"] != prepared["fixed_590_registry"]):
        raise ValueError("Panel cannot switch its watch, target cohort or old labels")
    return protocol, data_root, root, job_file, job, prepared_file, prepared


def register(job_file, out, *, data_root=DATA_ROOT, _clock=None):
    now = _now(_clock)
    out = Path(out).resolve()
    root, job_file, job, prepared_file, prepared = _job(job_file, data_root, now)
    start, stop = _slot(job)
    now = _now(_clock)
    if not start <= now < stop:
        raise ValueError("Register a prospective panel only during its actual saved watch slot")
    if not start <= _now(_clock) < stop:
        raise ValueError("Watch slot ended during registration preparation")
    out.mkdir(parents=True, exist_ok=False)
    (out / "anchors").mkdir(); (out / "outcomes").mkdir()
    protocol = {"schema": PROTOCOL_SCHEMA, "registered_at": now.isoformat(), "data_root": str(Path(data_root).resolve()),
                "job_file": str(job_file), "job_id": job["job_id"], "job_sha256_at_registration": digest(job_file),
                "prepared_source_file": str(prepared_file), "prepared_source_sha256": digest(prepared_file),
                "source_day": prepared["source_day"], "source_available_at": prepared["source_available_at"],
                "target_count": prepared["target_count"], "fixed_590_registry": prepared["fixed_590_registry"],
                "archetypes": [{"id": id_, "name": name} for id_, name in ARCHETYPES], "rule": deepcopy(RULE),
                "source_scope": SOURCE_SCOPE, "session": {key: job[key] for key in ("session_day", "session_name", "session_start", "stop_at")},
                "eod_context_use": "prior_close_background_only_not_intraday_phase", **FLAGS}
    _new(out / "protocol.json", protocol)
    _new(out / "registration_receipt.json", {"schema": "leader_intraday_panel_registration_seal_v1",
         "protocol_sha256": digest(out / "protocol.json"), "persisted_at": _now(_clock).isoformat(), **FLAGS})
    return _report(out, protocol, [], _now(_clock))


def _load_round(path, protocol, data_root, root, job_file, job, prepared_file, prepared, now):
    return _round(path, root=root, job=job, job_file=job_file, prepared=prepared, prepared_file=prepared_file,
                  prepared_sha=protocol["prepared_source_sha256"], data_root=data_root, now=now)


def _indices(targets):
    predicates = [("all", "完整前交易日名册", lambda target: True),
                  ("fixed_590", "固定590集合", lambda target: target["in_fixed_590"]),
                  ("rest", "固定非590其余名册", lambda target: not target["in_fixed_590"]),
                  ("prior_day_nonST", "前交易日非ST背景", lambda target: target["background_isST"] == "0"),
                  ("prior_day_ST", "前交易日ST背景", lambda target: target["background_isST"] == "1"),
                  ("prior_day_active_nonST", "前交易日正常交易非ST背景", lambda target: target["background_isST"] == "0" and target["background_trade_status"] == "1")]
    groups = [(id_, name, [i for i, target in enumerate(targets) if predicate(target)]) for id_, name, predicate in predicates]
    archetypes = [(id_, name, [i for i, target in enumerate(targets) if id_ in target["fixed_590_labels"]]) for id_, name in ARCHETYPES]
    return groups, archetypes


def _freeze(out, protocol, data_root, root, job_file, job, prepared_file, prepared, clock):
    now = _now(clock)
    start, stop = _slot(job)
    if not start <= now < stop:
        return "outside_watch_slot_no_new_anchor"
    pointer_bytes = (root / "latest.json").read_bytes()
    pointer, pointer_sha = json.loads(pointer_bytes), sha256(pointer_bytes).hexdigest()
    _boundary(pointer, LATEST_SCHEMA)
    path = Path(pointer["receipt_file"]).resolve()
    record = _read(path)
    if record.get("status") != SUCCESS or pointer.get("snapshot_status") != SUCCESS:
        return "latest_started_or_failed_no_fallback"
    if record.get("watch_job_id") != job["job_id"] or record.get("watch_job_file") != str(job_file):
        return "latest_belongs_to_another_watch_no_fallback"
    anchor_id = path.parent.name
    folder = out / "anchors" / anchor_id
    if folder.exists():
        return "latest_round_already_frozen"
    bundle = _load_round(path, protocol, data_root, root, job_file, job, prepared_file, prepared, now)
    created = _now(clock)
    if not start <= created < stop or created < _time(_read(out / "registration_receipt.json")["persisted_at"]):
        return "outside_actual_registration_or_slot_no_anchor_written"
    rows = [_adapt_row(row, target, now=created, start=bundle["start"], stop=bundle["stop"], finished=bundle["finished"],
                       available=bundle["available"], began=bundle["began"], received=bundle["received"])
            for row, target in zip(record["quotes"], bundle["targets"])]
    groups, archetypes = _indices(bundle["targets"])
    def feature(item):
        return {"id": item[0], "name": item[1], "member_count": len(item[2]), "summary": summarize([rows[i] for i in item[2]])}
    anchor = {"schema": ANCHOR_SCHEMA, "anchor_id": anchor_id, "created_at": created.isoformat(),
              "response_received_at": bundle["received"].isoformat(), "source_file": str(path), "source_sha256": digest(path),
              "targets_file": record["targets_file"], "targets_sha256": record["targets_sha256"],
              "prepared_source_sha256": protocol["prepared_source_sha256"], "target_count": len(rows),
              "current_mask": [{"symbol": row["symbol"], "current_at_anchor": row["current_at_read"], "reason": row["read_status"]} for row in rows],
              "features": {"summary": summarize(rows), "groups": [feature(item) for item in groups],
                           "archetype_groups": [feature(item) for item in archetypes],
                           "source_quote_clocks": {"all_parseable": _range(rows), "current_at_anchor": _range([row for row in rows if row["current_at_read"]])}},
              "feature_clock_basis": "actual_anchor_created_at_not_old_packet_capture_time", **FLAGS}
    if digest(root / "latest.json") != pointer_sha:
        return "latest_changed_during_freeze_no_anchor_written"
    if not start <= _now(clock) < stop:
        return "slot_ended_during_freeze_no_anchor_written"
    folder.mkdir(exist_ok=False)
    _new(folder / "anchor.json", anchor)
    _new(folder / "seal.json", {"schema": "leader_intraday_anchor_seal_v1", "anchor_sha256": digest(folder / "anchor.json"),
         "persisted_at": _now(clock).isoformat(), **FLAGS})
    return "new_actual_anchor_frozen"


def _anchor(folder, protocol, now):
    anchor, seal = _read(folder / "anchor.json"), _read(folder / "seal.json")
    if (anchor.get("schema") != ANCHOR_SCHEMA or anchor.get("anchor_id") != folder.name
            or anchor.get("prepared_source_sha256") != protocol["prepared_source_sha256"]
            or seal.get("anchor_sha256") != digest(folder / "anchor.json")
            or any(anchor.get(key) is not expected for key, expected in FLAGS.items())
            or not _time(anchor["created_at"]) <= _time(seal["persisted_at"]) <= now):
        raise ValueError("Immutable anchor seal or actual write clock changed")
    return anchor, seal


def _outcome_groups(rows, targets):
    groups, archetypes = _indices(targets)
    rest = _summary([row for row, target in zip(rows, targets) if not target["in_fixed_590"]])
    reference = rest["conditional_median_window_return_percent"]
    def group(item):
        summary = _summary([rows[i] for i in item[2]])
        median = summary["conditional_median_window_return_percent"]
        return {"id": item[0], "name": item[1], "member_count": len(item[2]), "summary": summary,
                "conditional_median_difference_vs_rest_pct": median - reference if median is not None and reference is not None else None}
    return [group(item) for item in groups], [group(item) for item in archetypes]


def _score(out, anchor_folder, protocol, data_root, root, job_file, job, prepared_file, prepared, clock):
    now = _now(clock)
    anchor, seal = _anchor(anchor_folder, protocol, now)
    destination = out / "outcomes" / (anchor["anchor_id"] + ".json")
    if destination.exists():
        return None
    baseline_path = Path(anchor["source_file"])
    if digest(baseline_path) != anchor["source_sha256"]:
        raise ValueError("Anchor raw quote reference changed")
    baseline = _load_round(baseline_path, protocol, data_root, root, job_file, job, prepared_file, prepared, now)
    if [row["symbol"] for row in anchor["current_mask"]] != [target["symbol"] for target in baseline["targets"]]:
        raise ValueError("Anchor current mask lost original full membership")
    choices = []
    for value in job.get("attempt_receipts", []):
        path = Path(value).resolve()
        record = _read(path)
        if record.get("status") != SUCCESS or record.get("all_batches_completed") is not True:
            continue
        received = _time(record["response_received_at"])
        span = (received - baseline["received"]).total_seconds()
        if 300 <= span <= 420:
            choices.append((received, str(path)))
    pending = {"anchor_id": anchor["anchor_id"], "status": "pending", "anchor_response_received_at": anchor["response_received_at"]}
    if not choices and now <= baseline["received"] + timedelta(seconds=420) and now < baseline["stop"]:
        return pending
    selected = min(choices) if choices else None
    written = max(_time(anchor["created_at"]), _time(seal["persisted_at"]))
    if selected:
        successor = _load_round(selected[1], protocol, data_root, root, job_file, job, prepared_file, prepared, now)
        rows = _pairs(baseline, successor, now)
        response_prospective = written < successor["received"]
        for row, mask in zip(rows, anchor["current_mask"]):
            reason = ("anchor_price_not_current_at_actual_creation" if not mask["current_at_anchor"] else
                      "successor_response_not_after_actual_anchor_persistence" if not response_prospective else
                      "successor_source_not_after_actual_anchor_persistence" if row["paired"] and _time(row["latest_source_quote_time"]) <= written else None)
            if reason:
                row.update(paired=False, current_at_read=False, window_return_percent=None, reason=reason)
    else:
        successor, response_prospective = None, False
        rows = [{"symbol": target["symbol"], "paired": False, "current_at_read": False, "window_return_percent": None,
                 "reason": "no_complete_future_counterpart_in_fixed_horizon"} for target in baseline["targets"]]
    summary = _summary(rows)
    groups, archetypes = _outcome_groups(rows, baseline["targets"])
    value = {"schema": OUTCOME_SCHEMA, "anchor_id": anchor["anchor_id"], "derived_at": _now(clock).isoformat(),
             "status": "terminal_unknown_no_future_counterpart" if not selected else "observed" if summary["paired_count"] else "observed_unknown",
             "anchor_created_at": anchor["created_at"], "anchor_persisted_at": seal["persisted_at"],
             "selected_successor_receipt": selected[1] if selected else None,
             "selected_successor_sha256": digest(selected[1]) if selected else None,
             "anchor_response_received_at": anchor["response_received_at"],
             "successor_response_received_at": successor["received"].isoformat() if successor else None,
             "response_span_seconds": (successor["received"] - baseline["received"]).total_seconds() if successor else None,
             "prospective_claim": bool(response_prospective and summary["paired_count"]), "prospective_response_eligible": response_prospective,
             "summary": summary, "groups": groups, "archetype_groups": archetypes, "members": rows,
             "unknown_reason_counts": dict(Counter(row["reason"] for row in rows if not row["paired"])),
             "future_outcome_is_prediction_accuracy": False, **FLAGS}
    _new(destination, value)
    return None


def _report(out, protocol, pending, now, freeze_status=None):
    anchors = []
    for folder in sorted((out / "anchors").iterdir()):
        anchor, seal = _anchor(folder, protocol, now)
        anchors.append({key: value for key, value in anchor.items() if key != "current_mask"} |
                       {"persisted_at": seal["persisted_at"], "anchor_file": str(folder / "anchor.json"), "anchor_sha256": digest(folder / "anchor.json")})
    outcomes = []
    for path in sorted((out / "outcomes").glob("*.json")):
        value = _read(path)
        outcomes.append({key: item for key, item in value.items() if key != "members"} |
                        {"outcome_file": str(path), "outcome_sha256": digest(path)})
    result = {"schema": REPORT_SCHEMA, "status": "observing_prospective_research_panel" if anchors else "registered_waiting_for_anchor",
              "registered_at": protocol["registered_at"], "derived_at": now.isoformat(), "job_id": protocol["job_id"],
              "source_day": protocol["source_day"], "target_count": protocol["target_count"],
              "protocol_file": str(out / "protocol.json"), "protocol_sha256": digest(out / "protocol.json"),
              "anchor_count": len(anchors), "outcome_count": len(outcomes), "pending_count": len(pending),
              "anchors": anchors, "outcomes": outcomes, "pending": pending, "latest_freeze_status": freeze_status,
              "overlapping_windows_not_independent": True, "group_labels_overlap": True, "rule": deepcopy(RULE), **FLAGS}
    write_json(out / "report.json", result)
    return result


def advance(out, *, _clock=None):
    out = Path(out).resolve()
    if not (out / "protocol.json").is_file():
        raise ValueError("Register this new panel before advancing")
    with _collector_lock(out):
        protocol, data_root, root, job_file, job, prepared_file, prepared = _panel(out, _now(_clock))
        pending = []
        for folder in sorted((out / "anchors").iterdir()):
            item = _score(out, folder, protocol, data_root, root, job_file, job, prepared_file, prepared, _clock)
            if item:
                pending.append(item)
        freeze = _freeze(out, protocol, data_root, root, job_file, job, prepared_file, prepared, _clock)
        # A just-created anchor remains pending until a later advance; it never
        # consumes already-inspected outcomes as if it had existed beforehand.
        known_pending = {item["anchor_id"] for item in pending}
        for folder in (out / "anchors").iterdir():
            if not (out / "outcomes" / (folder.name + ".json")).exists() and folder.name not in known_pending:
                anchor, _ = _anchor(folder, protocol, _now(_clock))
                pending.append({"anchor_id": folder.name, "status": "pending", "anchor_response_received_at": anchor["response_received_at"]})
        return _report(out, protocol, pending, _now(_clock), freeze)


def watch(out, *, interval_seconds=30, max_advances=120, _clock=None, _sleep=None):
    """Advance only local evidence in the same slot; no sources or retries."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 30:
        raise ValueError("Panel watch interval must be an integer of at least 30 seconds")
    if isinstance(max_advances, bool) or not isinstance(max_advances, int) or not 1 <= max_advances <= 120:
        raise ValueError("Panel watch advances must be an integer from 1 to 120")
    out = Path(out).resolve()
    protocol, _, _, _, job, _, _ = _panel(out, _now(_clock))
    start, stop = _slot(job)
    actual = _now(_clock)
    if not start <= actual < stop:
        raise ValueError("Start panel watch only in the actual saved slot")
    path = out / "watch_receipt.json"
    if path.exists():
        raise FileExistsError("This panel already has a watch receipt; do not retry or overwrite")
    sleep = _sleep or time.sleep
    with _collector_lock(out / "watch_owner"):
        if path.exists():
            raise FileExistsError("This panel already has a watch owner")
        actual = _now(_clock)
        if not start <= actual < stop:
            raise ValueError("Slot ended before panel watch ownership was acquired")
        record = {"schema": WATCH_SCHEMA, "status": "running", "started_at": actual.isoformat(),
                  "finished_at": None, "pid": os.getpid(), "job_id": protocol["job_id"],
                  "panel_out": str(out), "interval_seconds": interval_seconds, "max_advances": max_advances,
                  "advances": 0, "next_scheduled_at": actual.isoformat(), "stop_at": stop.isoformat(),
                  "last_report_derived_at": None, "auto_retry": False, **FLAGS}
        try:
            record["process_started_at"] = datetime.fromtimestamp(_process_start(os.getpid()), CN).isoformat()
            record["process_identity_status"] = "observed_os_process_start"
        except (OSError, ValueError, ImportError) as error:
            record.update(process_started_at=None, process_identity_status="identity_unverified", process_identity_error=str(error))
        write_json(path, record)
        try:
            while record["advances"] < max_advances:
                began = _now(_clock)
                report = advance(out, _clock=_clock)
                record.update(advances=record["advances"] + 1, last_report_derived_at=report["derived_at"],
                              anchor_count=report["anchor_count"], outcome_count=report["outcome_count"], pending_count=report["pending_count"])
                if began >= stop:
                    record.update(status="completed_session_boundary", stop_reason="actual_slot_boundary_finalized")
                    break
                if record["advances"] >= max_advances:
                    record.update(status="completed_max_advances", stop_reason="fixed_advance_cap_reached")
                    break
                if _now(_clock) >= stop:
                    # A pre-close read may finish after the boundary. Score once
                    # more with a post-close clock, without creating an anchor.
                    record["next_scheduled_at"] = _now(_clock).isoformat()
                    write_json(path, record)
                    continue
                next_due = min(began + timedelta(seconds=interval_seconds), stop)
                record["next_scheduled_at"] = next_due.isoformat()
                write_json(path, record)
                while True:
                    remaining = (next_due - _now(_clock)).total_seconds()
                    if remaining <= 0:
                        break
                    sleep(min(30, remaining))
        except KeyboardInterrupt:
            record.update(status="interrupted", stop_reason="operator_interrupted")
        except Exception as error:
            record.update(status="failed_stopped_no_retry", error=f"{type(error).__name__}: {error}", stop_reason="first_local_advance_failure")
        record.update(finished_at=_now(_clock).isoformat(), next_scheduled_at=None)
        write_json(path, record)
        return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register"); reg.add_argument("--job", type=Path, required=True); reg.add_argument("--out", type=Path, required=True)
    adv = sub.add_parser("advance"); adv.add_argument("--out", type=Path, required=True)
    watcher = sub.add_parser("watch"); watcher.add_argument("--out", type=Path, required=True)
    watcher.add_argument("--interval-seconds", type=int, default=30); watcher.add_argument("--max-advances", type=int, default=120)
    args = parser.parse_args(argv)
    if args.command == "watch":
        result = watch(args.out, interval_seconds=args.interval_seconds, max_advances=args.max_advances)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"].startswith("completed_") else 2
    result = register(args.job, args.out) if args.command == "register" else advance(args.out)
    print(json.dumps({key: result[key] for key in ("schema", "status", "anchor_count", "outcome_count", "pending_count", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
