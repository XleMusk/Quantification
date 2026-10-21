from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.market_dataset import write_json
from trading.market_snapshot import RESERVE_CORRECTION_FIELDS
from trading.research.distribution_references import extract, priorities, publish


def fixture():
    action = {"symbol": "301310.SZSE", "code": "sz.301310", "dividRegistDate": "2026-05-18",
              "dividOperateDate": "2026-05-19", "dividPayDate": "2026-05-19", "dividStockMarketDate": "2026-05-19",
              "dividCashPsBeforeTax": "0.25", "dividStocksPs": "0.000000", "dividReserveToStockPs": "0.400000"}
    text = ("\u8bc1\u5238\u4ee3\u7801\uff1a301310\u8bc1\u5238\u7b80\u79f0\uff1a\u946b\u5b8f\u4e1a\n2025\u5e74\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a\n"
            "\u4e8c\u3001\u672c\u6b21\u5b9e\u65bd\u7684\u6743\u76ca\u5206\u6d3e\u65b9\u6848"
            "\u6bcf10\u80a1\u6d3e2.500000\u5143\u4eba\u6c11\u5e01\u73b0\u91d1\uff08\u542b\u7a0e\uff09\u3002"
            "\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u6bcf10\u80a1\u8f6c\u589e4.000000\u80a1\u3002"
            "\u4e09\u3001\u80a1\u6743\u767b\u8bb0\u65e5\u4e3a\uff1a2026\u5e745\u670818\u65e5\uff0c"
            "\u9664\u6743\u9664\u606f\u65e5\u4e3a\uff1a2026\u5e745\u670819\u65e5\u3002"
            "\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e2026\u5e745\u670819\u65e5\u5212\u5165\u8d44\u91d1\u8d26\u6237\u3002"
            "\u65e0\u9650\u552e\u6761\u4ef6\u6d41\u901a\u80a1\u7684\u8d77\u59cb\u4ea4\u6613\u65e5\u4e3a2026\u5e745\u670819\u65e5\u3002"
            "\u9664\u6743\u9664\u606f\u4ef7\u683c=\uff08\u9664\u6743\u9664\u606f\u65e5\u524d\u4e00\u4ea4\u6613\u65e5\u6536\u76d8\u4ef7-0.2490049\u5143/\u80a1\uff09/\uff081+0.3984079\uff09\u3002"
            "\u8463\u4e8b\u4f1a2026\u5e745\u670812\u65e5")
    return action, text


def test_literal_formula_is_separate_from_unchanged_holder_entitlements():
    action, text = fixture()
    before = deepcopy(action)
    row = extract([text], action, "2026-05-13")
    assert row["reference_adjustment_cash_ps"] == "0.2490049"
    assert row["reference_adjustment_share_ratio"] == "0.3984079"
    assert row["dividCashPsBeforeTax"] == "0.25" and row["dividReserveToStockPs"] == "0.400000"
    assert row["source_pages"] == [1] and not row["execution_admitted"]
    assert action == before and "provider zero" in row["holder_bonus_basis"]


@pytest.mark.parametrize("old,new", [
    ("301310", "301311"), ("2.500000", "2.600000"), ("4.000000", "3.000000"),
    ("5\u670818", "5\u670817"), ("5\u670819", "5\u670820"), ("0.3984079", "0.4984079"),
    ("\u9664\u6743\u9664\u606f\u4ef7\u683c=", "\u6700\u4f4e\u51cf\u6301\u4ef7\u683c="),
])
def test_conflicting_identity_terms_dates_or_unlabeled_formula_are_not_accepted(old, new):
    action, text = fixture()
    with pytest.raises(ValueError):
        extract([text.replace(old, new)], action, "2026-05-13")


def test_duplicate_equal_formulas_are_allowed_but_different_literals_never_selected():
    action, text = fixture()
    repeated = text[text.index("\u9664\u6743\u9664\u606f\u4ef7\u683c="):]
    row = extract([text, repeated], action, "2026-05-13")
    assert row["source_pages"] == [1, 2]
    with pytest.raises(ValueError, match="Unique explicitly"):
        extract([text, repeated.replace("0.2490049", "0.2490050")], action, "2026-05-13")


