import json
import math

import pandas as pd
import pytest

from trading.report import metrics, quote_rmb
from trading.research.factor_risk_audit import daily_metrics


def daily(equities):
    return [{"date": f"2026-01-{index + 5:02d}", "equity": equity,
             "fees": 2, "turnover_value": 100, "stale_symbols": []}
            for index, equity in enumerate(equities)]


def test_account_risk_includes_first_session_and_preserves_existing_metrics():
    result = metrics(daily([110, 99]), 100)
    assert result["sharpe_zero_rf_252"] == pytest.approx(0, abs=1e-12)
    assert result["annualized_volatility"] == pytest.approx(math.sqrt(252 * .02))
    assert result["worst_day"] == pytest.approx(-.1)
    assert result["net_return"] == pytest.approx(-.01)
    assert result["max_drawdown"] == pytest.approx(-.1)
    assert result["fees"] == 4
    assert result["turnover_value"] == 200


@pytest.mark.parametrize("equities", [[100, 100, 100], [95]])
def test_undefined_sharpe_is_null_not_infinite_or_nan(equities):
    result = metrics(daily(equities), 100)
    assert result["sharpe_zero_rf_252"] is None
    assert result["annualized_volatility"] == (0 if len(equities) > 1 else None)
    json.dumps(result, allow_nan=False)


def test_core_metrics_match_existing_research_risk_basis():
    rows = daily([99, 101, 95, 100])
    core, research = metrics(rows, 100), daily_metrics(pd.DataFrame(rows), initial_cash=100)
    for field in ("sharpe_zero_rf_252", "annualized_volatility", "worst_day", "max_drawdown"):
        assert core[field] == pytest.approx(research[field])
    assert metrics([], 100) is None


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), 0, -1])
def test_missing_or_invalid_equity_is_not_filled_into_a_valid_sharpe(value):
    with pytest.raises(ValueError, match="Invalid account equity"):
        metrics(daily([100, value, 101]), 100)


@pytest.mark.parametrize("unit,value,expected", [("cent", 1000, 10), ("mill", 3937, 3.937),
                                                 ("cent", 0, 0), ("mill", 0, 0)])
def test_report_quotes_use_declared_unit(unit, value, expected):
    assert quote_rmb(value, unit) == expected


@pytest.mark.parametrize("unit,value", [("unknown", 3937), ("yuan", 0), ("mill", 3.937),
                                      ("mill", -1), ("cent", True)])
def test_report_quotes_reject_unknown_or_invalid_units(unit, value):
    with pytest.raises(ValueError):
        quote_rmb(value, unit)
