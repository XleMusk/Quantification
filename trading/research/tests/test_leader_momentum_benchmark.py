from types import SimpleNamespace

import numpy as np
import pandas as pd

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.research import leader_momentum_benchmark as benchmark
from trading.research.low_risk import features
from trading.research.shortline_panel_replay import _config


def test_existing_features_exclude_halts_and_do_not_read_future_prices():
    days = [str(i).zfill(3) for i in range(270)]
    prices = np.exp(np.arange(270) * .001 + np.sin(np.arange(270)) * .01)
    closes = pd.DataFrame({'A': prices, 'B': prices}, index=days)
    volumes = pd.DataFrame(100.0, index=days, columns=['A', 'B'])
    volumes.loc['100', 'B'] = 0
    base = SimpleNamespace(closes=closes, volumes=volumes,
        amounts=pd.DataFrame(30000000.0, index=days, columns=['A', 'B']), calendar=days)
    first = features(base, '269', feature_days=['260'])
    assert np.isclose(first['momentum'].at['260', 'A'], prices[239] / prices[8] - 1)
    assert np.isfinite(first['volatility'].at['260', 'A'])
    assert np.isnan(first['volatility'].at['260', 'B'])
    base.closes.loc['261':, 'A'] = 999999
    second = features(base, '269', feature_days=['260'])
    assert all(first[key].equals(second[key]) for key in first)


def test_monthly_rank_uses_own_day_status_qualification_and_stable_ties():
    day, members = '2026-04-30', ['600003.SSE', '600002.SSE', '600001.SSE']
    values = {key: pd.DataFrame([numbers], index=[day], columns=members) for key, numbers in {
        'momentum': [.2, .2, .8], 'volatility': [.1, .1, .1],
        'median_amount': [30000000., 30000000., 30000000.]}.items()}
    qualification = SimpleNamespace(assess=lambda symbol, date, **kw: {'engine_block_reason': None})
    rows = {symbol: dict(tradestatus='1', isST='0', volume='100') for symbol in members}
    rows['600001.SSE']['isST'] = '1'
    result = benchmark.monthly_batch(day, members, values, rows, qualification)
    assert result['status'] == 'complete'
    assert [row['symbol'] for row in result['candidates']] == ['600002.SSE', '600003.SSE']


def test_missing_batch_is_no_change_but_explicit_study_exit_is_full_empty():
    config = _config(100000, 5)
    execution = SimpleNamespace(base=SimpleNamespace())
    adapter = benchmark.MonthlyFeed(execution, {}, config, '2026-09-21', '2026-09-30')
    context = RunContext('benchmark-test', TradingMode.REPLAY)
    assert isinstance(adapter.signal_frame('2026-04-10', config, context), SignalUnavailable)
    frame = adapter.signal_frame('2026-09-21', config, context)
    assert frame.signals == ()
    assert isinstance(adapter.signal_frame('2026-09-30', config, context), SignalUnavailable)


def test_cumulative_capacity_does_not_refill_or_turn_filtered_empty_into_exit():
    batches = {str(day): dict(status='complete', candidates=[dict(symbol=f'{day * 5 + index:06}.SSE', score=1.)
        for index in range(5)]) for day in range(5)}
    capped = benchmark.capacity_batches(batches, 'causal_first_seen_cap20')
    assert capped['4']['status'] == 'incomplete'
    assert capped['4']['candidates'] == []
    assert len(capped['4']['original_top5']) == 5
    assert len(capped['4']['causal_seen_symbols']) == 20
    assert len(batches['4']['candidates']) == 5
    assert benchmark.capacity_batches(batches, 'uncapped')['4']['status'] == 'complete'