def test_blank_bonus_or_postrecord_source_remains_unresolved():
    action, text = fixture()
    with pytest.raises(ValueError, match="blank holder"):
        extract([text], {**action, "dividStocksPs": ""}, "2026-05-13")
    with pytest.raises(ValueError, match="chronology"):
        extract([text], action, "2026-05-19")


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("separator", ["\u548c", "\u53ca"])
def test_szse_profit_and_reserve_heading_keeps_exact_holder_cash(conflict, separator):
    action, text = fixture()
    text = text.replace("\u672c\u6b21\u5b9e\u65bd\u7684\u6743\u76ca\u5206\u6d3e\u65b9\u6848",
                        "\u672c\u6b21\u5b9e\u65bd\u7684\u5229\u6da6\u5206\u914d\u548c\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c\u65b9\u6848")
    text = text.replace("\u5229\u6da6\u5206\u914d\u548c", "\u5229\u6da6\u5206\u914d" + separator)
    text = text.replace("\u6bcf10\u80a1\u6d3e2.500000\u5143\u4eba\u6c11\u5e01\u73b0\u91d1",
                        "\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u5229" + ("2.600000" if conflict else "2.500000") + "\u5143")
    if conflict:
        with pytest.raises(ValueError, match="holder cash"):
            extract([text], action, "2026-05-13")
    else:
        row = extract([text], action, "2026-05-13")
        assert row["reference_adjustment_share_ratio"] == "0.3984079"
        assert row["dividCashPsBeforeTax"] == "0.25"


@pytest.mark.parametrize("change", [None, "cash", "reserve", "date", "blank_bonus", "positive_bonus"])
def test_sse_per_ten_reference_keeps_pinned_holder_terms_and_literal_dates(change):
    action = {"symbol": "603067.SSE", "code": "sh.603067", "dividRegistDate": "2025-06-23",
              "dividOperateDate": "2025-06-24", "dividPayDate": "2025-06-24", "dividStockMarketDate": "2025-06-24",
              "dividCashPsBeforeTax": "0.19", "dividStocksPs": "0.000000", "dividReserveToStockPs": "0.400000"}
    text = ("\u8bc1\u5238\u4ee3\u7801\uff1a603067\u8bc1\u5238\u7b80\u79f0\uff1a\u632f\u534e\u80a1\u4efd\n2024\u5e74\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a\n"
            "\u4e8c\u3001\u5206\u914d\u3001\u8f6c\u589e\u80a1\u672c\u65b9\u6848"
            "\u5411\u5168\u4f53\u80a1\u4e1c\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u52291.90\u5143\uff08\u542b\u7a0e\uff09\uff0c"
            "\u540c\u65f6\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u5411\u5168\u4f53\u80a1\u4e1c\u6bcf10\u80a1\u8f6c\u589e4\u80a1\u3002"
            "\u9664\u6743\uff08\u606f\uff09\u53c2\u8003\u4ef7\u683c=\uff08\u524d\u6536\u76d8\u4ef7\u683c-0.19)\u00f7(1+0.3963)\u3002"
            "\u4e09\u3001\u76f8\u5173\u65e5\u671f\u80a1\u4efd\u7c7b\u522b\u80a1\u6743\u767b\u8bb0\u65e5\u6700\u540e\u4ea4\u6613\u65e5"
            "\u9664\u6743\uff08\u606f\uff09\u65e5\u65b0\u589e\u65e0\u9650\u552e\u6761\u4ef6\u6d41\u901a\u80a1\u4efd\u4e0a\u5e02\u65e5"
            "\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5\uff21\u80a12025/6/23\uff0d2025/6/242025/6/242025/6/24\u3002"
            "\u8463\u4e8b\u4f1a2025\u5e746\u670818\u65e5")
    if change in {"cash", "reserve", "date"}:
        old, new = {"cash": ("1.90", "1.91"), "reserve": ("\u8f6c\u589e4", "\u8f6c\u589e5"),
                    "date": ("2025/6/23", "2025/6/22")}[change]
        text = text.replace(old, new)
    elif change == "blank_bonus":
        action["dividStocksPs"] = ""
    elif change == "positive_bonus":
        text = text.replace("\u4e09\u3001", "\u6bcf10\u80a1\u9001\u7ea2\u80a11\u80a1\u3002\u4e09\u3001")
    if change is None:
        row = extract([text], action, "2025-06-18")
        assert row["reference_adjustment_cash_ps"] == "0.19"
        assert row["reference_adjustment_share_ratio"] == "0.3963"
        assert row["dividReserveToStockPs"] == "0.400000" and row["dividStocksPs"] == "0.000000"
    else:
        with pytest.raises(ValueError):
            extract([text], action, "2025-06-18")


