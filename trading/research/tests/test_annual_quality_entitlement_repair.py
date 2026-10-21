"""The one late-discovered cash entitlement does not replay other economics."""
from copy import deepcopy
import io
import math

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.ledger import new_state
from trading.research import annual_quality_entitlement_repair as repair


D, E, A = "2026-10-15", "2026-10-16", "603605.SSE"
STAMP = "2026-10-18T18:35:21+08:00"


def fixture():
    config = StrategyConfig().validate()
    state = new_state(config.initial_cash)
    state.update(day=D, positions={A: [{"qty": 100, "available": "2026-10-12"}]},
        prices={A: 5679}, entitlements={"other:2026-10-16": 200},
        receivables={"older:2026-10-14": 1234}, pending_targets={A: .16})
    checkpoint = {"state": state, "daily": [{"date": D, "equity": 105691.34}],
        "config": config.to_dict(), "source": "synthetic_saved_record_close",
        "rights_replay_policy": None}
    action = {"symbol": A, "dividRegistDate": D, "dividOperateDate": E,
        "dividPayDate": E, "dividStockMarketDate": "", "dividCashPsBeforeTax": "1.18",
        "dividStocksPs": "0", "dividReserveToStockPs": "0"}
    return checkpoint, config, action


def test_adds_real_100_share_record_only_and_keeps_all_inputs_unchanged():
    checkpoint, config, action = fixture()
    old, old_action = deepcopy(checkpoint), deepcopy(action)
    fixed, audit = repair.repair_record_close(checkpoint, config, action, reviewed_at=STAMP)
    expected = deepcopy(old)
    expected["state"]["entitlements"][f"{A}:{E}"] = 100
    assert fixed == expected and checkpoint == old and action == old_action
    assert fixed is not checkpoint and fixed["state"] is not checkpoint["state"]
    assert audit["registered_quantity"] == 100
    assert audit["knowledge_mode"] == "posthoc_component_diagnostic"
    assert audit["session_advanced"] is False and audit["historically_timely_signal"] is False
    assert audit["execution_admitted"] is False


def test_existing_key_even_zero_is_not_replaced():
    checkpoint, config, action = fixture()
    checkpoint["state"]["entitlements"][f"{A}:{E}"] = 0
    with pytest.raises(ValueError, match="Existing entitlement"):
        repair.repair_record_close(checkpoint, config, action, reviewed_at=STAMP)


@pytest.mark.parametrize("part", ["state", "daily"])
def test_non_record_close_is_rejected(part):
    checkpoint, config, action = fixture()
    if part == "state":
        checkpoint["state"]["day"] = E
    else:
        checkpoint["daily"][-1]["date"] = "2026-10-14"
    with pytest.raises(ValueError, match="exact action record close"):
        repair.repair_record_close(checkpoint, config, action, reviewed_at=STAMP)


@pytest.mark.parametrize("field,value", [("dividStocksPs", "0.1"),
    ("dividReserveToStockPs", "0.1"), ("dividCashPsBeforeTax", ""),
    ("dividCashPsBeforeTax", "NaN"), ("dividCashPsBeforeTax", True)])
def test_non_cash_or_unknown_terms_are_rejected_without_input_mutation(field, value):
    checkpoint, config, action = fixture()
    action[field] = value
    old, old_action = deepcopy(checkpoint), deepcopy(action)
    with pytest.raises(ValueError, match="cash|numeric"):
        repair.repair_record_close(checkpoint, config, action, reviewed_at=STAMP)
    assert checkpoint == old and action == old_action


@pytest.mark.parametrize("stamp", ["2026-10-18T18:35:21", "2026-10-15T14:59:59+08:00"])
def test_review_clock_needs_timezone_and_cannot_be_backfilled(stamp):
    checkpoint, config, action = fixture()
    with pytest.raises(ValueError, match="review clock"):
        repair.repair_record_close(checkpoint, config, action, reviewed_at=stamp)


@pytest.mark.parametrize("case", ["paper", "broker", "rights", "later_event"])
def test_bound_or_non_replay_or_later_consumed_state_is_rejected(case):
    checkpoint, config, action = fixture()
    if case == "paper":
        checkpoint["context"] = RunContext("paper", TradingMode.PAPER)
    elif case == "broker":
        checkpoint["state"]["broker_binding"] = {"account": "not_allowed"}
    elif case == "rights":
        checkpoint["state"]["rights_replay_policy"] = {"id": "not_none"}
    else:
        checkpoint["state"]["events"].append({"day": E, "kind": "dividend_paid"})
    with pytest.raises(ValueError, match="REPLAY|rights|later consumed"):
        repair.repair_record_close(checkpoint, config, action, reviewed_at=STAMP)


def test_engine_side_effect_outside_added_key_is_rejected_and_original_remains(monkeypatch):
    checkpoint, config, action = fixture()
    old = deepcopy(checkpoint)
    original = repair.corporate_actions
    def wrong_engine(book, feed, day, at_close=False):
        original(book, feed, day, at_close=at_close)
        book.state["cash"] += 1
    monkeypatch.setattr(repair, "corporate_actions", wrong_engine)
    with pytest.raises(ValueError, match="beyond its one new entitlement"):
        repair.repair_record_close(checkpoint, config, action, reviewed_at=STAMP)
    assert checkpoint == old


def test_csv_loader_nan_diagnostics_remain_nan_in_copied_and_original_daily():
    checkpoint, config, action = fixture()
    checkpoint["daily"] = pd.read_csv(io.StringIO(
        "date,equity,cash,event_time_frontier\n2026-10-15,105691.34,100000.00,\n"
    ), dtype={"date": str}).to_dict("records")
    before = deepcopy(checkpoint)
    fixed, audit = repair.repair_record_close(checkpoint, config, action, reviewed_at=STAMP)
    assert math.isnan(checkpoint["daily"][0]["event_time_frontier"])
    assert math.isnan(fixed["daily"][0]["event_time_frontier"])
    assert fixed["daily"] is not checkpoint["daily"]
    pd.testing.assert_frame_equal(pd.DataFrame(checkpoint["daily"]), pd.DataFrame(before["daily"]))
    pd.testing.assert_frame_equal(pd.DataFrame(fixed["daily"]), pd.DataFrame(before["daily"]))
    assert fixed["state"]["entitlements"][f"{A}:{E}"] == 100
    assert checkpoint["state"] == before["state"] and audit["daily_unchanged"] is True
