from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.market_dataset import write_frame, write_json
from trading.market_snapshot import RESERVE_CORRECTION_FIELDS, reviewed_plan_scope
from trading.research import action_disclosures as disclosures
from trading.research.action_disclosures import NoticeTitle, explicit_cash_terms, explicit_plan_link, notice_url, prepare, reparse


def fixture():
    action = {"symbol": "001236.SZSE", "code": "sz.001236", "dividCashPsBeforeTax": "0.03",
              "dividStocksPs": "0.000000", "dividReserveToStockPs": "", "dividRegistDate": "2023-01-10",
              "dividOperateDate": "2023-01-11", "dividPayDate": "2023-01-11"}
    text = ("\u8bc1\u5238\u4ee3\u7801\uff1a001236\u6bcf10\u80a1\u6d3e0.30\u5143\u4eba\u6c11\u5e01\u73b0\u91d1\uff08\u542b\u7a0e\uff09\u3002"
            "\u4e0d\u9001\u7ea2\u80a1\uff0c\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c\u3002"
            "\u80a1\u6743\u767b\u8bb0\u65e5\u4e3a\uff1a2023\u5e741\u670810\u65e5\uff0c\u9664\u6743\u9664\u606f\u65e5\u4e3a2023\u5e741\u670811\u65e5\u3002"
            "\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e2023\u5e741\u670811\u65e5\u901a\u8fc7\u6258\u7ba1\u673a\u6784\u76f4\u63a5\u5212\u5165\u5176\u8d44\u91d1\u8d26\u6237\u3002")
    return action, text


def test_annual_plan_section_review_does_not_claim_the_entire_report_or_change_other_terms():
    row = dict(plan_review_status="manual_distribution_sections_and_pages_reviewed",
               kind="issuer_confirmed_reserve_zero_term", plan_document_kind="issuer_annual_report",
               plan_distribution_period="2022FY", plan_implementation_period="2022FY",
               source_date="2023-05-05", plan_source_date="2023-03-30", shareholder_approval_date="2023-04-19",
               plan_source_page_count=245, expected_provider_terms={"dividStocksPs": "0.000000"},
               plan_review_scope="issuer_identity_distribution_terms_and_board_approval")
    assert reviewed_plan_scope(row, [1, 2, 47])
    for change in ({"plan_distribution_period": "2021FY"}, {"plan_implementation_period": "2021FY"},
                   {"plan_source_page_count": 46}, {"shareholder_approval_date": "2023-05-06"},
                   {"kind": "issuer_cash_only_precision_correction"}, {"plan_review_scope": "title_only"},
                   {"expected_provider_terms": {"dividStocksPs": ""}},
                   {"expected_provider_terms": {"dividStocksPs": "0.1"}}):
        assert not reviewed_plan_scope({**row, **change}, [1, 2, 47])


@pytest.mark.parametrize("seeds", [
    [{"symbol": "301016.SZSE", "ex_date": "2023-05-11", "url": "https://example.org/notice.pdf"}],
    [{"symbol": "301016.SZSE", "ex_date": "2023-05-11", "url": "https://static.cninfo.com.cn/finalpage/2023-05-11/123.PDF"}],
])
def test_direct_seed_rejects_untrusted_or_late_pdf_before_network(tmp_path, monkeypatch, seeds):
    action, _ = fixture()
    action.update(symbol="301016.SZSE", dividOperateDate="2023-05-11", dividRegistDate="2023-05-10")
    write_frame(tmp_path / "canonical_actions.parquet", pd.DataFrame([action]))
    write_json(tmp_path / "result.json", {"status": "research_action_events_projected", "execution_admitted": False,
        "files": {"canonical_actions.parquet": sha256((tmp_path / "canonical_actions.parquet").read_bytes()).hexdigest()}})
    write_json(tmp_path / "seeds.json", seeds)
    monkeypatch.setattr(disclosures, "fetch", lambda *_: pytest.fail("Invalid seed must not access network"))
    with pytest.raises(ValueError):
        disclosures.collect_direct(tmp_path, tmp_path / "seeds.json", __import__("sys").executable)


def test_explicit_terms_require_notice_and_never_mutate_blank_source():
    action, text = fixture()
    original = deepcopy(action)
    terms = explicit_cash_terms([text[:60], text[60:]], action)
    assert terms == {"dividCashPsBeforeTax": "0.03", "dividStocksPs": "0", "dividReserveToStockPs": "0"}
    assert action == original and action["dividReserveToStockPs"] == ""


