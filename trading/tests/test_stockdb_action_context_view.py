import pandas as pd
import pytest

from trading.research.stockdb_action_context_view import build_context


def test_action_context_binds_same_date_and_keeps_pit_false():
    scores = pd.DataFrame([{"date": "2026-01-30", "symbol": "000001.SZSE", "score": 0.3},
                           {"date": "2026-01-30", "symbol": "600000.SSE", "score": 0.2}])
    events = pd.DataFrame([
        {"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30",
         "dividPlanAnnounceDate": "2026-01-10", "dividRegistDate": "2026-01-29",
         "dividCashPsBeforeTax": "0.1", "dividStocksPs": "0", "dividReserveToStockPs": "0"},
    ])
    result = build_context(scores, events)
    row = result.loc[result.symbol.eq("000001.SZSE")].iloc[0]
    assert bool(row.action_event_present)
    assert bool(row.publication_known_by_date)
    assert bool(row.terms_complete)
    assert not bool(row.pit_asof_safe)
    assert not bool(row.company_action_applied)
    assert not bool(result.loc[result.symbol.eq("600000.SSE"), "action_event_present"].iloc[0])


def test_missing_publication_or_terms_is_not_ready():
    scores = pd.DataFrame([{"date": "2026-01-30", "symbol": "000001.SZSE", "score": 0.3}])
    events = pd.DataFrame([
        {"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30",
         "dividPlanAnnounceDate": "", "dividRegistDate": "2026-01-29",
         "dividCashPsBeforeTax": "", "dividStocksPs": "0", "dividReserveToStockPs": "0"},
    ])
    result = build_context(scores, events)
    assert not bool(result.iloc[0].action_context_ready_without_pit)


def test_duplicate_score_identity_is_rejected():
    scores = pd.DataFrame([{"date": "2026-01-30", "symbol": "000001.SZSE", "score": 0.3},
                           {"date": "2026-01-30", "symbol": "000001.SZSE", "score": 0.2}])
    with pytest.raises(ValueError, match="unique"):
        build_context(scores, pd.DataFrame(columns=["symbol", "dividOperateDate", "dividPlanAnnounceDate",
                                                    "dividRegistDate", "dividCashPsBeforeTax",
                                                    "dividStocksPs", "dividReserveToStockPs"]))


def test_complete_publication_receipt_can_mark_only_matching_action_pit_safe():
    scores = pd.DataFrame([{"date": "2026-01-30", "symbol": "000001.SZSE", "score": 0.3}])
    events = pd.DataFrame([{
        "symbol": "000001.SZSE", "dividOperateDate": "2026-01-30",
        "dividPlanAnnounceDate": "2026-01-10", "dividRegistDate": "2026-01-29",
        "dividCashPsBeforeTax": "0.1", "dividStocksPs": "0", "dividReserveToStockPs": "0",
    }])
    evidence = pd.DataFrame([{
        "date": "2026-01-30", "symbol": "000001.SZSE", "publication_date": "2026-01-29",
        "retrieved_at": "2026-01-30T14:00:00+08:00", "asof_at": "2026-01-30T14:01:00+08:00",
        "revision_id": "rev-1", "source_file": "notice-1.pdf", "source_sha256": "a" * 64,
    }])
    result = build_context(scores, events, evidence)
    row = result.iloc[0]
    assert bool(row.publication_event_proven)
    assert bool(row.retrieved_asof_proven)
    assert bool(row.revision_identity_proven)
    assert bool(row.source_provenance_proven)
    assert bool(row.pit_asof_safe)
    assert not bool(row.company_action_applied)


def test_same_day_date_only_publication_remains_unknown_for_decision():
    scores = pd.DataFrame([{"date": "2026-01-30", "symbol": "000001.SZSE", "score": 0.3}])
    events = pd.DataFrame([{
        "symbol": "000001.SZSE", "dividOperateDate": "2026-01-30",
        "dividPlanAnnounceDate": "2026-01-10", "dividRegistDate": "2026-01-29",
        "dividCashPsBeforeTax": "0.1", "dividStocksPs": "0", "dividReserveToStockPs": "0",
    }])
    evidence = pd.DataFrame([{
        "date": "2026-01-30", "symbol": "000001.SZSE", "publication_date": "2026-01-30",
        "retrieved_at": "2026-01-30T14:00:00+08:00", "asof_at": "2026-01-30T14:01:00+08:00",
        "revision_id": "rev-1", "source_file": "notice-1.pdf", "source_sha256": "a" * 64,
    }])
    result = build_context(scores, events, evidence)
    assert not bool(result.iloc[0].pit_asof_safe)
    assert "publication_not_proven_before_decision" in result.iloc[0].publication_evidence_reasons
