import json

import pytest

from trading.research.candidate_library_binding import BLOCKED, VERIFIED, bind_library


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _inputs(tmp_path):
    catalog = _write(tmp_path / "catalog.json", {
        "execution_admitted": False,
        "entries": [
            {"id": "spec_a", "family": "trend", "state": "RESEARCH", "comparability_group": "snap",
             "metrics": {"net_return": -0.1}, "evidence": ["evidence.json"],
             "research_selection": {"decision": "CONTINUE_RESEARCH_ONLY"}},
            {"id": "shortline_native_lr_lgbm", "family": "shortline", "state": "SEPARATE",
             "comparability_group": "shortline", "metrics": {}, "evidence": ["shortline.json"]},
        ],
    })
    rollup = _write(tmp_path / "rollup.json", {
        "status": "COMPLETED_RESEARCH_ONLY_ADOPTION_ROLLUP",
        "execution_admitted": False, "candidate_summary": {"continue_research_only": 1},
        "candidate_decisions": [
            {"strategy_id": "panel_a", "research_action": "CONTINUE_RESEARCH_ONLY"},
            {"strategy_id": "panel_a_h60", "research_action": "CONTINUE_RESEARCH_ONLY"},
        ],
        "separate_gates": {"shortline_native_lr_lgbm": {"status": "SEPARATE_GATE", "paper_allowed": False}},
    })
    panel = _write(tmp_path / "panel.json", {
        "status": "COMPLETED_RESEARCH_ONLY_FULL_PANEL_COMMON_WINDOW",
        "execution_admitted": False, "common_window": {"sessions": 10},
        "comparison_contract": {"shared_stockdb_snapshot": True},
        "strategies": [
            {"id": "panel_a", "candidate_spec_id": "spec_a", "metrics": {"net_return": -0.1}},
            {"id": "panel_a_h60", "candidate_spec_id": "spec_a", "metrics": {"net_return": -0.2}},
        ],
    })
    return catalog, rollup, panel


def test_bind_library_keeps_panel_and_separate_gate_distinct(tmp_path):
    catalog, rollup, panel = _inputs(tmp_path)
    result = bind_library(catalog, rollup, panel, tmp_path / "bound.json")
    assert result["status"] == VERIFIED
    assert result["candidate_count"] == 2
    by_id = {row["id"]: row for row in result["bound_candidates"]}
    assert by_id["spec_a"]["binding_status"] == "COMMON_PANEL_DECISION_BOUND"
    assert by_id["spec_a"]["panel_binding"]["shared_spec_variant"] is True
    assert len(by_id["spec_a"]["adoption_decisions"]) == 2
    assert by_id["shortline_native_lr_lgbm"]["binding_status"] == "SEPARATE_GATE_BOUND"
    assert by_id["shortline_native_lr_lgbm"]["panel_strategies"] == []
    assert result["execution_admitted"] is False


def test_bind_library_rejects_live_source_boundary(tmp_path):
    catalog, rollup, panel = _inputs(tmp_path)
    rollup_data = json.loads(rollup.read_text())
    rollup_data["execution_admitted"] = True
    rollup.write_text(json.dumps(rollup_data), encoding="utf-8")
    with pytest.raises(ValueError, match="execution_admitted"):
        bind_library(catalog, rollup, panel, tmp_path / "bound.json")
