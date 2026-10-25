"""Deterministic research run identity for F2 idempotent submissions."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping


IDENTITY_FIELDS = (
    "study_id", "code_version", "snapshot_id", "calendar_id", "rules_id",
    "benchmark_config_id", "initial_account_ref", "study_config",
)


def canonical_identity(values: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise TypeError("run identity must be a mapping")
    missing = [key for key in IDENTITY_FIELDS if key not in values]
    if missing:
        raise ValueError("run identity missing: " + ", ".join(missing))
    result = {key: values[key] for key in IDENTITY_FIELDS}
    if any(value is None or value == "" for value in result.values()):
        raise ValueError("run identity fields cannot be empty")
    # Ensure the identity can be persisted and has no NaN/non-deterministic JSON.
    json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return result


def run_key(values: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 identity for one study/input/config combination."""

    encoded = json.dumps(canonical_identity(values), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def attempt_key(identity: Mapping[str, Any], attempt: int) -> str:
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    return f"{run_key(identity)}:{attempt}"


__all__ = ["IDENTITY_FIELDS", "attempt_key", "canonical_identity", "run_key"]
