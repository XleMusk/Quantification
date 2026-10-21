"""Explain saved-close sizing; never change targets to manufacture tradability."""
from copy import deepcopy
import csv

import pytest

from trading.config import StrategyConfig
from trading.research.next_session_preview import _fixed_daily_rows
from trading.research.opening_daily_detail import _read

D, E = '2026-10-16', '2026-10-19'


def preview(symbol, price, *, pending, positions=None):
    account = {'pending_targets': pending}
    before = deepcopy(account)
    quotes = ({symbol: {'trading_date': D, 'close_cents': price,
        'close_rmb': price / 100, 'tradestatus': 1, 'is_st': False}} if price else {})
    result = _fixed_daily_rows(account, {'remaining_full_exits': {}}, positions or {}, quotes,
        10000000, StrategyConfig().validate(), D, E, [])
    assert account == before
    return result


@pytest.mark.parametrize('symbol,price,minimum', [
    ('600001.SSE', 20000, 100), ('688001.SSE', 10000, 200)])
def test_unaffordable_target_stays_zero_and_remains_explained_in_saved_csv(tmp_path, symbol, price, minimum):
    targets, orders, missing, status = preview(symbol, price, pending={symbol: .16})
    row = targets[0]
    assert row['target_qty'] == 0 and orders[0]['qty'] == 0 and missing == []
    assert row['target_weight'] == .16 and row['effective_target_weight'] == .16
    assert row['target_budget_rmb'] == 16000 and row['minimum_buy_notional_rmb'] == 20000
    assert row['minimum_buy_quantity'] == minimum and row['budget_below_minimum_buy_notional'] is True
    assert row['sizing_status'] == 'blocked_below_minimum_buy_quantity'
    assert orders[0]['status'] == 'blocked_below_minimum_buy_quantity'
    assert status == 'PAPER_TARGET_PENDING' and orders[0]['execution_admitted'] is False
    path = tmp_path / 'target.csv'
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerows(targets)
    saved = _read(path, {}, kind='csv')[0]
    assert saved['target_qty'] == 0 and saved['minimum_buy_quantity'] == minimum
    assert saved['target_budget_rmb'] == 16000 and saved['minimum_buy_notional_rmb'] == 20000


def test_no_change_retains_exact_shares_without_inventing_a_target_budget():
    symbol = '688001.SSE'
    targets, orders, missing, status = preview(symbol, 20000, pending=None, positions={symbol: 201})
    row = targets[0]
    assert row['target_qty'] == 201 and row['delta_qty'] == 0 and orders[0]['qty'] == 0
    assert row['target_budget_rmb'] is None and row['budget_below_minimum_buy_notional'] is None
    assert row['sizing_status'] == 'no_change' and row['sizing_note'] == 'no_change_exact_saved_quantity'
    assert status == 'NO_TRADE' and missing == []


def test_missing_quote_stays_unknown_despite_a_known_saved_weight_budget():
    symbol = '600001.SSE'
    targets, orders, missing, _ = preview(symbol, None, pending={symbol: .16})
    row = targets[0]
    assert row['target_qty'] is None and orders[0]['qty'] is None and missing == [symbol]
    assert row['target_budget_rmb'] == 16000 and row['minimum_buy_quantity'] == 100
    assert row['minimum_buy_notional_rmb'] is None and row['budget_below_minimum_buy_notional'] is None
    assert row['sizing_status'] == 'blocked_missing_research_quote'


def test_affordable_saved_close_preserves_quantity_without_claiming_execution_or_fees():
    symbol = '600001.SSE'
    targets, orders, _, _ = preview(symbol, 1000, pending={symbol: .16})
    row = targets[0]
    assert row['target_qty'] == 1600 and orders[0]['qty'] == 1600
    assert row['minimum_buy_notional_rmb'] == 1000 and row['budget_below_minimum_buy_notional'] is False
    assert row['sizing_fees_included'] is False and row['sizing_slippage_included'] is False
    assert row['sizing_reference_basis'] == 'saved_source_raw_close_not_execution_quote'
    assert row['sizing_status'] == 'paper_preview_not_created' and orders[0]['execution_admitted'] is False
