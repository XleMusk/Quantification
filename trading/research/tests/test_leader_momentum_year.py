from types import SimpleNamespace
import json

import pandas as pd
import pytest

from trading.research.leader_momentum_year import AnnualFeed, schedule
from trading.research.experiment import replay
from trading.research.recent_alpha158_panel_replay import ExactSourceRawFeed
from trading.research.shortline_panel_replay import _config


def calendar_fixture():
    payload = dict(schema='cn_a_share_exchange_calendar_v1', year=2024,
                   closed_ranges=['2024-01-01/2024-01-01'], open_sessions=[])
    from trading.research.leader_daily_signals import _calendar_view
    days, _, _ = _calendar_view(payload, '2024-01-01', '2024-12-31', [])
    prior = pd.bdate_range('2023-01-02', periods=242).strftime('%Y-%m-%d').tolist()
    return payload, prior + days


def test_first_month_warmup_does_not_delay_first_day_cohort():
    payload, source = calendar_fixture()
    assert source.index('2024-01-02') == 242
    _, days, months = schedule(payload, source, '2024-01-02', '2024-12-23', '2024-12-31')
    assert days[0] == '2024-01-02' and months[0] == '2024-01-31'
    assert source.index(months[0]) >= 252


def test_wrong_tail_or_missing_exact_source_is_rejected():
    payload, source = calendar_fixture()
    with pytest.raises(ValueError, match='six fixed tail'):
        schedule(payload, source, '2024-01-02', '2024-12-24', '2024-12-31')
    with pytest.raises(ValueError, match='continuous native'):
        schedule(payload, [day for day in source if day != '2024-05-08'],
                 '2024-01-02', '2024-12-23', '2024-12-31')


def test_capital_admission_uses_current_trial_equity_and_same_ledger_T1(tmp_path):
    days = ['2025-01-02', '2025-01-03', '2025-01-06']
    symbols = [f'{index:06}.SSE' for index in range(600001, 600007)]
    raw = pd.DataFrame([dict(date=day, code='sh.' + symbol[:6], open=str(price), high=str(price),
        low=str(price), close=str(price), preclose=str(price), volume='1000000', amount='100000000',
        tradestatus='1', isST='0', adjustflag='3') for day in days for symbol in symbols
        for price in [2000. if symbol != symbols[-1] else 10.]])
    batches = {days[0]: dict(status='complete', reason='monthly_ranking',
        candidates=[dict(symbol=symbol, score=6-index) for index, symbol in enumerate(symbols)])}
    qualification = SimpleNamespace(assess=lambda *args, **kwargs: dict(engine_block_reason=None))
    for policy in ('original', 'known_close_minimum_entry'):
        base = ExactSourceRawFeed(raw, days + ['2025-01-07'], symbols)
        base.instruments = {symbol: dict(ipoDate='2000-01-01', outDate='') for symbol in symbols}
        execution = SimpleNamespace(base=base, raw_issues=lambda *args: [], references=lambda *args: [],
            reference_ok=lambda row: True, validate_session=lambda state, day: None)
        config = _config(100000, 5)
        feed = AnnualFeed(execution, batches, config, days[1], days[-1], qualification, 'uncapped', policy)
        folder = tmp_path / policy
        replay(feed, config, days, folder)
        state = json.loads((folder / 'account.json').read_text())
        fills = list(state['fills'].values())
        if policy == 'original':
            assert fills == []
        else:
            assert {fill['symbol'] for fill in fills} == {symbols[-1]}
            assert {fill['day'] for fill in fills if fill['side'] == 'buy'} == {days[1]}
            assert {fill['day'] for fill in fills if fill['side'] == 'sell'} == {days[2]}
            assert feed.admission[0]['slot_budget_cents'] == 1600000
            assert state['cash'] < 10000000
        assert len(batches[days[0]]['candidates']) == 6
        assert not any(sum(lot['qty'] for lot in lots) for lots in state['positions'].values())


def test_later_month_admission_uses_actual_D_close_book_not_previous_marks(tmp_path):
    days = ['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08']
    held, entrant = '600006.SSE', '600007.SSE'
    raw = pd.DataFrame([dict(date=day, code='sh.' + symbol[:6], open=str(price), high=str(price),
        low=str(price), close=str(price), preclose=str(price), volume='1000000', amount='100000000',
        tradestatus='1', isST='0', adjustflag='3') for day in days for symbol in (held, entrant)
        for price in [170. if symbol == entrant else 10. if day < days[2] else 20.]])
    batches = {days[0]: dict(status='complete', reason='monthly_ranking', candidates=[dict(symbol=held, score=1.)]),
        days[2]: dict(status='complete', reason='monthly_ranking',
                      candidates=[dict(symbol=entrant, score=2.), dict(symbol=held, score=1.)])}
    base = ExactSourceRawFeed(raw, days + ['2025-01-09'], [held, entrant])
    base.instruments = {symbol: dict(ipoDate='2000-01-01', outDate='') for symbol in (held, entrant)}
    execution = SimpleNamespace(base=base, raw_issues=lambda *args: [], references=lambda *args: [],
                                reference_ok=lambda row: True, validate_session=lambda state, day: None)
    qualification = SimpleNamespace(assess=lambda *args, **kwargs: dict(engine_block_reason=None))
    config = _config(100000, 5)
    feed = AnnualFeed(execution, batches, config, days[-2], days[-1], qualification,
                      'uncapped', 'known_close_minimum_entry')
    replay(feed, config, days, tmp_path / 'close_book')
    account = json.loads((tmp_path / 'close_book' / 'account.json').read_text())
    second = feed.admission[1]
    assert second['day'] == days[2] and second['close_equity_cents'] > 11000000
    assert second['slot_budget_cents'] > 1700500
    assert [row['symbol'] for row in second['candidates']] == [entrant, held]
    assert any(fill['symbol'] == entrant and fill['side'] == 'buy' and fill['day'] == days[3]
               for fill in account['fills'].values())
