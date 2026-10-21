"""Bounded public price observations of one sealed, pre-registered proxy cohort.

This collector does not update the identity registry or its close observation
chain. Quote volume and amount remain source strings, without normalization.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import requests

from ..market_dataset import write_json
from ..settings import DATA_ROOT
from .close_workbench import _process_start
from .exchange_calendar import DEFAULT_CALENDAR_PATH
from .index_observation import _aware, _collector_lock, trading_session
from .leader_identity_registry import _registry

CN = timezone(timedelta(hours=8))
JOB_SCHEMA = "leader_cohort_observation_session_job_v1"
OBSERVATION_SCHEMA = "sina_frozen_leader_cohort_quote_observation_v1"
LATEST_SCHEMA = "leader_cohort_observation_latest_v1"
SUCCESS = "completed_fixed_cohort_snapshot_with_member_unknowns_retained"
SOURCE_SCOPE = "fixed_pre_registered_leader_proxy_cohort_not_all_A"
REGISTRY_RELATIVE = Path("research/leader_identity_registry/20261015_initial_proxy_cohort_v1/registry.json")
CONTRACT_RELATIVE = Path("research/leader_intraday_contract/20261015_sina_stock_v1/contract_analysis.json")
MEMBER_COUNT = 590
BATCH_SIZE = 100
ENDPOINT = "https://hq.sinajs.cn/list="
FLAGS = {"candidate_panel_only": True, "no_account_access": True,
         "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
         "orders_generated": False, "ledger_mutated": False, "current_signal_admitted": False,
         "is_BrokerQuote": False, "all_A_breadth": False, "used_in_historical_close_signals": False,
         "registry_mutated": False, "close_observation_chain_mutated": False,
         "identified_capital_flow": False, "participant_intent_inferred": False}


def _now():
    return datetime.now(CN)


def _provider_symbol(symbol):
    if not re.fullmatch(r"(?:[036]\d{5})\.(?:SSE|SZSE)", symbol):
        raise ValueError("Only sealed ordinary SH/SZ A-share identities are supported")
    code, market = symbol.split(".")
    if (market == "SSE") != code.startswith("6"):
        raise ValueError("Canonical symbol and exchange disagree")
    return ("sh" if market == "SSE" else "sz") + code


def _member(member, batch_id):
    return {"symbol": member["symbol"], "provider_symbol": _provider_symbol(member["symbol"]),
            "registered_name": member.get("name"),
            "frozen_labels": [label["id"] for label in member["labels"]], "batch_id": batch_id,
            "status": "unknown_not_attempted", "unknown_reason": "batch_not_attempted",
            "source_name": None, "source_status": None, "source_date": None, "source_time": None,
            "source_quote_time": None, "received_at": None, "open_price_rmb": None,
            "previous_close_price_rmb": None, "last_price_rmb": None, "high_price_rmb": None,
            "low_price_rmb": None, "source_raw_volume": None, "source_raw_amount": None,
            "name_changed_from_registry": None, "current_quote": False,
            "fresh_at_observation": False, "age_seconds": None}


def parse_quote(member, value, received, *, session, registered_at, batch_id):
    """Parse stock prices without the website's last-or-previous-close fallback."""
    result = _member(member, batch_id)
    received = _aware(received)
    result["received_at"] = received.isoformat()
    if not value:
        result.update(status="unknown_empty_payload", unknown_reason="empty_provider_payload")
        return result
    try:
        fields = value.split(",")
        if len(fields) < 33:
            raise ValueError("Expected at least 33 ordinary-A quote fields")
        result.update(source_name=fields[0], source_status=fields[32], source_date=fields[30],
                      source_time=fields[31], field_count=len(fields), source_raw_volume=fields[8],
                      source_raw_amount=fields[9], name_changed_from_registry=fields[0] != member.get("name"))
        for key, index in (("open_price_rmb", 1), ("previous_close_price_rmb", 2),
                           ("last_price_rmb", 3), ("high_price_rmb", 4), ("low_price_rmb", 5)):
            result[key] = fields[index] or None
        when = datetime.strptime(fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
        age = (received - when).total_seconds()
        result.update(source_quote_time=when.isoformat(), age_seconds=age)
        if fields[32] != "00":
            result.update(status="unknown_nonregular_provider_status", unknown_reason="provider_status_not_regular_00")
            return result
        prices = [Decimal(fields[index]) for index in (1, 2, 3, 4, 5)]
        if not all(price.is_finite() and price > 0 for price in prices):
            raise ValueError("Finite positive source prices required; zero or missing is never filled")
        opening, previous, last, high, low = prices
        if high < max(opening, last) or low > min(opening, last) or low > high:
            raise ValueError("Source OHLC shape does not agree")
        reason = ("different_source_date" if when.date() != received.date() else
                  "future_source_time" if age < 0 else
                  "source_time_outside_current_slot" if not session["session_start"] <= when < session["stop_at"] else
                  "source_time_precedes_registration" if when < _aware(registered_at) else
                  "source_age_over_120_seconds" if age > 120 else None)
        if reason:
            result.update(status="unknown_" + reason, unknown_reason=reason)
        else:
            result.update(status="same_day_same_slot_provider_time_within_120_seconds", unknown_reason=None,
                          current_quote=True, fresh_at_observation=True)
    except (ValueError, ArithmeticError) as error:
        result.update(status="unknown_invalid_quote_or_source_time", unknown_reason=str(error))
    return result


def parse_batch(raw, members, received, *, session, registered_at, batch_id):
    """Reject unrequested/duplicate assignments and executable or trailing text."""
    requested = {_provider_symbol(member["symbol"]): member for member in members}
    text = raw.decode("gb18030")
    pattern = re.compile(r'\s*var\s+hq_str_([a-z]{2}\d{6})\s*=\s*"([^"\r\n]*)"\s*;')
    parts, position = {}, 0
    for match in pattern.finditer(text):
        if text[position:match.start()].strip() or match[1] not in requested or match[1] in parts:
            raise ValueError("Unexpected quote grammar, identity or duplicate assignment")
        parts[match[1]] = match[2]
        position = match.end()
    if text[position:].strip() or not parts:
        raise ValueError("Empty batch or unexpected trailing quote content")
    quotes = []
    for symbol, member in requested.items():
        if symbol in parts:
            quote = parse_quote(member, parts[symbol], received, session=session,
                                registered_at=registered_at, batch_id=batch_id)
        else:
            quote = _member(member, batch_id)
            quote.update(status="unknown_missing_assignment", unknown_reason="missing_provider_assignment",
                         received_at=_aware(received).isoformat())
        quotes.append(quote)
    return quotes, {"returned_target_count": len(parts), "missing_target_count": len(requested) - len(parts)}


def _slot(calendar, actual, expected):
    current = trading_session(calendar, actual)
    if current != expected:
        raise ValueError("Actual trading slot changed; collection stops without a retry")


def _attempt(job, folder, index, members, calendar, session, clock, get):
    started = _aware(clock())
    destination = folder / "observations" / (started.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:6])
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / "receipt.json"
    record = {key: job[key] for key in ("job_id", "registry_file", "registry_sha256", "registry_registered_at",
              "member_count", "batch_size", "planned_batches", "calendar_sha256", "session_day", "session_name",
              "session_start", "stop_at", "source_contract_file", "source_contract_sha256")}
    record.update(schema=OBSERVATION_SCHEMA, attempt_index=index, status="started", started_at=started.isoformat(),
                  finished_at=None, response_received_at=None, completed_batches=0, quote_request_count=0,
                  source_scope=SOURCE_SCOPE, all_batches_completed=False, auto_retry=False, redirect_followed=False,
                  freshness_is_as_of_observation_only=True, consumer_must_recompute_source_age=True,
                  units={"prices": "CNY", "volume_and_amount": "original_source_strings_not_normalized"},
                  quotes=[_member(member, i // BATCH_SIZE) for i, member in enumerate(members)], batches=[], **FLAGS)
    write_json(path, record)
    try:
        for offset in range(0, len(members), BATCH_SIZE):
            before = _aware(clock())
            _slot(calendar, before, session)
            batch_id = offset // BATCH_SIZE
            chunk = members[offset:offset + BATCH_SIZE]
            batch = {"batch_id": batch_id, "target_count": len(chunk), "requested_at": before.isoformat(),
                     "response_received_at": None, "status": "started", "raw_file": None, "raw_sha256": None}
            record["batches"].append(batch)
            for quote in record["quotes"][offset:offset + len(chunk)]:
                quote.update(status="unknown_batch_incomplete", unknown_reason="batch_request_incomplete")
            record["quote_request_count"] += 1
            write_json(path, record)
            remaining = (session["stop_at"] - before).total_seconds()
            endpoint = ENDPOINT + ",".join(_provider_symbol(member["symbol"]) for member in chunk)
            with get(endpoint, timeout=(min(5, remaining), min(10, remaining)), allow_redirects=False, stream=True,
                     headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}) as response:
                batch.update(http_status=response.status_code,
                             response_headers={k: response.headers.get(k) for k in ("Date", "Content-Type", "Content-Length", "Location")})
                pieces, size = [], 0
                for piece in response.iter_content(16384):
                    size += len(piece)
                    if size > 262144:
                        raise ValueError("Quote batch response exceeds 256 KB")
                    pieces.append(piece)
                raw = b"".join(pieces)
                received = _aware(clock())
                record["response_received_at"] = batch["response_received_at"] = received.isoformat()
                raw_file = destination / f"batch_{batch_id:03d}.raw"
                raw_file.write_bytes(raw)
                batch.update(raw_file=str(raw_file), raw_sha256=sha256(raw).hexdigest(), raw_bytes=len(raw))
                _slot(calendar, received, session)
                if response.status_code != 200:
                    raise ValueError("Expected one nonredirect HTTP200 response; collection stops without retry")
            parsed, counts = parse_batch(raw, chunk, received, session=session,
                                         registered_at=datetime.fromisoformat(job["registry_registered_at"]), batch_id=batch_id)
            record["quotes"][offset:offset + len(chunk)] = parsed
            batch.update(status="completed_transport_and_grammar", **counts)
            record["completed_batches"] += 1
            write_json(path, record)
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
    record.update(finished_at=_aware(clock()).isoformat(),
                  current_quote_count=sum(quote["current_quote"] for quote in record["quotes"]))
    record["unknown_quote_count"] = len(members) - record["current_quote_count"]
    write_json(path, record)
    return path, record


def run(*, calendar=DEFAULT_CALENDAR_PATH, data_root=DATA_ROOT, watch=False,
        interval_seconds=60, max_observations=1, _clock=None, _sleep=None, _get=None):
    """Observe the actual current slot once, or for a bounded minute-spaced watch."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 60:
        raise ValueError("Observation interval must be an integer of at least 60 seconds")
    if isinstance(max_observations, bool) or not isinstance(max_observations, int) or not 1 <= max_observations <= 120:
        raise ValueError("Max observations must be an integer from 1 to 120")
    if not watch and max_observations != 1:
        raise ValueError("Single mode permits exactly one observation")
    clock, sleep, get = _clock or _now, _sleep or time.sleep, _get or requests.get
    actual = _aware(clock())
    data_root, calendar = Path(data_root).resolve(), Path(calendar).resolve()
    calendar_bytes = calendar.read_bytes()
    payload = json.loads(calendar_bytes)
    try:
        session = trading_session(payload, actual)
    except ValueError as error:
        return None, {"schema": JOB_SCHEMA, "status": "waiting_outside_current_trading_slot", "reason": str(error),
                      "observed_at": actual.isoformat(), "quote_request_count": 0, "source_scope": SOURCE_SCOPE, **FLAGS}
    registry_path = data_root / REGISTRY_RELATIVE
    registry, registry_sha, _, _ = _registry(registry_path, actual)
    members = registry["members"]
    if len(members) != MEMBER_COUNT or sha256(calendar_bytes).hexdigest() != registry["binding"]["calendar_sha256"]:
        raise ValueError("Fixed 590-member registry and its pinned calendar are required")
    for member in members:
        _provider_symbol(member["symbol"])
    contract_file = data_root / CONTRACT_RELATIVE
    contract_bytes = contract_file.read_bytes()
    contract = json.loads(contract_bytes)
    if (contract.get("schema") != "sina_ordinary_SH_SZ_A_price_time_contract_assessment_v1"
            or _aware(datetime.fromisoformat(contract["created_at"])) > actual):
        raise ValueError("Actually available ordinary-A quote contract proof required")
    root = data_root / "research/leader_cohort_observation"
    with _collector_lock(root):
        started = _aware(clock())
        _slot(payload, started, session)
        job_id = started.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
        folder = root / job_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "calendar.json").write_bytes(calendar_bytes)
        job = {"schema": JOB_SCHEMA, "job_id": job_id, "status": "running", "started_at": started.isoformat(),
               "pid": os.getpid(), "calendar_path": str(calendar), "calendar_sha256": sha256(calendar_bytes).hexdigest(),
               "pinned_calendar_path": str(folder / "calendar.json"), "registry_file": str(registry_path),
               "registry_sha256": registry_sha, "registry_registered_at": registry["registered_at"],
               "source_contract_file": str(contract_file), "source_contract_sha256": sha256(contract_bytes).hexdigest(),
               "member_count": len(members), "batch_size": BATCH_SIZE,
               "planned_batches": (len(members) + BATCH_SIZE - 1) // BATCH_SIZE,
               "watch": bool(watch), "interval_seconds": interval_seconds, "max_observations": max_observations,
               "attempts": 0, "successes": 0, "latest_attempt_receipt": None, "last_success_receipt": None,
               "source_scope": SOURCE_SCOPE, **FLAGS,
               **{key: value.isoformat() if isinstance(value, datetime) else value for key, value in session.items()}}
        try:
            job["process_started_at"] = datetime.fromtimestamp(_process_start(os.getpid()), timezone.utc).isoformat()
            job["process_identity_status"] = "observed_os_process_start"
        except (OSError, ImportError, ValueError) as error:
            job.update(process_started_at=None, process_identity_status="identity_unverified", process_identity_error=str(error))
        job_path = folder / "job_receipt.json"
        write_json(job_path, job)
        try:
            while True:
                before = _aware(clock())
                if before >= session["stop_at"]:
                    job.update(status="completed_session_boundary", stop_reason="current_slot_boundary_reached")
                    break
                _slot(payload, before, session)
                path, record = _attempt(job, folder, job["attempts"] + 1, members, payload, session, clock, get)
                job.update(attempts=job["attempts"] + 1, latest_attempt_receipt=str(path), updated_at=record["finished_at"])
                if record["status"] == SUCCESS:
                    job.update(successes=job["successes"] + 1, last_success_receipt=str(path))
                write_json(root / "latest.json", {"schema": LATEST_SCHEMA, "job_id": job_id,
                           "job_receipt": str(job_path), "attempt_receipt": str(path), "updated_at": record["finished_at"],
                           "attempt_status": record["status"], "registry_sha256": registry_sha,
                           "source_scope": SOURCE_SCOPE, **FLAGS})
                if record["status"] != SUCCESS:
                    job.update(status="failed_stopped_no_retry", error=record.get("error"), stop_reason="latest_attempt_failed")
                    break
                if not watch or job["attempts"] >= max_observations:
                    job.update(status="completed_max_observations" if watch else "completed_single_observation")
                    break
                write_json(job_path, job)
                next_due = datetime.fromisoformat(record["started_at"]) + timedelta(seconds=interval_seconds)
                while True:
                    remaining = (min(next_due, session["stop_at"]) - _aware(clock())).total_seconds()
                    if remaining <= 0:
                        break
                    sleep(min(30, remaining))
        except KeyboardInterrupt:
            job.update(status="interrupted", stop_reason="operator_interrupted")
        except Exception as error:
            job.update(status="failed_stopped_no_retry", error=f"{type(error).__name__}: {error}", stop_reason="collector_failure")
        job["finished_at"] = job["updated_at"] = _aware(clock()).isoformat()
        write_json(job_path, job)
        return folder, job


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--max-observations", type=int, default=None, help="1..120; single default 1, watch default 90")
    args = parser.parse_args(argv)
    maximum = args.max_observations if args.max_observations is not None else (90 if args.watch else 1)
    try:
        folder, job = run(calendar=args.calendar, watch=args.watch, interval_seconds=args.interval_seconds,
                          max_observations=maximum)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps({"folder": str(folder) if folder else None, **job}, ensure_ascii=False), flush=True)
    return 0 if job["status"].startswith(("completed_", "waiting_")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
