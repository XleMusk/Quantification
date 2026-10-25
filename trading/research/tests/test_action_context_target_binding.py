from __future__ import annotations

from trading.research.action_context_target_binding import build_binding


def test_binding_keeps_target_and_action_context_pit_blocked():
    result = build_binding(
        {
            "common_terminal_day": "2026-10-13",
            "strategy_ids": ["trend_low_vol_half_momentum_20"],
            "target_rows": [{"symbol": "000001.SZSE", "contributors": ["trend_low_vol_half_momentum_20"], "target_quantity": 100}],
        },
        {"strategies": [{"strategy_id": "trend_low_vol_half_momentum_20", "symbol_breakdown": [{"symbol": "000001.SZSE", "action_events": 2, "plan_announce_present": 2, "operate_date_present": 2, "source_artifact_complete": 0}]}]},
    )

    assert result["status"] == "BOUND_TARGET_CONTEXT_PIT_BLOCKED"
    assert result["summary"]["target_rows"] == 1
    assert result["summary"]["rows_with_action_event"] == 1
    assert result["summary"]["pit_asof_safe_rows"] == 0
    assert result["execution_admitted"] is False
