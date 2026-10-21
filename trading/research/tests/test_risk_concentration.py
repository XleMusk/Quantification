import pandas as pd
import pytest

from trading.research.risk_concentration import daily_stress, summarize, symbol_concentration


def test_symbol_concentration_merges_saved_pnl_and_turnover():
    pnl = pd.DataFrame([
        {"symbol": "A", "observed_pnl_cny": -100.0, "fees_cny": 5.0},
        {"symbol": "B", "observed_pnl_cny": -50.0, "fees_cny": 4.0},
        {"symbol": "C", "observed_pnl_cny": 200.0, "fees_cny": 6.0},
    ])
    costs = pd.DataFrame([
        {"symbol": "A", "notional_cny": 1000.0, "matched_price_difference_cny": 2.0, "unmatched_fills": 0},
        {"symbol": "B", "notional_cny": 900.0, "matched_price_difference_cny": 1.0, "unmatched_fills": 1},
        {"symbol": "C", "notional_cny": 1200.0, "matched_price_difference_cny": 3.0, "unmatched_fills": 0},
    ])
    orders = pd.DataFrame([
        {"order_id": "a1", "symbol": "A", "notional_cny": 1000.0},
        {"order_id": "b1", "symbol": "B", "notional_cny": 900.0},
        {"order_id": "c1", "symbol": "C", "notional_cny": 1200.0},
    ])
    result = symbol_concentration(pnl, costs, orders)
    assert result.symbol.tolist() == ["A", "B", "C"]
    assert result.loc[result.symbol == "A", "gross_loss_share"].iloc[0] == 2 / 3
    assert result.loc[result.symbol == "C", "filled_order_count"].iloc[0] == 1


def test_daily_stress_keeps_stale_marks_and_orders_worst_days():
    daily = pd.DataFrame([
        {"date": "2026-01-01", "equity": 100.0, "cash": 20.0, "holdings": 2, "stale_symbols": "[]"},
        {"date": "2026-01-02", "equity": 90.0, "cash": 20.0, "holdings": 2, "stale_symbols": "['A']"},
        {"date": "2026-01-03", "equity": 95.0, "cash": 20.0, "holdings": 1, "stale_symbols": "[]"},
    ])
    result = daily_stress(daily)
    assert result.iloc[0].date == "2026-01-02"
    assert int(result.loc[result.date == "2026-01-02", "stale_count"].iloc[0]) == 1
    assert float(result.loc[result.date == "2026-01-02", "drawdown"].iloc[0]) == pytest.approx(-0.1)


def test_summary_does_not_promote_concentration_to_strategy_validation():
    symbols = pd.DataFrame({"observed_pnl_cny": [-100.0, 200.0]})
    stress = pd.DataFrame({"daily_return": [-0.1, 0.02], "drawdown": [-0.1, -0.08],
                           "date": ["2026-01-02", "2026-01-03"], "stale_count": [1, 0]})
    result = summarize(symbols, stress, {"case_id": "r:s", "blocked_attempts": {}}, "attr")
    assert result["top_5_loss_share"] == 1.0
    assert result["execution_admitted"] is False
    assert result["strategy_validated"] is False
    assert result["stale_valuation_days"] == 1
