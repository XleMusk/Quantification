"""Load the saved StockDB/Tencent independent-window evidence for consumers.

The artifact is supplemental review evidence only.  This helper deliberately
accepts only completed, non-admitted receipts so a strategy board cannot turn a
partial or execution-enabled file into an implicit promotion signal.
"""

from __future__ import annotations

import json
from pathlib import Path


def latest_artifact(root: str | Path | None = None) -> Path | None:
    root = Path(root) if root else Path(__file__).parents[2] / "outputs" / "stockdb_independent_window_stability"
    if not root.exists():
        return None
    for candidate in sorted(root.rglob("run.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            payload.get("schema") == "stockdb_independent_window_stability_v1"
            and payload.get("status") == "completed_research_only"
            and payload.get("selection_enabled") is False
            and payload.get("execution_admitted") is False
        ):
            return candidate.resolve()
    return None


def by_strategy(path: str | Path | None) -> tuple[Path | None, dict[str, dict]]:
    if not path:
        return None, {}
    path = Path(path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return path, {}
    rows = {}
    for row in payload.get("strategy_summaries") or []:
        candidate = str(row.get("candidate") or "")
        if candidate:
            calendar_diagnostics = payload.get("calendar_diagnostics") or {}
            calendar_provided = bool(calendar_diagnostics.get("provided"))
            calendar_meta = calendar_diagnostics.get("calendar") or {}
            rows[candidate] = {
                "source": str(path),
                "window_count": payload.get("window_count"),
                "window_start": payload.get("window_start"),
                "window_end": payload.get("window_end"),
                "alternate_provider": payload.get("alternate_provider"),
                "positive_windows": row.get("positive_windows"),
                "mean_net_return": row.get("mean_net_return"),
                "worst_net_return": row.get("worst_net_return"),
                "worst_max_drawdown": row.get("worst_max_drawdown"),
                "paired_rows_all_windows": row.get("paired_rows_all_windows"),
                "non_overlapping": payload.get("non_overlapping"),
                "calendar_window_coverage_verified": payload.get("calendar_window_coverage_verified"),
                "calendar_continuity_verified": payload.get("calendar_continuity_verified"),
                "calendar_gap_count": (
                    len(calendar_diagnostics.get("gaps") or [])
                    if calendar_provided else None
                ),
                "calendar_source": calendar_meta.get("path", "") if calendar_provided else "",
            }
    return path, rows
