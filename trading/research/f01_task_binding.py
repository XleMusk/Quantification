"""Readiness-gated binding from financial input tasks to the shared store."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, Mapping

from .run_identity import attempt_key, canonical_identity, run_key


SCHEMA = "f01_task_binding_v1"


def bind_f01_task(readiness: Mapping[str, Any], identity: Mapping[str, Any], attempt: int = 0,
                  *, persistent_store: Any | None = None) -> dict[str, Any]:
    """Bind a ready F01 input to the existing task store, or fail closed."""

    if not isinstance(readiness, Mapping) or not isinstance(identity, Mapping):
        raise TypeError("F01 readiness and identity mappings are required")
    identity = canonical_identity(identity)
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    blockers = list(readiness.get("blockers") or [])
    if readiness.get("status") != "READY_FOR_F01_FACT_ASSEMBLY":
        if not blockers:
            blockers.append("readiness_status_not_ready")
    if readiness.get("execution_admitted") is not False:
        blockers.append("execution_gate_not_false")
    request = {
        "schema": SCHEMA,
        "status": "BLOCKED_DATA" if blockers else "READY_TO_BIND",
        "run_key": run_key(identity),
        "attempt_key": attempt_key(identity, attempt),
        "attempt": attempt,
        "identity": deepcopy(identity),
        "readiness_sha256": sha256(json.dumps(readiness, ensure_ascii=False, sort_keys=True,
                                               allow_nan=False).encode("utf-8")).hexdigest(),
        "input_run": readiness.get("input_run"),
        "blockers": blockers,
        "task_created": False,
        "execution_admitted": False,
        "account_replay": False,
    }
    if blockers:
        return request
    if persistent_store is None:
        request["status"] = "READY_TO_BIND"
        return request
    persistent_store.register_research_attempt(
        request["run_key"], request["attempt_key"], identity["study_id"], request)
    request.update(status="BOUND_PAPER_RESEARCH_TASK", task_created=True)
    return request


__all__ = ["SCHEMA", "bind_f01_task"]
