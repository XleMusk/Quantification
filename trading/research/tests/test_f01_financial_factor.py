from datetime import date, timezone, timedelta
from decimal import Decimal

import pytest

from trading.research.f01_financial_factor import (
    CONFLICT, FinancialFact, INCOMPARABLE, MISSING_INPUT, NONPOSITIVE_DENOMINATOR,
    OBSERVED, PRE_EVENT_UNPROVEN, READY, compute_f01, cumulative_to_single_quarter,
)
from trading.research.financial_factor_contracts import TimeWindow, publication_interval


TZ = timezone(timedelta(hours=8))
BEFORE = TimeWindow.exact("2026-10-19T15:00:00+08:00")
EVENT = publication_interval("2026-10-20")


def fact(fid, metric, value, *, period_end, available=BEFORE, period_kind="single_quarter", status=OBSERVED,
         scope="consolidated", currency="CNY", unit="yuan"):
    return FinancialFact(fid, "issuer-1", metric, date(2025, 1, 1), period_end, period_kind,
                         scope, currency, unit, value, available, status)


def test_f01_matches_independent_decimal_calculation():
    result = compute_f01(fact("cur", "parent_net_profit", Decimal("50"), period_end=date(2026, 9, 30)),
                         fact("prior", "parent_net_profit", Decimal("40"), period_end=date(2025, 9, 30)),
                         fact("assets", "total_assets", Decimal("1000"), period_end=date(2026, 9, 30)), EVENT)
    assert result.status == READY
    assert result.value == Decimal("0.01")
    assert result.fact_ids == ("cur", "prior", "assets")


def test_f01_rejects_unproven_same_day_asset_and_preserves_status():
    same_day = TimeWindow.exact("2026-10-20T09:00:00+08:00")
    result = compute_f01(fact("cur", "parent_net_profit", 50, period_end=date(2026, 9, 30)),
                         fact("prior", "parent_net_profit", 40, period_end=date(2025, 9, 30)),
                         fact("assets", "total_assets", 1000, period_end=date(2026, 9, 30), available=same_day), EVENT)
    assert result.status == PRE_EVENT_UNPROVEN and result.value is None


@pytest.mark.parametrize("status, expected", [(None, MISSING_INPUT), (CONFLICT, CONFLICT)])
def test_f01_missing_or_conflicting_input_is_not_zero(status, expected):
    value_status = status or "NOT_OBSERVED"
    result = compute_f01(fact("cur", "parent_net_profit", 50, period_end=date(2026, 9, 30), status=value_status),
                         fact("prior", "parent_net_profit", 40, period_end=date(2025, 9, 30)),
                         fact("assets", "total_assets", 1000, period_end=date(2026, 9, 30)), EVENT)
    assert result.status == expected and result.value is None


def test_f01_rejects_nonpositive_assets_and_scope_conflict():
    result = compute_f01(fact("cur", "parent_net_profit", 50, period_end=date(2026, 9, 30)),
                         fact("prior", "parent_net_profit", 40, period_end=date(2025, 9, 30)),
                         fact("assets", "total_assets", 0, period_end=date(2026, 9, 30)), EVENT)
    assert result.status == NONPOSITIVE_DENOMINATOR
    result = compute_f01(fact("cur", "parent_net_profit", 50, period_end=date(2026, 9, 30)),
                         fact("prior", "parent_net_profit", 40, period_end=date(2025, 9, 30), scope="separate"),
                         fact("assets", "total_assets", 1000, period_end=date(2026, 9, 30)), EVENT)
    assert result.status == INCOMPARABLE


def test_cumulative_to_single_quarter_keeps_decimal_units_and_latest_availability():
    prior = fact("h1", "parent_net_profit", Decimal("70"), period_end=date(2026, 6, 30), period_kind="cumulative")
    current = fact("9m", "parent_net_profit", Decimal("120"), period_end=date(2026, 9, 30), period_kind="cumulative")
    derived = cumulative_to_single_quarter(current, prior, derived_fact_id="q3")
    assert derived.value == Decimal("50")
    assert derived.period_kind == "single_quarter"
    assert derived.period_start == date(2026, 6, 30)
