"""Build a reproducible publication sidecar from an action-source cache.

The command only inventories metadata already saved with the source cache.  It
never derives retrieval time from file mtime or treats a cache read as a
network retrieval.  The resulting rows are suitable for the optional
``stockdb_action_context_view`` sidecar input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .publication_event_contract import evaluate_publication_evidence


def _walk_records(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if value.get("source_file") and (value.get("source_date") or value.get("publication_date")):
            yield value
        for child in value.values():
            yield from _walk_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_records(child)


def _read_json_records(path: Path) -> Iterable[Mapping[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    yield from _walk_records(payload)


def _first(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return ""


def _first_name(record: Mapping[str, Any], *names: str) -> str:
    for name in names:
        if record.get(name) not in (None, ""):
            return name
    return ""


def _load_upstream_input_receipts(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Load annual bulk-action input provenance without treating it as PIT evidence."""
    path = root / "input_input_receipts.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for symbol_key, item in payload.items():
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or symbol_key).upper()
        tasks = item.get("tasks")
        if not isinstance(tasks, Mapping):
            continue
        for task_name, task in tasks.items():
            if not isinstance(task, Mapping) or not str(task_name).startswith("actions_"):
                continue
            year = str(task_name)[len("actions_"):]
            if len(year) != 4 or not year.isdigit():
                continue
            result[(symbol, year)] = {
                "retrieved_at": str(task.get("retrieved_at") or ""),
                "path": str(task.get("path") or ""),
                "sha256": str(task.get("sha256") or ""),
                "original_file": str(task.get("original_file") or ""),
                "original_sha256": str(task.get("original_sha256") or ""),
                "origin": str(task.get("origin") or ""),
                "source_sdk_present": bool(task.get("source_sdk")),
                "source_error_code": str(task.get("source_error_code") or ""),
            }
    return result


