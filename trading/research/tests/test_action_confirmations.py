from copy import deepcopy

import pandas as pd
import pytest

from trading.research.action_confirmations import KIND, LIMITATION, extract, load_confirmations
from trading.research.action_reviewed_amendment import corrected_actions


def fixture():
    action = {"symbol": "603056.SSE", "code": "sh.603056", "dividRegistDate": "2025-06-10",
              "dividOperateDate": "2025-06-11", "dividPayDate": "2025-06-11", "dividStockMarketDate": "",
              "dividCashPsBeforeTax": "0.144", "dividCashPsAfterTax": "0.1296", "dividStocksPs": "0.000000",
              "dividReserveToStockPs": "", "dividCashStock": "10\u6d3e1.44\u5143"}
    final = ("\u8bc1\u5238\u4ee3\u7801\uff1a603056\u516c\u544a\u7f16\u53f7\uff1a2025-028\u3002\n"
             "2024\u5e74\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a\n"
             "\u6bcf\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u52290.144\u5143\uff08\u542b\u7a0e\uff09\u3002"
             "\u80a1\u4efd\u7c7b\u522b\u80a1\u6743\u767b\u8bb0\u65e5\u6700\u540e\u4ea4\u6613\u65e5"
             "\u9664\u6743\uff08\u606f\uff09\u65e5\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5"
             "\uff21\u80a12025/6/10\uff0d2025/6/112025/6/11\n\u8463\u4e8b\u4f1a2025\u5e746\u67085\u65e5")
    confirmation = ("\u8bc1\u5238\u4ee3\u7801\uff1a603056\n"
                    "\u5b9e\u65bd2024\u5e74\u5ea6\u6743\u76ca\u5206\u6d3e\u540e\u8c03\u6574\u80a1\u4efd\u56de\u8d2d\u4ef7\u683c\u4e0a\u9650\n"
                    "\u516c\u544a\u7f16\u53f7\uff1a2025-028\u3002"
                    "\u6bcf\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u52290.144\u5143\uff08\u542b\u7a0e\uff09\u3002\n"
                    "\u516c\u53f8\u672c\u6b21\u4ec5\u8fdb\u884c\u73b0\u91d1\u7ea2\u5229\u5206\u914d\uff0c"
                    "\u65e0\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d\uff0c\u6d41\u901a\u80a1\u53d8\u52a8\u4e3a0\u3002\n"
                    "\u8463\u4e8b\u4f1a2025\u5e746\u670812\u65e5")
    return action, final, confirmation


def test_actual_event_zero_is_labeled_postevent_not_fabricated_pre_event_knowledge():
    action, final, confirmation = fixture()
    original = deepcopy(action)
    row = extract([final], [confirmation], action, "2025-06-05", "2025-06-12")
    assert row["kind"] == KIND and row["confirmation_known_after_event"] is True
    assert row["source_date"] == "2025-06-12" and row["basis"] == LIMITATION
    assert row["dividReserveToStockPs"] == "0" and row["execution_admitted"] is False
    assert original == action and action["dividReserveToStockPs"] == ""


@pytest.mark.parametrize("old,new", [
    ("603056", "600056"), ("2025-028", "2025-027"), ("0.144", "0.145"),
    ("2024\u5e74\u5ea6", "2023\u5e74\u5ea6"),
    ("\u516c\u53f8\u672c\u6b21", "\u5982\u679c\u516c\u53f8\u672c\u6b21"),
    ("\u65e0\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d", "\u65e0\u9001\u80a1"),
])
def test_confirmation_mismatch_omission_and_hypothetical_never_supply_zero(old, new):
    action, final, confirmation = fixture()
    with pytest.raises(ValueError):
        extract([final], [confirmation.replace(old, new)], action, "2025-06-05", "2025-06-12")


def test_positive_share_terms_or_wrong_chronology_remain_unresolved():
    action, final, confirmation = fixture()
    for changed in ({**action, "dividStocksPs": "0.1"}, {**action, "dividStockMarketDate": "2025-06-11"}):
        with pytest.raises(ValueError):
            extract([final], [confirmation], changed, "2025-06-05", "2025-06-12")
    with pytest.raises(ValueError, match="POST-event"):
        extract([final], [confirmation.replace("6\u670812", "6\u670811")], action, "2025-06-05", "2025-06-11")
    with pytest.raises(ValueError, match="positive share-plan"):
        extract([final], [confirmation + "\u6bcf10\u80a1\u8f6c\u589e1\u80a1"], action, "2025-06-05", "2025-06-12")


def test_targeted_amendment_preserves_all_known_and_unselected_terms():
    action, final, confirmation = fixture()
    terms = extract([final], [confirmation], action, "2025-06-05", "2025-06-12")
    untouched = {**action, "symbol": "600000.SSE", "code": "sh.600000"}
    actions = pd.DataFrame([untouched, action], index=[19, 9258])
    corrected, audit = corrected_actions(actions, [terms], "frozen.parquet")
    expected = actions.copy()
    expected.at[9258, "dividReserveToStockPs"] = "0"
    pd.testing.assert_frame_equal(corrected, expected)
    assert audit[0]["status"] == KIND and audit[0]["source_date"] == "2025-06-12"
    altered = actions.copy()
    altered.at[9258, "dividCashPsBeforeTax"] = "0.145"
    with pytest.raises(ValueError, match="source terms changed"):
        corrected_actions(altered, [terms], "frozen.parquet")


def test_unresolved_confirmation_cannot_be_published(tmp_path):
    (tmp_path / "result.json").write_text('{"status":"unresolved_postevent_confirmation","execution_admitted":false}', encoding="utf-8")
    with pytest.raises(ValueError, match="replay-only"):
        load_confirmations(tmp_path, {"603056.SSE"})
