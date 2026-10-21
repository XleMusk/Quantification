from copy import deepcopy
from types import SimpleNamespace

import pytest

from trading.research.distribution_references import extract
from trading.contracts import TradingMode
from trading.research.capital_allocation_replay import (
    ARMS, CASES, END, START, PROFITABILITY_CASES, PROFITABILITY_FAMILY,
    SavedRankFeed, source_layout, validate_capital_resume,
)
from trading.research.continuous_momentum import fractional_allocation_contract, check_fractional_continuation


def sse_fixture():
    action = {"symbol": "688479.SSE", "code": "sh.688479", "dividRegistDate": "2026-06-02",
              "dividOperateDate": "2026-06-03", "dividPayDate": "2026-06-03",
              "dividStockMarketDate": "2026-06-03", "dividCashPsBeforeTax": "0.26",
              "dividStocksPs": "0.000000", "dividReserveToStockPs": "0.40"}
    pages = ["\u8bc1\u5238\u4ee3\u7801\uff1a688479\n\u8bc1\u5238\u7b80\u79f0\uff1a\u53cb\u8f66\u79d1\u6280\n2025\u5e74\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a\n\u91cd\u8981\u5185\u5bb9\u63d0\u793a",
             "\u4e8c\u3001\u5206\u914d\u3001\u8f6c\u589e\u80a1\u672c\u65b9\u6848\n"
             "\u6bcf\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u52290.26\u5143\uff08\u542b\u7a0e\uff09\uff0c"
             "\u4ee5\u8d44\u672c\u516c\u79ef\u6bcf\u80a1\u8f6c\u589e0.40\u80a1\uff0c\u4e0d\u9001\u7ea2\u80a1\u3002",
             "\u9664\u6743\uff08\u606f\uff09\u53c2\u8003\u4ef7\u683c=\uff08\u524d\u6536\u76d8\u4ef7\u683c-0.257\uff09\u00f7\uff081+0.395\uff09\u5143/\u80a1\u3002\n"
             "\u4e09\u3001\u76f8\u5173\u65e5\u671f\n\u80a1\u6743\u767b\u8bb0\u65e5 \u9664\u6743\uff08\u606f\uff09\u65e5 "
             "\u65b0\u589e\u65e0\u9650\u552e\u6761\u4ef6\u6d41\u901a\u80a1\u4efd\u4e0a\u5e02\u65e5 \u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5\n"
             "2026/6/2 2026/6/3 2026/6/3 2026/6/3\n\u56db\u3001\u5b9e\u65bd\u529e\u6cd5\n2026\u5e745\u670828\u65e5"]
    return pages, action


def test_sse_literal_reference_does_not_replace_holder_entitlements():
    pages, action = sse_fixture()
    result = extract(pages, action, "2026-05-28")
    assert result["reference_adjustment_cash_ps"] == "0.257"
    assert result["reference_adjustment_share_ratio"] == "0.395"
    assert result["dividCashPsBeforeTax"] == "0.26"
    assert result["dividReserveToStockPs"] == "0.40"
    assert result["source_pages"] == [3]
    assert result["execution_admitted"] is False


@pytest.mark.parametrize("change", ["date", "cash", "missing_zero", "ambiguous_formula"])
def test_sse_conflicts_and_missing_explicit_terms_remain_unresolved(change):
    pages, action = sse_fixture()
    if change == "date":
        pages[2] = pages[2].replace("2026/6/2", "2026/6/1")
    elif change == "cash":
        action["dividCashPsBeforeTax"] = "0.27"
    elif change == "missing_zero":
        pages[1] = pages[1].replace("\u4e0d\u9001\u7ea2\u80a1", "")
    else:
        pages[2] += pages[2].split("\n")[0].replace("0.257", "0.258")
    with pytest.raises(ValueError):
        extract(pages, action, "2026-05-28")


def test_capital_resume_rejects_policy_or_source_and_incomplete_case_contract():
    current = {"source_run": "original", "start": "2025-01-02", "end": "2026-09-30",
               "sessions": 424, "symbols": 5368, "scope": "research",
               "protocol": {"signals": ["case"], "lot_allocation": "fixed"}}
    previous = {**deepcopy(current), "status": "failed_capital_allocation_diagnostics",
                "research_family": "capital_lot_fee_implementation", "results": [{"signal": "case"}]}
    previous["protocol"]["stock_manifest"] = {"event_view": "old"}
    validate_capital_resume(previous, current)
    for key in ("source_run", "policy", "missing_case", "financial_receipt", "cohort_policy", "source_rankings"):
        changed = deepcopy(previous)
        if key == "source_run":
            changed[key] = "other"
        elif key == "policy":
            changed["protocol"]["lot_allocation"] = "other"
        elif key == "missing_case":
            changed["results"] = []
        else:
            changed["protocol"][key] = "changed"
        with pytest.raises(ValueError):
            validate_capital_resume(changed, current)


def test_capital_source_keeps_original_arms_and_uses_only_fixed_lots_for_profitability():
    source = {"start": START, "end": END, "status": "completed_family_portfolio_diagnostics",
              "research_family": "fixed_family_incremental", "protocol": {"fresh_holdout_claimed": False}}
    assert source_layout(source) == (CASES, ARMS, False)
    source.update(status="completed_profitability_diagnostics", research_family=PROFITABILITY_FAMILY,
                  rankings="saved-ranks")
    source["protocol"].update(id=PROFITABILITY_FAMILY, signals=list(PROFITABILITY_CASES),
                             signal_portfolio_configs={case: {} for case in PROFITABILITY_CASES},
                             financial_receipt={"frozen": True}, cohort_policy={"quarter": "fixed"})
    assert source_layout(source) == (PROFITABILITY_CASES, ("close_lots",), True)
    for field in ("signals", "financial_receipt", "cohort_policy", "fresh_holdout_claimed"):
        changed = deepcopy(source)
        changed["protocol"][field] = True if field == "fresh_holdout_claimed" else None
        with pytest.raises(ValueError):
            source_layout(changed)


