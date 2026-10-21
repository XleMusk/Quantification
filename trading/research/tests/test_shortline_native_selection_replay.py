"""Saved decisions plus real shared Engine/Ledger, with synthetic prices only."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.ledger import new_state
from trading.research import shortline_native_selection_replay as m
from trading.research.stockdb_action_accounting import StockDBActionAccounting

A, B = '600001.SSE', '600002.SSE'
CONTEXT = '2026-09-25'
DAYS = ['2026-09-28', '2026-09-29', m.END] + m.TAIL
QUAL = SimpleNamespace(assess=lambda *args, **kwargs: {'engine_block_reason': None})


def raw():
    return pd.DataFrame([dict(date=day, code='sh.' + symbol[:6], open='10', high='10.1', low='9.9',
        close='10', preclose='10', volume='1000000', amount='30000000', tradestatus='1', isST='0',
        adjustflag='3') for day in [CONTEXT] + DAYS for symbol in (A, B)])


def accounting(symbols, covered):
    codes = ['sh.' + s[:6] for s in symbols]
    universe = pd.DataFrame({'symbol': symbols, 'code': codes})
    return StockDBActionAccounting([], [], [], {(c, 2026): True for c in codes}, universe,
        {'rename': None}, symbols, [CONTEXT] + covered, covered,
        {'price_source': 'independent_baostock_raw', 'end': covered[-1]})


def plans():
    return {DAYS[0]: dict(decision='FULL_TARGET', reason='saved_first', symbols=[A]),
        DAYS[1]: dict(decision='NO_CHANGE', reason='saved_unknown_preserve', symbols=[]),
        m.END: dict(decision='FULL_TARGET', reason='saved_last_not_overwritten', symbols=[B])}


def adapter():
    config = m._config(100000, 5)
    context = RunContext(m.METHOD_ID + ':lr', TradingMode.REPLAY)
    base = m.ExactSourceRawFeed(raw(), [CONTEXT] + DAYS + ['2026-10-16'], [A, B])
    base.instruments = {s: {'ipoDate': '2000-01-01', 'outDate': ''} for s in (A, B)}
    execution = m.HistoricalExecutionPolicy(base, accounting([A, B], DAYS), DAYS,
        prior_volume_policy=m.PriorVolumeReplayPolicy(context))
    return m.SavedSelectionFeed(execution, plans(), config, context, QUAL, DAYS[-1]), config, context


def test_real_engine_NC_fixed_weight_T1_and_last_target_before_tail_exit():
    feed, config, context = adapter()
    state = new_state(config.initial_cash)
    for index, day in enumerate(DAYS):
        m.advance(state, feed, day, config, index, context=context, target_policy=m.fixed_weight_policy,
            full_exit_lifecycle=m.RISK_COMPAT_FULL_EXIT_LIFECYCLE)
        if day == DAYS[0]:
            assert not state['fills'] and state['pending_targets'] == {A: .16}
        if day == DAYS[1]:
            assert m._positions(state) == {A: 1600}
            assert state['pending_targets'] is None  # NC preserves stock, not empty FULL.
            assert all(f['available_day'] == m.END for f in state['fills'].values())
        if day == m.END:
            assert state['pending_targets'] == {B: .16} and A in m._positions(state)
        if day == m.TAIL[0]:
            assert A not in m._positions(state) and B in m._positions(state)
            assert state['pending_targets'] == {}  # Study exit only after the last target's open.
            buys = [f for f in state['fills'].values() if f['symbol'] == B and f['side'] == 'buy']
            assert buys and all(f['available_day'] == m.TAIL[1] for f in buys)
    assert not m._positions(state) and state['pending_targets'] is None
    assert not state['full_exit_lifecycle']['remaining_full_exits']
    assert sum(r['decision'] == 'FULL_EMPTY' for r in feed.decisions) == 1
    assert state['prior_volume_execution_policy']['context'] == context.to_dict()


def test_two_isolated_real_Engine_cases_keep_source_failure_and_fixed_six_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(m, '_QualifiedFeed', lambda base, qualification: base)
    monkeypatch.setattr(m, 'load_action_accounting', lambda *args, **kwargs:
        accounting(kwargs['symbols'], kwargs['replay_days']))
    record = dict(source={'context_day': CONTEXT, 'next_session_after_tail': '2026-10-16',
        'action_event': {'path': str(tmp_path / 'events' / 'result.json')},
        'action_reference': {'path': str(tmp_path / 'refs' / 'result.json')}},
        expected_sessions=DAYS, action_source_end='2026-10-13', symbols={'lr': [A, B], 'lgbm': [A, B]})
    broken = raw()
    broken.loc[broken.date.eq(DAYS[1]) & broken.code.eq('sh.600001'), 'tradestatus'] = None
    first = m._case('lr', plans(), broken, record, tmp_path / 'lr', QUAL)
    second = m._case('lgbm', plans(), raw(), record, tmp_path / 'lgbm', QUAL)
    assert first['last_committed_day'] == DAYS[0] and first['main_window']['marked_NAV'] is None
    assert second['last_committed_day'] == '2026-10-13'
    assert second['blocking_evidence']['day'] == '2026-10-14'
    assert second['source_issues'][-1]['reason'] == 'unsupported_action_source_cutoff'
    assert second['main_window']['source_prefix_completed'] is True
    assert second['main_window']['closed_total_return'] is None
    assert second['full_tail']['expected_tail'] == m.TAIL
    assert second['full_tail']['metrics'] is None and not second['full_tail']['complete_PnL_available']
    assert first['context']['account_id'] != second['context']['account_id']
    assert (tmp_path / 'lr' / 'account.json').is_file() and (tmp_path / 'lgbm' / 'account.json').is_file()


def saved_evaluation(folder):
    folder.mkdir()
    days = pd.bdate_range(m.START, periods=180).strftime('%Y-%m-%d').tolist() + [m.END]
    decisions, selection = [], []
    for day in days:
        for kind in m.KINDS:
            full = day == m.END
            decisions.append(dict(date=day, model_kind=kind, decision='FULL_TARGET' if full else 'NO_CHANGE',
                reason='saved', selected_count=int(full)))
            if full:
                selection.append(dict(date=day, symbol=A, event_id=f'{A}|{day}|s1', stage=1,
                    model_kind=kind, candidate_selected=True))
    pd.DataFrame(decisions).to_csv(folder / 'daily_decisions.csv', index=False)
    pd.DataFrame(selection).to_csv(folder / 'selection.csv', index=False)
    seal = {k: m._pin(folder / n) for k, n in [('selection', 'selection.csv'), ('daily_decisions', 'daily_decisions.csv')]}
    m._save(folder / 'prelabel_seal.json', seal)
    result = dict(schema='native_frozen_shortline_evaluation_result_v1',
        status='completed_retrospective_frozen_stagewise_quality', event_start=m.START, event_end=m.END,
        execution_admitted=False, model_refit=False,
        files={n: m.digest(folder / n) for n in ('daily_decisions.csv', 'selection.csv')})
    m._save(folder / 'result.json', result)


def test_only_sealed_saved_selection_loaded_hash_and_NC_mismatch_rejected(tmp_path):
    folder = tmp_path / 'evaluation'
    saved_evaluation(folder)
    plans_by_kind, days, _ = m.load_selection(folder)
    assert len(days) == 181 and plans_by_kind['lr'][m.END]['symbols'] == [A]
    assert plans_by_kind['lr'][m.START]['symbols'] == []
    selection = folder / 'selection.csv'
    selection.write_text(selection.read_text() + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='pre-label sealed'):
        m.load_selection(folder)


def test_fixed_weight_reduction_keeps_metadata_and_rejects_wrong_context():
    feed, config, context = adapter()
    frame = feed.signal_frame(DAYS[0], config, context)
    assert [r['symbol'] for r in frame.ranked_candidates()] == [A]
    original = m.PortfolioIntent.full_target({A: .2}, context=context,
        decided_at='2026-09-28T15:00:00+08:00', execution_not_before=DAYS[1], source='saved')
    reduced = m.fixed_weight_policy(original)
    assert dict(reduced.weights) == {A: .16} and reduced.source == original.source
    assert reduced.context == context and reduced.decided_at == original.decided_at
    assert reduced.execution_not_before == original.execution_not_before
    with pytest.raises(ValueError, match='independent REPLAY'):
        feed.signal_frame(DAYS[0], config, RunContext('other', TradingMode.REPLAY))
