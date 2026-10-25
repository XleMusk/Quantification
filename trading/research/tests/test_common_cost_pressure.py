from __future__ import annotations

import csv
from pathlib import Path

import pytest

from trading.research.common_cost_pressure import build_receipt


def _csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_cost_pressure_uses_turnover_and_keeps_decision(tmp_path: Path):
    daily = tmp_path / "daily.csv"
    _csv(daily, ["date", "equity", "turnover_value"], [
        {"date": "2024-01-02", "equity": 100.0, "turnover_value": 1000.0},
        {"date": "2024-01-03", "equity": 110.0, "turnover_value": 2000.0},
    ])
    source = {
        "schema": "quantification_common_window_strategy_comparison_v1",
        "common_window": {"start": "2024-01-02", "end": "2024-01-03", "sessions": 2},
        "stockdb_snapshot": "snapshot-v1",
        "strategies": [{
            "id": "candidate",
            "label": "candidate",
            "research_decision": "CONTINUE_RESEARCH_ONLY",
            "metrics": {"net_return": 0.1, "fees_rmb": 1.0, "window_turnover_value": 3000.0},
            "source_paths": {"daily": str(daily)},
        }],
    }
    result = build_receipt(source, [50.0, 100.0])
    pressure = result["strategies"][0]["pressure"]
    assert pressure[0]["extra_cost_rmb"] == pytest.approx(15.0)
    assert pressure[0]["net_return"] == pytest.approx(0.0)
    assert pressure[1]["extra_cost_rmb"] == pytest.approx(30.0)
    assert result["strategies"][0]["research_decision_unchanged"] == "CONTINUE_RESEARCH_ONLY"
    assert result["execution_admitted"] is False
    assert result["contract"]["strategy_rerun"] is False
