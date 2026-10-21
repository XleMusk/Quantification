"""Read a same-slot change window from saved fixed-index observations only.

This describes three public index proxies, not market breadth, leader quality,
capital flow or a strategy selector. It never starts or changes a collector.
"""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


SCHEMA = "fixed_three_index_observation_change_v1"
TARGETS = {"sh000001": "上证指数", "sz399006": "创业板指", "sh000688": "科创50"}
SUCCESS = "complete_current_fixed_index_proxy_observation"
CN = timezone(timedelta(hours=8))
FALSE_FLAGS = ("selection_enabled", "execution_admitted", "ledger_mutated", "orders_generated",
               "is_BrokerQuote", "all_A_breadth", "used_in_historical_close_signals")


def _time(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Timezone-aware observation time required")
    return stamp.astimezone(CN)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _boundary(record, schema):
    if (record.get("schema") != schema or any(record.get(key) is not False for key in FALSE_FLAGS)
            or record.get("no_account_access") is not True):
        raise ValueError("Saved index observation boundary changed")


def _slot(job):
    day, name = job["session_day"], job["session_name"]
    ranges = {"morning": (time(9, 30), time(11, 30)), "afternoon": (time(13), time(15))}
    if name not in ranges:
        raise ValueError("Known morning or afternoon slot required")
    first, last = ranges[name]
    start, stop = _time(job["session_start"]), _time(job["stop_at"])
    if (start.date().isoformat() != day or stop.date().isoformat() != day
            or start.timetz().replace(tzinfo=None) != first or stop.timetz().replace(tzinfo=None) != last):
        raise ValueError("Job slot does not match the fixed exchange time range")
    return start, stop


def _positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("Finite positive index points required")
    return number


def _batch(record, *, job_id, start, stop, now):
    _boundary(record, "sina_fixed_three_index_quote_observation_v1")
    if record.get("job_id") != job_id or record.get("source_scope") != "three_fixed_index_proxies":
        raise ValueError("Observation belongs to a different job or scope")
    if record.get("status") != SUCCESS:
        raise ValueError("A complete successful index observation is required")
    if (record.get("http_status") != 200 or record.get("complete_target_coverage") is not True
            or record.get("current_quote_count") != 3 or record.get("returned_target_count") != 3
            or set(record.get("returned_targets", [])) != set(TARGETS)
            or record.get("missing_targets") != []):
        raise ValueError("Observation target coverage is incomplete")
    received = _time(record["response_received_at"])
    began = _time(record["started_at"])
    if not start <= began <= received < stop or received > now:
        raise ValueError("Response is in the future or outside its job slot")
    rows = record["quotes"]
    if len(rows) != 3 or {row["symbol"] for row in rows} != set(TARGETS):
        raise ValueError("All three unique index identities are required")
    values = {}
    for row in rows:
        symbol = row["symbol"]
        stamp = _time(row["source_quote_time"])
        if (row.get("source_name") != TARGETS[symbol] or row.get("source_status") != "00"
                or row.get("status") != "same_day_provider_time_within_120_seconds"
                or row.get("fresh_at_observation") is not True
                or not start <= stamp < stop or not 0 <= (received - stamp).total_seconds() <= 120):
            raise ValueError("Index source clock, regular status or identity is invalid")
        values[symbol] = {"stamp": stamp, "last": _positive(row["last_index_points"]),
                          "previous": _positive(row["previous_close_index_points"])}
    return {"received": received, "values": values}


def index_observation_change(data_root, *, now=None, expected_latest_receipt=None):
    """Describe a roughly five-minute saved window without contacting a provider.

    Baselines must precede the latest attempt, belong to the same job and slot,
    and span 300..420 source seconds for every index. The smallest mean excess
    over 300 seconds wins; equal scores use the newest received batch. Gaps are
    never interpolated. Expired or out-of-slot results are historical only.
    """
    now = _time(now or datetime.now().astimezone())
    result = {"schema": SCHEMA, "status": "missing", "read_at": now.isoformat(),
        "source_file": None, "baseline_source_file": None, "indices": [], "summary": None,
        "current_hint": None, "provider_contacted": False, "source_scope": "three_fixed_index_proxies",
        "used_for_close_analysis": False, "no_account_access": True,
        **{key: False for key in FALSE_FLAGS},
        "baseline_policy": "same_job_same_slot_all_source_spans_300_to_420_seconds_minimum_mean_excess_then_newest_response",
        "limitations": ["Three index changes are not all-A breadth, leader/theme diffusion or identified capital flow",
                        "No automatic strategy adaptation, security selection or order generation",
                        "Public website receipts are not independently verified exchange or broker quotes"]}
    root = Path(data_root).resolve() / "research/index_observation"
    try:
        pointer_path = root / "latest.json"
        if not pointer_path.is_file():
            return result
        pointer = _read(pointer_path)
        _boundary(pointer, "index_observation_latest_v1")
        job_id = pointer["job_id"]
        job_dir = (root / job_id).resolve()
        job_path, latest_path = (Path(pointer[key]).resolve() for key in ("job_receipt", "attempt_receipt"))
        if (job_dir.parent != root or job_dir.name != job_id or job_path != job_dir / "job_receipt.json"
                or not latest_path.is_relative_to(job_dir / "observations") or latest_path.name != "receipt.json"
                or latest_path.parent.parent != job_dir / "observations"):
            raise ValueError("Index change locator escapes the declared job paths")
        result.update(source_file=str(latest_path), job_id=job_id)
        if expected_latest_receipt is not None and Path(expected_latest_receipt).resolve() != latest_path:
            return {**result, "status": "unavailable_locator_changed_during_read"}
        job = _read(job_path)
        _boundary(job, "index_observation_session_job_v1")
        if job.get("job_id") != job_id:
            raise ValueError("Job identity differs from its locator")
        start, stop = _slot(job)
        result.update(session_day=job["session_day"], session_name=job["session_name"])
        latest_record = _read(latest_path)
        _boundary(latest_record, "sina_fixed_three_index_quote_observation_v1")
        if latest_record.get("job_id") != job_id:
            raise ValueError("Latest attempt belongs to another job")
        result["latest_attempt_status"] = latest_record.get("status")
        if latest_record.get("status") != SUCCESS:
            return {**result, "status": "unavailable_latest_attempt", "error": latest_record.get("error")}
        latest = _batch(latest_record, job_id=job_id, start=start, stop=stop, now=now)
        choices, scanned = [], 0
        for candidate in (job_dir / "observations").glob("*/receipt.json"):
            path = candidate.resolve()
            if path == latest_path:
                continue
            if path.parent.parent != job_dir / "observations" or path.name != "receipt.json":
                raise ValueError("Saved baseline escapes the declared observation directory")
            record = _read(path)
            # A newer in-progress attempt can coexist with a captured locator.
            # Failed or pending historical attempts do not supply a baseline.
            if record.get("status") != SUCCESS:
                continue
            candidate_received = _time(record["response_received_at"])
            if candidate_received >= latest["received"]:
                continue
            baseline = _batch(record, job_id=job_id, start=start, stop=stop, now=now)
            scanned += 1
            spans = [(latest["values"][symbol]["stamp"] - baseline["values"][symbol]["stamp"]).total_seconds()
                     for symbol in TARGETS]
            if not all(300 <= span <= 420 for span in spans):
                continue
            if any(latest["values"][symbol]["previous"] != baseline["values"][symbol]["previous"] for symbol in TARGETS):
                raise ValueError("Previous-close references changed within one index slot")
            choices.append((sum(spans) / 3 - 300, -baseline["received"].timestamp(), str(path), baseline))
        result["scanned_prior_success_count"] = scanned
        result["latest_response_received_at"] = latest["received"].isoformat()
        if not choices:
            return {**result, "status": "unknown_no_common_300_to_420_second_baseline"}
        _, _, baseline_path, baseline = min(choices, key=lambda choice: choice[:3])
        indices = []
        for symbol, name in TARGETS.items():
            end, first = latest["values"][symbol], baseline["values"][symbol]
            age = (now - end["stamp"]).total_seconds()
            indices.append({"symbol": symbol, "name": name,
                "start_source_quote_time": first["stamp"].isoformat(), "end_source_quote_time": end["stamp"].isoformat(),
                "window_span_seconds": (end["stamp"] - first["stamp"]).total_seconds(),
                "window_return_bp": float((end["last"] / first["last"] - 1) * 10000),
                "day_change_delta_pp": float((end["last"] / end["previous"] - first["last"] / first["previous"]) * 100),
                "age_seconds_at_read": age, "current_at_read": start <= now < stop and 0 <= age <= 120})
        returns = {row["symbol"]: row["window_return_bp"] for row in indices}
        summary = {"index_proxy_positive_count": sum(value > 0 for value in returns.values()),
            "index_proxy_negative_count": sum(value < 0 for value in returns.values()),
            "index_proxy_flat_count": sum(value == 0 for value in returns.values()),
            "tech_minus_sh_window_return_bp": {symbol: returns[symbol] - returns["sh000001"]
                                                for symbol in ("sz399006", "sh000688")}}
        current = all(row["current_at_read"] for row in indices)
        return {**result, "status": "current_same_slot_index_change" if current else "historical_same_slot_index_change_not_current",
            "baseline_source_file": baseline_path, "baseline_response_received_at": baseline["received"].isoformat(),
            "indices": indices, "summary": summary, "current_hint": summary if current else None}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, InvalidOperation) as error:
        return {**result, "status": "unavailable_invalid_saved_observation_change", "error": str(error),
                "indices": [], "summary": None, "current_hint": None}
