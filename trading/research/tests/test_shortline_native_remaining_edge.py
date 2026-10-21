from types import SimpleNamespace

import numpy as np
import pandas as pd

from trading.research.shortline_native_remaining_edge import (
    make_schedule, observations, endpoint_proxy, price_open, reference_reason)


def quote(opening='10', close='10', preclose='10', active='1'):
    return {'raw': dict(open=opening, close=close, preclose=preclose,
                volume='10000', tradestatus=active, isST='0'),
            'adjusted': dict(open=opening, volume='10000', tradestatus=active)}


QUALIFIED = SimpleNamespace(assess=lambda symbol, day: {'engine_block_reason': None})


def test_calendar_horizons_are_exact_and_never_shift_for_missing_or_cutoff():
    days = ['2026-09-23', '2026-09-24', '2026-09-25', '2026-09-28',
            '2026-09-29', '2026-09-30', '2026-10-08', '2026-10-09']
    schedule = make_schedule([days[0], days[5]], days)
    assert schedule[days[0]] == dict(entry_day=days[1], h1_exit_day=days[2], h5_exit_day=days[6])
    assert schedule[days[5]] == dict(entry_day=days[6], h1_exit_day=days[7], h5_exit_day=None)
    assert endpoint_proxy('600000.SSE', days[6], quote('12'), quote()['raw'], QUALIFIED, 'buy')[0] == 'unknown'


def test_reference_changes_stay_unknown_and_up_limit_is_only_proxy_blocked():
    previous = quote()['raw']
    assert reference_reason(quote(preclose='9.8')['raw'], previous) == 'unresolved_action_or_reference_change'
    status, reason = endpoint_proxy('600000.SSE', '2026-01-06', quote('11'), previous, QUALIFIED, 'buy')
    assert (status, reason) == ('proxy_blocked', 'limit_up_queue_unknown')
    assert endpoint_proxy('600000.SSE', '2026-01-06', quote('10.50'), previous, QUALIFIED, 'buy')[0] == 'proxy_clear'
    assert endpoint_proxy('600000.SSE', '2026-01-06', quote(preclose='9.8'), previous, QUALIFIED, 'buy')[0] == 'unknown'


def test_missing_and_halted_prices_do_not_become_zero_return_or_last_price():
    assert price_open(None)[0] is None
    assert price_open(quote(active='0'))[0] is None
    assert price_open(quote(opening=''))[0] is None


def test_selected_and_pool_identity_survive_missing_five_day_path():
    days = ['2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08',
            '2026-01-09', '2026-01-12', '2026-01-13']
    formation = pd.DataFrame([dict(event_id='x', date=days[0], symbol='600000.SSE', stage=1,
        model_kind='lr', candidate_selected=True), dict(event_id='x', date=days[0], symbol='600000.SSE', stage=1,
        model_kind='lgbm', candidate_selected=False)])
    data = {(day, '600000.SSE'): quote() for day in days}
    data[(days[2], '600000.SSE')] = quote(opening='10.5')
    data.pop((days[-1], '600000.SSE'))
    result = observations(formation, {'calendar': days}, make_schedule([days[0]], days), data, QUALIFIED)
    row = result.iloc[0]
    assert bool(row.candidate_selected) and row.entry_status == 'proxy_clear'
    assert np.isclose(row.h1_return, .05) and np.isnan(row.h5_return)
    assert row.h5_exit_status == 'unknown' and len(result) == 2
    assert not bool(result.iloc[1].candidate_selected)
    assert result.iloc[1].h1_return == row.h1_return and np.isnan(result.iloc[1].h5_return)