def _source_row(record: Mapping[str, Any], root: Path, canonical_keys: set[tuple[str, str]],
                upstream_inputs: Mapping[tuple[str, str], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    symbol = str(_first(record, "symbol", "security_id")).upper()
    date = str(_first(record, "dividOperateDate", "operate_date", "date"))
    source_file = str(record.get("source_file", ""))
    source_path = Path(source_file)
    if not source_path.is_absolute():
        source_path = (root / source_path).resolve()
    provided_sha = str(_first(record, "source_sha256", "sha256"))
    actual_sha = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else ""
    source_sha = provided_sha or actual_sha
    retrieved_field = _first_name(
        record, "actual_retrieved_at", "original_network_retrieved_at", "retrieved_at", "source_retrieved_at")
    upstream = dict((upstream_inputs or {}).get((symbol, date[:4]), {}))
    return {
        "date": date,
        "symbol": symbol,
        "publication_date": str(_first(record, "source_date", "publication_date")),
        "publication_time": str(_first(record, "publication_time")),
        "retrieved_at": str(_first(
            record, "actual_retrieved_at", "original_network_retrieved_at", "retrieved_at", "source_retrieved_at")),
        "retrieved_at_field": retrieved_field,
        "asof_at": str(_first(record, "asof_at", "asof_date")),
        "revision_id": str(_first(record, "revision_id", "source_notice_id")),
        "source_file": str(source_path),
        "source_sha256": source_sha,
        "source_sha256_verified": bool(actual_sha and source_sha.lower() == actual_sha),
        "source_url": str(_first(record, "source_url")),
        "cache_read_at": str(_first(record, "actual_cache_read_at", "cache_read_at")),
        "original_network_retrieved_at": str(_first(record, "original_network_retrieved_at")),
        "upstream_input_retrieved_at": str(upstream.get("retrieved_at", "")),
        "upstream_input_path": str(upstream.get("path", "")),
        "upstream_input_sha256": str(upstream.get("sha256", "")),
        "upstream_input_original_file": str(upstream.get("original_file", "")),
        "upstream_input_original_sha256": str(upstream.get("original_sha256", "")),
        "upstream_input_origin": str(upstream.get("origin", "")),
        "upstream_input_source_sdk_present": bool(upstream.get("source_sdk_present", False)),
        "upstream_input_source_error_code": str(upstream.get("source_error_code", "")),
        "upstream_input_receipt_present": bool(upstream),
        "canonical_event_match": (symbol, date) in canonical_keys,
    }


def _collapse_event_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose one contract row per event while retaining every source artifact."""

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["symbol"], row["date"]), []).append(row)
    collapsed = []
    for _, candidates in sorted(grouped.items()):
        primary = max(candidates, key=lambda row: (
            bool(row["asof_at"]), bool(row["retrieved_at"]), bool(row["revision_id"]),
            bool(row["source_sha256_verified"]), bool(row["source_file"]), row["source_file"],
        ))
        merged = dict(primary)
        merged["source_artifact_count"] = len(candidates)
        merged["source_artifacts"] = [
            {"source_file": row["source_file"], "source_sha256": row["source_sha256"],
             "source_sha256_verified": row["source_sha256_verified"],
             "publication_date": row["publication_date"],
             "publication_time": row["publication_time"],
             "retrieved_at": row["retrieved_at"],
             "retrieved_at_field": row["retrieved_at_field"],
             "asof_at": row["asof_at"],
             "revision_id": row["revision_id"],
             "source_url": row["source_url"],
             "cache_read_at": row["cache_read_at"],
             "original_network_retrieved_at": row["original_network_retrieved_at"],
             "upstream_input_retrieved_at": row["upstream_input_retrieved_at"],
             "upstream_input_path": row["upstream_input_path"],
             "upstream_input_sha256": row["upstream_input_sha256"],
             "upstream_input_original_file": row["upstream_input_original_file"],
             "upstream_input_original_sha256": row["upstream_input_original_sha256"],
             "upstream_input_origin": row["upstream_input_origin"],
             "upstream_input_source_sdk_present": row["upstream_input_source_sdk_present"],
             "upstream_input_source_error_code": row["upstream_input_source_error_code"],
             "upstream_input_receipt_present": row["upstream_input_receipt_present"],
             "contract_evaluation": evaluate_publication_evidence(row, row["date"])}
            for row in sorted(candidates, key=lambda item: item["source_file"])
        ]
        collapsed.append(merged)
    return collapsed


def build_sidecar(action_root: str | Path, *, action_parquet: str | Path | None = None) -> dict[str, Any]:
    root = Path(action_root).resolve()
    parquet = Path(action_parquet).resolve() if action_parquet else root / "canonical_actions.parquet"
    if not root.is_dir() or not parquet.is_file():
        raise FileNotFoundError("action root or canonical action parquet is missing")
    canonical = pd.read_parquet(parquet, columns=["symbol", "dividOperateDate", "source_file"])
    canonical_keys = set(zip(canonical["symbol"].astype(str).str.upper(),
                             canonical["dividOperateDate"].fillna("").astype(str)))
    upstream_inputs = _load_upstream_input_receipts(root)
    candidates = []
    for path in root.rglob("*.json"):
        candidates.extend(_source_row(row, root, canonical_keys, upstream_inputs)
                          for row in _read_json_records(path))
    raw_rows = len(candidates)
    # A source row can be present in the root supplement and its audit file.
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in candidates:
        key = (row["symbol"], row["date"], row["source_file"], row["source_sha256"])
        unique.setdefault(key, row)
    rows = _collapse_event_rows(list(unique.values()))
    for row in rows:
        row["contract_evaluation"] = evaluate_publication_evidence(row, row["date"])
    artifacts = [artifact for row in rows for artifact in row["source_artifacts"]]
    pit_safe = sum(bool(row["contract_evaluation"]["pit_asof_safe"]) for row in rows)
    return {
        "schema": "a_share_action_publication_sidecar_v2",
        "status": "REAL_SOURCE_MAPPED_PIT_BLOCKED" if rows and not pit_safe else (
            "REAL_SOURCE_MAPPED_PARTIAL_PIT" if pit_safe else "NO_SOURCE_SIDECAR_ROWS"),
        "execution_admitted": False,
        "action_root": str(root),
        "canonical_action_parquet": str(parquet),
        "rows": rows,
        "summary": {
            "rows_mapped": len(rows),
            "raw_rows_mapped": raw_rows,
            "source_artifacts_mapped": sum(row["source_artifact_count"] for row in rows),
            "canonical_event_match_rows": sum(bool(row["canonical_event_match"]) for row in rows),
            "source_files_present": sum(bool(artifact["source_sha256_verified"])
                                       for row in rows for artifact in row["source_artifacts"]),
            "publication_date_rows": sum(bool(row["publication_date"]) for row in rows),
            "retrieved_at_rows": sum(bool(row["retrieved_at"]) for row in rows),
            "asof_at_rows": sum(bool(row["asof_at"]) for row in rows),
            "revision_id_rows": sum(bool(row["revision_id"]) for row in rows),
            "artifact_retrieved_at_rows": sum(bool(artifact["retrieved_at"]) for artifact in artifacts),
            "artifact_asof_at_rows": sum(bool(artifact["asof_at"]) for artifact in artifacts),
            "artifact_revision_id_rows": sum(bool(artifact["revision_id"]) for artifact in artifacts),
            "upstream_input_receipt_rows": sum(bool(row["upstream_input_receipt_present"]) for row in rows),
            "upstream_input_retrieved_at_rows": sum(bool(row["upstream_input_retrieved_at"]) for row in rows),
            "upstream_input_sha256_rows": sum(bool(row["upstream_input_sha256"]) for row in rows),
            "artifact_upstream_input_receipt_rows": sum(
                bool(artifact["upstream_input_receipt_present"]) for artifact in artifacts),
            "artifact_pit_safe_rows": sum(
                bool(artifact["contract_evaluation"]["pit_asof_safe"]) for artifact in artifacts),
            "pit_safe_rows": pit_safe,
        },
        "limitations": [
            "Only metadata saved with the source cache is inventoried.",
            "File mtime and cache_read_at are never used as retrieved_at.",
            "Per-artifact retrieval/as-of/revision fields and contract outcomes are retained after event deduplication.",
            "Upstream input receipts describe annual bulk-action cache retrieval and are retained as lineage only; they do not establish issuer-notice retrieval, as-of or revision safety.",
            "The sidecar does not apply cash/share actions or alter a replay.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action-root", required=True, type=Path)
    parser.add_argument("--action-parquet", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = build_sidecar(args.action_root, action_parquet=args.action_parquet)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
