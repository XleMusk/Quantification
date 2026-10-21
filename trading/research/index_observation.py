"""Bounded public observations of three fixed indices, separate from trading feeds.

The Sina quote grammar and fields were verified against the site's own page and
parsers saved under outputs/leader_environment_research/20261015_data_availability_v1/
sina_index_probe_v1. Returned JavaScript is parsed as data, never executed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta, timezone
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
from .exchange_calendar import DEFAULT_CALENDAR_PATH, _closed_days

CN = timezone(timedelta(hours=8))
TARGETS = {"sh000001": "上证指数", "sz399006": "创业板指", "sh000688": "科创50"}
ENDPOINT = "https://hq.sinajs.cn/list=" + ",".join(TARGETS)
OBSERVATION_SCHEMA = "sina_fixed_three_index_quote_observation_v1"
JOB_SCHEMA = "index_observation_session_job_v1"
LATEST_SCHEMA = "index_observation_latest_v1"
SUCCESS = "complete_current_fixed_index_proxy_observation"
SOURCE_REFERENCES = ["https://finance.sina.com.cn/realstock/company/sh000001/nc.shtml",
                     "https://n.sinaimg.cn/finance/stock/hq/src/stock_A.js?ts=3.9",
                     "https://n.sinaimg.cn/finance/hq2018index/js/utils-hq.js?ts=3.7",
                     "https://raw.githubusercontent.com/akfamily/akshare/main/akshare/index/index_stock_zh.py"]
FLAGS = {"selection_enabled": False, "execution_admitted": False, "ledger_mutated": False,
         "orders_generated": False, "no_account_access": True, "is_BrokerQuote": False,
         "all_A_breadth": False, "used_in_historical_close_signals": False}


def _now():
    return datetime.now(CN)


def _aware(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware observation clock required")
    return value.astimezone(CN)


def trading_session(calendar, observed):
    """Resolve only the currently open pinned-calendar morning/afternoon slot."""
    observed = _aware(observed)
    if calendar is None or calendar.get("schema") != "cn_a_share_exchange_calendar_v1":
        raise ValueError("Pinned exchange calendar unavailable")
    if calendar.get("year") != observed.year:
        raise ValueError("Pinned calendar does not support the actual observation year")
    day = observed.date()
    overrides = {date.fromisoformat(value) for value in calendar.get("open_sessions", [])}
    if day not in overrides and (day.weekday() >= 5 or day in _closed_days(calendar)):
        raise ValueError("Actual observation day is not a pinned open exchange session")
    for name, start, stop in (("morning", day_time(9, 30), day_time(11, 30)),
                              ("afternoon", day_time(13), day_time(15))):
        if start <= observed.time() < stop:
            return {"session_day": day.isoformat(), "session_name": name,
                    "session_start": datetime.combine(day, start, CN),
                    "stop_at": datetime.combine(day, stop, CN)}
    raise ValueError("Actual observation time is outside the current trading slot")


def parse_quote(symbol, value, observed, *, session=None):
    """Preserve Decimal precision and original provider date/time/status."""
    observed = _aware(observed)
    result = {"symbol": symbol, "expected_name": TARGETS[symbol], "status": "unknown",
              "current_quote": False, "fresh_at_observation": False, "source_quote_time": None,
              "age_seconds": None, "last_index_points": None, "change_percent": None}
    try:
        fields = value.split(",")
        if len(fields) < 33:
            raise ValueError("Expected the site's documented >=33-field A/index quote")
        result.update(source_name=fields[0], field_count=len(fields), source_date=fields[30],
                      source_time=fields[31], source_status=fields[32],
                      source_raw_volume=fields[8], source_raw_amount_cny=fields[9])
        when = datetime.strptime(fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
        opening, previous, last, high, low, amount = [Decimal(fields[i]) for i in (1, 2, 3, 4, 5, 9)]
        if (not all(x.is_finite() for x in (opening, previous, last, high, low, amount))
                or min(opening, previous, last, high, low) <= 0 or amount < 0):
            raise ValueError("Finite positive index prices and nonnegative CNY turnover required")
        if high < max(opening, last) or low > min(opening, last) or low > high:
            raise ValueError("Index OHLC shape does not agree")
        age = (observed - when).total_seconds()
        result.update(source_quote_time=when.isoformat(), age_seconds=age,
                      freshness_evaluated_at=observed.isoformat(),
                      expires_at=(when + timedelta(seconds=120)).isoformat(),
                      last_index_points=str(last), previous_close_index_points=str(previous),
                      open_index_points=str(opening), high_index_points=str(high), low_index_points=str(low),
                      amount_cny=str(amount), change_percent=str((last / previous - 1) * 100),
                      change_percent_basis="100 * (provider last / provider previous close - 1)")
        if fields[0] != TARGETS[symbol]:
            result["status"] = "unknown_provider_name_identity_changed"
        elif fields[32] != "00":
            result["status"] = "unknown_or_nonregular_provider_status"
        elif when.date() != observed.date():
            result["status"] = "stale_different_source_date"
        elif age < 0:
            result["status"] = "unknown_future_provider_time"
        elif age > 120:
            result["status"] = "stale_over_120_seconds"
        elif session and not session["session_start"] <= when < session["stop_at"]:
            result["status"] = "unknown_provider_time_outside_current_slot"
        else:
            result.update(status="same_day_provider_time_within_120_seconds",
                          current_quote=True, fresh_at_observation=True)
    except (ValueError, ArithmeticError) as error:
        result.update(status="unknown_invalid_quote_or_source_time", error=str(error))
    return result


def parse_batch(raw, observed, *, session=None):
    text = raw.decode("gb18030")
    pattern = re.compile(r'\s*var\s+hq_str_([a-z]{2}\d{6})\s*=\s*"([^"\r\n]*)"\s*;')
    parts, position = {}, 0
    for match in pattern.finditer(text):
        if text[position:match.start()].strip() or match[1] not in TARGETS or match[1] in parts:
            raise ValueError("Unexpected quote grammar, identity or duplicate assignment")
        parts[match[1]] = match[2]
        position = match.end()
    if text[position:].strip():
        raise ValueError("Unexpected trailing quote content")
    quotes = [parse_quote(symbol, parts[symbol], observed, session=session) if symbol in parts else
              {"symbol": symbol, "status": "unknown_missing_target", "current_quote": False,
               "fresh_at_observation": False, "source_quote_time": None} for symbol in TARGETS]
    return {"returned_targets": sorted(parts), "missing_targets": sorted(set(TARGETS) - set(parts)),
            "quotes": quotes, "returned_target_count": len(parts),
            "current_quote_count": sum(item["current_quote"] for item in quotes),
            "complete_target_coverage": set(parts) == set(TARGETS)}


def _record(job_id, index, started):
    return {"schema": OBSERVATION_SCHEMA, "job_id": job_id, "attempt_index": index,
            "status": "started", "started_at": started.isoformat(), "endpoint": ENDPOINT,
            "targets": TARGETS, "quote_request_count": 0, "max_quote_requests": 1,
            "auto_retry": False, "redirect_followed": False, "source_scope": "three_fixed_index_proxies",
            "freshness_is_as_of_observation_only": True, "consumer_must_recompute_source_age": True,
            "old_cache_modified": False, "source_references": SOURCE_REFERENCES,
            "verified_contract_artifact": "outputs/leader_environment_research/20261015_data_availability_v1/sina_index_probe_v1",
            "units": {"prices": "index_points", "computed_change": "percentage_points", "amount": "CNY",
                      "raw_volume": "provider value retained; not normalized or aggregated"},
            "limitations": ["Three index proxies are not stock-market breadth or leader/theme diffusion",
                            "Public website quotes are not independently verified exchange or broker feeds",
                            "Request and HTTP times never replace provider date/time",
                            "No hidden participant intent or net inflow is identified"], **FLAGS}


def _attempt(job, folder, index, clock, get, session, *, blocked_reason=None):
    started = _aware(clock())
    destination = folder / "observations" / (started.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:6])
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / "receipt.json"
    record = _record(job["job_id"], index, started)
    write_json(path, record)
    try:
        if blocked_reason:
            record.update(status="blocked_outside_trading_session", error=blocked_reason,
                          current_quote_count=None, complete_target_coverage=False)
        else:
            remaining = (session["stop_at"] - started).total_seconds()
            if remaining <= 0 or started.date().isoformat() != session["session_day"]:
                raise ValueError("Current trading slot ended before the HTTP request")
            record["quote_request_count"] = 1
            write_json(path, record)
            with get(ENDPOINT, timeout=(min(5, remaining), min(10, remaining)), allow_redirects=False, stream=True,
                     headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}) as response:
                record.update(http_status=response.status_code,
                              response_headers={k: response.headers.get(k) for k in ("Date", "Content-Type", "Content-Length", "Location")})
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > 262144:
                        raise ValueError("Quote response exceeds 256 KB")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                (destination / "quote_response.raw").write_bytes(raw)
                record["raw_bytes"] = len(raw)
                if response.status_code != 200:
                    raise ValueError("Expected one nonredirect HTTP200 quote response")
            observed = _aware(clock())
            record["response_received_at"] = observed.isoformat()
            record.update(parse_batch(raw, observed, session=session))
            if observed >= session["stop_at"]:
                raise ValueError("Current trading slot ended during the HTTP request")
            if not record["complete_target_coverage"] or record["current_quote_count"] != len(TARGETS):
                raise ValueError("Missing, stale, invalid or nonregular fixed index observation; no retry")
            record["status"] = SUCCESS
    except Exception as error:
        record.update(status="unavailable_single_attempt_stopped", error=f"{type(error).__name__}: {error}")
        record.setdefault("current_quote_count", None)
        record.setdefault("complete_target_coverage", False)
    record["finished_at"] = _aware(clock()).isoformat()
    write_json(path, record)
    return path, record


@contextmanager
def _collector_lock(root):
    """One nonblocking owner of this observation root; separate from BaoStock."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".collector.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
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
        try:
            acquire()
        except OSError as error:
            raise RuntimeError("Index observation collector busy; no job, request or latest update started") from error
        try:
            yield
        finally:
            handle.seek(0)
            release()


