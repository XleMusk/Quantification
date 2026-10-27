"""Deterministic selection of historical source candidates.

This module only chooses the latest observed candidate on or before a cutoff.
It does not claim that the candidate was available then, that it is revision
safe, or that it is suitable for a factor or account.  Downstream F2 gates
must still prove publication, retrieval/as-of, revision and source bytes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


SCHEMA = "f2_historical_source_selection_v1"
BLOCKED = "BLOCKED_DATA"
SELECTED = "SOURCE_CANDIDATE_SELECTED"


def _parse_cutoff(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_timestamp(value: Any, *, timestamp_format: str | None = None) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        if timestamp_format:
            return datetime.strptime(raw, timestamp_format).replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_latest_on_or_before(
    candidates: Sequence[Mapping[str, Any]],
    cutoff: str | date | datetime,
    *,
    timestamp_key: str = "captured_at",
    timestamp_format: str | None = None,
) -> dict[str, Any]:
    """Select deterministically without promoting the selected row."""

    cutoff_date = _parse_cutoff(cutoff)
    observed: list[dict[str, Any]] = []
    invalid_count = 0
    for index, raw in enumerate(candidates):
        if not isinstance(raw, Mapping):
            invalid_count += 1
            continue
        row = dict(raw)
        parsed = _parse_timestamp(row.get(timestamp_key), timestamp_format=timestamp_format)
        if parsed is None:
            invalid_count += 1
            continue
        row["_selection_timestamp"] = parsed.isoformat()
        row["_selection_index"] = index
        observed.append(row)
    ordered = sorted(observed, key=lambda row: (row["_selection_timestamp"], row["_selection_index"]))
    eligible = [row for row in ordered if row["_selection_timestamp"][:10] <= cutoff_date.isoformat()]
    selected = eligible[-1] if eligible else (ordered[-1] if ordered else None)

    def clean(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: value for key, value in row.items() if not key.startswith("_selection_")}

    return {
        "schema": SCHEMA,
        "policy": "latest_on_or_before_cutoff_v1",
        "cutoff": cutoff_date.isoformat(),
        "timestamp_key": timestamp_key,
        "observed_count": len(observed),
        "invalid_count": invalid_count,
        "eligible_count": len(eligible),
        "selected_timestamp": selected.get("_selection_timestamp") if selected else None,
        "selected": clean(selected),
        "eligible": [clean(row) for row in eligible],
        "observed_after_cutoff_only": bool(observed) and not eligible,
    }


def build_receipt(input_path: str | Path, *, output: str | Path | None = None) -> dict[str, Any]:
    """Build a local selection receipt from an existing source probe."""

    path = Path(input_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("selection input must be an object")
    captures = payload.get("captures")
    if not isinstance(captures, list):
        raise ValueError("selection input requires captures")
    decision_day = payload.get("decision_day")
    if not isinstance(decision_day, str):
        raise ValueError("selection input requires decision_day")
    selection = select_latest_on_or_before(
        captures, decision_day, timestamp_key="timestamp", timestamp_format="%Y%m%d%H%M%S")
    receipt = {
        "schema": SCHEMA,
        "status": SELECTED if selection["eligible_count"] else BLOCKED,
        "execution_admitted": False,
        "factor_computed": False,
        "account_replay": False,
        "source": {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
                   "schema": payload.get("schema")},
        "symbol": payload.get("symbol"),
        "period": payload.get("period"),
        "decision_day": decision_day,
        "selection": selection,
        "next_gate": "pass selected candidate through publication/as-of/revision and source-byte gates before F2 admission",
    }
    if output is not None:
        output_path = Path(output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = build_receipt(args.input, output=args.output)
    print(json.dumps({"status": receipt["status"], "eligible_count": receipt["selection"]["eligible_count"],
                      "selected_timestamp": receipt["selection"]["selected_timestamp"],
                      "execution_admitted": False, "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


__all__ = ["BLOCKED", "SCHEMA", "SELECTED", "build_receipt", "main", "select_latest_on_or_before"]
