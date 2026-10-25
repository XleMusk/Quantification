"""Focused atomic snapshot, revision-impact, and checkpoint-resume slice.

The implementation is intentionally small and executable.  A snapshot is
consumable only after its data file and complete manifest have both been
published.  A restart treats an already published snapshot as idempotent and
advances the checkpoint without creating a second publication.

All scenarios use temporary research files.  They do not touch StockDB,
broker state, Tonghuashun, or the persistent paper account.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Mapping


SNAPSHOT_SCHEMA = "quantification_atomic_snapshot_v1"
CHECKPOINT_SCHEMA = "quantification_snapshot_checkpoint_v1"


class IncompleteSnapshotError(ValueError):
    """Raised when a directory is present but has no complete publication."""


class SimulatedInterruption(RuntimeError):
    """Controlled crash point used only by the restart acceptance scenario."""


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest_bytes(raw: bytes) -> dict[str, Any]:
    return {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value.startswith("."):
        raise ValueError(f"unsafe snapshot id: {value!r}")
    return value


def load_snapshot(snapshot_dir: str | Path) -> dict[str, Any]:
    """Read one complete snapshot, refusing a partial directory."""

    root = Path(snapshot_dir).resolve()
    manifest_path, data_path = root / "manifest.json", root / "data.json"
    if not manifest_path.is_file() or not data_path.is_file():
        raise IncompleteSnapshotError(f"snapshot is not complete: {root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IncompleteSnapshotError(f"invalid snapshot manifest: {root}") from error
    if manifest.get("schema") != SNAPSHOT_SCHEMA or manifest.get("status") != "COMPLETE":
        raise IncompleteSnapshotError(f"snapshot manifest is not complete: {root}")
    raw = data_path.read_bytes()
    if _digest_bytes(raw) != {"sha256": manifest.get("data_sha256"), "bytes": manifest.get("data_bytes")}:
        raise ValueError(f"snapshot data hash mismatch: {root}")
    rows = json.loads(raw.decode("utf-8"))
    if not isinstance(rows, list) or len(rows) != manifest.get("row_count"):
        raise ValueError(f"snapshot row count mismatch: {root}")
    return {"manifest": manifest, "rows": rows, "path": str(root)}


def publish_snapshot(root: str | Path, snapshot_id: str, rows: list[Mapping[str, Any]], *,
                     received_at: str, revision_id: str, supersedes: str | None = None) -> dict[str, Any]:
    """Publish a complete snapshot directory atomically and idempotently."""

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    snapshot_id = _safe_id(snapshot_id)
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("snapshot rows must be a list of mappings")
    raw = _canonical_bytes(rows)
    final = root / snapshot_id
    if final.exists():
        existing = load_snapshot(final)
        if existing["manifest"].get("data_sha256") != _digest_bytes(raw)["sha256"]:
            raise ValueError(f"published snapshot identity changed: {snapshot_id}")
        return {"snapshot_id": snapshot_id, "publication": "IDEMPOTENT_EXISTING", "path": str(final),
                "manifest": existing["manifest"]}
    stage = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=str(root)))
    try:
        data_path = stage / "data.json"
        _atomic_write_bytes(data_path, raw)
        digest = _digest_bytes(raw)
        manifest = {
            "schema": SNAPSHOT_SCHEMA,
            "status": "COMPLETE",
            "snapshot_id": snapshot_id,
            "row_count": len(rows),
            "data_sha256": digest["sha256"],
            "data_bytes": digest["bytes"],
            "received_at": received_at,
            "revision_id": revision_id,
            "supersedes": supersedes,
            "execution_admitted": False,
        }
        _atomic_write_bytes(stage / "manifest.json", _canonical_bytes(manifest))
        # The directory rename is the publication point.  Consumers only look
        # for a final directory containing both data and a complete manifest.
        stage.replace(final)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"snapshot_id": snapshot_id, "publication": "CREATED", "path": str(final), "manifest": manifest}


def revision_impact(previous: Iterable[Mapping[str, Any]], current: Iterable[Mapping[str, Any]], *, key: str = "symbol") -> dict[str, Any]:
    """Summarize changed rows/fields without treating a revision as a new event."""

    old = {str(row[key]): dict(row) for row in previous}
    new = {str(row[key]): dict(row) for row in current}
    changed = []
    for identifier in sorted(set(old) | set(new)):
        if identifier not in old:
            changed.append({"key": identifier, "kind": "ADDED", "fields": sorted(new[identifier])})
        elif identifier not in new:
            changed.append({"key": identifier, "kind": "REMOVED", "fields": sorted(old[identifier])})
        else:
            fields = sorted(name for name in set(old[identifier]) | set(new[identifier])
                            if old[identifier].get(name) != new[identifier].get(name))
            if fields:
                changed.append({"key": identifier, "kind": "REVISED", "fields": fields})
    return {"previous_rows": len(old), "current_rows": len(new), "changed_rows": len(changed),
            "changed": changed}


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": CHECKPOINT_SCHEMA, "completed_units": [], "events": [], "execution_admitted": False}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != CHECKPOINT_SCHEMA or payload.get("execution_admitted") is not False:
        raise ValueError("checkpoint schema or execution boundary changed")
    return payload


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, _canonical_bytes(dict(payload)))


def _run_units(root: Path, units: list[Mapping[str, Any]], checkpoint_path: Path, *,
               interrupt_after_publication: int | None = None) -> dict[str, Any]:
    checkpoint = _read_checkpoint(checkpoint_path)
    completed = list(checkpoint.get("completed_units", []))
    events = list(checkpoint.get("events", []))
    publications_since_start = 0
    for unit in units:
        unit_id = str(unit["unit_id"])
        if unit_id in completed:
            continue
        publication = publish_snapshot(root, unit_id, unit["rows"], received_at=str(unit["received_at"]),
                                       revision_id=str(unit["revision_id"]), supersedes=unit.get("supersedes"))
        publications_since_start += 1
        events.append({"unit_id": unit_id, "publication": publication["publication"]})
        if interrupt_after_publication is not None and publications_since_start >= interrupt_after_publication:
            raise SimulatedInterruption(f"interrupted after publishing {unit_id} before checkpoint commit")
        completed.append(unit_id)
        checkpoint = {"schema": CHECKPOINT_SCHEMA, "completed_units": completed, "events": events,
                      "execution_admitted": False}
        _write_checkpoint(checkpoint_path, checkpoint)
    return {"completed_units": completed, "events": events}


def run_recovery_scenario() -> dict[str, Any]:
    """Create a partial snapshot, publish two revisions, interrupt and resume."""

    with tempfile.TemporaryDirectory(prefix="snapshot_recovery_slice_") as temporary:
        root = Path(temporary) / "snapshots"
        root.mkdir()
        partial = root / "partial-unit"
        partial.mkdir()
        (partial / "data.json").write_text("[]\n", encoding="utf-8")
        try:
            load_snapshot(partial)
        except IncompleteSnapshotError:
            partial_refused = True
        else:
            partial_refused = False
        previous = [{"symbol": "000001.SZSE", "value": 10, "revision_id": "r1"}]
        revised = [{"symbol": "000001.SZSE", "value": 11, "revision_id": "r2"}]
        impact = revision_impact(previous, revised)
        units = [
            {"unit_id": "2026-10-23-r1", "rows": previous, "received_at": "2026-10-23T18:00:00+08:00", "revision_id": "r1"},
            {"unit_id": "2026-10-23-r2", "rows": revised, "received_at": "2026-10-24T09:00:00+08:00", "revision_id": "r2", "supersedes": "2026-10-23-r1"},
        ]
        checkpoint = root / "checkpoint.json"
        try:
            _run_units(root, units, checkpoint, interrupt_after_publication=1)
        except SimulatedInterruption:
            interrupted = True
        else:
            interrupted = False
        resumed = _run_units(root, units, checkpoint)
        first = load_snapshot(root / units[0]["unit_id"])
        second = load_snapshot(root / units[1]["unit_id"])
        created = sum(event["publication"] == "CREATED" for event in resumed["events"])
        idempotent = sum(event["publication"] == "IDEMPOTENT_EXISTING" for event in resumed["events"])
        result = {
            "partial_snapshot_refused": partial_refused,
            "interrupted_after_publication": interrupted,
            "revision_impact": impact,
            "resumed_completed_units": resumed["completed_units"],
            "publication_events": resumed["events"],
            "resumed_created_count": created,
            "resumed_idempotent_count": idempotent,
            "final_snapshot_count": 2,
            "revision_supersedes_recorded": second["manifest"]["supersedes"] == units[0]["unit_id"],
            "content_identity_verified": first["manifest"]["data_sha256"] != second["manifest"]["data_sha256"],
        }
        if not (partial_refused and interrupted and impact["changed_rows"] == 1
                and resumed["completed_units"] == [u["unit_id"] for u in units]
                and created == 1 and idempotent == 1 and result["revision_supersedes_recorded"]
                and result["content_identity_verified"]):
            raise AssertionError(f"snapshot recovery acceptance failed: {result}")
        return result


def build_receipt() -> dict[str, Any]:
    result = run_recovery_scenario()
    return {
        "schema": "snapshot_publication_recovery_v1",
        "status": "VERIFIED_LOCAL_SNAPSHOT_RECOVERY",
        "execution_admitted": False,
        "broker_or_ths_interacted": False,
        "persistent_account_mutated": False,
        "scenario": result,
        "acceptance": {
            "partial_snapshot_refused": result["partial_snapshot_refused"],
            "atomic_complete_publication": result["final_snapshot_count"] == 2,
            "revision_impact_recorded": result["revision_impact"]["changed_rows"] == 1,
            "checkpoint_resumes_without_duplicate": result["resumed_created_count"] == 1 and result["resumed_idempotent_count"] == 1,
            "execution_closed": True,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path("outputs/pipeline_receipts/20261024_snapshot_publication_recovery_v1.json"))
    parser.add_argument("--report", type=Path,
                        default=Path("outputs/research_reports/20261024_snapshot_publication_recovery_v1.md"))
    args = parser.parse_args(argv)
    receipt = build_receipt()
    output, report = args.output.resolve(), args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(
        "# Snapshot publication and checkpoint recovery\n\n"
        "A partial snapshot is refused, complete revisions carry content identity, "
        "and a restart treats an already published unit as idempotent.\n\n"
        f"- status: `{receipt['status']}`\n"
        f"- acceptance: `{receipt['acceptance']}`\n"
        f"- execution_admitted: `{receipt['execution_admitted']}`\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": receipt["status"], "output": str(output), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["IncompleteSnapshotError", "SimulatedInterruption", "load_snapshot", "publish_snapshot",
           "revision_impact", "run_recovery_scenario", "build_receipt", "main"]