def run(*, calendar=DEFAULT_CALENDAR_PATH, data_root=DATA_ROOT, watch=False,
        interval_seconds=60, max_observations=1, _clock=None, _sleep=None, _get=None):
    """Run a single request or a bounded loop; injected dependencies are tests only."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 60:
        raise ValueError("Observation interval must be an integer of at least 60 seconds")
    if isinstance(max_observations, bool) or not isinstance(max_observations, int) or not 1 <= max_observations <= 120:
        raise ValueError("Max observations must be an integer from 1 to 120")
    if not watch and max_observations != 1:
        raise ValueError("Single mode permits exactly one observation")
    root = Path(data_root).resolve() / "research" / "index_observation"
    with _collector_lock(root):
        return _run_locked(calendar=calendar, data_root=data_root, watch=watch,
                           interval_seconds=interval_seconds, max_observations=max_observations,
                           _clock=_clock, _sleep=_sleep, _get=_get)


def _run_locked(*, calendar, data_root, watch, interval_seconds, max_observations, _clock, _sleep, _get):
    clock, sleep, get = _clock or _now, _sleep or time.sleep, _get or requests.get
    started = _aware(clock())
    root = Path(data_root).resolve() / "research" / "index_observation"
    job_id = started.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    folder = root / job_id
    folder.mkdir(parents=True, exist_ok=False)
    calendar = Path(calendar).resolve()
    calendar_bytes = calendar.read_bytes() if calendar.is_file() else None
    try:
        payload = json.loads(calendar_bytes) if calendar_bytes is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if calendar_bytes is not None:
        (folder / "calendar.json").write_bytes(calendar_bytes)
    job = {"schema": JOB_SCHEMA, "job_id": job_id, "status": "running", "pid": os.getpid(),
           "started_at": started.isoformat(), "calendar_path": str(calendar),
           "calendar_sha256": sha256(calendar_bytes).hexdigest() if calendar_bytes is not None else None,
           "pinned_calendar_path": str(folder / "calendar.json") if calendar_bytes is not None else None,
           "watch": bool(watch), "interval_seconds": interval_seconds, "max_observations": max_observations,
           "attempts": 0, "successes": 0, "last_success_receipt": None, "latest_attempt_receipt": None,
           "session_day": None, "session_name": None, "stop_at": None, **FLAGS}
    try:
        job["process_started_at"] = datetime.fromtimestamp(_process_start(os.getpid()), timezone.utc).isoformat()
        job["process_identity_status"] = "observed_os_process_start"
    except (OSError, ImportError, ValueError) as error:
        job.update(process_started_at=None, process_identity_status="identity_unverified",
                   process_identity_error=str(error))
    job_path = folder / "job_receipt.json"
    session, blocked = None, None
    try:
        session = trading_session(payload, started)
        job.update({k: v.isoformat() if isinstance(v, datetime) else v for k, v in session.items()})
    except (TypeError, ValueError) as error:
        blocked = str(error)
    job["updated_at"] = started.isoformat()
    write_json(job_path, job)
    try:
        while True:
            before = _aware(clock())
            if session and before >= session["stop_at"]:
                job.update(status="completed_session_boundary", stop_reason="current_slot_boundary_reached")
                break
            path, record = _attempt(job, folder, job["attempts"] + 1, clock, get, session, blocked_reason=blocked)
            job.update(attempts=job["attempts"] + 1, latest_attempt_receipt=str(path), updated_at=record["finished_at"])
            if record["status"] == SUCCESS:
                job.update(successes=job["successes"] + 1, last_success_receipt=str(path))
            write_json(root / "latest.json", {"schema": LATEST_SCHEMA, "job_id": job_id,
                       "job_receipt": str(job_path), "attempt_receipt": str(path),
                       "updated_at": record["finished_at"], "attempt_status": record["status"], **FLAGS})
            if record["status"] != SUCCESS:
                job.update(status="blocked_outside_trading_session" if blocked else "failed_stopped_no_retry",
                           error=record.get("error"), stop_reason="latest_attempt_not_complete_current")
                break
            if not watch:
                job.update(status="completed_single_observation", stop_reason="single_request_complete")
                break
            if job["attempts"] >= max_observations:
                job.update(status="completed_max_observations", stop_reason="fixed_observation_cap_reached")
                break
            write_json(job_path, job)
            next_due = datetime.fromisoformat(record["started_at"]) + timedelta(seconds=interval_seconds)
            while True:
                current = _aware(clock())
                remaining = (min(next_due, session["stop_at"]) - current).total_seconds()
                if remaining <= 0:
                    break
                sleep(min(30, remaining))
    except KeyboardInterrupt:
        job.update(status="interrupted", stop_reason="operator_interrupted")
    except Exception as error:
        job.update(status="failed_stopped_no_retry", error=f"{type(error).__name__}: {error}", stop_reason="collector_failure")
    job["finished_at"] = _aware(clock()).isoformat()
    job["updated_at"] = job["finished_at"]
    write_json(job_path, job)
    return folder, job


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--watch", action="store_true", help="Observe only the actual current trading slot; stop at its boundary or first failure")
    parser.add_argument("--interval-seconds", type=int, default=60, help="At least 60 seconds between request starts")
    parser.add_argument("--max-observations", type=int, default=None, help="1..120; default single=1, watch=90")
    args = parser.parse_args(argv)
    maximum = args.max_observations if args.max_observations is not None else (90 if args.watch else 1)
    try:
        folder, job = run(calendar=args.calendar, watch=args.watch,
                          interval_seconds=args.interval_seconds, max_observations=maximum)
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps({"folder": str(folder), **job}, ensure_ascii=False), flush=True)
    return 0 if job["status"].startswith("completed_") else 2


if __name__ == "__main__":
    raise SystemExit(main())
