"""Read saved dated broad industries as an explicit posthoc research assumption.

Requested snapshot dates and returned updateDate values are not historical
publication clocks. Actual retrieval times remain intact and this provider is
deliberately incompatible with the strict prospective industry's available_at.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from .style_exposure import load_industries


METHOD_ID = "saved_dated_broad_industry_posthoc_backward_snapshot_v1"
SOURCE = "BaoStock.query_stock_industry(date=decision_day)"


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


def _day(value):
    value = str(value)
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("An exact ISO anchor/snapshot date is required")
    return value


def _time(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Actual industry receipt clocks need an explicit timezone")
    return stamp


def _label(value):
    if pd.isna(value):
        return None
    value = str(value).strip()
    return None if value.lower() in {"", "nan", "none"} else value


class _Provider:
    def __init__(self, snapshots, provenance):
        self._snapshots = snapshots
        self.provenance = provenance

    def __call__(self, anchor_day):
        anchor_day = _day(anchor_day)
        eligible = [value for value in self._snapshots if value["snapshot_date"] <= anchor_day]
        if not eligible:
            return None
        chosen = max(eligible, key=lambda value: value["snapshot_date"])
        return {**deepcopy(chosen), "anchor_day": anchor_day,
                "snapshot_staleness_calendar_days":
                    (date.fromisoformat(anchor_day) - date.fromisoformat(chosen["snapshot_date"])).days}


def load(root, observed_at):
    """Return a callable whose .provenance binds the full saved receipt order.

    No network, source mutation or historical availability claim occurs here.
    Missing symbols have no dictionary key; unknown labels retain None values.
    """
    root = Path(root).resolve()
    receipt_file = root / "result.json"
    receipt_sha = _digest(receipt_file)
    receipt = json.loads(receipt_file.read_text(encoding="utf-8-sig"))
    if receipt.get("schema") == "saved_dated_industry_composite_v1":
        from .leader_industry_composite import load as load_composite
        return load_composite(root, observed_at)
    if receipt.get("schema") == "saved_official_capco_industry_posthoc_v1":
        from .leader_official_industry import load as load_official
        return load_official(root, observed_at)
    entries = receipt.get("entries", [])
    if (receipt.get("status") != "completed_requested_industry_history"
            or receipt.get("source") != SOURCE or receipt.get("execution_admitted") is not False
            or receipt.get("revision_safe_pit_verified") is not False or not entries):
        raise ValueError("A completed saved posthoc-only dated industry receipt is required")
    days = [_day(entry["date"]) for entry in entries]
    if len(set(days)) != len(days) or receipt.get("requested_dates") != days:
        raise ValueError("The full distinct requested-date receipt order must be preserved")
    observed = _time(observed_at)
    files = []
    for entry in entries:
        path = (root / entry["file"]).resolve()
        path.relative_to(root)
        if path.name != entry["date"] + ".parquet" or _time(entry["retrieved_at"]) > observed:
            raise ValueError("One own-date saved snapshot, actually retrieved by observation, is required")
        files.append((path, _digest(path)))
    frame, loaded_receipt = load_industries(root, days)
    if loaded_receipt != receipt or _digest(receipt_file) != receipt_sha:
        raise ValueError("Industry receipt changed during the read")
    snapshots, source_entries = [], []
    for entry, (path, source_sha) in zip(entries, files):
        own = frame.loc[frame.date.eq(entry["date"])]
        if len(own) != entry["rows"] or own.empty or _digest(path) != source_sha:
            raise ValueError("Saved own-date industry scope changed during the read")
        update_dates = sorted(own.updateDate.astype(str).unique().tolist())
        if entry.get("update_dates") != update_dates:
            raise ValueError("Returned provider update dates differ from their saved receipt")
        classifications = sorted(own.industryClassification.dropna().astype(str).unique().tolist())
        if len(classifications) != 1 or not classifications[0].strip():
            raise ValueError("One broad-industry classification is required")
        membership = {row.symbol: _label(row.industry) for row in own.itertuples()}
        evidence = {"snapshot_date": entry["date"], "source_file": str(path),
            "source_sha256": source_sha, "receipt_file": str(receipt_file),
            "receipt_sha256": receipt_sha, "actual_available_at": entry["retrieved_at"],
            "source_update_dates": update_dates, "classification": classifications[0],
            "member_count": len(membership), "mapped_member_count": sum(value is not None for value in membership.values()),
            "revision_safe_PIT_verified": False, "theme_membership": False,
            "posthoc_only": True, "historical_publication_verified": False,
            "date_basis": "provider_requested_date_not_historical_publication_clock"}
        source_entries.append(deepcopy(evidence))
        snapshots.append({**evidence, "membership": membership})
    provenance = {"method_id": METHOD_ID, "source": SOURCE, "root": str(root),
        "receipt_file": str(receipt_file), "receipt_sha256": receipt_sha,
        "observed_at": observed.isoformat(), "snapshot_count": len(snapshots),
        "source_entries": source_entries, "source_entry_order": "original_completed_receipt_order",
        "selection": "latest_snapshot_date_at_or_before_anchor_no_current_backfill",
        "actual_available_at": max(entries, key=lambda entry: _time(entry["retrieved_at"]))["retrieved_at"],
        "revision_safe_PIT_verified": False, "theme_membership": False,
        "posthoc_only": True, "historical_publication_verified": False,
        "execution_admitted": False, "provider_contacted": False}
    return _Provider(snapshots, provenance)
