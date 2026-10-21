from copy import deepcopy

import pytest

from trading.research.leader_case_structure_observations import observe_case
from trading.research.leader_monthly_engine_cases import CASE_SCHEMA


SESSIONS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
            "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16", "2026-01-19"]


def card():
    slots = [{"offset_sessions": i - 5, "day": day, "status": "source_native_row_available",
              "raw": {"open": "10", "high": "10.5", "low": "9.8", "close": "10", "volume": 1000}}
             for i, day in enumerate(SESSIONS)]
    slots[5]["raw"]["low"] = "9.5"
    return {"schema": CASE_SCHEMA, "case_id": "fixed-first-buy", "family": "trend_leader_20_60",
            "phase": "mixed_breadth_snapshot", "signal_day": SESSIONS[5], "symbol": "000001.SZSE",
            "order_id": "selected-buy", "original_order_annotation": {"original_order": {"day": SESSIONS[6]}},
            "original_execution_diagnostics": [{"mechanical_reason": "internal_reweight", "engine_target_quantity": 1300}],
            "daily_slots": slots, "quantity_lifecycle": {"status": "attributed_original_fifo_quantities",
                "buy_fills": {"buy:1": {"day": SESSIONS[6], "available_day": SESSIONS[7],
                    "original_qty": 100, "sold_qty": 100, "remaining_qty": 0,
                    "sell_allocations": [{"day": SESSIONS[8], "qty": 100}]}}}}


def test_intraday_low_close_equality_and_settlement_are_distinct():
    c = card()
    c["daily_slots"][6]["raw"].update(low="9.4", close="9.5")
    c["daily_slots"][7]["raw"].update(low="9.3", close="9.4")
    result = observe_case(c, SESSIONS)
    entry, available, sold = result["daily_observations"][:3]
    assert entry["native_intraday_low_lt_signal_low"] is True
    assert entry["native_close_lt_signal_low"] is False  # equal is not below
    assert result["first_native_close_breach"]["first_exact_day"] == "2026-01-14"
    assert entry["original_quantity_context"]["selected_settled_remaining_before_day_qty"] == 0
    assert available["original_quantity_context"]["selected_settled_remaining_before_day_qty"] == 100
    assert sold["original_quantity_context"]["selected_sell_qty_on_day"] == 100
    assert sold["original_quantity_context"]["selected_remaining_through_day_qty"] == 0
    assert sold["intraday_low_time_relative_to_original_fills_unknown"] is True
    assert sold["original_sale_on_same_day_intraday_order_unknown"] is True
    assert entry["original_sale_on_same_day_intraday_order_unknown"] is False
    assert result["selected_buy_quantity"] == 100  # not the 1300-share target
    assert result["selected_final_original_sell_day"] == "2026-01-15"
    assert result["executable_structure_failure_admitted"] is False


def test_missing_prefix_never_becomes_an_exact_first_breach_or_known_zero_quantity():
    c = card()
    c["daily_slots"][6].update(status="missing_native_row", raw=None)
    c["daily_slots"][7]["raw"].update(low="9.3", close="9.4")
    c["quantity_lifecycle"].update(status="unknown_symbol_fill_attribution", buy_fills=None)
    result = observe_case(c, SESSIONS)
    first = result["first_native_close_breach"]
    assert first["first_observed_day"] == "2026-01-14"
    assert first["first_exact_day"] is None
    assert first["earlier_unknown_days"] == ["2026-01-13"]
    assert first["original_quantity_context_on_observed_day"]["selected_remaining_through_day_qty"] is None
    assert result["selected_final_remaining_qty"] is None


def test_touch_is_not_strict_breach_and_halted_or_zero_volume_is_unknown():
    c = card()
    c["daily_slots"][6]["raw"].update(low="9.5", close="9.5")
    result = observe_case(c, SESSIONS)
    assert result["first_native_intraday_low_touch"]["first_exact_day"] == "2026-01-13"
    assert result["first_native_intraday_low_breach"]["status"] == "not_observed_in_complete_five_session_window"
    for field, value in [("volume", 0), ("tradestatus", "0")]:
        broken = deepcopy(c)
        broken["daily_slots"][6]["raw"][field] = value
        r = observe_case(broken, SESSIONS)
        assert r["daily_observations"][0]["native_close_lt_signal_low"] is None
        assert r["first_native_close_breach"]["status"] == "unknown_incomplete_five_session_window"


def test_shifted_observed_row_cannot_replace_exact_session_and_open_batch_stays_open():
    c = card()
    c["quantity_lifecycle"]["buy_fills"]["buy:1"].update(sold_qty=0, remaining_qty=100, sell_allocations=[])
    result = observe_case(c, SESSIONS)
    assert result["selected_final_remaining_qty"] == 100
    assert result["selected_final_original_sell_day"] is None
    c["daily_slots"][6]["day"] = "2026-01-14"
    with pytest.raises(ValueError, match="exact session"):
        observe_case(c, SESSIONS)