def test_sse_differential_notice_accepts_top_level_each_share_transfer_ratio():
    action = {"symbol": "603629.SSE", "code": "sh.603629", "dividRegistDate": "2026-07-06",
              "dividOperateDate": "2026-07-07", "dividPayDate": "2026-07-07",
              "dividStockMarketDate": "2026-07-07", "dividCashPsBeforeTax": "0.37",
              "dividStocksPs": "0.000000", "dividReserveToStockPs": "0.400000"}
    text = ("证券代码：603629 证券简称：利通电子 2025年年度权益分派实施公告\n"
            "重要内容提示：A股每股现金红利0.37元 每股转增股份0.4股\n"
            "股份类别 股权登记日 最后交易日 除权（息）日 新增无限售条件流通股份上市日 现金红利发放日"
            "Ａ股 2026/7/6 － 2026/7/7 2026/7/7 2026/7/7\n"
            "二、分配、转增股本方案\n"
            "每股派发现金红利0.37元（含税）。\n"
            "除权（息）参考价格=(前收盘价格-0.3682)/(1+0.3980)元/股\n"
            "三、相关日期\n董事会2026年7月1日")
    result = extract([text], action, "2026-07-01")
    assert result["reference_adjustment_cash_ps"] == "0.3682"
    assert result["reference_adjustment_share_ratio"] == "0.3980"
    assert result["issuer_confirmed_reference"] and not result["execution_admitted"]


