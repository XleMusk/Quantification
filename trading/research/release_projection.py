"""Read-only projection of the newest research release manifest.

This adapter is shared by the local workbench and the research package.  It
only reads a release manifest; it never creates a release or enables execution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


RELEASE_ROOT = (
    Path(os.environ.get("QUANT_TRADING_HOME", r"D:\QuantificationData\trading"))
    / "research"
    / "releases"
).resolve()
SCHEMA = "quantification_workbench_release_status_v1"


def _missing(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "MISSING",
        "reason": reason,
        "version": None,
        "created_at": None,
        "manifest_path": None,
        "git_commit": None,
        "execution_admitted": False,
        "live_execution_ready": False,
        "missing_required_code": [],
        "missing_evidence": [],
        "missing_paper_evidence": [],
        "account_readiness": {},
        "read_only": True,
    }


def _sort_key(path: Path) -> tuple[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        created = str(payload.get("created_at") or "")
    except (OSError, UnicodeError, json.JSONDecodeError):
        created = ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return created, mtime


def latest_release(root: str | Path | None = None) -> dict[str, Any]:
    """Return a bounded projection of the newest readable release manifest."""

    release_root = Path(root).resolve() if root is not None else RELEASE_ROOT
    if not release_root.is_dir():
        return _missing("release_directory_missing")
    manifests = sorted(release_root.glob("*/release_manifest.json"), key=_sort_key, reverse=True)
    if not manifests:
        return _missing("release_manifest_missing")
    path = manifests[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _missing("release_manifest_unreadable")
    if not isinstance(payload, dict):
        return _missing("release_manifest_invalid")
    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    return {
        "schema": SCHEMA,
        "status": "AVAILABLE",
        "reason": None,
        "version": payload.get("version"),
        "created_at": payload.get("created_at"),
        "manifest_path": str(path),
        "git_commit": git.get("commit"),
        "execution_admitted": bool(payload.get("execution_admitted", False)),
        "live_execution_ready": bool(payload.get("live_execution_ready", False)),
        "missing_required_code": list(payload.get("missing_required_code") or []),
        "missing_evidence": list(payload.get("missing_evidence") or []),
        "missing_paper_evidence": list(payload.get("missing_paper_evidence") or []),
        "account_readiness": payload.get("account_readiness") or {},
        "read_only": True,
    }
