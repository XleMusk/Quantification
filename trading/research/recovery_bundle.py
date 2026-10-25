"""Versioned, paper-only recovery bundles for research and daily state.

The bundle records the exact configuration, strategy, input, ledger and daily
cursor files used by a run.  It is deliberately independent of broker state:
restoring it recreates research artifacts and paper bookkeeping only.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping


SCHEMA = "quantification_recovery_bundle_v1"


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe recovery path: {value}")
    return path


def _digest(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}


def _validate_entries(source_root: Path, entries: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    if not isinstance(entries, Mapping) or not entries:
        raise ValueError("Recovery bundle needs at least one named file")
    result: dict[str, dict[str, Any]] = {}
    for role, relative_name in entries.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError("Recovery file roles must be non-empty strings")
        relative = _safe_relative(relative_name)
        source = (source_root / relative).resolve()
        try:
            source.relative_to(source_root)
        except ValueError as error:
            raise ValueError(f"Recovery source escapes root: {relative_name}") from error
        if not source.is_file():
            raise FileNotFoundError(f"Recovery source is missing: {relative_name}")
        result[role] = {"restore_path": relative.as_posix(), **_digest(source)}
    return result


def publish_recovery_bundle(source_root: str | Path, entries: Mapping[str, str],
                            destination: str | Path, *, versions: Mapping[str, str] | None = None) -> Path:
    """Copy named source files into a new immutable recovery bundle."""

    source_root = Path(source_root).resolve()
    destination = Path(destination).resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    if destination.exists():
        raise FileExistsError(f"Recovery bundle destination must be new: {destination}")
    files = _validate_entries(source_root, entries)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for role, record in files.items():
            source = source_root / record["restore_path"]
            artifact = destination / "files" / role
            artifact.parent.mkdir(parents=True, exist_ok=True)
            temporary = artifact.with_name(artifact.name + ".tmp")
            shutil.copy2(source, temporary)
            temporary.replace(artifact)
            record["artifact"] = artifact.relative_to(destination).as_posix()
        manifest = {
            "schema": SCHEMA,
            "status": "PUBLISHED",
            "execution_admitted": False,
            "versions": dict(versions or {}),
            "files": files,
            "limitations": [
                "Paper/research artifacts only; no broker or account channel is restored.",
                "The source files remain unchanged; a restore creates a separate destination.",
            ],
        }
        temporary_manifest = destination / "restore_manifest.json.tmp"
        temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_manifest.replace(destination / "restore_manifest.json")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def load_recovery_bundle(bundle: str | Path) -> dict[str, Any]:
    """Validate a published bundle and return its manifest."""

    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "restore_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("status") != "PUBLISHED":
        raise ValueError("Recovery manifest is not published")
    if manifest.get("execution_admitted") is not False:
        raise ValueError("Recovery bundle is not explicitly unadmitted")
    for role, record in manifest.get("files", {}).items():
        artifact_name = _safe_relative(record.get("artifact", ""))
        artifact = (bundle / artifact_name).resolve()
        try:
            artifact.relative_to(bundle)
        except ValueError as error:
            raise ValueError(f"Recovery artifact escapes bundle: {role}") from error
        if not artifact.is_file() or _digest(artifact) != {"sha256": record.get("sha256"), "bytes": record.get("bytes")}:
            raise ValueError(f"Recovery artifact hash mismatch: {role}")
    return {"manifest": manifest, "bundle": str(bundle)}


def restore_recovery_bundle(bundle: str | Path, destination: str | Path) -> dict[str, Any]:
    """Restore a validated bundle into a new or empty directory."""

    bundle = Path(bundle).resolve()
    destination = Path(destination).resolve()
    loaded = load_recovery_bundle(bundle)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Recovery destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        for role, record in loaded["manifest"]["files"].items():
            restore_path = _safe_relative(record["restore_path"])
            artifact = bundle / _safe_relative(record["artifact"])
            target = destination / restore_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            shutil.copy2(artifact, temporary)
            temporary.replace(target)
        restored = {
            "schema": SCHEMA,
            "status": "RESTORED",
            "execution_admitted": False,
            "source_bundle": str(bundle),
            "destination": str(destination),
            "roles": sorted(loaded["manifest"]["files"]),
            "versions": loaded["manifest"].get("versions", {}),
        }
        (destination / "restore_receipt.json").write_text(json.dumps(restored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return restored


def backup_sqlite_database(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Create and verify a consistent SQLite backup using SQLite's backup API."""

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise ValueError("SQLite backup source and destination must differ")
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(f"SQLite backup destination must be new: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(str(source), uri=False)
    destination_db = sqlite3.connect(str(destination), uri=False)
    try:
        source_db.backup(destination_db)
        destination_db.commit()
        check = destination_db.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"SQLite backup integrity check failed: {check}")
    except Exception:
        destination_db.close()
        source_db.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        destination_db.close()
        source_db.close()
    return {
        "schema": "quantification_sqlite_backup_receipt_v1",
        "status": "BACKUP_VERIFIED",
        "execution_admitted": False,
        "source": str(source),
        "destination": str(destination),
        "source_sha256": sha256(source.read_bytes()).hexdigest(),
        "destination_sha256": sha256(destination.read_bytes()).hexdigest(),
        "integrity_check": "ok",
        "limitations": ["Paper/research SQLite state only; no broker or external account is accessed."],
    }


__all__ = ["SCHEMA", "backup_sqlite_database", "load_recovery_bundle", "publish_recovery_bundle", "restore_recovery_bundle"]
