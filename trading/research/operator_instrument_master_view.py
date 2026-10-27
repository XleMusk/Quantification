"""Read-only application view for cutoff-matched instrument identity."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class UnsupportedInstrumentMaster(ValueError):
    """Raised when a persisted coverage artifact is not this view's contract."""


def build_view(
    *,
    package: Mapping[str, Any],
    coverage: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    snapshot_path: str,
    coverage_path: str,
) -> dict[str, Any]:
    """Build the instrument identity/scope view without side effects."""

    if (
        coverage.get("schema") != "instrument_master_coverage_v1"
        or coverage.get("execution_admitted") is not False
    ):
        raise UnsupportedInstrumentMaster("证券主表覆盖诊断版本或执行边界不受支持")
    snapshot_master = snapshot.get("instrument_master") or {}
    snapshot_coverage = snapshot.get("instrument_master_coverage") or {
        "status": coverage.get("status"),
        "source_only": coverage.get("source_only") or [],
        "master_only": coverage.get("master_only") or [],
    }
    return {
        "schema": "offline_instrument_master_view_v1",
        "status": snapshot_master.get("status", "unknown"),
        "source_cutoff": package.get("source_cutoff"),
        "instrument_master": snapshot_master,
        "coverage": snapshot_coverage,
        "source_files": {
            "snapshot": snapshot_path,
            "coverage": coverage_path,
            "master": snapshot_master.get("master"),
        },
        "research_only": True,
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "basis": "cutoff-matched identity metadata and source-scope diagnostic; not PIT or broker authority",
    }


__all__ = ["UnsupportedInstrumentMaster", "build_view"]