def test_sse_cash_only_three_column_table_requires_exact_dates_and_explicit_zero_shares():
    action = {"symbol": "688010.SSE", "dividCashPsBeforeTax": "0.057",
              "dividStocksPs": "0.000000", "dividReserveToStockPs": "",
              "dividRegistDate": "2025-06-23", "dividOperateDate": "2025-06-24",
              "dividPayDate": "2025-06-24", "dividStockMarketDate": ""}
    clauses = ("\u8bc1\u5238\u4ee3\u7801\uff1a688010\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u52290.57\u5143\uff08\u542b\u7a0e\uff09\u3002"
               "\u4e0d\u8fdb\u884c\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c\uff0c\u4e0d\u9001\u7ea2\u80a1\u3002")
    table = ("\u80a1\u6743\u767b\u8bb0\u65e5 \u9664\u6743\uff08\u606f\uff09\u65e5 \u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5\n"
             "2025/6/23 2025/6/24 2025/6/24\n")
    assert explicit_cash_terms([clauses + table + table], action)["dividReserveToStockPs"] == "0"
    for broken in (clauses + table.replace("2025/6/23", "2025/6/22"),
                   clauses + table + table.replace("2025/6/24", "2025/6/25"),
                   clauses + table.replace("2025/6/24\n", "2025/6/25\n"),
                   clauses + table.replace("\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5", "\u65b0\u80a1\u4e0a\u5e02\u65e5"),
                   clauses.replace("\u4e0d\u8fdb\u884c\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c", "") + table):
        with pytest.raises(ValueError):
            explicit_cash_terms([broken], action)
    with pytest.raises(ValueError):
        explicit_cash_terms([clauses + table], {**action, "dividStockMarketDate": "2025-06-24"})


def test_equivalent_explicit_phrases_are_not_inferred_from_omissions():
    action, text = fixture()
    alternate = text.replace("\u8bc1\u5238\u4ee3\u7801", "\u80a1\u7968\u4ee3\u7801").replace(
        "\u4e0d\u9001\u7ea2\u80a1", "\u4e0d\u5206\u7ea2\u80a1").replace(
        "\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1", "\u4e0d\u5b9e\u65bd\u8d44\u672c\u516c\u79ef")
    assert explicit_cash_terms([alternate], action)["dividReserveToStockPs"] == "0"
    for missing in ("\u4e0d\u5206\u7ea2\u80a1", "\u4e0d\u5b9e\u65bd\u8d44\u672c\u516c\u79ef\u8f6c\u589e\u80a1\u672c"):
        with pytest.raises(ValueError, match="explicitly"):
            explicit_cash_terms([alternate.replace(missing, "")], action)


def test_literal_cash_dividend_payment_synonym_keeps_date_and_account_guards():
    action, text = fixture()
    text = text.replace("\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e", "\u73b0\u91d1\u80a1\u5229\u5c06\u4e8e")
    assert explicit_cash_terms([text], action)["dividCashPsBeforeTax"] == "0.03"
    for broken in (text.replace("\u5c06\u4e8e2023\u5e741\u670811", "\u5c06\u4e8e2023\u5e741\u670812"),
                   text.replace("\u8d44\u91d1\u8d26\u6237", "\u5176\u4ed6\u8d26\u6237"),
                   text + "\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e2023\u5e741\u670812\u65e5\u5212\u5165\u8d44\u91d1\u8d26\u6237\u3002"):
        with pytest.raises(ValueError, match="payment"):
            explicit_cash_terms([broken], action)


def test_archived_discovery_index_requires_terminal_receipt_and_unchanged_file(tmp_path):
    raw = json.dumps({"stockList": [{"code": "000513", "orgId": "gssz0000513"}]}).encode()
    (tmp_path / "index.json").write_bytes(raw)
    record = {"stock_index_file": "index.json", "stock_index_sha256": sha256(raw).hexdigest()}
    state = {"status": "batch_completed", "execution_admitted": False, "records": [record]}
    write_json(tmp_path / "result.json", state)
    saved, receipt = disclosures.archived_stock_index(tmp_path)
    assert saved == raw and receipt["url"] == disclosures.INDEX
    write_json(tmp_path / "result.json", {**state, "status": "collecting"})
    with pytest.raises(ValueError, match="completed"):
        disclosures.archived_stock_index(tmp_path)
    write_json(tmp_path / "result.json", state)
    (tmp_path / "index.json").write_bytes(raw + b" ")
    with pytest.raises(ValueError):
        disclosures.archived_stock_index(tmp_path)


