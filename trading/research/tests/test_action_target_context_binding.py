from __future__ import annotations

import pandas as pd

from trading.research.action_target_context_binding import bind_target_context


def test_target_is_bound_but_pit_blocked_without_adjustment():
    target = {
        "schema": "quantification_joint_portfolio_paper_package_v1",
        "joint": {
            "common_terminal_day": "2026-10-13",
            "target_rows": [
                {"symbol": "000001.SZSE", "contributors": ["candidate"], "target_weight": 0.1},
                {"symbol": "600000.SSE", "contributors": ["candidate"], "target_weight": 0.1},
            ],
        },
    }
    action = {"schema": "stockdb_dated_action_context_sidecar_v4_receipt_v1"}
    context = pd.DataFrame([
        {"date": "2026-10-13", "symbol": "000001.SZSE", "action_event_present": True,
         "terms_complete": True, "pit_asof_safe": False, "company_action_applied": False,
         "publication_known_by_date": True, "publication_evidence_reasons": ["missing"]},
    ])

    result = bind_target_context(target, action, context)

    assert result["status"] == "BOUND_ACTION_CONTEXT_PIT_BLOCKED"
    assert result["target_count"] == 2
    assert result["action_context_bound_rows"] == 1
    assert result["pit_asof_safe_rows"] == 0
    assert result["company_action_applied_rows"] == 0
    assert result["target_mutated"] is False
    assert result["execution_admitted"] is False