def test_pinned_cash_reference_does_not_complete_missing_share_terms():
    pages = ["\u8bc1\u5238\u4ee3\u7801\uff1a000333\u8bc1\u5238\u7b80\u79f0\uff1a\u7f8e\u7684\u96c6\u56e2\n"
             "2024\u5e74\u5ea6A\u80a1\u5229\u6da6\u5206\u914d\u5b9e\u65bd\u516c\u544a\n\u91cd\u8981\u5185\u5bb9\u63d0\u793a\n"
             "\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d135\u5143\uff08\u542b\u7a0e\uff09\uff0c\u4e0d\u4ee5\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c\u3002",
             "\u80a1\u6743\u767b\u8bb0\u65e5\u4e3a2025\u5e746\u670811\u65e5\uff0c\u9664\u606f\u65e5\u4e3a2025\u5e746\u670812\u65e5\u3002"
             "\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e2025\u5e746\u670812\u65e5\u5212\u5165\u8d44\u91d1\u8d26\u6237\u3002",
             "\u9664\u6743\u9664\u606f\u4ef7\u683c=\u80a1\u6743\u767b\u8bb0\u65e5\u6536\u76d8\u4ef7-3.4788428\u3002\n2025\u5e746\u67086\u65e5"]
    action = {"symbol": "000333.SZSE", "code": "sz.000333", "dividRegistDate": "2025-06-11",
              "dividOperateDate": "2025-06-12", "dividPayDate": "2025-06-12", "dividStockMarketDate": "",
              "dividCashPsBeforeTax": "3.5", "dividStocksPs": "0.000000", "dividReserveToStockPs": "0"}
    result = extract(pages, action, "2025-06-06")
    assert result["reference_adjustment_cash_ps"] == "3.4788428"
    assert result["dividCashPsBeforeTax"] == "3.5"
    assert result["dividStocksPs"] == "0.000000"
    for field in ("dividStocksPs", "dividReserveToStockPs"):
        blank = {**action, field: ""}
        with pytest.raises(ValueError, match="blank holder"):
            extract(pages, blank, "2025-06-06")


def test_cash_payment_literal_funds_ledger_keeps_date_and_sentence_checks():
    from trading.research.action_disclosures import explicit_cash_terms

    action = {"symbol": "002582.SZSE", "dividRegistDate": "2025-05-23",
              "dividOperateDate": "2025-05-26", "dividPayDate": "2025-05-26",
              "dividCashPsBeforeTax": "1", "dividStocksPs": "0", "dividReserveToStockPs": "0"}
    text = ("\u8bc1\u5238\u4ee3\u7801\uff1a002582\u3002"
            "\u6bcf10\u80a1\u6d3e10\u5143\uff08\u542b\u7a0e\uff09\u3002"
            "\u4e0d\u9001\u7ea2\u80a1\uff0c\u4e0d\u8fdb\u884c\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c\u3002"
            "\u80a1\u6743\u767b\u8bb0\u65e5\u4e3a2025\u5e745\u670823\u65e5\uff0c\u9664\u6743\u9664\u606f\u65e5\u4e3a2025\u5e745\u670826\u65e5\u3002"
            "\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e2025\u5e745\u670826\u65e5\u76f4\u63a5\u5212\u5165\u5176\u8d44\u91d1\u8d26\u3002")
    assert explicit_cash_terms([text], action)["dividCashPsBeforeTax"] == "1"
    for changed in (text.replace("\u76f4\u63a5\u5212\u5165\u5176", ""),
                    text[:-1] + "\u672a\u786e\u8ba4\u3002",
                    text.replace("\u5c06\u4e8e2025\u5e745\u670826", "\u5c06\u4e8e2025\u5e745\u670827")):
        with pytest.raises(ValueError, match="payment date"):
            explicit_cash_terms([changed], action)


def test_saved_rank_fractional_assumptions_are_explicit_and_replay_only():
    _, action = sse_fixture()
    context = SimpleNamespace(mode=TradingMode.REPLAY, account_id="case")
    feed = SavedRankFeed.__new__(SavedRankFeed)
    feed.fractional_allocation = None
    assert feed.ordinary_share_allocation_receipt(action, 201, context) is None
    for policy, expected in (("floor", 80), ("ceil", 81)):
        feed.fractional_allocation = fractional_allocation_contract(policy)
        receipt = feed.ordinary_share_allocation_receipt(action, 201, context)
        assert receipt["allocated_shares"] == expected
        assert receipt["source_kind"] == "explicit_replay_assumption"
        assert receipt["account_id"] == "case"
        with pytest.raises(ValueError, match="replay-only"):
            feed.ordinary_share_allocation_receipt(action, 201, SimpleNamespace(mode=TradingMode.PAPER))
    checkpoint = {"state": {"events": [], "corporate_action_reconciliations": {}}}
    case = {"error": "Unresolved fractional ordinary allocation"}
    floor = {"fractional_allocation": fractional_allocation_contract("floor")}
    check_fractional_continuation({}, floor, checkpoint, case)
    with pytest.raises(ValueError, match="Cannot change"):
        check_fractional_continuation(floor, {"fractional_allocation": fractional_allocation_contract("ceil")}, checkpoint, case)
