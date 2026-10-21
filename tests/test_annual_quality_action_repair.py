from copy import deepcopy

import pytest

from trading.research.annual_quality_action_repair import apply_action_changes, validate_reference_changes


def checkpoint(day="2026-10-15"):
    return {"state": {"day": day, "entitlements": {"603995.SSE:2026-10-14": 0}}, "daily": []}


def reserve_change():
    old = dict(symbol="603995.SSE", dividOperateDate="2026-10-14",
               dividRegistDate="2026-10-13", dividReserveToStockPs="", dividCashPsBeforeTax="0.3")
    return {"old": old, "new": dict(old, dividReserveToStockPs="0")}


def test_past_zero_entitlement_completion_does_not_mutate_seed():
    seed = checkpoint()
    expected = deepcopy(seed)
    updated, repairs = apply_action_changes(seed, None, [reserve_change()], reviewed_at="2026-10-18T19:00:00+08:00")
    assert seed == updated == expected
    assert repairs == []
    seed["state"]["entitlements"]["603995.SSE:2026-10-14"] = 200
    with pytest.raises(ValueError, match="consumed held"):
        apply_action_changes(seed, None, [reserve_change()], reviewed_at="2026-10-18T19:00:00+08:00")


def test_known_cash_change_and_missing_past_registration_are_rejected():
    change = reserve_change()
    change["new"]["dividCashPsBeforeTax"] = "0.31"
    with pytest.raises(ValueError, match="Only a reviewed"):
        apply_action_changes(checkpoint(), None, [change], reviewed_at="2026-10-18T19:00:00+08:00")
    change = reserve_change()
    change["old"] = None
    with pytest.raises(ValueError, match="predates"):
        apply_action_changes(checkpoint(), None, [change], reviewed_at="2026-10-18T19:00:00+08:00")


def test_future_reference_keeps_all_observed_prices_and_exact_date():
    before = dict(code="sh.603605", date="2026-10-16", close=57.79, preclose=55.62,
                  tradestatus=1, previous_observed_close=56.79, reference_delta=-1.17,
                  reference_formula_status="unresolved_incremental_raw_reference_change")
    item = {"old": before, "new": dict(before, reference_formula_status="reference_match")}
    validate_reference_changes([item], "2026-10-15")
    for changed in (dict(item["new"], preclose=55.61), dict(item["new"], date="2026-10-15")):
        with pytest.raises(ValueError, match="future reference"):
            validate_reference_changes([dict(item, new=changed)], "2026-10-15")
