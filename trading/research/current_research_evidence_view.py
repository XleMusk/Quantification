"""Read-only application views for the current research evidence panel."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("saved research result must be an object")
    return value


def leader_comparison(project_root: str | Path) -> dict[str, Any]:
    """Read the newest saved leader comparison without replay or selection."""

    root = Path(project_root).resolve() / "outputs" / "leader_daily_replay"
    paths = sorted(root.glob("*/result.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        return {
            "status": "missing",
            "source_file": None,
            "cases": [],
            "candidate_panel_only": True,
            "strategy_validated": False,
            "selection_enabled": False,
            "execution_admitted": False,
        }
    payload = _read_json(paths[0])
    required = {
        "schema": "leader_daily_comparison_v2",
        "candidate_panel_only": True,
        "execution_admitted": False,
        "strategy_validated": False,
        "selection_enabled": False,
    }
    if any(payload.get(key) != value for key, value in required.items()):
        raise ValueError("leader comparison result lacks candidate-panel boundary")
    return {
        **payload,
        "source_file": str(paths[0].resolve()),
        "research_only": True,
        "automatic_promotion": False,
        "execution_admitted": False,
    }


def market_scoreboard(project_root: str | Path) -> dict[str, Any]:
    """Read the saved forecast scoreboard without refitting or updating it."""

    path = Path(project_root).resolve() / "outputs" / "market_regime_scoreboard" / "scoreboard.json"
    if not path.is_file():
        return {
            "status": "missing",
            "source_file": None,
            "summary_by_horizon": {},
            "selection_enabled": False,
            "strategy_validated": False,
            "execution_admitted": False,
        }
    payload = _read_json(path)
    if payload.get("schema") != "market_regime_scoreboard_v1" or payload.get("execution_admitted") is not False or payload.get("model_refit") is not False:
        raise ValueError("market scoreboard lacks frozen-model research boundary")
    return {
        **payload,
        "status": "saved_forecast_outcome_audit",
        "source_file": str(path.resolve()),
        "research_only": True,
        "automatic_promotion": False,
        "selection_enabled": False,
        "strategy_validated": False,
        "execution_admitted": False,
    }


__all__ = ["leader_comparison", "market_scoreboard"]
