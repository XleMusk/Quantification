from pathlib import Path

import trading.research_diagnostics as module


def test_shortline_candidate_panel_replay_is_indexed_read_only(tmp_path: Path):
    record = {
        "schema": "a_share_shortline_candidate_panel_replay_v1",
        "strategy_id": "shortline_limitup_continuation_v1",
        "max_symbols": 20,
        "candidate_panel_only": True,
        "engine_ledger_replay_completed": True,
        "future_labels_used_for_score": False,
        "execution_admitted": False,
        "strategy_validated": False,
        "metrics": {"net_return": 0.01, "max_drawdown": -0.02,
                     "sharpe_zero_rf_252": 0.5, "fees": 10.0,
                     "turnover_value": 1000.0},
        "orders": [{"id": "o1"}],
        "fills": [{"id": "f1"}],
    }
    rows = module._shortline_candidate_panel_replay_rows(tmp_path, record)
    assert len(rows) == 1
    row = rows[0]
    assert row["family"] == "shortline_candidate_panel_replay_diagnostic"
    assert row["candidate_panel_only"] is True
    assert row["selection_enabled"] is False
    assert row["execution_admitted"] is False


def test_shortline_stage_selection_is_indexed_without_admission(tmp_path: Path):
    record = {
        "schema": "a_share_shortline_stage_selection_diagnostic_v1",
        "stages": [{"stage": 1, "hit_5_rate": 0.01}],
        "future_labels_used_for_selection": False,
        "candidate_generated": False,
        "target_generated": False,
        "orders_generated": False,
        "execution_admitted": False,
    }
    rows = module._shortline_stage_selection_rows(tmp_path, record)
    assert len(rows) == 1
    assert rows[0]["family"] == "shortline_stage_selection_diagnostic"
    assert rows[0]["future_labels_used_for_selection"] is False
    assert rows[0]["candidate_generated"] is False