def test_data_priority_ignores_empty_historical_position_keys(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    case = artifacts / "reversal"
    case.mkdir()
    (case / "account.json").write_text(json.dumps({"day": "2026-05-18", "positions": {
        "301310.SZSE": [{"qty": 100}], "301233.SZSE": []}}), encoding="utf-8")
    pd.DataFrame([{"date": "2026-05-15", "symbol": "000001.SZSE", "rank": 1}]).to_csv(
        artifacts / "source_target_ranks.csv", index=False)
    references = tmp_path / "references"
    references.mkdir()
    pd.DataFrame([{"symbol": symbol, "date": "2026-05-19", "dividRegistDate": "2026-05-18",
                   "reference_formula_status": "reference_mismatch"}
                  for symbol in ("301310.SZSE", "301233.SZSE", "000001.SZSE")]).to_parquet(
        references / "raw_reference_changes.parquet", index=False)
    (tmp_path / "run.json").write_text(json.dumps({"research_family": "weekly_reversal",
        "status": "failed_weekly_reversal_diagnostic", "artifacts": str(artifacts),
        "protocol": {"signals": ["reversal"]}, "inputs": {"reference_view": str(references)}}), encoding="utf-8")
    _, plan = priorities(tmp_path)
    assert plan["events"] == [("000001.SZSE", "2026-05-19"), ("301310.SZSE", "2026-05-19")]


def cash_fixture():
    action = {"symbol": "601318.SSE", "code": "sh.601318", "dividRegistDate": "2025-06-27",
              "dividOperateDate": "2025-06-30", "dividPayDate": "2025-06-30", "dividStockMarketDate": "",
              "dividCashPsBeforeTax": "1.62", "dividStocksPs": "0", "dividReserveToStockPs": "0"}
    text = ("\u8bc1\u5238\u4ee3\u7801\uff1a601318\u8bc1\u5238\u7b80\u79f0\uff1a\u4e2d\u56fd\u5e73\u5b89\n2024\u5e74\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a\n"
            "\u91cd\u8981\u5185\u5bb9\u63d0\u793a\uff1a\n"
            "\u6bcf\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u5229\u4eba\u6c11\u5e011.62\u5143\uff08\u542b\u7a0e\uff09\u3002"
            "H\u80a1\u80a1\u4e1c\u7684\u7ea2\u5229\u64cd\u4f5c\u4e0d\u9002\u7528\u672c\u516c\u544a\u3002"
            "\u80a1\u4efd\u7c7b\u522b\u80a1\u6743\u767b\u8bb0\u65e5\u6700\u540e\u4ea4\u6613\u65e5"
            "\u9664\u6743\uff08\u606f\uff09\u65e5\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5"
            "\uff21\u80a12025/6/27\uff0d2025/6/302025/6/30\u3002"
            "\u672c\u516c\u53f8\u672c\u6b21\u4ec5\u8fdb\u884c\u73b0\u91d1\u7ea2\u5229\u5206\u914d\uff0c"
            "\u65e0\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d\u3002"
            "\u672c\u6b21A\u80a1\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c=\u524d\u6536\u76d8\u4ef7\u683c-1.6046\u5143\u3002"
            "\u8463\u4e8b\u4f1a2025\u5e746\u670820\u65e5")
    return action, text


def test_literal_cash_only_reference_and_ashare_notice_with_hshare_disclaimer():
    action, text = cash_fixture()
    result = extract([text], action, "2025-06-20")
    assert result["reference_adjustment_cash_ps"] == "1.6046"
    assert result["reference_adjustment_share_ratio"] == "0"
    assert result["dividCashPsBeforeTax"] == "1.62" and result["issuer_confirmed_reference"]


@pytest.mark.parametrize("change", [None, "unlabeled", "conflict"])
def test_evaluated_cash_reference_accepts_literal_joined_yuan_share_unit(change):
    action, text = cash_fixture()
    label = "\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7"
    formula = label + "=\u9664\u6743\u9664\u606f\u65e5\u524d\u4e00\u4ea4\u6613\u65e5\u6536\u76d8\u4ef7-1.6046\u5143\u80a1\u3002"
    text = text.replace("\u672c\u6b21A\u80a1\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c=\u524d\u6536\u76d8\u4ef7\u683c-1.6046\u5143\u3002", formula)
    if change == "unlabeled":
        text = text.replace(label, "\u56de\u8d2d\u4ef7")
    elif change == "conflict":
        text += formula.replace("1.6046", "1.6045")
    if change is None:
        result = extract([text], action, "2025-06-20")
        assert result["reference_adjustment_cash_ps"] == "1.6046"
        assert result["dividCashPsBeforeTax"] == "1.62"
    else:
        with pytest.raises(ValueError, match="Unique explicitly"):
            extract([text], action, "2025-06-20")


def test_numbered_cash_only_sentence_and_fullwidth_plus_preserve_literal_reference():
    action, text = cash_fixture()
    text = text.replace("\u672c\u516c\u53f8\u672c\u6b21", "\n2 \u516c\u53f8\u672c\u6b21")
    text = text.replace("\u672c\u6b21A\u80a1\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c=\u524d\u6536\u76d8\u4ef7\u683c-1.6046\u5143\u3002",
                        "\u9664\u6743(\u606f)\u53c2\u8003\u4ef7\u683c=(\u524d\u6536\u76d8\u4ef7\u683c-1.6046)/(1\uff0b0)\u3002")
    assert extract([text], action, "2025-06-20")["reference_adjustment_cash_ps"] == "1.6046"
    with pytest.raises(ValueError):
        extract([text.replace("\u65e0\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d", "\u8be6\u89c1\u65b9\u6848")], action, "2025-06-20")


@pytest.mark.parametrize("change", [None, "unlabeled", "conflict"])
@pytest.mark.parametrize("close_label", ["\u6536\u76d8\u4ef7", "\u6536\u76d8\u4ef7\u683c"])
def test_parenthesized_cash_reference_needs_its_label_and_unique_literal(change, close_label):
    action, text = cash_fixture()
    text = text.replace("\u672c\u6b21A\u80a1\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c=\u524d\u6536\u76d8\u4ef7\u683c-1.6046\u5143\u3002",
                        "\u9664\u6743(\u606f)\u53c2\u8003\u4ef7=(\u80a1\u6743\u767b\u8bb0\u65e5\u6536\u76d8\u4ef7-1.6046)\u5143/\u80a1\u3002")
    if change == "unlabeled":
        text = text.replace("\u9664\u6743(\u606f)\u53c2\u8003\u4ef7", "\u56de\u8d2d\u4ef7")
    elif change == "conflict":
        text += "\u9664\u6743(\u606f)\u53c2\u8003\u4ef7=(\u80a1\u6743\u767b\u8bb0\u65e5\u6536\u76d8\u4ef7-1.60)\u5143/\u80a1\u3002"
    text = text.replace("\u6536\u76d8\u4ef7", close_label)
    if change is None:
        assert extract([text], action, "2025-06-20")["reference_adjustment_cash_ps"] == "1.6046"
    else:
        with pytest.raises(ValueError):
            extract([text], action, "2025-06-20")


@pytest.mark.parametrize("change", [None, "missing_zero", "cash", "date", "shares", "blank_bonus", "unlabeled", "conflict"])
def test_sse_cash_divisor_formula_requires_explicit_zero_terms_and_exact_holder(change):
    action, text = cash_fixture()
    text = text.replace("\u65e0\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d", "\u65e0\u914d\u80a1\u3001\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d")
    text = text.replace(
        "\u672c\u6b21A\u80a1\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c=\u524d\u6536\u76d8\u4ef7\u683c-1.6046\u5143\u3002",
        "\u672c\u6b21\u9664\u6743\uff08\u606f\uff09\u53c2\u8003\u4ef7\u683c=\uff08\u524d\u6536\u76d8\u4ef7\u683c-1.6046\uff09\u00f7\uff081+0\uff09\u3002")
    text = text.replace("2025\u5e746\u670820\u65e5", "2025-06-20")
    if change == "missing_zero":
        text = text.replace("\u65e0\u914d\u80a1\u3001\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d", "\u8be6\u89c1\u5206\u914d\u65b9\u6848")
    elif change == "cash":
        text = text.replace("1.62", "1.63")
    elif change == "date":
        text = text.replace("2025/6/27", "2025/6/26")
    elif change == "shares":
        text = text.replace("1+0", "1+0.1")
    elif change == "blank_bonus":
        action["dividStocksPs"] = ""
    elif change == "unlabeled":
        text = text.replace("\u9664\u6743\uff08\u606f\uff09\u53c2\u8003\u4ef7\u683c", "\u51cf\u6301\u4ef7\u683c")
    elif change == "conflict":
        text += "\u9664\u6743(\u606f)\u53c2\u8003\u4ef7\u683c=(\u524d\u6536\u76d8\u4ef7\u683c-1.60)/(1+0)\u3002"
    before = deepcopy(action)
    if change is None:
        row = extract([text], action, "2025-06-20")
        assert row["reference_adjustment_cash_ps"] == "1.6046"
        assert row["reference_adjustment_share_ratio"] == "0"
        assert row["dividCashPsBeforeTax"] == "1.62" and action == before
    else:
        with pytest.raises(ValueError):
            extract([text], action, "2025-06-20")


def test_special_notice_heading_separator_never_admits_hshare_title():
    action, text = cash_fixture()
    text = text.replace("\u91cd\u8981\u5185\u5bb9\u63d0\u793a", "\u7279\u522b\u63d0\u793a")
    assert extract([text], action, "2025-06-20")["reference_adjustment_cash_ps"] == "1.6046"
    text = text.replace("\u5b9e\u65bd\u516c\u544a", "\uff08H\u80a1\uff09\u5b9e\u65bd\u516c\u544a", 1)
    with pytest.raises(ValueError, match="title"):
        extract([text], action, "2025-06-20")


@pytest.mark.parametrize("change", ["missing_zero_clause", "hshare_title", "conflicting_literal", "cash_conflict"])
def test_cash_reference_keeps_explicit_terms_title_and_unique_literal_guards(change):
    action, text = cash_fixture()
    if change == "missing_zero_clause":
        text = text.replace("\u65e0\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d", "\u8be6\u89c1\u5176\u4ed6\u516c\u544a")
    elif change == "hshare_title":
        text = text.replace("\u5b9e\u65bd\u516c\u544a", "\uff08H\u80a1\uff09\u5b9e\u65bd\u516c\u544a", 1)
    elif change == "conflicting_literal":
        text += "\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c=\u524d\u6536\u76d8\u4ef7\u683c-1.60\u5143\u3002"
    else:
        text = text.replace("1.62", "1.63")
    with pytest.raises(ValueError):
        extract([text], action, "2025-06-20")


@pytest.mark.parametrize("change", [None, "no_label", "different_literal", "holder_conflict", "blank_reserve"])
def test_evaluated_cash_chain_remains_literal_and_bound_to_holder_terms(change):
    action, text = cash_fixture()
    old = "\u672c\u6b21A\u80a1\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c=\u524d\u6536\u76d8\u4ef7\u683c-1.6046\u5143\u3002"
    chain = ("\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7=\u9664\u6743\u9664\u606f\u65e5\u524d\u4e00\u4ea4\u6613\u65e5\u6536\u76d8\u4ef7-\u6309\u603b\u80a1\u672c\u6298\u7b97\u7684\u73b0\u91d1\u7ea2\u5229="
             "\u9664\u6743\u9664\u606f\u65e5\u524d\u4e00\u4ea4\u6613\u65e5\u6536\u76d8\u4ef7-1.6046\u5143/\u80a1\u3002")
    text = text.replace(old, chain)
    if change == "no_label":
        text = text.replace("\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7=", "\u8f6c\u80a1\u4ef7\u683c=")
    elif change == "different_literal":
        text += chain.replace("1.6046", "1.60")
    elif change == "holder_conflict":
        text = text.replace("1.6046", "1.63")
    elif change == "blank_reserve":
        action["dividReserveToStockPs"] = ""
    if change is None:
        row = extract([text], action, "2025-06-20")
        assert row["reference_adjustment_cash_ps"] == "1.6046"
        assert row["reference_adjustment_share_ratio"] == "0"
    else:
        with pytest.raises(ValueError):
            extract([text], action, "2025-06-20")


@pytest.mark.parametrize("direct,szse", [(False, False), (True, False), (True, True)])
def test_publisher_reuses_exact_review_without_rebinding_archived_provider_facts(tmp_path, monkeypatch, direct, szse):
    import trading.research.distribution_references as module
    action, text = cash_fixture()
    action.update(dividCashPsAfterTax="", dividCashStock="10\u6d3e16.2\u5143", dividReserveToStockPs="")
    source, review = tmp_path / "disclosures", tmp_path / "review"
    source.mkdir()
    review.mkdir()
    pd.DataFrame([action]).to_parquet(source / "input_actions.parquet", index=False)
    raw = b"%PDF-fixture only"
    (source / "notice.pdf").write_bytes(raw)
    write_json(source / "text.json", {"pages": [text]})
    row = {**action, "kind": "issuer_confirmed_cash_only_terms", "dividStocksPs": "0", "dividReserveToStockPs": "0",
           "expected_provider_terms": {f: action[f] for f in RESERVE_CORRECTION_FIELDS},
           "review_status": "manual_fulltext_and_pages_reviewed", "source_date": "2025-06-20",
           "source_url": "https://static.cninfo.com.cn/finalpage/2025-06-20/123.PDF",
           "source_file": "review.pdf", "source_sha256": sha256(raw).hexdigest()}
    if szse:
        row["source_url"] = "https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-06-20/95e7a514-5b1b-4176-bf3b-b79e9eb67b60.PDF"
    (review / "review.pdf").write_bytes(raw)
    write_json(review / "supplements/provider_action_resolutions.json", [row])
    entry = {"symbol": action["symbol"], "date": action["dividOperateDate"],
             **{f: row[f] for f in ("expected_provider_terms", "source_date", "source_url", "source_sha256")},
             "source_file": "notice.pdf", "text_file": "text.json",
             "text_sha256": sha256((source / "text.json").read_bytes()).hexdigest()}
    write_json(source / "plan.json", {"execution_admitted": False,
        "explicit_priority_events": [[action["symbol"], action["dividOperateDate"]]],
        "input_actions_sha256": sha256((source / "input_actions.parquet").read_bytes()).hexdigest()})
    state = {"status": "direct_notice_batch_completed" if direct else "batch_completed",
             "records": [entry], "execution_admitted": False, "unattempted_events": 0}
    write_json(source / "result.json", state)
    monkeypatch.setattr(module, "DATA_ROOT", tmp_path / "data")
    _, unchanged = publish(source)
    assert unchanged["accepted"] == 0
    folder, result = publish(source, reviewed_root=review)
    assert result["accepted"] == 1
    accepted = json.loads((folder / "supplements/distribution_references.json").read_text())[0]
    assert accepted["dividReserveToStockPs"] == "0" and accepted["holder_review_file"]
    assert (folder / "holder_reviews/601318.SSE_2025-06-30.pdf").read_bytes() == raw
    assert pd.read_parquet(source / "input_actions.parquet").iloc[0].dividReserveToStockPs == ""
    if direct:
        write_json(source / "result.json", {**state, "unattempted_events": 1})
        with pytest.raises(ValueError, match="Completed unadmitted"):
            publish(source, reviewed_root=review)
        write_json(source / "result.json", state)
    row["expected_provider_terms"]["dividCashPsAfterTax"] = "changed original provider field"
    write_json(review / "supplements/provider_action_resolutions.json", [row])
    with pytest.raises(ValueError, match="source terms changed"):
        publish(source, reviewed_root=review)


def test_official_pdf_routes_reject_mirrors_queries_and_bad_dates():
    from trading.market_snapshot import official_distribution_notice_date
    url = "https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-05-21/95e7a514-5b1b-4176-bf3b-b79e9eb67b60.PDF"
    assert official_distribution_notice_date(url) == "2026-05-21"
    for invalid in (url + "?redirect=1", url + "#page=1", url.replace("https://", "http://"),
                    url.replace("disc.static.szse.cn", "szse.cn.example.com"),
                    url.replace("2026-05-21", "2026-02-30"), url.replace("95e7a514", "invalid")):
        with pytest.raises(ValueError):
            official_distribution_notice_date(invalid)
