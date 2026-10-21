"""Offline reported daily turnover inputs, without historical roster admission.

The fixed 183 closes retain every cached row and original string field. Actual
cache retrieval/derivation clocks are not historical signal availability. This
separate research contract cannot replace the strict full-close source inputs.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from .leader_daily_signals import _calendar_view

SCHEMA = "leader_historical_reported_turnover_inputs_v1"
REGISTRATION_SCHEMA = "leader_historical_reported_turnover_inputs_registration_v1"
SOURCE_CONTRACT = "observed_rows_without_historical_dated_roster"
START, END = "2026-01-05", "2026-10-09"
FIELDS = ("date", "code", "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "tradestatus", "isST", "adjustflag", "pctChg")
ORIGINAL_FIELDS = ("date", "code", "open", "high", "low", "close", "preclose", "volume", "amount",
    "adjustflag", "turn", "tradestatus", "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "isST")
UNIT_EVIDENCE = Path(__file__).with_name("baostock_bulk_extras_units.json")
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "provider_contacted": False,
    "account_created": False, "orders_generated": False, "ledger_mutated": False,
    "model_refit": False, "future_labels_used_for_score": False,
    "strict_full_close_admitted": False, "historical_publication_verified": False,
    "historical_dated_roster_verified": False, "pit_verified": False,
    "corporate_actions_verified": False}
PROTOCOL = {"id": SCHEMA, "source_contract": SOURCE_CONTRACT, "start": START, "end": END,
    "expected_sessions": 183, "first_previous_session": "unknown_no_2025_context",
    "turn_unit": "percent", "turn_scaled": False, "halt_blank_is_unknown_not_zero": True,
    "all_original_rows_retained": True, "native_StockDB_turnover_substituted": False,
    "old_strict_eight_type_inputs_or_unknowns_modified": False,
    "historical_retrieval_clock_is_historical_publication": False,
    "independent_circulating_shares_verified": False}


def _now():
    return datetime.now(MARKET_TZ)


def _stamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Historical cache evidence requires actual timezone-aware clocks")
    return stamp.astimezone(MARKET_TZ)


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _source(root, role):
    root = Path(root).resolve()
    plan_path, receipt_path = root / "plan.json", root / "receipts.json"
    plan, receipt = _read(plan_path), _read(receipt_path)
    if (plan.get("kind") != "stock" or plan.get("execution_admitted") is not False
            or receipt.get("status") != "source_cache_complete"
            or receipt.get("execution_admitted") is not False or not isinstance(receipt.get("tasks"), dict)
            or not isinstance(receipt.get("failures"), list)):
        raise ValueError("Completed stock cache with original task and failure records required")
    proof = {"role": role, "root": str(root), "plan_file": str(plan_path), "plan_sha256": _digest(plan_path),
        "receipts_file": str(receipt_path), "receipts_sha256": _digest(receipt_path)}
    return {"root": root, "plan": plan, "receipt": receipt, "proof": proof}


def _sessions(calendar):
    value = _read(calendar)
    days, previous, status = _calendar_view(value, START, END, [])
    if status != "pinned_static_exchange_calendar" or value.get("year") != 2026 or len(days) != 183:
        raise ValueError("Exact pinned 2026 calendar with the original 183 sessions required")
    return value, days, previous


def _counts(rows):
    active = rows.tradestatus.eq("1") & rows.isST.eq("0")
    return {"observed_rows": len(rows), "reported_turn_known": int(rows.turn_percent.notna().sum()),
        "reported_turn_unknown": int(rows.turn_percent.isna().sum()),
        "halt_rows": int(rows.tradestatus.eq("0").sum()),
        "ST_rows": int(rows.isST.eq("1").sum()), "status_unknown_rows": int(rows.tradestatus.eq("").sum()),
        "ST_unknown_rows": int(rows.isST.eq("").sum()),
        "ordinary_SH_SZ_proxy_rows": int(rows.ordinary_SH_SZ_proxy.sum()),
        "observed_active_nonST_rows": int(active.sum()),
        "observed_active_nonST_known_turn_rows": int((active & rows.turn_percent.notna()).sum())}


def _day(source, day, previous, observed):
    plan, receipt = source["plan"], source["receipt"]
    task = receipt["tasks"].get(day)
    if (day not in plan.get("days", []) or not isinstance(task, dict) or task.get("date") != day
            or task.get("kind") != "stock" or task.get("method") != "query_daily_history_k_AStock"
            or task.get("error_code") != "0" or not task.get("sha256")):
        raise ValueError(f"Missing or unsuccessful exact cached daily task: {day}")
    path = (source["root"] / task["file"]).resolve()
    path.relative_to(source["root"])
    before = _digest(path)
    if before != task["sha256"] or pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns.tolist() != list(ORIGINAL_FIELDS):
        raise ValueError(f"Original daily task bytes/18-field schema changed: {day}")
    rows = pd.read_csv(path, usecols=list(FIELDS), dtype="string", keep_default_na=False, encoding="utf-8-sig")
    if (rows.empty or len(rows) != task.get("quality", {}).get("rows") or not rows.date.eq(day).all()
            or not rows.code.str.fullmatch(r"(?:sh|sz)\.\d{6}").all() or rows.code.duplicated().any()
            or not rows.tradestatus.isin(["", "0", "1"]).all() or not rows.isST.isin(["", "0", "1"]).all()
            or not rows.adjustflag.eq("3").all()):
        raise ValueError(f"Exact daily rows/identity/status/raw basis changed: {day}")
    # Keep original strings; typed percent is a separate nullable observation.
    turn = pd.to_numeric(rows.turn, errors="coerce")
    rows["turn_percent"] = turn.where(np.isfinite(turn) & turn.ge(0)).astype("Float64")
    rows["symbol"] = rows.code.str[3:] + np.where(rows.code.str.startswith("sh."), ".SSE", ".SZSE")
    rows["ordinary_SH_SZ_proxy"] = (rows.code.str.fullmatch(r"(?:sh\.(?:60|68)\d{4}|sz\.(?:00|30)\d{4})")
        & rows.code.ne("sh.689009"))
    # A nonblank provider halt value remains a source observation. We neither
    # fill blanks nor silently discard anomalous recorded values.
    rows["turn_observation_status"] = np.where(rows.turn_percent.isna(), "unknown_blank_or_invalid_reported_turn", "known_reported_percent")
    available = _stamp(receipt["updated_at"])
    if available > observed or available < _stamp(day + "T15:00:00+08:00") or _digest(path) != before:
        raise ValueError("Actual cache retrieval bound is future/before its close or bytes changed during read")
    return rows, {"day": day, "previous_exact_session": previous.get(day),
        "source_contract": SOURCE_CONTRACT, "source_role": source["proof"]["role"],
        "file": str(path), "sha256": before, "original_task": task,
        "raw_source_available_at": receipt["updated_at"],
        "source_clock_basis": "original_multiday_receipts_updated_at_conservative_completion_bound_not_per_call_clock",
        "halt_nonblank_turn_rows": int((rows.tradestatus.eq("0") & rows.turn.ne("")).sum()),
        **_counts(rows)}


def build(cache_root, extension_root, calendar, out, *, registration, observed_at=None):
    observed = _stamp(observed_at) if observed_at is not None else _now()
    started = _now()
    if observed > started:
        raise ValueError("Historical input observed_at cannot be in the future of the actual read clock")
    calendar = Path(calendar).resolve()
    calendar_payload, days, previous = _sessions(calendar)
    sources = [_source(cache_root, "base"), _source(extension_root, "extension")]
    registration = Path(registration).resolve()
    registration_sha = _digest(registration)
    saved = _read(registration)
    proof = {"file": str(registration), "sha256": registration_sha, "registered_at": saved["registered_at"]}
    if (saved.get("schema") != REGISTRATION_SCHEMA or saved.get("start") != START or saved.get("end") != END
            or saved.get("sessions") != days or saved.get("calendar") != {"file": str(calendar), "sha256": _digest(calendar)}
            or saved.get("sources") != [s["proof"] for s in sources] or saved.get("protocol") != PROTOCOL
            or not _stamp(saved["registered_at"]) <= observed or _stamp(saved["registered_at"]) > started
            or any(saved.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Historical reported-turnover registration/calendar/source/protocol changed")
    units = _read(UNIT_EVIDENCE)
    if (units.get("status") != "official_provider_document_read" or units.get("turn_unit") != "percent"
            or _stamp(units["retrieved_at"]) > observed):
        raise ValueError("Saved original official percentage-unit evidence required")
    output = Path(out).resolve()
    if output.exists(): raise FileExistsError("Use a new historical research input directory")
    frames, batches = [], []
    for day in days:
        owners = [s for s in sources if day in s["plan"].get("days", [])]
        if len(owners) != 1:
            raise ValueError(f"Exactly one declared source cache must own {day}; do not fill or choose another date")
        rows, batch = _day(owners[0], day, previous, observed)
        frames.append(rows); batches.append(batch)
    rows = pd.concat(frames, ignore_index=True)
    if rows.duplicated(["date", "symbol"]).any() or sorted(rows.date.unique()) != days:
        raise ValueError("Historical observed panel lost exact date/security identities")
    for source in sources:
        if (_digest(source["proof"]["plan_file"]) != source["proof"]["plan_sha256"]
                or _digest(source["proof"]["receipts_file"]) != source["proof"]["receipts_sha256"]):
            raise ValueError("Original cache provenance changed during read")
    if _digest(registration) != registration_sha: raise ValueError("Registration changed during read")
    output.mkdir(parents=True, exist_ok=False)
    data = output / "dated_reported_turnover.parquet"
    rows.to_parquet(data, index=False)
    data_sha = _digest(data)
    finished = _now()
    result = {"schema": SCHEMA, "status": "completed_observed_historical_reported_turnover_inputs",
        "source_contract": SOURCE_CONTRACT, "started_at": started.isoformat(), "actual_derived_at": finished.isoformat(),
        "available_at": finished.isoformat(), "observed_at": observed.isoformat(), "registration": proof,
        "protocol": PROTOCOL, "calendar": {"file": str(calendar), "sha256": _digest(calendar)},
        "sessions": days, "first_previous_session": None, "first_previous_session_reason": "outside_2026_pinned_calendar_no_context_added",
        "data_file": str(data), "data_sha256": data_sha, "unit_evidence_file": str(UNIT_EVIDENCE.resolve()),
        "unit_evidence_sha256": _digest(UNIT_EVIDENCE), "unit_evidence": units, "turn_unit": "percent",
        "sources": [s["proof"] | {"original_global_status": s["receipt"]["status"],
            "original_failures": s["receipt"]["failures"], "raw_source_available_at": s["receipt"]["updated_at"]} for s in sources],
        "day_batches": batches, "counts": _counts(rows), "original_fields_read": list(FIELDS),
        "omitted_valuation_fields": [f for f in ORIGINAL_FIELDS if f not in FIELDS],
        "reported_turn_is_independent_share_denominator": False,
        "limitations": ["All rows are cached provider observations, not independently dated exchange roster coverage.",
            "Original retrieval and actual derivation times are not historical publication times.",
            "Reported daily percent is not net capital flow, intraday queue quality or independent circulating-share evidence.",
            "This input does not repair or promote original strict eight-type unknowns."], **FLAGS}
    write_json(output / "manifest.json", result)
    return output / "manifest.json", result


def load_historical_turnover_inputs(manifest, observed_at=None):
    path = Path(manifest).resolve()
    before = _digest(path)
    value = _read(path)
    observed = _stamp(observed_at) if observed_at is not None else _now()
    if observed > _now():
        raise ValueError("Historical input observed_at cannot be in the future of the actual read clock")
    data = Path(value["data_file"]).resolve()
    data.relative_to(path.parent)
    payload, sessions, previous = _sessions(value["calendar"]["file"])
    if (value.get("schema") != SCHEMA or value.get("source_contract") != SOURCE_CONTRACT
            or value.get("status") != "completed_observed_historical_reported_turnover_inputs"
            or value.get("protocol") != PROTOCOL or value.get("sessions") != sessions
            or value.get("turn_unit") != "percent" or value.get("first_previous_session") is not None
            or not _stamp(value["started_at"]) <= _stamp(value["actual_derived_at"]) <= _stamp(value["available_at"]) <= observed
            or any(value.get(k) is not v for k, v in FLAGS.items()) or _digest(data) != value["data_sha256"]
            or _digest(value["calendar"]["file"]) != value["calendar"]["sha256"]
            or _digest(value["registration"]["file"]) != value["registration"]["sha256"]
            or _digest(value["unit_evidence_file"]) != value["unit_evidence_sha256"]
            or _read(value["unit_evidence_file"]) != value["unit_evidence"]):
        raise ValueError("Saved historical reported-turnover contract/actual clocks/data pins changed")
    registered = _read(value["registration"]["file"])
    sources = [_source(s["root"], s["role"]) for s in registered["sources"]]
    if (registered.get("schema") != REGISTRATION_SCHEMA or registered.get("protocol") != PROTOCOL
            or registered.get("start") != START or registered.get("end") != END
            or registered.get("sessions") != sessions or registered.get("calendar") != value["calendar"]
            or registered.get("sources") != [s["proof"] for s in sources]
            or value["registration"].get("registered_at") != registered["registered_at"]
            or _stamp(registered["registered_at"]) > _stamp(value["started_at"])
            or any(registered.get(k) is not v for k, v in FLAGS.items())
            or value["sources"] != [s["proof"] | {"original_global_status": s["receipt"]["status"],
                "original_failures": s["receipt"]["failures"], "raw_source_available_at": s["receipt"]["updated_at"]} for s in sources]):
        raise ValueError("Saved reported-turnover source metadata differs from its actual registration")
    rows = pd.read_parquet(data)
    if (rows.duplicated(["date", "symbol"]).any() or sorted(rows.date.unique()) != sessions
            or _counts(rows) != value["counts"] or [b["day"] for b in value["day_batches"]] != sessions):
        raise ValueError("Saved reported-turnover full observed denominators changed")
    days = []
    for batch in value["day_batches"]:
        day = batch["day"]
        owners = [s for s in sources if day in s["plan"].get("days", [])]
        if len(owners) != 1:
            raise ValueError("Saved daily batch lost its exact registered source owner")
        source = owners[0]
        task = source["receipt"]["tasks"].get(day)
        expected_file = (source["root"] / task["file"]).resolve() if task else None
        if expected_file is not None: expected_file.relative_to(source["root"])
        if (not task or task.get("error_code") != "0" or task.get("date") != day
                or task.get("kind") != "stock" or task.get("method") != "query_daily_history_k_AStock"
                or batch.get("original_task") != task or batch.get("file") != str(expected_file)
                or batch.get("sha256") != task.get("sha256")
                or batch.get("previous_exact_session") != previous.get(day)
                or batch.get("source_role") != source["proof"]["role"] or batch.get("source_contract") != SOURCE_CONTRACT
                or batch.get("raw_source_available_at") != source["receipt"]["updated_at"]
                or not _stamp(day + "T15:00:00+08:00") <= _stamp(batch["raw_source_available_at"]) <= _stamp(value["actual_derived_at"])
                or batch.get("source_clock_basis") != "original_multiday_receipts_updated_at_conservative_completion_bound_not_per_call_clock"):
            raise ValueError("Saved daily batch source clock/task/previous-session metadata changed")
        selected = rows.loc[rows.date.eq(batch["day"])].copy()
        if len(selected) != task.get("quality", {}).get("rows") or any(batch.get(k) != v for k, v in _counts(selected).items()):
            raise ValueError("Saved daily reported-turnover counts changed")
        days.append({**batch, "rows": selected, "available_at": value["available_at"], "unit": "percent",
            "units_verified": True, "strict_full_close_admitted": False, "source_contract": SOURCE_CONTRACT})
    if _digest(path) != before: raise ValueError("Historical input manifest changed during read")
    return {"rows": rows, "day_batches": days, "source_proof": {"manifest": str(path), "sha256": before},
        "contract": SOURCE_CONTRACT, "calendar_payload": payload, "record": value}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cache-root", "extension-root", "calendar", "out", "registration"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    path, result = build(args.cache_root, args.extension_root, args.calendar, args.out, registration=args.registration)
    print(json.dumps({"manifest": str(path), "status": result["status"], "counts": result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
