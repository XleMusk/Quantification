"""Exact saved-D research closes for native opening plans, without model work.

The native account already sealed a strict close proof.  This reader checks that
same proof against the current plan's feature component, then reads one dated
aggregate and its roster.  It does not repeat the historical/bulk import replay.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from .baostock_all_stock_probe import load_cached_day
from .baostock_daily_slice import FIELDS
from .baostock_full_close import roster_symbols
from .leader_eight_type_opening import _quote_rows
from .next_session_preview import _symbol
from .provider_close_batches import _acquisition_times
from .saved_leader_account_intent import _stamp


SCHEMA = "native_saved_opening_exact_close_quotes_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "broker_quote": False,
    "source_verified": False, "pit_verified": False, "corporate_actions_verified": False,
    "engine_advanced": False, "ledger_mutated": False, "model_refit": False,
    "network_accessed": False}
PROOF_KEYS = ("day", "source_folder", "raw_file", "raw_sha256", "plan_sha256",
    "receipt_sha256", "roster_raw_sha256", "calendar_sha256", "source_available_at")
SOURCE_SCHEMAS = {"baostock_full_dated_close_inventory_v1", "baostock_bulk_exact_dated_close_inventory_v1"}
COMPLETE = "complete_full_dated_close_inventory_not_admitted"


def _ref(path, expected=None, *, limit=10*1024*1024):
    path = Path(path).resolve()
    try:
        if path.stat().st_size > limit:
            raise ValueError("Saved exact-D price evidence exceeds its bounded reader")
        data = path.read_bytes()
    except OSError as error:
        raise ValueError("Declared saved exact-D price evidence is missing or unreadable") from error
    result = {"path": str(path), "sha256": sha256(data).hexdigest()}
    if expected is not None and result["sha256"] != expected:
        raise ValueError("Saved exact-D price evidence hash changed")
    return data, result


def _json(path, refs, expected=None):
    data, reference = _ref(path, expected)
    refs.append(reference)
    return json.loads(data.decode("utf-8-sig"))


def _unknown(source, wanted, current, reason):
    return {}, {"schema": SCHEMA, "status": "unavailable", "reason": reason,
        "source_day": source["source_day"], "execution_day": source["execution_day"],
        "read_at": current.isoformat(), "available_at": None,
        "plan_published_at": source.get("published_at"), "price_basis": "research_raw_close_rmb",
        "wanted_symbols": sorted(wanted), "priced_symbols": None, "missing_symbols": sorted(wanted),
        "refs": [], **FLAGS}


def _canonical_proof(value):
    result = {key: value[key] for key in PROOF_KEYS}
    for field in ("source_folder", "raw_file"):
        result[field] = str(Path(result[field]).resolve())
    return result


def read_quotes(source, facts, wanted, *, current):
    """Return original-D close quotes and explicit missing/status evidence.

    Missing proof is unavailable.  A declared proof that changes dates, bytes,
    calendar or origin raises; it never falls back to a mark or another session.
    """
    current = _stamp(current)
    wanted = set(wanted)
    if any(not isinstance(s, str) or _symbol(s) != s for s in wanted):
        raise ValueError("Exact canonical saved-plan symbols are required")
    d, e = source["source_day"], source["execution_day"]
    head, plan = facts["head"], facts["plan"]
    if (plan["source_day"] != d or plan["execution_day"] != e or plan["policy_id"] != source["policy_id"]
            or plan["published_at"] != source["published_at"]):
        raise ValueError("Price reader source differs from its exact published native plan")
    own = [row for row in head.get("raw_sources", []) if row.get("day") == d]
    if not own or (len(own) == 1 and any(own[0].get(key) is None for key in PROOF_KEYS)):
        return _unknown(source, wanted, current, "exact_D_sealed_native_raw_source_proof_unavailable")
    if len(own) != 1:
        raise ValueError("Exactly one sealed native raw proof is required for source D")
    own = own[0]
    if (own.get("read_only_verified") is not True or own.get("full_dated_roster_inventory_complete") is not True
            or own.get("receipt_status") != COMPLETE):
        return _unknown(source, wanted, current, "exact_D_native_raw_inventory_not_known_complete")
    refs = []
    native_ref = source["refs"]["native_head"]
    _json(native_ref["path"], refs, native_ref["sha256"])
    plan_ref = source["refs"]["plan"]
    if _json(plan_ref["path"], refs, plan_ref["sha256"]) != plan:
        raise ValueError("Exact saved native plan changed during quote reading")
    pub_ref = plan.get("source_publication")
    if not isinstance(pub_ref, dict) or not pub_ref.get("file") or not pub_ref.get("sha256"):
        return _unknown(source, wanted, current, "saved_plan_score_publication_reference_unavailable")
    pub = _json(pub_ref["file"], refs, pub_ref["sha256"])
    if (pub.get("schema") != "native_shortline_raw_score_publication_v1"
            or pub.get("execution_admitted") is not False or pub.get("model_refit") is not False
            or pub.get("future_labels_used_for_score") is not False):
        raise ValueError("Saved score publication does not retain its native research boundary")
    batch_file = pub.get("batch_result_file")
    if batch_file is None:
        return _unknown(source, wanted, current, "score_publication_batch_result_reference_unavailable")
    batch_path = Path(batch_file).resolve()
    bindings = [row for row in pub.get("files", [])
                if (Path(pub_ref["file"]).resolve().parent / row["path"]).resolve() == batch_path]
    if len(bindings) != 1:
        raise ValueError("Native score publication must pin its one exact batch result")
    batch = _json(batch_path, refs, bindings[0]["sha256"])
    if (batch.get("schema") != "native_shortline_frozen_score_batch_v1"
            or any(batch.get(k) is not False for k in
                   ("execution_admitted", "model_refit", "future_labels_used_for_score"))):
        raise ValueError("Saved native score batch research boundary changed")
    if batch.get("panel_result") is None:
        return _unknown(source, wanted, current, "score_batch_panel_metadata_reference_unavailable")
    panel = _json(batch["panel_result"], refs, batch.get("panel_result_sha256"))
    published, score_published = _stamp(plan["published_at"]), _stamp(pub["published_at"])
    if (panel.get("last_source_day") != d or panel.get("feature_contract_id") != plan["spec"]["feature_contract_id"]
            or panel.get("execution_admitted") is not False
            or not _stamp(panel["available_at"]) <= score_published <= published <= current):
        raise ValueError("Saved native panel day, contract or source publication clock changed")
    proof = panel.get("source_proof", {})
    days = [row for row in proof.get("day_sources", []) if row.get("day") == d]
    if not days:
        if proof.get("day_sources"):
            raise ValueError("Saved native panel names other days instead of exact source D")
        return _unknown(source, wanted, current, "score_panel_exact_D_component_unavailable")
    if len(days) != 1 or days[0].get("component_kind") != "strict_dated_close_csv":
        raise ValueError("Native plan D must name exactly one strict dated-close component")
    components = [row for row in proof.get("component_sources", [])
                  if row.get("component_id") == days[0].get("component_id")]
    if (len(components) != 1 or components[0].get("component_kind") != "strict_dated_close_csv"
            or components[0].get("source_days") != [d]):
        raise ValueError("Native D feature source cannot switch its physical component")
    component = components[0]["source_proof"]
    if (any(component.get(key) is None or days[0].get(key) is None for key in PROOF_KEYS)
            or _canonical_proof(component) != _canonical_proof(own)
            or _canonical_proof(days[0]) != _canonical_proof(own)):
        raise ValueError("Native account and current plan D prices are not the same exact source")
    folder, raw_file = Path(own["source_folder"]).resolve(), Path(own["raw_file"]).resolve()
    if raw_file != folder / "raw_daily.csv":
        raise ValueError("Native exact-D raw price file escaped its saved close package")
    close_plan = _json(folder / "plan.json", refs, own["plan_sha256"])
    receipt = _json(folder / "receipt.json", refs, own["receipt_sha256"])
    if (close_plan.get("schema") not in SOURCE_SCHEMAS or receipt.get("schema") != close_plan["schema"]
            or close_plan.get("day") != d or receipt.get("day") != d
            or receipt.get("status") != COMPLETE or receipt.get("full_dated_roster_inventory_complete") is not True
            or receipt.get("pending_symbols") or receipt.get("preserved_failed_batches")
            or close_plan.get("adjustflag") != "3" or close_plan.get("fields") != FIELDS
            or receipt.get("raw_daily_sha256") != own["raw_sha256"]
            or close_plan.get("roster_raw_sha256") != own["roster_raw_sha256"]
            or close_plan.get("calendar_sha256") != own["calendar_sha256"]
            or close_plan.get("calendar_sha256") != plan["calendar"]["sha256"]):
        raise ValueError("Exact-D close inventory schema, source identity or completion changed")
    for payload in (close_plan, receipt):
        if any(payload.get(k) is not False for k in
               ("execution_admitted", "selection_enabled", "strategy_validated", "source_verified", "pit_verified", "stockdb_modified", "ledger_mutated")):
            raise ValueError("Exact-D close source research boundary changed")
    _json(plan["calendar"]["file"], refs, own["calendar_sha256"])
    roster_folder = (Path(close_plan["roster_folder"]).resolve()
        if close_plan["schema"] == "baostock_bulk_exact_dated_close_inventory_v1" else folder / "roster")
    roster_receipt, roster, roster_pin = load_cached_day(roster_folder, d)
    ordinary = roster_symbols(roster)
    if (roster_pin["verified_current_raw_sha256"] != own["roster_raw_sha256"]
            or ordinary.symbol.tolist() != close_plan["symbols"]
            or len(roster) != close_plan["roster_total_rows"] or len(ordinary) != close_plan["ordinary_symbols"]
            or len(ordinary) != own["ordinary_inventory_rows"]):
        raise ValueError("Exact-D raw quotes do not retain their own dated roster")
    roster_raw = Path(roster_pin["raw_csv"]).resolve()
    refs.extend([{"path": str(roster_raw), "sha256": own["roster_raw_sha256"]}])
    # The native account sealed both acquisition receipts; no new clock is
    # substituted for those source completion times.
    clocks = _acquisition_times({"plan": close_plan, "receipt": receipt, "source_folder": str(folder)}, folder)
    roster_ref = {"path": clocks["roster_receipt"], "sha256": clocks["roster_receipt_sha256"]}
    refs.append(roster_ref)
    for key in ("roster_receipt_sha256", "terminal_completion_observed_at", "roster_receipt_completed_at"):
        if own.get(key) != clocks[key] or component.get(key) != clocks[key]:
            raise ValueError("Exact-D acquisition receipt clocks differ from the saved native proof")
    available = max(_stamp(clocks["terminal_completion_observed_at"]), _stamp(clocks["roster_receipt_completed_at"]))
    captured = [*clocks["acquisition_timestamps"].values(), *clocks["roster_acquisition_timestamps"].values()]
    if (available.isoformat() != own["source_available_at"]
            or not _stamp(d + "T15:00:00+08:00") <= available <= _stamp(panel["available_at"]) <= published
            or any(_stamp(value) > available for value in captured)):
        raise ValueError("Exact-D prices were not actually available by the original plan publication")
    raw_bytes, raw_ref = _ref(raw_file, own["raw_sha256"])
    refs.append(raw_ref)
    raw = pd.read_csv(io.BytesIO(raw_bytes), dtype=str, keep_default_na=False)
    if (list(raw.columns) != FIELDS.split(",") or raw.code.duplicated().any()
            or not raw.date.eq(d).all() or raw.code.tolist() != ordinary.code.tolist()
            or len(raw) != receipt.get("completed_rows") or len(raw) != receipt.get("completed_symbols")):
        raise ValueError("Raw close rows differ from the exact complete D inventory")
    status = dict(zip(ordinary.code, ordinary.tradeStatus))
    if any(value in {"0", "1"} and value != status[code] for code, value in zip(raw.code, raw.tradestatus)):
        raise ValueError("Known raw trading status contradicts the exact-D roster")
    if not raw.adjustflag.eq("3").all():
        raise ValueError("Only original adjustflag=3 RMB closes may price a native saved target")
    rows, nulls, unknown_status, unknown_st, suspended = [], [], [], [], []
    observed = set()
    for row in raw.to_dict("records"):
        symbol = _symbol(row["code"])
        if symbol not in wanted:
            continue
        observed.add(symbol)
        if row["tradestatus"] not in {"0", "1"}: unknown_status.append(symbol)
        if row["isST"] not in {"0", "1"}: unknown_st.append(symbol)
        if row["tradestatus"] == "0": suspended.append(symbol)
        try:
            value = Decimal(row["close"])
            valid = value.is_finite() and value > 0
        except (InvalidOperation, ValueError):
            valid = False
        if valid:
            rows.append(row)
        else:
            nulls.append(symbol)
    quotes = _quote_rows(rows, d, wanted)
    evidence = {"schema": SCHEMA, "status": "available", "reason": None,
        "source_day": d, "execution_day": e, "read_at": current.isoformat(),
        "available_at": available.isoformat(), "plan_published_at": published.isoformat(),
        "price_basis": "research_raw_close_rmb", "wanted_symbols": sorted(wanted),
        "priced_symbols": sorted(quotes), "missing_symbols": sorted(wanted-observed),
        "null_price_symbols": sorted(nulls), "unknown_trade_status_symbols": sorted(unknown_status),
        "unknown_ST_symbols": sorted(unknown_st), "suspended_symbols": sorted(suspended),
        "observed_rows": [{"symbol": _symbol(row["code"]), "date": row["date"], "close_raw": row["close"],
            "adjustflag_raw": row["adjustflag"], "tradestatus_raw": row["tradestatus"], "isST_raw": row["isST"]}
            for row in raw.to_dict("records") if _symbol(row["code"]) in wanted],
        "refs": list({r["path"]: r for r in refs}.values()),
        "component_id": days[0]["component_id"],
        "validation_basis": "already_sealed_native_proof_plus_exact_D_aggregate_and_roster",
        "original_bulk_projection_revalidated": False,
        "panel_metadata_originally_pinned_by_score_publication": batch.get("panel_result_sha256") is not None,
        "panel_metadata_pin_basis": "batch_explicit_hash" if batch.get("panel_result_sha256") else "actual_reader_capture_of_same_D_component_metadata",
        **FLAGS}
    return quotes, evidence


def validate_quote_evidence(evidence, *, current=None):
    """Recheck only the saved evidence pins, without loading rows or models."""
    current = _stamp(current or datetime.now(MARKET_TZ))
    if (not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA
            or any(evidence.get(k) is not v for k, v in FLAGS.items())
            or evidence.get("price_basis") != "research_raw_close_rmb"
            or _stamp(evidence["read_at"]) > current):
        raise ValueError("Native saved quote evidence or its research boundary changed")
    if evidence["status"] == "unavailable":
        if evidence.get("refs") != [] or evidence.get("available_at") is not None or not evidence.get("reason"):
            raise ValueError("Unavailable native quote evidence cannot claim source validation")
        return None
    if (evidence["status"] != "available" or not evidence.get("refs")
            or not _stamp(evidence["source_day"]+"T15:00:00+08:00") <= _stamp(evidence["available_at"])
                <= _stamp(evidence["plan_published_at"]) <= _stamp(evidence["read_at"])):
        raise ValueError("Native saved quote availability cannot be backdated")
    for ref in evidence["refs"]:
        _ref(ref["path"], ref["sha256"])
    return None
