"""Read-only application views for same-sample strategy comparison.

HTTP routes own status-code mapping. These functions own the research result
use case and delegate to the existing comparison reader without creating an
account, replaying a source, or changing a saved artifact.
"""

from __future__ import annotations

from .. import research_comparison


def window(runs: str, start: str, end: str) -> dict:
    """Compare saved strategy accounts on one explicit common window."""

    if not isinstance(runs, str):
        raise TypeError("runs must be a comma-separated string")
    return research_comparison.compare(runs.split(","), start, end)


def snapshots(limit: int = 12) -> dict:
    """Return the saved comparison catalog for the research workbench."""

    return research_comparison.saved_catalog(limit=limit)


__all__ = ["snapshots", "window"]
