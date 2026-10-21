"""Shared recent-source composition and explicit immutable prefix binding."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from .baostock_daily_slice import digest
from .leader_close_preview import canonical
from .leader_daily_signals import _calendar_view, read_physical_snapshot

VIEW_SCHEMA = "leader_recent_source_feature_view_v1"
METHOD_ID = "fixed_recent_provider_history_prefix_v1"
COMPLETE = "complete_full_dated_close_inventory_not_admitted"


def _stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Recent-source history availability requires an explicit timezone")
    return result


def source_proof(raw, source, calendar_hash):
    day = source["plan"]["day"]
    if (source.get("read_only_verified") is not True or raw.empty or not raw.date.eq(day).all()
            or source["receipt"].get("day") != day
            or source["plan"].get("calendar_sha256") != calendar_hash):
        raise ValueError("Each complete source must retain its exact day and same pinned calendar")
    return {"day": day, "source_folder": source["source_folder"],
        "raw_daily_sha256": source["receipt"]["raw_daily_sha256"],
        "roster_raw_sha256": source["plan"]["roster_raw_sha256"],
        "calendar_sha256": calendar_hash, "ordinary_inventory_rows": len(raw),
        "read_only_verified": True, "source_inventory_complete": True,
        "receipt_status": source["receipt"]["status"],
        "full_dated_roster_inventory_complete": source["receipt"]["full_dated_roster_inventory_complete"],
        "source_schema": source["receipt"].get("schema"),
        "source_updated_at": source["receipt"].get("updated_at"),
        "source_completed_at": source["receipt"].get("completed_at")}


def compose_recent_sources(loaded, history_snapshot, calendar, *, base_cutoff=None,
                           history_reader=None, observed_at=None):
    """Compose already strictly loaded unique consecutive dates, read only.

    Both the descriptive preview and explicit provider bundle use this same
    cutoff and conflict policy. This function neither chooses a strategy nor
    promotes the original StockDB partition coverage.
    """
    loaded = sorted(loaded, key=lambda pair: pair[1]["plan"]["day"])
    days = [source["plan"]["day"] for _, source in loaded]
    if len(days) < 3 or len(days) != len(set(days)):
        raise ValueError("At least three distinct complete recent source dates are required")
    calendar, history_snapshot = Path(calendar).resolve(), Path(history_snapshot).resolve()
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    sessions, previous, status = _calendar_view(payload, days[0], days[-1], [])
    if status != "pinned_static_exchange_calendar" or sessions != days:
        raise ValueError("Recent sources must be consecutive sessions of one pinned calendar")
    cutoff = previous.get(days[0])
    if cutoff is None or (base_cutoff is not None and base_cutoff != cutoff):
        raise ValueError("Exact recent-source historical predecessor/cutoff is unavailable or changed")
    observed = _stamp(observed_at or datetime.now().astimezone().isoformat())
    if observed < _stamp(days[-1] + "T15:00:00+08:00"):
        raise ValueError("A future or not-yet-completed source close cannot enter the recent view")
    calendar_hash = digest(calendar)
    proofs = [source_proof(raw, source, calendar_hash) for raw, source in loaded]
    reader = history_reader or read_physical_snapshot
    original, metadata = reader(history_snapshot, end=cutoff)
    base = original.loc[original.date.le(cutoff)].copy()
    if base.empty or str(base.date.max()) != cutoff:
        raise ValueError("Original history lacks the exact session before the recent sources")
    combined = pd.concat([base, *[canonical(raw) for raw, _ in loaded]], ignore_index=True)
    if combined.duplicated(["code", "date"]).any() or combined.date.gt(days[-1]).any():
        raise ValueError("Recent view has duplicate identities or future observations")
    return {"combined": combined, "history_metadata": metadata, "base_history": base,
        "calendar_payload": payload, "calendar_status": status, "calendar_sha256": calendar_hash,
        "base_history_end": cutoff, "days": days, "sources": proofs, "loaded_sources": loaded,
        "original_reader_rows_after_cutoff_ignored": int(original.date.gt(cutoff).sum())}


def read_recent_source_view(path, *, now=None):
    """Read only the saved view manifest, boundaries and clock, not raw prices."""
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    false_flags = ("history_inventory_complete", "continuous_history_claimed", "source_verified", "pit_verified",
        "company_actions_verified", "provider_contacted", "actual_signal_publication_verified",
        "historical_publication_verified", "stockdb_modified", "native_features_modified", "prior_scores_modified",
        "prior_previews_modified", "future_bars_used", "strategy_validated", "selection_enabled", "signal_admitted",
        "execution_admitted", "orders_generated", "ths_payload_generated", "ledger_mutated", "model_refit", "future_labels_used_for_score")
    if (value.get("schema") != VIEW_SCHEMA or value.get("candidate_panel_only") is not True
            or value.get("remaining_symbol_gaps_retained") is not True
            or any(value.get(key) is not False for key in false_flags)
            or value.get("original_rows_after_cutoff_loaded_into_view") != 0
            or value.get("producer_method") != "original_five_family_default_single_build_no_IPO_feature_opt_in"
            or value.get("producer_pass_count") != 1):
        raise ValueError("Expected the explicit unadmitted original-method recent-source view manifest")
    if Path(value["root_lineage_file"]).resolve() != path or Path(value["view_root"]).resolve() != path.parent:
        raise ValueError("Select the exact root recent-source lineage, not a different daily view")
    calendar, history = Path(value["calendar"]).resolve(), Path(value["original_history_snapshot"]).resolve()
    if digest(calendar) != value["calendar_sha256"] or digest(history/"manifest.json") != value["original_history_manifest_sha256"]:
        raise ValueError("Recent-source calendar or original native manifest changed")
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    days, proofs = value["view_sessions"], value["recent_sources"]
    if not isinstance(days, list) or len(days) < 3 or days != sorted(set(days)):
        raise ValueError("Recent-source prefix dates must be explicit unique sessions")
    sessions, previous, status = _calendar_view(payload, days[0], days[-1], [])
    cutoff = value["base_history_max_observed_day"]
    if (status != "pinned_static_exchange_calendar" or sessions != days
            or value.get("base_history_requested_end") != cutoff or previous.get(days[0]) != cutoff
            or [proof["day"] for proof in proofs] != days):
        raise ValueError("Recent-source prefix dates or exact historical cutoff changed")
    available = _stamp(value["derived_at"])
    current = _stamp(now or datetime.now().astimezone().isoformat())
    if available > current or available < _stamp(days[-1]+"T15:00:00+08:00"):
        raise ValueError("Recent-source view availability cannot be future or before its last close")
    for proof in proofs:
        if (proof.get("read_only_verified") is not True or proof.get("source_inventory_complete") is not True
                or proof.get("full_dated_roster_inventory_complete") is not True or proof.get("receipt_status") != COMPLETE
                or proof.get("calendar_sha256") != value["calendar_sha256"]):
            raise ValueError("Recent-source view contains incomplete or inconsistent source proofs")
        terminal = _stamp(proof.get("source_completed_at") or proof.get("source_updated_at"))
        if terminal > available or terminal < _stamp(proof["day"]+"T15:00:00+08:00"):
            raise ValueError("Recent-source captured completion clock contradicts view availability")
    expected_id = sha256(json.dumps({"derived_at": value["derived_at"], "sources": proofs,
                                    "base_end": cutoff}, sort_keys=True).encode()).hexdigest()
    if value.get("shared_view_id") != expected_id:
        raise ValueError("Recent-source shared view identity changed")
    return {"method_id": METHOD_ID, "view_file": str(path), "view_sha256": digest(path),
        "shared_view_id": value["shared_view_id"], "base_history_end": cutoff, "available_at": value["derived_at"],
        "original_history_manifest_sha256": value["original_history_manifest_sha256"],
        "original_history_snapshot": str(history), "calendar": str(calendar), "calendar_sha256": value["calendar_sha256"],
        "view_sessions": list(days), "source_proofs": deepcopy(proofs)}
