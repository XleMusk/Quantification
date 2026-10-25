import json

import pytest

from trading.research.candidate_adoption_decision import build_decision


def _write_receipts(tmp_path, *, same_window=True):
    common = {
        "schema": "quantification_common_window_strategy_comparison_v1",
        "execution_admitted": False,
        "common_window": {"start": "2024-01-01", "end": "2024-01-03", "sessions": 3},
        "stockdb_snapshot": "snapshot-v1",
        "strategies": [{
            "id": "candidate-a", "label": "Candidate A", "candidate_spec_id": "spec-a",
            "strategy_validated": False,
            "metrics": {"net_return": 0.1},
        }],
    }
    cost = {
        "schema": "quantification_common_window_cost_pressure_v1",
        "execution_admitted": False,
        "common_window": common["common_window"] if same_window else {"start": "other"},
        "stockdb_snapshot": "snapshot-v1",
        "strategies": [{
            "id": "candidate-a", "base_net_return": 0.1,
            "pressure": [{"bps": 50, "net_return": 0.05}, {"bps": 100, "net_return": -0.01}],
        }],
    }
    selection = {
        "schema": "quantification_candidate_research_selection_v1",
        "rows": [
            {
                "strategy_id": "candidate-a", "research_action": "CONTINUE_RESEARCH_ONLY",
                "catalog_id": "spec-a", "research_followup_allowed": True,
            },
            {
                "strategy_id": None, "research_action": "STOP_RESEARCH_ONLY",
                "catalog_id": "stopped-family", "rationale": "preserve negative evidence",
            },
        ],
    }
    paths = []
    for name, payload in (("common.json", common), ("cost.json", cost), ("selection.json", selection)):
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths.append(path)
    return paths


def test_gate_preserves_research_only_boundary_and_cost_pressure(tmp_path):
    common, cost, selection = _write_receipts(tmp_path)
    result = build_decision(common, cost, selection)
    row = result["strategies"][0]
    assert result["status"] == "COMPLETED_RESEARCH_ONLY_ADOPTION_GATE"
    assert row["research_action"] == "CONTINUE_RESEARCH_ONLY"
    assert row["max_cost_pressure"]["bps"] == 100
    assert row["strategy_validated"] is False
    assert row["paper_allowed"] is False
    assert result["promotion_allowed"] is False
    assert result["summary"]["stop_research_only"] == 1
    assert any(item["selection_catalog_id"] == "stopped-family" and item["common_window_metrics"] is None
               for item in result["strategies"])


def test_gate_rejects_mismatched_comparison_inputs(tmp_path):
    common, cost, selection = _write_receipts(tmp_path, same_window=False)
    with pytest.raises(ValueError, match="windows differ"):
        build_decision(common, cost, selection)


def test_variant_inherits_selection_by_candidate_family(tmp_path):
    common, cost, selection = _write_receipts(tmp_path)
    common_payload = json.loads(common.read_text(encoding="utf-8"))
    cost_payload = json.loads(cost.read_text(encoding="utf-8"))
    common_payload["strategies"][0]["id"] = "candidate-a-h60"
    cost_payload["strategies"][0]["id"] = "candidate-a-h60"
    common.write_text(json.dumps(common_payload), encoding="utf-8")
    cost.write_text(json.dumps(cost_payload), encoding="utf-8")
    result = build_decision(common, cost, selection)
    row = result["strategies"][0]
    assert row["strategy_id"] == "candidate-a-h60"
    assert row["selection_strategy_id"] == "candidate-a"
    assert row["research_action"] == "CONTINUE_RESEARCH_ONLY"