def test_joint_negative_share_clause_is_explicit_not_an_absent_field_default():
    action, text = fixture()
    old = "\u4e0d\u9001\u7ea2\u80a1\uff0c\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c"
    joint = "\u4e0d\u8fdb\u884c\u8d44\u672c\u516c\u79ef\u8f6c\u589e\u80a1\u672c\u548c\u9001\u7ea2\u80a1"
    assert explicit_cash_terms([text.replace(old, joint)], action)["dividReserveToStockPs"] == "0"
    with pytest.raises(ValueError, match="explicitly"):
        explicit_cash_terms([text.replace(old, joint.replace("\u4e0d", ""))], action)
    with pytest.raises(ValueError, match="positive share-plan"):
        explicit_cash_terms([text.replace(old, joint) + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1\u3002"], action)


def test_reversed_joint_negative_and_cash_distribution_wording_require_explicit_terms():
    action, text = fixture()
    old = "\u4e0d\u9001\u7ea2\u80a1\uff0c\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c"
    joint = "\u4e0d\u5b9e\u65bd\u9001\u80a1\u548c\u8d44\u672c\u516c\u79ef\u8f6c\u589e\u80a1\u672c"
    alternate = text.replace(old, joint).replace("\u6d3e0.30", "\u6d3e\u9001\u73b0\u91d1\u80a1\u52290.30")
    assert explicit_cash_terms([alternate], action)["dividCashPsBeforeTax"] == "0.03"
    for broken in (alternate.replace(joint, joint.replace("\u4e0d", "")),
                   alternate.replace(joint, "\u4e0d\u5b9e\u65bd\u9001\u80a1"),
                   alternate.replace("\u542b\u7a0e", "\u6263\u7a0e\u540e")):
        with pytest.raises(ValueError):
            explicit_cash_terms([broken], action)
    with pytest.raises(ValueError, match="positive share-plan"):
        explicit_cash_terms([alternate + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1"], action)


def test_explicit_zero_bonus_is_equivalent_to_no_bonus_not_a_blank_default():
    action, text = fixture()
    zero = text.replace("\u4e0d\u9001\u7ea2\u80a1", "\u6bcf10\u80a1\u90010\u80a1")
    assert explicit_cash_terms([zero], action)["dividStocksPs"] == "0"
    assert explicit_cash_terms([zero.replace("\u90010\u80a1", "\u90010.0000\u80a1")], action)["dividStocksPs"] == "0"
    for value in ("\u90010.1\u80a1", "\u90010.0001\u80a1", "\u9001\u80a1"):
        with pytest.raises(ValueError, match="explicitly"):
            explicit_cash_terms([zero.replace("\u90010\u80a1", value)], action)
    with pytest.raises(ValueError, match="positive share-plan"):
        explicit_cash_terms([zero + "\u6bcf10\u80a1\u90011\u80a1"], action)


def test_plain_no_bonus_shares_still_requires_explicit_reserve_and_no_positive_plan():
    action, text = fixture()
    text = text.replace("\u4e0d\u9001\u7ea2\u80a1", "\u4e0d\u9001\u80a1")
    assert explicit_cash_terms([text], action)["dividStocksPs"] == "0"
    for broken in (text.replace("\u4e0d\u9001\u80a1", ""),
                   text.replace("\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c", ""),
                   text + "\u6bcf10\u80a1\u90011\u80a1"):
        with pytest.raises(ValueError):
            explicit_cash_terms([broken], action)


def test_explicit_no_transfer_wording_requires_bonus_negative_and_no_positive_plan():
    action, text = fixture()
    original = "\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c"
    clause = "\u4e0d\u8fdb\u884c\u8f6c\u589e\u80a1\u672c"
    alternate = text.replace(original, clause)
    assert explicit_cash_terms([alternate], action)["dividReserveToStockPs"] == "0"
    for broken in (alternate.replace(clause, ""), alternate.replace(clause, clause[1:]),
                   alternate.replace("\u4e0d\u9001\u7ea2\u80a1", ""), alternate + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1"):
        with pytest.raises(ValueError):
            explicit_cash_terms([broken], action)


@pytest.mark.parametrize("plain", ["\u4e0d\u8f6c\u589e\u80a1\u672c", "\u4e0d\u8f6c\u589e", "\u4e0d\u8fdb\u884c\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e"])
def test_plain_no_share_capital_transfer_requires_bonus_and_matching_cash_dates(plain):
    action, text = fixture()
    old = "\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c"
    text = text.replace(old, plain)
    assert explicit_cash_terms([text], action)["dividReserveToStockPs"] == "0"
    for broken in (text.replace(plain, ""), text.replace(plain, plain[1:]),
                   text.replace("\u4e0d\u9001\u7ea2\u80a1", ""),
                   text + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1", text.replace("0.30", "0.31"),
                   text.replace("1\u670810\u65e5", "1\u67089\u65e5")):
        with pytest.raises(ValueError):
            explicit_cash_terms([broken], action)


def plan_pair():
    action = {"symbol": "600971.SSE", "dividCashPsBeforeTax": "0.85", "dividStocksPs": "0.000000",
              "dividReserveToStockPs": "", "dividRegistDate": "2024-05-23",
              "dividOperateDate": "2024-05-24", "dividPayDate": "2024-05-24"}
    final = ("\u8bc1\u5238\u4ee3\u7801\uff1a600971\u3002 2023\u5e74\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a\u3002"
             "\u672c\u6b21\u5229\u6da6\u5206\u914d\u65b9\u6848\u7ecf\u516c\u53f82024\u5e744\u670826\u65e5\u76842023\u5e74\u5e74\u5ea6\u80a1\u4e1c\u5927\u4f1a\u5ba1\u8bae\u901a\u8fc7\u3002"
             "\u603b\u80a1\u672c1,200,004,884\u80a1\uff0c\u6bcf\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u52290.85\u5143\uff08\u542b\u7a0e\uff09\u3002"
             "\u80a1\u4efd\u7c7b\u522b\u80a1\u6743\u767b\u8bb0\u65e5\u6700\u540e\u4ea4\u6613\u65e5\u9664\u6743\uff08\u606f\uff09\u65e5\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5"
             "\uff21\u80a12024/5/23\uff0d2024/5/242024/5/24")
    plan = ("\u8bc1\u5238\u4ee3\u7801\uff1a600971\u3002 2023\u5e74\u5ea6\u5229\u6da6\u5206\u914d\u9884\u6848\u3002"
            "\u603b\u80a1\u672c1,200,004,884\u80a1\uff0c\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u5229\u4eba\u6c11\u5e018.5\u5143\uff08\u542b\u7a0e\uff09\u3002"
            "\u4e0d\u9001\u7ea2\u80a1\uff0c\u4e0d\u8f6c\u589e\u3002"
            "2024\u5e743\u670828\u65e5\uff0c\u516c\u53f8\u53ec\u5f00\u8463\u4e8b\u4f1a\uff0c\u5ba1\u8bae\u901a\u8fc7\u4e86\u8be5\u9884\u6848\u3002")
    return action, final, plan


def test_companion_link_requires_approved_unchanged_annual_final_not_proposal_alone():
    action, final, plan = plan_pair()
    before = deepcopy(action)
    pair = explicit_plan_link([final], [plan], action, "2024-05-17", "2024-03-30")
    assert pair["status"] == "machine_linked_explicit_cash_terms_candidate"
    assert pair["explicit_terms"]["dividReserveToStockPs"] == "0"
    assert pair["plan_approval_date"] == "2024-03-28" and pair["shareholder_approval_date"] == "2024-04-26"
    assert action == before and action["dividReserveToStockPs"] == ""
    for changed_final, changed_plan in ((final.replace("\u5ba1\u8bae\u901a\u8fc7", "\u5f85\u5ba1\u8bae"), plan),
            (final, plan.replace("600971", "600972")), (final, plan.replace("2023\u5e74\u5ea6", "2022\u5e74\u5ea6")),
            (final, plan.replace("1,200,004,884", "1,200,004,885")),
            (final, plan.replace("8.5\u5143", "8.6\u5143")),
            (final, plan.replace("\u4e0d\u8f6c\u589e\u3002", "")),
            (final, plan + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1\u3002")):
        with pytest.raises(ValueError):
            explicit_plan_link([changed_final], [changed_plan], action, "2024-05-17", "2024-03-30")
    with pytest.raises(ValueError, match="publication/approval"):
        explicit_plan_link([final], [plan], action, "2024-05-17", "2024-04-27")


def test_highlighted_metadata_title_is_parsed_as_text_not_ignored():
    title = NoticeTitle("\u5229\u6da6<em>\u5206\u914d</em>\u9884\u6848&amp;\u516c\u544a").text
    assert title == "\u5229\u6da6\u5206\u914d\u9884\u6848&\u516c\u544a"


def test_interim_profit_distribution_a_share_title_is_not_a_proposal_or_h_share():
    title = "\u4e2d\u56fd\u79fb\u52a8\uff1a2025\u5e74\u4e2d\u671f\u5229\u6da6\u5206\u6d3eA\u80a1\u5b9e\u65bd\u516c\u544a"
    assert disclosures.is_implementation_title(title)
    assert disclosures.is_implementation_title(title.replace("A\u80a1", "(A \u80a1)"))
    assert not disclosures.is_implementation_title(title.replace("A\u80a1", "H\u80a1"))
    assert not disclosures.is_implementation_title(title.replace("\u5b9e\u65bd", "\u9884\u6848"))


@pytest.mark.parametrize("label", ["\u9664\u6743\u65e5\uff08\u9664\u606f\u65e5\uff09", "\u9664\u6743\u65e5(\u9664\u606f\u65e5)"])
def test_parenthetical_ex_date_label_requires_unique_matching_date(label):
    action, text = fixture()
    alternate = text.replace("\u9664\u6743\u9664\u606f\u65e5", label)
    assert explicit_cash_terms([alternate], action)["dividReserveToStockPs"] == "0"
    with pytest.raises(ValueError, match="record/ex date"):
        explicit_cash_terms([alternate.replace("2023\u5e741\u670811\u65e5", "2023\u5e741\u670812\u65e5", 1)], action)
    with pytest.raises(ValueError, match="record/ex date"):
        explicit_cash_terms([alternate + "\u9664\u6743\u9664\u606f\u65e5\u4e3a2023\u5e741\u670812\u65e5"], action)


def test_sse_per_share_cash_and_ordered_date_table_keep_virtual_cash_separate():
    action, text = fixture()
    action["dividCashPsBeforeTax"] = "0.145"
    table = ("\u80a1\u4efd\u7c7b\u522b \u80a1\u6743\u767b\u8bb0\u65e5 \u6700\u540e\u4ea4\u6613\u65e5 \u9664\u6743\uff08\u606f\uff09\u65e5 \u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5 "
             "\uff21\u80a1 2023/1/10 \uff0d 2023/1/11 2023/1/11")
    share_terms = "\u4e0d\u9001\u7ea2\u80a1\uff0c\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c"
    cash = "\u6bcf\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u52290.145\u5143\uff08\u542b\u7a0e\uff09"
    text = "\u8bc1\u5238\u4ee3\u7801\uff1a001236" + share_terms + cash + table + table
    text += "\u9664\u606f\u53c2\u8003\u4ef7=\u524d\u6536\u76d8\u4ef7-0.1444\u5143"
    assert explicit_cash_terms([text], action)["dividCashPsBeforeTax"] == "0.145"
    for broken in (text + table.replace("2023/1/11", "2023/1/12"),
                   text.replace("\u6700\u540e\u4ea4\u6613\u65e5", "\u5176\u4ed6\u65e5\u671f"),
                   text.replace("\u542b\u7a0e", "\u6263\u7a0e\u540e"),
                   text + cash.replace("0.145", "0.1444")):
        with pytest.raises(ValueError):
            explicit_cash_terms([broken], action)


@pytest.mark.parametrize("old,new", [
    ("001236", "001237"),
    ("\u4e0d\u9001\u7ea2\u80a1", ""),
    ("\u4e0d\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c", ""),
    ("0.30", "0.31"),
    ("1\u670810\u65e5", "1\u67089\u65e5"),
    ("\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e", "\u8ba1\u5212\u5b9e\u65bd\u4e8e"),
])
def test_missing_or_conflicting_explicit_evidence_is_not_zero(old, new):
    action, text = fixture()
    with pytest.raises(ValueError):
        explicit_cash_terms([text.replace(old, new)], action)


@pytest.mark.parametrize("field,value", [("dividCashPsBeforeTax", ""), ("dividReserveToStockPs", "0.1"), ("dividStocksPs", "0.1")])
def test_notice_cannot_overwrite_nonzero_source_shares_or_unknown_cash(field, value):
    action, text = fixture()
    with pytest.raises(ValueError):
        explicit_cash_terms([text], {**action, field: value})


def test_image_only_pages_multiple_gross_amounts_and_share_clauses_need_review():
    action, text = fixture()
    for pages in ([text, ""], [text + text.replace("0.30", "0.40")],
                  [text + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1\u3002"]):
        with pytest.raises(ValueError):
            explicit_cash_terms(pages, action)


@pytest.mark.parametrize("header", ["\u4e8c\u3001\u672c\u6b21\u5b9e\u65bd\u7684\u6743\u76ca\u5206\u6d3e\u65b9\u6848", "\u4e8c\u3001\u6743\u76ca\u5206\u6d3e\u65b9\u6848"])
def test_current_implementation_excludes_annual_and_paid_instalments(header):
    action, text = fixture()
    cash = "\u6bcf10\u80a1\u6d3e0.30\u5143\u4eba\u6c11\u5e01\u73b0\u91d1\uff08\u542b\u7a0e\uff09"
    history = ("\u4e00\u3001\u80a1\u4e1c\u5927\u4f1a\u5ba1\u8bae\u901a\u8fc7\u7684\u65b9\u6848"
               "\u5168\u5e74" + cash.replace("0.30", "0.50") + "\u3002"
               "\u4e2d\u671f\u5df2\u6d3e" + cash.replace("0.30", "0.20") + "\u3002")
    scoped = text.replace(cash, history + header + cash + "\u4e09\u3001\u76f8\u5173\u65e5\u671f")
    assert explicit_cash_terms([scoped], action)["dividCashPsBeforeTax"] == "0.03"
    with pytest.raises(ValueError, match="Unique explicit gross cash"):
        explicit_cash_terms([scoped.replace(header + cash, header + cash.replace("0.30", "0.31"))], action)
    with pytest.raises(ValueError, match="Unique explicit gross cash"):
        explicit_cash_terms([scoped.replace(header + cash, header + cash + cash.replace("0.30", "0.40"))], action)
    with pytest.raises(ValueError, match="Multiple implementation-plan"):
        explicit_cash_terms([scoped + header + cash], action)


def test_implementation_gross_cash_allows_comma_before_explicit_tax_label():
    action, text = fixture()
    header = "\u4e8c\u3001\u672c\u6b21\u5b9e\u65bd\u7684\u6743\u76ca\u5206\u6d3e\u65b9\u6848"
    scoped = header + text.replace("\u5143\u4eba\u6c11\u5e01\u73b0\u91d1\uff08", "\u5143\uff0c\uff08")
    assert explicit_cash_terms([scoped], action)["dividCashPsBeforeTax"] == "0.03"
    with pytest.raises(ValueError, match="Unique explicit gross cash"):
        explicit_cash_terms([scoped.replace("\u542b\u7a0e", "\u6263\u7a0e\u540e")], action)


@pytest.mark.parametrize("url", ["../secret.pdf", "https://example.test/a.pdf", "finalpage/2023-01-04/x.pdf", "finalpage/2023-01-04/12.PDF?token=x"])
def test_notice_routes_do_not_follow_arbitrary_metadata_urls(url):
    with pytest.raises(ValueError):
        notice_url(url)


def test_prepare_keeps_all_blank_event_targets_not_a_stock_budget(tmp_path):
    view = tmp_path / "view"
    view.mkdir()
    action, _ = fixture()
    rows = [action, {**action, "symbol": "002027.SZSE", "code": "sz.002027", "dividReserveToStockPs": "0"},
            {**action, "symbol": "300700.SZSE", "code": "sz.300700", "dividCashPsBeforeTax": "", "dividReserveToStockPs": "0.8"}]
    write_frame(view / "canonical_actions.parquet", pd.DataFrame(rows))
    digest = sha256((view / "canonical_actions.parquet").read_bytes()).hexdigest()
    write_json(view / "result.json", {"status": "research_action_events_projected", "start": "2023-01-01", "end": "2026-09-30",
                                     "files": {"canonical_actions.parquet": digest}})
    folder = prepare(view, tmp_path / "out")
    plan = json.loads((folder / "plan.json").read_text())
    assert plan["source_events"] == 3 and plan["target_events"] == 2
    assert set(pd.read_parquet(folder / "targets.parquet").symbol) == {"001236.SZSE", "300700.SZSE"}
    assert sha256((view / "canonical_actions.parquet").read_bytes()).hexdigest() == digest
    assert plan["automatic_promotion"] is False and plan["execution_admitted"] is False
    complete_key = ("002027.SZSE", action["dividOperateDate"])
    explicit = prepare(view, tmp_path / "explicit", priority_events=[complete_key])
    targets = pd.read_parquet(explicit / "targets.parquet")
    assert len(targets) == 3
    assert targets.loc[targets.symbol.eq(complete_key[0]), "dividReserveToStockPs"].item() == "0"
    assert json.loads((explicit / "plan.json").read_text())["explicit_priority_events"] == [list(complete_key)]
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="one pinned canonical event"):
        prepare(view, missing, priority_events=[("999999.SZSE", action["dividOperateDate"])])
    assert not missing.exists()
    with pytest.raises(ValueError, match="Unique explicit"):
        prepare(view, tmp_path / "duplicate", priority_events=[complete_key] * 2)


@pytest.mark.parametrize("tamper", [False, True])
def test_reparse_retains_original_classification_and_checks_archived_text(tmp_path, tamper):
    action, text = fixture()
    root = tmp_path / "root"
    root.mkdir()
    write_frame(root / "input_actions.parquet", pd.DataFrame([action]))
    write_frame(root / "targets.parquet", pd.DataFrame([action]))
    (root / "implementation.py").write_bytes(b"old parser")
    write_json(root / "text.json", {"pages": [text]})
    digest = lambda name: sha256((root / name).read_bytes()).hexdigest()
    write_json(root / "plan.json", {"input_actions_sha256": digest("input_actions.parquet"),
        "targets_sha256": digest("targets.parquet"), "implementation_sha256": digest("implementation.py"), "target_events": 1})
    write_json(root / "result.json", {"records": [{"symbol": action["symbol"], "date": action["dividOperateDate"],
        "status": "needs_review", "reason": "old phrase", "text_file": "text.json", "text_sha256": digest("text.json"), "extracted_pages": 1}]})
    before = digest("result.json")
    if tamper:
        write_json(root / "text.json", {"pages": [text + "changed"]})
        with pytest.raises(ValueError, match="hash"):
            reparse(root)
    else:
        folder, result = reparse(root)
        row = result["records"][0]
        assert row["original_status"] == "needs_review" and row["original_reason"] == "old phrase"
        assert result["explicit_candidates"] == 1 and result["automatic_promotion"] is False
        assert sha256((folder / "text.json").read_bytes()).hexdigest() == digest("text.json")
    assert digest("result.json") == before


@pytest.mark.parametrize("changed", [False, True])
def test_preparation_reuses_only_exact_unchanged_unresolved_events(tmp_path, changed):
    action, _ = fixture()
    action = {**{field: "" for field in RESERVE_CORRECTION_FIELDS}, **action}
    view = tmp_path / "view"
    view.mkdir()
    write_frame(view / "canonical_actions.parquet", pd.DataFrame([action]))
    digest = lambda path: sha256(path.read_bytes()).hexdigest()
    write_json(view / "result.json", {"status": "research_action_events_projected", "start": "2023-01-01", "end": "2026-09-30",
        "files": {"canonical_actions.parquet": digest(view / "canonical_actions.parquet")}})
    old = prepare(view, tmp_path / "old")
    (old / "notice.pdf").write_bytes(b"%PDF-original")
    expected = {field: action[field] for field in RESERVE_CORRECTION_FIELDS}
    if changed:
        expected["dividCashPsBeforeTax"] = "0.04"
    write_json(old / "result.json", {"status": "batch_completed", "records": [
        {"symbol": action["symbol"], "date": action["dividOperateDate"], "expected_provider_terms": expected,
         "status": "needs_review", "reason": "retained negative", "source_file": "notice.pdf", "source_sha256": digest(old / "notice.pdf")},
        {"symbol": "other already-completed event", "date": "2023-01-11"}]})
    before = digest(old / "result.json")
    if changed:
        with pytest.raises(ValueError, match="source terms changed"):
            prepare(view, tmp_path / "new", old)
    else:
        new = prepare(view, tmp_path / "new", old)
        result = json.loads((new / "result.json").read_text(encoding="utf-8"))
        assert len(result["records"]) == 1 and result["records"][0]["reason"] == "retained negative"
        assert digest(new / "notice.pdf") == digest(old / "notice.pdf")
    assert digest(old / "result.json") == before


@pytest.mark.parametrize("with_plans", [False, True])
@pytest.mark.parametrize("share_label", ["", "\uff08A\u80a1\uff09"])
def test_actual_collector_path_links_only_opted_in_companion_and_reuses_it(tmp_path, monkeypatch, with_plans, share_label):
    action, final, companion = plan_pair()
    action = {**{field: "" for field in RESERVE_CORRECTION_FIELDS}, **action,
              "code": "sh.600971", "dividPlanDate": "2024-05-17"}
    view = tmp_path / "view"
    view.mkdir()
    write_frame(view / "canonical_actions.parquet", pd.DataFrame([action]))
    write_json(view / "result.json", {"status": "research_action_events_projected",
        "start": "2023-01-01", "end": "2026-09-30", "files": {
            "canonical_actions.parquet": sha256((view / "canonical_actions.parquet").read_bytes()).hexdigest()}})
    root = prepare(view, tmp_path / "root")
    calls = []

    def fetch(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == disclosures.INDEX:
            return json.dumps({"stockList": [{"code": "600971", "orgId": "test-org"}]}).encode()
        if method == "POST":
            query = kwargs["data"]
            is_plan = bool(query["searchkey"])
            if is_plan:
                assert query["seDate"] == "2024-01-01~2024-05-17"
                title, route = "2023\u5e74\u5ea6<em>\u5229\u6da6\u5206\u914d</em>\u9884\u6848\u7684\u516c\u544a", "finalpage/2024-03-30/12.PDF"
            else:
                title, route = "2023\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e" + share_label + "\u5b9e\u65bd\u516c\u544a", "finalpage/2024-05-17/13.PDF"
            return json.dumps({"hasMore": False, "announcements": [
                {"secCode": "600971", "announcementTitle": title, "adjunctUrl": route}]}).encode()
        return b"%PDF-synthetic-test"

    monkeypatch.setattr(disclosures, "fetch", fetch)
    monkeypatch.setattr(disclosures.subprocess, "run", lambda args, **kwargs: SimpleNamespace(
        stdout=json.dumps({"pages": [companion if args[-1].endswith("plan_notice.pdf") else final]})))
    state = disclosures.collect(root, disclosures.Path(__file__), 1, with_plans=with_plans)
    assert state["status"] == "batch_completed" and state["automatic_promotion"] is False
    row = state["records"][0]
    assert action["dividReserveToStockPs"] == ""
    if with_plans:
        assert state["explicit_candidates"] == 1
        assert row["status"] == "machine_linked_explicit_cash_terms_candidate"
        assert row["plan_source_pages"] == [1] and row["explicit_terms"]["dividReserveToStockPs"] == "0"
        linked, parsed = reparse(root)
        assert parsed["explicit_candidates"] == 1 and parsed["records"][0]["status"] == row["status"]
        reused = prepare(view, tmp_path / "reuse", root)
        for field in ("plan_metadata_file", "plan_source_file", "plan_text_file"):
            assert (reused / row[field]).is_file() and (linked / row[field]).is_file()
    else:
        assert state["explicit_candidates"] == 0 and row["status"] == "needs_review"
        assert not any(call[2].get("data", {}).get("searchkey") for call in calls)
        result_before = (root / "result.json").read_bytes()
        linked, result = disclosures.link_plans(root, disclosures.Path(__file__), 1, acquire=True)
        assert result["explicit_candidates"] == 1 and result["network_acquisition_enabled"] is True
        assert (root / "result.json").read_bytes() == result_before
        assert (linked / result["records"][0]["plan_source_file"]).is_file()


@pytest.mark.parametrize("share_label", ["\uff08H\u80a1\uff09", "(H\u80a1)"])
def test_implementation_title_does_not_admit_h_share_distribution(share_label):
    title = "2024\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e" + share_label + "\u5b9e\u65bd\u516c\u544a"
    assert not disclosures.is_implementation_title(title)
    assert not disclosures.is_implementation_title("H \u80a1\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a")
    assert not disclosures.is_implementation_title("h\u80a1\u5206\u7ea2\u6d3e\u606f\u5b9e\u65bd\u516c\u544a")


@pytest.mark.parametrize("fault", ["ambiguous", "incomplete", "late", "other_issuer", "html_pdf"])
def test_auto_companion_does_not_select_revisions_or_accept_incomplete_sources(tmp_path, monkeypatch, fault):
    action, final, companion = plan_pair()
    folder = tmp_path / "event"
    folder.mkdir()
    notice = {"secCode": "600971", "announcementTitle": "2023\u5e74\u5ea6\u5229\u6da6\u5206\u914d\u9884\u6848\u7684\u516c\u544a",
              "adjunctUrl": "finalpage/2024-03-30/12.PDF"}
    if fault == "late":
        notice["adjunctUrl"] = "finalpage/2024-05-18/12.PDF"
    if fault == "other_issuer":
        notice["secCode"] = "600018"
    page = {"hasMore": fault == "incomplete", "announcements": [notice, notice] if fault == "ambiguous" else [notice]}
    monkeypatch.setattr(disclosures, "fetch", lambda session, method, url, **kwargs:
        json.dumps(page).encode() if method == "POST" else b"<html>not a PDF</html>")
    entry = {"source_date": "2024-05-17"}
    with pytest.raises(ValueError):
        disclosures.acquire_companion(None, disclosures.Path(__file__), tmp_path, folder, action,
                                      [final], entry, "test-org")
    assert "explicit_terms" not in entry and "status" not in entry
    assert (folder / "plan_metadata.json").is_file() and action["dividReserveToStockPs"] == ""


def test_companion_scheme_titles_do_not_hide_distribution_revisions_or_treat_convertible_prices_as_plans():
    original = {"secCode": "600018", "announcementTitle": "2023\u5e74\u5e74\u5ea6<em>\u5229\u6da6\u5206\u914d</em>\u65b9\u6848\u516c\u544a",
                "adjunctUrl": "finalpage/2024-03-30/12.PDF"}
    convertible = {**original, "announcementTitle": "\u5173\u4e8e\u6839\u636e2023\u5e74\u5ea6\u5229\u6da6\u5206\u914d\u65b9\u6848\u8c03\u6574\u53ef\u8f6c\u6362\u516c\u53f8\u503a\u5238\u8f6c\u80a1\u4ef7\u683c\u7684\u516c\u544a"}
    page = {"hasMore": False, "announcements": [original, convertible]}
    assert disclosures.companion_notice(page, "600018") == original
    for title in ("\u5173\u4e8e\u8c03\u65742023\u5e74\u5ea6\u5229\u6da6\u5206\u914d\u65b9\u6848\u7684\u63d0\u793a\u6027\u516c\u544a",
                  "2023\u5e74\u5ea6\u5229\u6da6\u5206\u914d\u65b9\u6848\u66f4\u6b63\u516c\u544a"):
        with pytest.raises(ValueError, match="revision"):
            disclosures.companion_notice({**page, "announcements": [original, {**original, "announcementTitle": title}]}, "600018")
    with pytest.raises(ValueError, match="Unique"):
        disclosures.companion_notice({**page, "announcements": [original, original]}, "600018")


def test_companion_period_scopes_interim_without_using_annual_approval_references():
    header = "2026\u5e74\u534a\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a\n\u672c\u516c\u53f8\u8463\u4e8b\u4f1a\u4fdd\u8bc1\u3002"
    final = header + "2025\u5e74\u5ea6\u5229\u6da6\u5206\u914d\u65b9\u6848\u5df2\u83b7\u6279\u3002"
    interim = {"secCode": "000423", "announcementTitle": "\u5173\u4e8e2026\u5e74\u4e2d\u671f<em>\u5229\u6da6\u5206\u914d</em>\u65b9\u6848\u7684\u516c\u544a"}
    annual = {"secCode": "000423", "announcementTitle": "\u5173\u4e8e2025\u5e74\u5ea6\u5229\u6da6\u5206\u914d\u9884\u6848\u7684\u516c\u544a"}
    period = disclosures.final_distribution_period([final])
    assert period == ("2026", "interim")
    page = {"hasMore": False, "announcements": [annual, interim]}
    assert disclosures.companion_notice(page, "000423", period) == interim
    with pytest.raises(ValueError, match="Unique companion"):
        disclosures.companion_notice(page, "000423")
    with pytest.raises(ValueError, match="same unique annual"):
        action, _, annual_plan = plan_pair()
        issuer_header = "\u8bc1\u5238\u4ee3\u7801\uff1a" + action["symbol"].split(".")[0] + "\u3002" + header
        explicit_plan_link([issuer_header], [annual_plan], action, "2026-08-25", "2026-08-21")


def test_companion_period_still_rejects_matching_revisions_and_unknown_periods():
    original = {"secCode": "000423", "announcementTitle": "\u5173\u4e8e2026\u5e74\u4e2d\u671f\u5229\u6da6\u5206\u914d\u65b9\u6848\u7684\u516c\u544a"}
    for title, reason in (
            ("2026\u5e74\u4e2d\u671f\u5229\u6da6\u5206\u914d\u65b9\u6848\u66f4\u6b63\u516c\u544a", "revision"),
            ("\u5229\u6da6\u5206\u914d\u65b9\u6848\u66f4\u6b63\u516c\u544a", "period")):
        page = {"hasMore": False, "announcements": [original, {**original, "announcementTitle": title}]}
        with pytest.raises(ValueError, match=reason):
            disclosures.companion_notice(page, "000423", ("2026", "interim"))
    with pytest.raises(ValueError, match="Unique companion"):
        disclosures.companion_notice({"hasMore": False, "announcements": [original, original]}, "000423", ("2026", "interim"))
    with pytest.raises(ValueError, match="incomplete"):
        disclosures.companion_notice({"hasMore": True, "announcements": [original]}, "000423", ("2026", "interim"))


def test_cash_only_ex_dividend_label_still_requires_matching_unique_dates_and_no_share_plan():
    action, text = fixture()
    text = text.replace("\u9664\u6743\u9664\u606f\u65e5", "\u9664\u606f\u65e5")
    assert explicit_cash_terms([text], action)["dividReserveToStockPs"] == "0"
    for broken in (text.replace("1\u670811\u65e5", "1\u670812\u65e5", 1),
                   text + "\u9664\u606f\u65e5\u4e3a2023\u5e741\u670812\u65e5",
                   text + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1"):
        with pytest.raises(ValueError):
            explicit_cash_terms([broken], action)
