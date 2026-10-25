import json

import pandas as pd

from trading.research.f2_alternate_financials import build_sidecar


def test_alternate_financials_sidecar_preserves_field_timing_and_blocks_pit(tmp_path):
    root = tmp_path / "alternate"
    root.mkdir()
    (root / "result.json").write_text(json.dumps({
        "status": "completed_candidate_statements", "execution_admitted": False,
        "started_at": "2026-10-25T18:00:00+08:00", "finished_at": "2026-10-25T18:01:00+08:00",
    }), encoding="utf-8")
    pd.DataFrame([
        {"symbol": "600004.SSE", "report_day": "2024-03-31", "statement": "lrb", "item_field": "PARENETP", "value_numeric_unscaled": 10.0, "publish_day": "2024-04-27", "provider_update_time": 1},
        {"symbol": "600004.SSE", "report_day": "2024-03-31", "statement": "fzb", "item_field": "TOTASSET", "value_numeric_unscaled": 100.0, "publish_day": "2024-04-28", "provider_update_time": 2},
    ]).to_parquet(root / "statement_items.parquet", index=False)
    result = build_sidecar(root, decision_day="2024-05-01")
    row = result["rows"][0]
    assert result["status"] == "ALTERNATE_PROVIDER_MAPPED_PIT_BLOCKED"
    assert row["parent_net_profit"] == 10.0
    assert row["total_assets"] == 100.0
    assert row["publication_date"] == "2024-04-28"
    assert "required_field_publication_dates_inconsistent" in row["contract_evaluation"]["publication_evidence_reasons"]
    assert result["execution_admitted"] is False
