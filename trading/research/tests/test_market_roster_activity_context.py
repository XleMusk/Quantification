"""Exact quantity arithmetic and research-clock admission, without source calls."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json

import pytest

from trading.research import market_roster_activity_context as a
from trading.research import market_roster_quote_view as price_view
from trading.research import market_roster_observation_change as change_view
from trading.research import prior_dated_market_quote_snapshot as collector
from trading.research.tests.test_prior_dated_market_quote_snapshot import fixture, execute, transport, dump
from trading.research.tests.test_market_roster_watch import WatchClock


def quote(symbol="000001.SZSE", volume="100", amount="1000.000"):
    return {"symbol": symbol, "source_raw_volume": volume, "source_raw_amount": amount,
            "low_price_rmb": "9.990", "high_price_rmb": "10.010"}


@pytest.fixture
def contract(tmp_path, monkeypatch):
    value = json.loads(a.CONTRACT_FILE.read_text(encoding="utf8"))
    read_at = datetime.now(a.CN).isoformat()
    value["actual_derived_at"] = (datetime.fromisoformat(read_at) - timedelta(seconds=60)).isoformat()
    path = tmp_path / "assessment.json"
    dump(path, value)
    monkeypatch.setattr(a, "CONTRACT_FILE", path)
    return path, value, read_at


def inputs():
    targets = [{"symbol": f"{index:06d}.SZSE", "in_fixed_590": index < 3,
                "background_isST": "1" if index == 3 else "0", "background_trade_status": "1",
                "fixed_590_labels": ["trend_leader"] if index < 3 else []} for index in range(1, 4)]
    prices = [{"symbol": target["symbol"], "current_at_read": True} for target in targets]
    quotes = [quote(target["symbol"]) for target in targets]
    return targets, prices, quotes


def cumulative(contract, *, missing=False, expired=False):
    targets, prices, quotes = inputs()
    if missing:
        quotes[0]["source_raw_amount"] = ""
    reads = deepcopy(prices)
    if expired:
        for row in reads:
            row["current_at_read"] = False
    return a.cumulative_activity_context(read_rows=reads, saved_rows=prices, quotes=quotes, targets=targets,
                                         source_clocks={"source_day": "2026-10-14"}, read_at=contract[2])


def test_fractional_money_is_exact_and_zero_activity_has_no_vwap():
    item = quote(volume="3", amount="30.00123")
    assert a.parse_quantity(item)["amount"] == Decimal("30.00123")
    zero = a.parse_quantity(quote(volume="0", amount="0.000"))
    assert zero["known"] is True and zero["amount"] == 0 and zero["vwap"] is None


@pytest.mark.parametrize("volume,amount", [("", "10"), ("-1", "10"), ("1", "-1"), ("1.5", "15"),
                                          ("NaN", "10"), ("1", "Infinity"), ("0", "10"), ("1", "0")])
def test_missing_negative_fractional_nonfinite_and_incoherent_zero_are_unknown(volume, amount):
    parsed = a.parse_quantity(quote(volume=volume, amount=amount))
    assert parsed["known"] is False and parsed["amount"] is parsed["volume"] is None


def test_range_tolerance_is_fixed_half_coarsest_display_quantum():
    item = quote(volume="100", amount="1105.0000")
    item.update(low_price_rmb="10.00", high_price_rmb="11.0")
    assert a.parse_quantity(item)["known"] is True
    item["source_raw_amount"] = "1105.0001"
    assert a.parse_quantity(item)["known"] is False


def test_delta_zero_known_but_reset_or_one_zero_revision_unknown():
    first = quote()
    zero = a.parse_delta(first, quote())
    assert zero["known"] is True and zero["amount"] == zero["volume"] == 0 and zero["vwap"] is None
    reset = a.parse_delta(first, quote(volume="99", amount="990"))
    assert reset["known"] is False and "negative_cumulative_delta" in reset["reason"]
    revision = a.parse_delta(first, quote(amount="1000.001"))
    assert revision["known"] is False and revision["reason"] == "one_zero_one_positive_quantity"


def test_full_total_partial_subtotal_and_empty_groups_are_distinct(contract):
    full = cumulative(contract)
    assert full["status"] == "observed_research_quantity_activity"
    assert full["current_summary"]["known_amount_cny"] == full["current_summary"]["full_group_amount_cny"] == "3000.000"
    assert full["current_summary"]["known_volume_shares"] == "300"
    partial = cumulative(contract, missing=True)
    summary = partial["current_summary"]
    assert summary["member_count"] == summary["price_known_count"] == 3
    assert summary["known_activity_count"] == 2 and summary["unknown_activity_count"] == 1
    assert summary["known_amount_cny"] == "2000.000" and summary["full_group_amount_cny"] is None
    empty = next(group for group in partial["archetype_groups"] if group["member_count"] == 0)
    assert empty["observation_summary"]["known_amount_cny"] is empty["observation_summary"]["full_group_amount_cny"] is None
    assert len(partial["groups"]) == 6 and len(partial["archetype_groups"]) == 8


def test_full_unknown_does_not_become_zero_and_known_zero_is_string_zero(contract):
    targets, prices, quotes = inputs()
    for item in quotes:
        item.update(source_raw_volume="0", source_raw_amount="0.000")
    result = a.cumulative_activity_context(read_rows=prices, saved_rows=prices, quotes=quotes, targets=targets,
                                          source_clocks={}, read_at=contract[2])
    assert result["current_summary"]["known_amount_cny"] == result["current_summary"]["full_group_amount_cny"] == "0"
    for row in prices:
        row["current_at_read"] = False
    unknown = a.cumulative_activity_context(read_rows=prices, saved_rows=prices, quotes=quotes, targets=targets,
                                           source_clocks={}, read_at=contract[2])
    assert unknown["current_summary"] is None and unknown["read_summary"]["unknown_activity_count"] == 3
    assert unknown["read_summary"]["known_amount_cny"] is unknown["observation_summary"]["known_amount_cny"] is None


def test_expiry_only_removes_current_quantity_and_preserves_saved_context(contract):
    result = cumulative(contract, expired=True)
    assert result["current_summary"] is None and result["read_summary"]["unknown_activity_count"] == 3
    assert result["observation_summary"]["known_activity_count"] == 3


@pytest.mark.parametrize("change", ["future", "production", "missing"])
def test_semantic_contract_time_and_boundaries_are_required(contract, change):
    path, value, read_at = contract
    if change == "missing":
        path.unlink()
    else:
        if change == "future":
            value["actual_derived_at"] = (datetime.fromisoformat(read_at) + timedelta(seconds=1)).isoformat()
        else:
            value["production_volume_or_orderbook_admitted"] = True
        dump(path, value)
    result = cumulative(contract)
    assert result["status"] == "unavailable_activity_context" and result["current_summary"] is None


def test_window_uses_existing_pairs_and_read_freshness_without_quantity_reselection(contract):
    targets, _, first = inputs()
    latest = [quote(target["symbol"], volume="102", amount="1020.001") for target in targets]
    pairs = [{"symbol": target["symbol"], "paired": index != 1, "current_at_read": index != 2}
             for index, target in enumerate(targets)]
    result = a.window_activity_context(pairs=pairs, baseline_quotes=first, latest_quotes=latest, targets=targets,
                                      source_clocks={}, read_at=contract[2])
    assert result["current_summary"]["price_pair_count"] == result["current_summary"]["known_activity_count"] == 1
    assert result["current_summary"]["unknown_activity_count"] == 2 and result["current_summary"]["known_amount_cny"] == "20.001"
    assert result["observation_summary"]["known_activity_count"] == 2 and result["observation_summary"]["full_group_amount_cny"] is None


def test_cumulative_failure_isolated_from_price_result(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    execute(value)
    monkeypatch.setattr(price_view, "process_identity", lambda *args: {})
    monkeypatch.setattr(price_view, "industry_context", lambda **kwargs: {})
    def broken(**kwargs):
        raise RuntimeError("isolated activity failure")
    monkeypatch.setattr(a, "cumulative_activity_context", broken)
    result = price_view.market_roster_quote_view(value["data"], now="2026-10-15T13:05:30+08:00")
    assert result["status"] == "current_prior_dated_roster_snapshot" and result["current_summary"]["known_count"] == 205
    assert result["activity_context"]["status"] == "unavailable_activity_context"


def test_window_failure_isolated_from_price_pair_result(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock()
    get, _ = transport(clock)
    collector.watch(package=value["package"], calendar=value["calendar"], data_root=value["data"], max_observations=3,
                    _clock=clock.now, _sleep=clock.sleep, _get=get)
    def broken(**kwargs):
        raise RuntimeError("isolated window activity failure")
    monkeypatch.setattr(a, "window_activity_context", broken)
    result = change_view.market_roster_observation_change(value["data"], now="2026-10-15T13:11:30+08:00")
    assert result["status"] == "current_same_watch_roster_change" and result["summary"]["paired_count"] == 205
    assert result["activity_context"]["status"] == "unavailable_activity_context"
