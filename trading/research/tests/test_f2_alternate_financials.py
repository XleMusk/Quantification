import json

import pandas as pd

from trading.research.f2_alternate_financials import build_sidecar, build_single_source_decision


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


def test_single_source_decision_binds_timing_and_bridge_blockers(tmp_path):
    sidecar = tmp_path / "sidecar.json"
    bridge = tmp_path / "bridge.json"
    sidecar.write_text(json.dumps({
        "schema": "f01_publication_sidecar_v1",
        "execution_admitted": False,
        "decision_day": "2024-05-01",
        "rows": [{
            "symbol": "600004.SSE", "period": "2024Q1", "source_kind": "alternate_financials_sina",
            "source_root": "cache", "parent_net_profit": 10, "total_assets": 100,
            "field_publication_dates": {"parent_net_profit": "2024-04-27", "total_assets": "2024-04-28"},
            "field_provider_update_times": {"parent_net_profit": 1, "total_assets": 2},
            "publication_date": "2024-04-28", "unit": None, "currency": None,
            "scope_group": None, "retrieved_at": "2026-10-25T18:01:00+08:00",
            "asof_at": "2026-10-25T18:01:00+08:00", "revision_id": None,
            "contract_evaluation": {"pit_asof_safe": False},
        }],
    }), encoding="utf-8")
    bridge.write_text(json.dumps({
        "schema": "f2_sidecar_fact_bridge_v1", "execution_admitted": False,
        "rows": [{
            "symbol": "600004.SSE", "period": "2024Q1", "status": "BLOCKED_DATA",
            "reason_codes": ["publication:retrieval_or_asof_after_decision", "unit_missing", "scope_missing"],
            "facts": [], "factor_status": "NOT_COMPUTED",
        }],
    }), encoding="utf-8")

    result = build_single_source_decision(sidecar, bridge, symbol="600004.SSE", period="2024Q1")

    assert result["status"] == "BLOCKED_DATA"
    assert result["execution_admitted"] is False
    assert result["fact_contract"]["field_publication_dates"]["parent_net_profit"] == "2024-04-27"
    assert result["bridge_decision"]["reason_codes"] == [
        "publication:retrieval_or_asof_after_decision", "unit_missing", "scope_missing"
    ]
    assert "authoritative issuer evidence" in result["next_action"]
