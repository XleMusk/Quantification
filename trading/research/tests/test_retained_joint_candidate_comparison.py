from __future__ import annotations

from trading.research.retained_joint_candidate_comparison import build_comparison


def _common():
    return {
        "stockdb_snapshot": "snapshot-v1",
        "common_window": {"start": "2024-01-31", "end": "2026-10-13", "sessions": 648},
        "strategies": [
            {
                "id": "low_vol_half_momentum_20",
                "research_decision": "CONTINUE_RESEARCH_ONLY",
                "stockdb_snapshot": "snapshot-v1",
                "metrics": {"net_return": 0.14, "max_drawdown": -0.16, "sharpe_zero_rf_252": 0.5, "orders": 10, "fills": 20, "fees_rmb": 10.0},
            },
            {
                "id": "mature_factor_balanced_ridge",
                "research_decision": "CONTINUE_RESEARCH_ONLY",
                "stockdb_snapshot": "snapshot-v1",
                "metrics": {"net_return": 0.36, "max_drawdown": -0.18, "sharpe_zero_rf_252": 0.7, "orders": 8, "fills": 16, "fees_rmb": 12.0},
            },
        ],
    }


def _joint():
    return {
        "joint": {
            "strategy_ids": ["trend_low_vol_half_momentum_20", "stockdb_factor_monthly_ridge_v1"],
            "common_terminal_day": "2026-10-13",
            "target_rows": [
                {"symbol": "000001.SZSE", "contributors": ["trend_low_vol_half_momentum_20"]},
                {"symbol": "600000.SSE", "contributors": ["stockdb_factor_monthly_ridge_v1"]},
            ],
            "dropped_rows": [{"symbol": "300000.SZSE"}],
            "constraints": {"actual_gross_weight": 0.5},
        },
    }


def test_build_comparison_binds_union_and_keeps_return_increment_blocked():
    result = build_comparison(
        _common(),
        _joint(),
        {"paper_order_count": 2, "route_available_count": 2, "route_blocked_count": 0, "modeled_fees_rmb": 4.0, "projected_final_equity_rmb": 139996.0},
    )

    assert result["status"] == "COMPLETED_RESEARCH_ONLY_RETAINED_JOINT_COMPARISON"
    assert result["joint_target"]["target_count"] == 2
    assert result["joint_target"]["contributor_target_counts"] == {
        "trend_low_vol_half_momentum_20": 1,
        "stockdb_factor_monthly_ridge_v1": 1,
    }
    assert result["incremental_decision"]["return_incremental_status"] == "NOT_COMPARABLE_PROJECTION_ONLY"
    assert result["execution_admitted"] is False
