import json
from pathlib import Path

from trading.research.full_panel_strategy_decision import build_decision


def _inputs(tmp_path: Path):
    panel = tmp_path / "panel.json"
    cost = tmp_path / "cost.json"
    common = {"start": "2026-01-01", "end": "2026-03-31", "sessions": 60}
    panel.write_text(json.dumps({
        "schema": "quantification_full_panel_common_window_strategy_comparison_v1",
        "execution_admitted": False,
        "strategy_validated": False,
        "ledger_mutated": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "common_window": common,
        "stockdb_snapshot": "snapshot-v1",
        "strategies": [
            {"id": "good", "label": "Good", "metrics": {"net_return": 0.1, "sharpe_zero_rf_252": 1.2}},
            {"id": "bad", "label": "Bad", "metrics": {"net_return": -0.1, "sharpe_zero_rf_252": -0.2}},
        ],
    }), encoding="utf-8")
    cost.write_text(json.dumps({
        "schema": "quantification_common_window_cost_pressure_v1",
        "execution_admitted": False,
        "common_window": common,
        "stockdb_snapshot": "snapshot-v1",
        "strategies": [
            {"id": "good", "pressure": [{"bps": 50, "net_return": 0.02, "sharpe_zero_rf_252": 0.2}, {"bps": 100, "net_return": 0.01, "sharpe_zero_rf_252": 0.1}]},
            {"id": "bad", "pressure": [{"bps": 50, "net_return": -0.2, "sharpe_zero_rf_252": -0.3}]},
        ],
    }), encoding="utf-8")
    return panel, cost


def test_full_panel_decision_uses_base_and_worst_cost_slice(tmp_path: Path):
    panel, cost = _inputs(tmp_path)

    result = build_decision(panel, cost)

    assert result["strategy_count"] == 2
    assert result["summary"] == {"continue_research_only": 1, "stop_research_only": 1, "strategy_validated": 0}
    assert result["decisions"][0]["highest_cost_pressure"]["bps"] == 100
    assert result["decisions"][0]["research_action"] == "CONTINUE_RESEARCH_ONLY"
    assert result["decisions"][1]["research_action"] == "STOP_RESEARCH_ONLY"
    assert result["paper_allowed"] is False
    assert result["execution_admitted"] is False


def test_full_panel_decision_rejects_window_mismatch(tmp_path: Path):
    panel, cost = _inputs(tmp_path)
    payload = json.loads(cost.read_text(encoding="utf-8"))
    payload["common_window"]["end"] = "2026-04-01"
    cost.write_text(json.dumps(payload), encoding="utf-8")

    try:
        build_decision(panel, cost)
    except ValueError as error:
        assert "windows differ" in str(error)
    else:
        raise AssertionError("window mismatch must be rejected")
