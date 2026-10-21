"""One public snapshot of a strictly complete prior-session SH/SZ A-share roster.

Prior-day status and ST fields remain background labels. This is neither a
current-universe eligibility feed nor the prospective leader identity registry.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import requests

from ..market_dataset import write_json
from ..settings import DATA_ROOT
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete_close
from .close_workbench import _process_start
from .exchange_calendar import DEFAULT_CALENDAR_PATH
from .index_observation import CN, _aware, _collector_lock, trading_session
from .leader_cohort_observation import (BATCH_SIZE, CONTRACT_RELATIVE, ENDPOINT, FLAGS as COHORT_FLAGS,
                                       REGISTRY_RELATIVE, _member, _provider_symbol, parse_batch)
from .leader_daily_signals import _calendar_view
from .leader_identity_registry import _registry
from .leader_recent_source_history import source_proof
from .provider_close_batches import _acquisition_times

TARGETS_SCHEMA = "prior_dated_SH_SZ_A_quote_targets_v1"
OBSERVATION_SCHEMA = "sina_prior_dated_SH_SZ_A_quote_snapshot_v1"
LATEST_SCHEMA = "market_roster_quote_latest_v1"
JOB_SCHEMA = "prior_dated_market_roster_watch_job_v1"
JOB_LATEST_SCHEMA = "market_roster_watch_latest_job_v1"
PREPARED_SCHEMA = "prior_dated_market_roster_watch_prepared_source_v1"
SUCCESS = "completed_prior_dated_roster_snapshot_with_member_unknowns_retained"
SOURCE_SCOPE = "prior_complete_dated_SH_SZ_A_roster_intraday_price_proxy"
FLAGS = {**COHORT_FLAGS, "research_only": True, "current_universe_verified": False,
         "new_listings_unknown": True, "removals_unknown": True, "today_ST_eligibility_verified": False,
         "source_verified": False, "pit_verified": False, "corporate_actions_verified": False,
         "frozen_prior_dated_roster": True, "includes_BSE": False}


def _now():
    return datetime.now(CN)


def _stamp(value):
    return _aware(datetime.fromisoformat(str(value)))


def _member_input(target):
    # Pure parser input only: no registration, registry file, or leader labels
    # are constructed for the wider prior-dated roster.
    return {"symbol": target["symbol"], "name": target["roster_name"], "labels": []}


def _adapt(quote, target):
    quote = dict(quote)
    quote.pop("registered_name")
    quote.pop("frozen_labels")
    quote["name_changed_from_dated_roster"] = quote.pop("name_changed_from_registry")
    if quote.get("unknown_reason") == "source_time_precedes_registration":
        quote["unknown_reason"] = "source_time_precedes_roster_availability"
    if quote.get("status") == "unknown_source_time_precedes_registration":
        quote["status"] = "unknown_source_time_precedes_roster_availability"
    return {**quote, **target}


def _pending(target, batch_id):
    return _adapt(_member(_member_input(target), batch_id), target)


def _same_slot(payload, actual, expected):
    if trading_session(payload, actual) != expected:
        raise ValueError("Actual trading slot changed; snapshot stops without retry")


def _source(package, calendar, payload, observed, data_root):
    metadata, raw, ordinary = load_complete_close(package)
    day = metadata["plan"]["day"]
    observed_day = observed.date().isoformat()
    _, previous, status = _calendar_view(payload, observed_day, observed_day, [])
    calendar_sha = digest(calendar)
    if status != "pinned_static_exchange_calendar" or previous.get(observed_day) != day:
        raise ValueError("Source must be the exact prior session of the actual observation day")
    proof = source_proof(raw, metadata, calendar_sha)
    clocks = _acquisition_times(metadata, package)
    available = clocks["terminal_completion_observed_at"]
    if available is None or not _stamp(day + "T15:00:00+08:00") <= _stamp(available) <= observed:
        raise ValueError("Actual full-source completion must be recorded, after its close and already available")
    if (len(raw) != len(ordinary) or raw.code.tolist() != ordinary.code.tolist()
            or raw.tradestatus.tolist() != ordinary.tradeStatus.tolist()
            or ordinary.symbol.duplicated().any() or not raw.isST.isin(["0", "1"]).all()):
        raise ValueError("Prior raw/status/ST background must match the complete own dated roster")
    registry_file = data_root / REGISTRY_RELATIVE
    registry, registry_sha, _, _ = _registry(registry_file, observed)
    members = {member["symbol"]: member for member in registry["members"]}
    targets = []
    for source, roster in zip(raw.to_dict("records"), ordinary.to_dict("records")):
        symbol = roster["symbol"]
        targets.append({"symbol": symbol, "provider_symbol": _provider_symbol(symbol),
                        "roster_code": roster["code"], "roster_name": roster["code_name"], "source_day": day,
                        "background_isST": source["isST"], "background_trade_status": source["tradestatus"],
                        "in_fixed_590": symbol in members,
                        "fixed_590_labels": [label["id"] for label in members[symbol]["labels"]] if symbol in members else []})
    if not targets:
        raise ValueError("Complete ordinary prior-dated target inventory cannot be empty")
    proof.update(source_receipt_file=str(package / "receipt.json"), source_receipt_sha256=digest(package / "receipt.json"),
                 source_plan_file=str(package / "plan.json"), source_plan_sha256=digest(package / "plan.json"),
                 raw_daily_file=str(package / "raw_daily.csv"),
                 roster_raw_file=str(Path(clocks["roster_receipt"]).parent / f"query_all_stock_{day}.csv"),
                 **clocks)
    return targets, proof, available, {"file": str(registry_file), "sha256": registry_sha,
           "registered_at": registry["registered_at"], "member_count": len(members),
           "present_in_prior_roster_count": sum(target["in_fixed_590"] for target in targets),
           "absent_from_prior_roster_symbols": sorted(set(members) - {target["symbol"] for target in targets}),
           "relationship_only_not_registry_mutation": True}


def _progress(out, record):
    write_json(out / "progress.json", {key: record[key] for key in ("schema", "status", "source_scope", "started_at",
               "response_received_at", "planned_batches", "completed_batches", "quote_request_count", "batches")})


def _latest(root, out, record, actual):
    write_json(root / "latest.json", {"schema": LATEST_SCHEMA, "observation_id": out.name,
               "receipt_file": str(out / "receipt.json"), "targets_file": str(out / "targets.json"),
               "targets_sha256": record["targets_sha256"], "updated_at": actual,
               "snapshot_status": record["status"], "source_scope": SOURCE_SCOPE, **FLAGS})


def _prepare_source_once(package, calendar, calendar_bytes, payload, session, observed, data_root, clock):
    """Pin strict source/registry/contract evidence once, before any snapshots."""
    targets, proof, available, registry = _source(package, calendar, payload, observed, data_root)
    actual = _aware(clock())
    _same_slot(payload, actual, session)
    if digest(calendar) != sha256(calendar_bytes).hexdigest():
        raise ValueError("Pinned source calendar changed during snapshot preparation")
    contract_file = data_root / CONTRACT_RELATIVE
    contract_bytes = contract_file.read_bytes()
    contract = json.loads(contract_bytes)
    if (contract.get("schema") != "sina_ordinary_SH_SZ_A_price_time_contract_assessment_v1"
            or _stamp(contract["created_at"]) > actual):
        raise ValueError("Actually available ordinary-A source contract required")
    return {"targets": targets, "proof": proof, "available": available, "registry": registry,
            "package": package, "calendar_bytes": calendar_bytes, "payload": payload, "session": session,
            "contract_file": contract_file, "contract_bytes": contract_bytes, "prepared_at": actual.isoformat()}


def _sample_prepared(prepared, out, root, clock, get, *, watch_job=None):
    """One serial sampling loop shared by one-shot and bounded watch."""
    targets, proof, available, registry = [prepared[key] for key in ("targets", "proof", "available", "registry")]
    package, calendar_bytes, payload, session = [prepared[key] for key in ("package", "calendar_bytes", "payload", "session")]
    contract_file, contract_bytes = prepared["contract_file"], prepared["contract_bytes"]
    actual = _aware(clock())
    _same_slot(payload, actual, session)
    if out.parent != root or out.exists():
        raise ValueError("Snapshot must use one new direct directory; no overwrite or resume")
    out.mkdir(parents=True, exist_ok=False)
    (out / "calendar.json").write_bytes(calendar_bytes)
    manifest = {"schema": TARGETS_SCHEMA, "created_at": actual.isoformat(), "source_scope": SOURCE_SCOPE,
                "source_day": proof["day"], "observation_day": actual.date().isoformat(),
                "source_available_at": available, "source_package": str(package), "source_proof": proof,
                "calendar_file": str(out / "calendar.json"), "calendar_sha256": sha256(calendar_bytes).hexdigest(),
                "fixed_590_registry": registry, "target_count": len(targets), "targets": targets,
                "target_order": "original_strict_complete_dated_ordinary_roster_order",
                "background_labels_are_not_current_eligibility": True, **FLAGS}
    write_json(out / "targets.json", manifest)
    record = {key: manifest[key] for key in ("source_day", "observation_day", "source_available_at", "source_package",
              "source_proof", "calendar_file", "calendar_sha256", "fixed_590_registry", "target_count")}
    record.update(schema=OBSERVATION_SCHEMA, status="started", started_at=actual.isoformat(),
                  observation_id=out.name, pid=os.getpid(),
                  response_received_at=None, finished_at=None, source_scope=SOURCE_SCOPE, targets_file=str(out / "targets.json"),
                  targets_sha256=digest(out / "targets.json"), batch_size=BATCH_SIZE,
                  planned_batches=(len(targets) + BATCH_SIZE - 1) // BATCH_SIZE,
                  completed_batches=0, quote_request_count=0, all_batches_completed=False,
                  auto_retry=False, redirect_followed=False, watch=watch_job is not None,
                  freshness_is_as_of_observation_only=True, consumer_must_recompute_source_age=True,
                  source_contract_file=str(contract_file), source_contract_sha256=sha256(contract_bytes).hexdigest(),
                  units={"prices": "CNY", "volume_and_amount": "original_source_strings_not_normalized"},
                  quotes=[_pending(target, index // BATCH_SIZE) for index, target in enumerate(targets)], batches=[],
                  **{key: value.isoformat() if isinstance(value, datetime) else value for key, value in session.items()}, **FLAGS)
    try:
        record["process_started_at"] = datetime.fromtimestamp(_process_start(os.getpid()), timezone.utc).isoformat()
        record["process_identity_status"] = "observed_os_process_start"
    except (OSError, ImportError, ValueError) as error:
        record.update(process_started_at=None, process_identity_status="identity_unverified", process_identity_error=str(error))
    if watch_job is not None:
        record.update(watch_job_file=watch_job["file"], watch_job_id=watch_job["job_id"],
                      prepared_source_file=watch_job["prepared_source_file"], prepared_source_sha256=watch_job["prepared_source_sha256"])
    write_json(out / "receipt.json", record)
    _latest(root, out, record, actual.isoformat())
    try:
        for offset in range(0, len(targets), BATCH_SIZE):
            before = _aware(clock())
            _same_slot(payload, before, session)
            batch_id = offset // BATCH_SIZE
            chunk = targets[offset:offset + BATCH_SIZE]
            batch = {"batch_id": batch_id, "target_count": len(chunk), "requested_at": before.isoformat(),
                     "response_received_at": None, "status": "started", "raw_file": None, "raw_sha256": None}
            record["batches"].append(batch)
            for quote in record["quotes"][offset:offset + len(chunk)]:
                quote.update(status="unknown_batch_incomplete", unknown_reason="batch_request_incomplete")
            record["quote_request_count"] += 1
            _progress(out, record)
            remaining = (session["stop_at"] - before).total_seconds()
            endpoint = ENDPOINT + ",".join(target["provider_symbol"] for target in chunk)
            with get(endpoint, timeout=(min(5, remaining), min(10, remaining)), allow_redirects=False, stream=True,
                     headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}) as response:
                batch.update(http_status=response.status_code,
                             response_headers={key: response.headers.get(key) for key in ("Date", "Content-Type", "Content-Length", "Location")})
                pieces, size = [], 0
                for piece in response.iter_content(16384):
                    size += len(piece)
                    if size > 262144:
                        raise ValueError("Quote batch response exceeds 256 KB")
                    pieces.append(piece)
                raw = b"".join(pieces)
                received = _aware(clock())
                record["response_received_at"] = batch["response_received_at"] = received.isoformat()
                raw_file = out / "batches" / f"batch_{batch_id:03d}.raw"
                raw_file.parent.mkdir(exist_ok=True)
                raw_file.write_bytes(raw)
                batch.update(raw_file=str(raw_file), raw_sha256=sha256(raw).hexdigest(), raw_bytes=len(raw))
                _same_slot(payload, received, session)
                if response.status_code != 200:
                    raise ValueError("Expected one nonredirect HTTP200 response; no retry")
            parsed, counts = parse_batch(raw, [_member_input(target) for target in chunk], received,
                                         session=session, registered_at=_stamp(available), batch_id=batch_id)
            record["quotes"][offset:offset + len(chunk)] = [_adapt(quote, target) for quote, target in zip(parsed, chunk)]
            batch.update(status="completed_transport_and_grammar", **counts)
            record["completed_batches"] += 1
            _progress(out, record)
        record.update(status=SUCCESS, all_batches_completed=True)
    except (Exception, KeyboardInterrupt) as error:
        record.update(status="unavailable_single_attempt_stopped", error=f"{type(error).__name__}: {error}")
        if record["batches"] and record["batches"][-1]["status"] == "started":
            failed = record["batches"][-1]
            failed.update(status="failed_stopped_no_retry", error=record["error"])
            for quote in record["quotes"]:
                if quote["batch_id"] == failed["batch_id"]:
                    quote.update(status="unknown_failed_batch", unknown_reason="whole_batch_transport_or_grammar_failed",
                                 received_at=failed["response_received_at"])
    record.update(finished_at=_aware(clock()).isoformat(), current_quote_count=sum(quote["current_quote"] for quote in record["quotes"]))
    record["unknown_quote_count"] = len(targets) - record["current_quote_count"]
    write_json(out / "receipt.json", record)
    _progress(out, record)
    _latest(root, out, record, record["finished_at"])
    return out, record


def run(*, package, calendar=DEFAULT_CALENDAR_PATH, out, data_root=DATA_ROOT, _clock=None, _get=None):
    """Perform one serial snapshot; a failed stage is never retried or resumed."""
    clock, get = _clock or _now, _get or requests.get
    observed = _aware(clock())
    data_root, package, calendar, out = [Path(path).resolve() for path in (data_root, package, calendar, out)]
    calendar_bytes = calendar.read_bytes()
    payload = json.loads(calendar_bytes)
    try:
        session = trading_session(payload, observed)
    except ValueError as error:
        return None, {"schema": OBSERVATION_SCHEMA, "status": "waiting_outside_current_trading_slot",
                      "observed_at": observed.isoformat(), "reason": str(error), "quote_request_count": 0,
                      "source_scope": SOURCE_SCOPE, **FLAGS}
    root = data_root / "research/market_roster_observation"
    if out.parent != root:
        raise ValueError("Snapshot destination must be one new directory directly under research/market_roster_observation")
    if out.exists():
        raise ValueError("Snapshot destination already exists; do not overwrite or resume")
    with _collector_lock(root):
        prepared = _prepare_source_once(package, calendar, calendar_bytes, payload, session, observed, data_root, clock)
        return _sample_prepared(prepared, out, root, clock, get)


def _save_job(folder, job, actual):
    job["updated_at"] = actual
    write_json(folder / "job_receipt.json", job)
    write_json(folder.parent / "latest.json", {"schema": JOB_LATEST_SCHEMA, "job_id": job["job_id"],
               "job_receipt": str(folder / "job_receipt.json"), "updated_at": actual,
               "job_status": job["status"], "source_scope": SOURCE_SCOPE, **FLAGS})


def watch(*, package, calendar=DEFAULT_CALENDAR_PATH, data_root=DATA_ROOT,
          interval_seconds=180, max_observations=30, _clock=None, _sleep=None, _get=None):
    """Reuse one frozen source for bounded same-slot snapshots, without retries."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 180:
        raise ValueError("Wide-roster interval must be an integer of at least 180 seconds")
    if isinstance(max_observations, bool) or not isinstance(max_observations, int) or not 1 <= max_observations <= 30:
        raise ValueError("Wide-roster max observations must be an integer from 1 to 30")
    clock, sleep, get = _clock or _now, _sleep or time.sleep, _get or requests.get
    observed = _aware(clock())
    data_root, package, calendar = [Path(path).resolve() for path in (data_root, package, calendar)]
    calendar_bytes = calendar.read_bytes()
    payload = json.loads(calendar_bytes)
    try:
        session = trading_session(payload, observed)
    except ValueError as error:
        return None, {"schema": JOB_SCHEMA, "status": "waiting_outside_current_trading_slot",
                      "observed_at": observed.isoformat(), "reason": str(error), "quote_request_count": 0,
                      "source_scope": SOURCE_SCOPE, **FLAGS}
    root = data_root / "research/market_roster_observation"
    with _collector_lock(root):
        prepared = _prepare_source_once(package, calendar, calendar_bytes, payload, session, observed, data_root, clock)
        started = _aware(clock())
        _same_slot(payload, started, session)
        job_id = started.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
        folder = root / "jobs" / job_id
        folder.mkdir(parents=True, exist_ok=False)
        frozen = {"schema": PREPARED_SCHEMA, "created_at": prepared["prepared_at"], "source_scope": SOURCE_SCOPE,
                  "source_package": str(package), "source_day": prepared["proof"]["day"],
                  "source_available_at": prepared["available"], "source_proof": prepared["proof"],
                  "calendar_sha256": sha256(calendar_bytes).hexdigest(), "target_count": len(prepared["targets"]),
                  "targets": prepared["targets"], "fixed_590_registry": prepared["registry"],
                  "source_contract_file": str(prepared["contract_file"]),
                  "source_contract_sha256": sha256(prepared["contract_bytes"]).hexdigest(),
                  "strict_source_preparation_count": 1, **FLAGS}
        prepared_file = folder / "prepared_source.json"
        write_json(prepared_file, frozen)
        job_file = folder / "job_receipt.json"
        job = {"schema": JOB_SCHEMA, "job_id": job_id, "status": "running", "started_at": started.isoformat(),
               "finished_at": None, "source_scope": SOURCE_SCOPE, "pid": os.getpid(), "watch": True,
               "interval_seconds": interval_seconds, "max_observations": max_observations,
               "attempts": 0, "successes": 0, "quote_request_count": 0, "attempt_receipts": [],
               "latest_receipt": None, "last_success_receipt": None, "next_scheduled_at": started.isoformat(),
               "prepared_source_file": str(prepared_file), "prepared_source_sha256": digest(prepared_file),
               "source_package": str(package), "source_day": frozen["source_day"], "source_available_at": frozen["source_available_at"],
               "target_count": frozen["target_count"], "planned_batches_per_snapshot": (frozen["target_count"] + BATCH_SIZE - 1) // BATCH_SIZE,
               "strict_source_preparation_count": 1, "freshness_limit_seconds": 120, "auto_retry": False,
               **{key: value.isoformat() if isinstance(value, datetime) else value for key, value in session.items()}, **FLAGS}
        try:
            job["process_started_at"] = datetime.fromtimestamp(_process_start(os.getpid()), timezone.utc).isoformat()
            job["process_identity_status"] = "observed_os_process_start"
        except (OSError, ImportError, ValueError) as error:
            job.update(process_started_at=None, process_identity_status="identity_unverified", process_identity_error=str(error))
        _save_job(folder, job, started.isoformat())
        context = {"file": str(job_file), "job_id": job_id,
                   "prepared_source_file": str(prepared_file), "prepared_source_sha256": job["prepared_source_sha256"]}
        try:
            while True:
                before = _aware(clock())
                if before >= session["stop_at"]:
                    job.update(status="completed_session_boundary", stop_reason="current_slot_boundary_reached")
                    break
                _same_slot(payload, before, session)
                destination = root / ("watch_" + job_id + f"_{job['attempts'] + 1:03d}")
                out, record = _sample_prepared(prepared, destination, root, clock, get, watch_job=context)
                receipt = str(out / "receipt.json")
                job.update(attempts=job["attempts"] + 1, latest_receipt=receipt,
                           quote_request_count=job["quote_request_count"] + record["quote_request_count"])
                job["attempt_receipts"].append(receipt)
                if record["status"] != SUCCESS:
                    job.update(status="failed_stopped_no_retry", error=record.get("error"), stop_reason="latest_snapshot_failed")
                    break
                job.update(successes=job["successes"] + 1, last_success_receipt=receipt)
                if job["attempts"] >= max_observations:
                    job.update(status="completed_max_observations", stop_reason="fixed_snapshot_cap_reached")
                    break
                next_due = _stamp(record["started_at"]) + timedelta(seconds=interval_seconds)
                job["next_scheduled_at"] = min(next_due, session["stop_at"]).isoformat()
                _save_job(folder, job, _aware(clock()).isoformat())
                while True:
                    remaining = (min(next_due, session["stop_at"]) - _aware(clock())).total_seconds()
                    if remaining <= 0:
                        break
                    sleep(min(30, remaining))
        except KeyboardInterrupt:
            job.update(status="interrupted", stop_reason="operator_interrupted")
        except Exception as error:
            job.update(status="failed_stopped_no_retry", error=f"{type(error).__name__}: {error}", stop_reason="watch_failure")
        job.update(finished_at=_aware(clock()).isoformat(), next_scheduled_at=None)
        _save_job(folder, job, job["finished_at"])
        return folder, job


def watch_main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded prior-dated wide-roster public observations; no account access")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--interval-seconds", type=int, default=180)
    parser.add_argument("--max-observations", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        folder, job = watch(package=args.package, calendar=args.calendar,
                            interval_seconds=args.interval_seconds, max_observations=args.max_observations)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps({"folder": str(folder) if folder else None, **job}, ensure_ascii=False), flush=True)
    return 0 if job["status"].startswith(("completed_", "waiting_")) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        folder, result = run(package=args.package, calendar=args.calendar, out=args.out)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    answer = {key: value for key, value in result.items() if key not in {"quotes", "batches", "source_proof"}}
    print(json.dumps({"folder": str(folder) if folder else None, **answer}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == SUCCESS or result["status"].startswith("waiting_") else 2


if __name__ == "__main__":
    raise SystemExit(main())
