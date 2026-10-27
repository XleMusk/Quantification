"""Read-only application view for the saved candidate evidence catalog.

The catalog reader owns bounded file and provenance checks. This application
view gives the API and research workbench one explicit consumer boundary; it
never refreshes a source, runs a producer, selects a strategy, or mutates an
account.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .. import research_evidence_catalog


SCHEMA = research_evidence_catalog.SCHEMA


def build_view(
    project_root: str | Path,
    data_root: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project the saved candidate catalog for the workbench consumer."""

    return research_evidence_catalog.read_catalog(project_root, data_root, now=now)


__all__ = ["SCHEMA", "build_view"]
