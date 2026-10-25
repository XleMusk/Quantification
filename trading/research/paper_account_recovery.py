"""Copied-account recovery rehearsal for the persistent paper route.

The persistent daily directory is deliberately small: ``head.json`` and
``registration.json`` point to the immutable replay inputs and account
artifacts.  This module resolves those references, verifies their recorded
hashes, copies the state needed to reconstruct cash/positions into a versioned
recovery bundle, and restores it into an empty directory.  It never opens a
broker client or mutates the source account.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .recovery_bundle import (load_recovery_bundle, publish_recovery_bundle,
                              restore_recovery_bundle)


SCHEMA = "quantification_paper_account_recovery_rehearsal_v1"
_REQUIRED_STATE = ("head.json", "registration.json")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _digest(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}


def _require_unadmitted(value: Mapping[str, Any], label: str) -> None:
    if value.get("execution_admitted") is not False:
        raise ValueError(f"{label} is not explicitly unadmitted")
    if value.get("research_only") is not True:
        raise ValueError(f"{label} is not research-only")


def _recorded_ref(ref: Mapping[str, Any], label: str) -> tuple[Path, dict[str, Any]]:
    path = Path(str(ref.get("path", ""))).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Missing {label}: {path}")
    actual = _digest(path)
    expected = {"sha256": ref.get("sha256"), "bytes": actual["bytes"]}
    if actual["sha256"] != expected["sha256"]:
        raise ValueError(f"Hash mismatch for {label}: {path}")
    return path, {"path": str(path), **actual, "label": label}


def _checkpoint(run_dir: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = Path(run_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    state = {name: _read_json(root / name) for name in _REQUIRED_STATE}
    _require_unadmitted(state["head.json"], "head")
    _require_unadmitted(state["registration.json"], "registration")
    if state["head.json"].get("schema") != "stockdb_fixed_persistent_daily_research_v1":
        raise ValueError("Unsupported persistent daily checkpoint schema")
    if state["registration.json"].get("schema") != state["head.json"].get("schema"):
        raise ValueError("Head and registration schemas differ")
    return root, state["head.json"], state["registration.json"]


def _refs(root: Path, head: Mapping[str, Any], registration: Mapping[str, Any]) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    """Return copied role paths and explicitly tracked external inputs."""

    copied: dict[str, Path] = {
        "head": root / "head.json",
        "registration": root / "registration.json",
    }
    external: list[dict[str, Any]] = []

    for key in ("run", "protocol", "account", "replay", "daily"):
        ref = head.get("source", {}).get(key)
        if not isinstance(ref, Mapping):
            raise ValueError(f"head.source.{key} is missing")
        source, record = _recorded_ref(ref, f"head.source.{key}")
        copied[f"source_{key}"] = source

    supplement = head.get("supplement")
    if isinstance(supplement, Mapping):
        # The combined parquet can be hundreds of MB.  Its immutable identity
        # is part of the receipt; the account/state copy remains independently
        # restorable without pretending that a large external input was copied.
        _, record = _recorded_ref(supplement, "head.supplement")
        record["included"] = False
        external.append(record)

    for label, ref in (("registration.snapshot_manifest", registration.get("snapshot_manifest")),
                       ("registration.initial_supplement", registration.get("initial_supplement"))):
        if isinstance(ref, Mapping):
            source, record = _recorded_ref(ref, label)
            if label.endswith("snapshot_manifest"):
                copied["snapshot_manifest"] = source
            else:
                record["included"] = False
                external.append(record)

    schedule = registration.get("month_end_schedule", {})
    for index, ref in enumerate(schedule.get("calendar_files", []) if isinstance(schedule, Mapping) else []):
        if not isinstance(ref, Mapping):
            raise ValueError("month_end_schedule.calendar_files contains a non-object")
        source, _ = _recorded_ref(ref, f"calendar[{index}]")
        copied[f"calendar_{index}"] = source
    return copied, external


def _json_equal(path_a: Path, path_b: Path) -> bool:
    return _read_json(path_a) == _read_json(path_b)


def rehearse_paper_account_recovery(run_dir: str | Path, destination: str | Path,
                                    restore_dir: str | Path) -> dict[str, Any]:
    """Publish and restore a copied real paper checkpoint, then verify state.

    ``destination`` and ``restore_dir`` must be new/empty paths.  The caller
    receives a JSON-safe receipt suitable for an evidence file.
    """

    root, head, registration = _checkpoint(run_dir)
    copied, external = _refs(root, head, registration)
    destination = Path(destination).resolve()
    restore_dir = Path(restore_dir).resolve()
    if destination.exists():
        raise FileExistsError(f"Recovery bundle destination must be new: {destination}")
    if restore_dir.exists() and any(restore_dir.iterdir()):
        raise FileExistsError(f"Recovery restore destination must be empty: {restore_dir}")

    with tempfile.TemporaryDirectory(prefix="paper_recovery_stage_") as staging_name:
        staging = Path(staging_name)
        entries: dict[str, str] = {}
        restore_paths = {
            "head": "state/head.json", "registration": "state/registration.json",
            "source_run": "inputs/run.json", "source_protocol": "inputs/protocol.json",
            "source_account": "account/account.json", "source_replay": "account/replay.json",
            "source_daily": "account/daily.csv", "snapshot_manifest": "inputs/snapshot_manifest.json",
        }
        for role, source in copied.items():
            relative = restore_paths.get(role, f"calendar/{role}.json")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            entries[role] = relative
        bundle = publish_recovery_bundle(
            staging,
            entries,
            destination,
            versions={
                "rehearsal": SCHEMA,
                "method_id": str(head.get("method_id", "")),
                "last_completed_day": str(head.get("last_completed_day", "")),
                "generation": str(head.get("generation", "")),
            },
        )

    restored = restore_recovery_bundle(bundle, restore_dir)
    source_account = copied["source_account"]
    restored_account = restore_dir / "account" / "account.json"
    if not _json_equal(source_account, restored_account):
        raise ValueError("Restored account JSON differs from source account")
    source_value = _read_json(source_account)
    restored_value = _read_json(restored_account)
    source_state = {"cash": source_value.get("cash"), "positions": source_value.get("positions")}
    restored_state = {"cash": restored_value.get("cash"), "positions": restored_value.get("positions")}
    if source_state != restored_state:
        raise ValueError("Restored cash/positions differ from source account")
    manifest = load_recovery_bundle(bundle)["manifest"]
    return {
        "schema": SCHEMA,
        "status": "RECOVERY_REHEARSAL_VERIFIED",
        "execution_admitted": False,
        "research_only": True,
        "candidate_panel_only": bool(head.get("candidate_panel_only")),
        "source_run_dir": str(root),
        "bundle": str(bundle),
        "restore_dir": str(restore_dir),
        "generation": head.get("generation"),
        "last_completed_day": head.get("last_completed_day"),
        "manifest_file_roles": sorted(manifest["files"]),
        "source_account_sha256": _digest(source_account)["sha256"],
        "restored_account_sha256": _digest(restored_account)["sha256"],
        "cash_equal": source_state["cash"] == restored_state["cash"],
        "positions_equal": source_state["positions"] == restored_state["positions"],
        "source_cash": source_state["cash"],
        "position_symbols": len(source_state["positions"] or {}),
        "external_dependencies": external,
        "limitations": [
            "Paper/research checkpoint only; no broker or personal account is accessed.",
            "External raw inputs are hash-pinned in this receipt and are not copied into the bundle.",
        ],
        "restore_receipt": restored,
    }


def main(argv: list[str] | None = None) -> int:
    """Run one explicit paper-account recovery rehearsal from the CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True,
                        help="new recovery bundle directory")
    parser.add_argument("--restore", type=Path, required=True,
                        help="new or empty restore directory")
    parser.add_argument("--receipt", type=Path,
                        help="optional new JSON receipt path")
    args = parser.parse_args(argv)
    result = rehearse_paper_account_recovery(args.run_dir, args.bundle, args.restore)
    if args.receipt is not None:
        receipt = args.receipt.resolve()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        result = {**result, "receipt": str(receipt)}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


__all__ = ["SCHEMA", "main", "rehearse_paper_account_recovery"]
