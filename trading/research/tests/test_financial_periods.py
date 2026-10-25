from decimal import Decimal

import pytest

from trading.research.financial_periods import FlowValue, ttm


def value(amount, period, kind, *, scope="consolidated"):
    return FlowValue(Decimal(str(amount)), period, kind, scope, "CNY", "million_yuan", period + "-v1")


def test_ttm_is_an_independent_decimal_flow_calculation():
    result = ttm(value("120", "2026-09-30", "YTD"), value("160", "2025-12-31", "FY"),
                 value("90", "2025-09-30", "YTD"), version_id="ttm-v1")
    assert result.value == Decimal("190")
    assert result.period_kind == "TTM" and result.version_id == "ttm-v1"


@pytest.mark.parametrize("bad", ["scope", "period_kind"])
def test_ttm_rejects_incomparable_or_wrong_period_inputs(bad):
    current = value("120", "2026-09-30", "YTD")
    full = value("160", "2025-12-31", "FY", scope="separate" if bad == "scope" else "consolidated")
    if bad == "period_kind":
        full = value("160", "2025-12-31", "YTD")
    with pytest.raises(ValueError):
        ttm(current, full, value("90", "2025-09-30", "YTD"), version_id="ttm-v1")
