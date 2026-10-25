import json

from trading.research.f01_coverage_funnel import (
    classify_scoped_row,
    from_scoped_receipt,
)


def test_scoped_receipt_funnel_preserves_blocked_pit_and_market_reasons():
    result = from_scoped_receipt()
    assert result["status"] == "BLOCKED_DATA"
    assert result["summary"]["event_master_rows"] == 3
    assert result["summary"]["required_fields_rows"] == 3
    assert result["summary"]["publication_date_rows"] == 3
    assert result["summary"]["revision_safe_pit_rows"] == 0
    assert result["summary"]["f01_eligible_rows"] == 0
    assert result["zero_fill_applied"] is False
    for row in result["rows"]:
        assert "revision_safe_pit_unverified" in row["reasons"]
        assert "retrieval_asof_evidence_missing" in row["reasons"]
        assert "market_join_not_supplied" in row["reasons"]


def test_missing_required_field_is_explicit_and_not_zero_filled():
    row = {
        "symbol": "000001.SZSE", "period": "2024Q1",
        "parent_net_profit": None, "total_assets": "10",
        "pub_date": "2024-04-30",
        "report_source": {"url": "https://example.invalid/report.pdf"},
        "source_checks": {
            "unit_explicit_as_rmb_yuan": True,
            "parent_field_explicit": False,
            "total_assets_explicit": True,
        },
        "pit": {"revision_safe_pit_verified": False},
    }
    result = classify_scoped_row(row, market_symbols={"000001.SZSE"})
    assert result["required_fields_observed"] is False
    assert result["eligible_for_f01"] is False
    assert result["reasons"] == [
        "required_field_missing", "units_or_scope_unverified",
        "revision_safe_pit_unverified", "retrieval_asof_evidence_missing",
    ]


def test_market_join_is_only_positive_when_explicitly_supplied():
    result = from_scoped_receipt(market_symbols={"600004.SSE", "688008.SSE", "000002.SZSE"})
    assert result["summary"]["market_join_rows"] == 3
    assert result["summary"]["f01_eligible_rows"] == 0
    assert all("market_join_not_supplied" not in row["reasons"] for row in result["rows"])
    assert all("retrieval_asof_evidence_missing" in row["reasons"] for row in result["rows"])


def test_publication_sidecar_is_consumed_but_real_rows_remain_blocked():
    result = from_scoped_receipt(
        publication_sidecar="outputs/pipeline_receipts/20261024_f01_publication_sidecar_v1.json",
        market_symbols={"600004.SSE", "688008.SSE", "000002.SZSE"},
        as_of="2024-05-01",
    )
    assert result["source_kind"] == "scoped_quarterly_receipt_with_publication_sidecar"
    assert result["summary"]["event_master_rows"] == 3
    assert sum(row["publication_sidecar_attached"] for row in result["rows"]) == 3
    assert result["summary"]["retrieval_asof_rows"] == 0
    assert result["summary"]["f01_eligible_rows"] == 0


def test_complete_publication_contract_can_unlock_row_classification():
    row = {
        "symbol": "000001.SZSE", "period": "2024Q1",
        "parent_net_profit": "10", "total_assets": "100",
        "pub_date": "2024-04-30",
        "report_source": {"url": "https://example.invalid/report.pdf"},
        "source_checks": {
            "unit_explicit_as_rmb_yuan": True,
            "parent_field_explicit": True,
            "total_assets_explicit": True,
        },
        "pit": {"revision_safe_pit_verified": False},
    }
    result = classify_scoped_row(
        row,
        market_symbols={"000001.SZSE"},
        publication_evidence={"contract_evaluation": {
            "pit_asof_safe": True,
            "retrieved_asof_proven": True,
        }},
    )
    assert result["publication_sidecar_attached"] is True
    assert result["revision_safe_pit_verified"] is True
    assert result["retrieval_asof_verified"] is True
    assert result["eligible_for_f01"] is True

