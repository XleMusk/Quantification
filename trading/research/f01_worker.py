"""Durable paper/research worker contract for F01 account replays.

This is a local task wrapper around the existing F01 adapter.  It records one
immutable request and one terminal receipt per run/attempt, and refuses to
rerun an attempt whose prior process stopped after the request was written.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from ..config import StrategyConfig
from ..contracts import RunContext
from .f01_account_replay import publish_bundle, replay_f01, load_bundle
from .run_identity import attempt_key, canonical_identity, run_key


REQUEST_SCHEMA = "f01_worker_request_v1"
TERMINAL_SCHEMA = "f01_worker_terminal_v1"


def attempt_directory(root: str | Path, identity: Mapping[str, Any], attempt: int) -> Path:
    identity = canonical_identity(identity)
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    return Path(root).resolve() / run_key(identity) / f"attempt_{attempt}"


def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"F01 worker receipt must be an object: {path}")
    return value


def prepare_attempt(root: str | Path, identity: Mapping[str, Any], attempt: int) -> Path:
    """Create the immutable request marker without running the replay."""

    identity = canonical_identity(identity)
    directory = attempt_directory(root, identity, attempt)
    request_file = directory / "request.json"
    terminal_file = directory / "terminal.json"
    if terminal_file.exists():
        raise FileExistsError("Attempt already has a terminal receipt")
    if request_file.exists():
        raise FileExistsError("Attempt request already exists and may be in flight")
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("Attempt directory contains unknown files")
    request = {
        "schema": REQUEST_SCHEMA,
        "status": "REQUESTED",
        "run_key": run_key(identity),
        "attempt_key": attempt_key(identity, attempt),
        "attempt": attempt,
        "identity": deepcopy(identity),
        "execution_admitted": False,
        "retry_permitted": False,
    }
    _atomic(request_file, request)
    return directory


def run_attempt(feed: Any, results_by_day: Mapping[str, Mapping[str, Any]], config: StrategyConfig,
                context: RunContext, root: str | Path, identity: Mapping[str, Any], attempt: int = 0,
                persistent_store: Any | None = None) -> dict[str, Any]:
    """Run one F01 attempt or refuse a duplicate/in-flight attempt."""

    identity = canonical_identity(identity)
    directory = attempt_directory(root, identity, attempt)
    request_file, terminal_file = directory / "request.json", directory / "terminal.json"
    if terminal_file.exists():
        terminal = _read(terminal_file)
        if terminal.get("status") == "COMPLETED":
            load_bundle(directory / "bundle")
        return terminal
    if not request_file.exists():
        prepare_attempt(root, identity, attempt)
        if persistent_store is not None:
            persistent_store.register_research_attempt(
                run_key(identity), attempt_key(identity, attempt), identity["study_id"], _read(request_file))
    else:
        request = _read(request_file)
        if request.get("attempt_key") != attempt_key(identity, attempt):
            raise ValueError("F01 worker request identity changed")
        raise RuntimeError("F01 attempt is already requested; recover before retrying")
    try:
        replay = replay_f01(feed, results_by_day, config, context, identity=identity, attempt=attempt)
        bundle = publish_bundle(replay, directory / "bundle")
        manifest = bundle / "manifest.json"
        terminal = {
            "schema": TERMINAL_SCHEMA,
            "status": "COMPLETED",
            "run_key": run_key(identity),
            "attempt_key": attempt_key(identity, attempt),
            "attempt": attempt,
            "bundle": str(bundle),
            "manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
            "execution_admitted": False,
            "retry_permitted": False,
            "account_replay": True,
        }
    except Exception as error:
        terminal = {
            "schema": TERMINAL_SCHEMA,
            "status": "FAILED",
            "run_key": run_key(identity),
            "attempt_key": attempt_key(identity, attempt),
            "attempt": attempt,
            "error": f"{type(error).__name__}: {error}",
            "execution_admitted": False,
            "retry_permitted": False,
            "account_replay": False,
        }
        _atomic(terminal_file, terminal)
        if persistent_store is not None:
            persistent_store.record_research_terminal(attempt_key(identity, attempt), terminal)
        raise
    _atomic(terminal_file, terminal)
    if persistent_store is not None:
        persistent_store.record_research_terminal(attempt_key(identity, attempt), terminal)
    return terminal


def recover_attempt(root: str | Path, identity: Mapping[str, Any], attempt: int = 0) -> dict[str, Any]:
    """Read a terminal receipt or classify a stopped request without rerunning."""

    identity = canonical_identity(identity)
    directory = attempt_directory(root, identity, attempt)
    request_file, terminal_file = directory / "request.json", directory / "terminal.json"
    if terminal_file.exists():
        terminal = _read(terminal_file)
        if terminal.get("attempt_key") != attempt_key(identity, attempt):
            raise ValueError("F01 terminal receipt identity changed")
        if terminal.get("status") == "COMPLETED":
            load_bundle(directory / "bundle")
        return {**terminal, "recovered": True}
    if request_file.exists():
        request = _read(request_file)
        if request.get("attempt_key") != attempt_key(identity, attempt):
            raise ValueError("F01 request identity changed")
        return {
            "schema": TERMINAL_SCHEMA,
            "status": "UNKNOWN_INTERRUPTED_NO_RETRY",
            "run_key": run_key(identity),
            "attempt_key": attempt_key(identity, attempt),
            "attempt": attempt,
            "execution_progress_unknown": True,
            "retry_permitted": False,
            "execution_admitted": False,
            "recovered": True,
        }
    raise FileNotFoundError(f"F01 attempt request is missing: {directory}")


__all__ = ["REQUEST_SCHEMA", "TERMINAL_SCHEMA", "attempt_directory", "prepare_attempt", "recover_attempt", "run_attempt"]
