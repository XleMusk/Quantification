"""Synthetic chain integration; no market replay or external source is used."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from trading.research import leader_momentum_source_continue as consumer
from trading.research.action_events import reference_concordance
from trading.research.experiment import baseline_ledger_result
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_momentum_year import pin, read, save
from trading.research.shortline_panel_replay import _config
from trading.research.stockdb_action_accounting import StockDBActionAccounting
from trading.target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE


SYMBOL, CODE = '600001.SSE', 'sh.600001'
DAYS = ['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07',
        '2025-01-08', '2025-01-09', '2025-01-10', '2025-01-13']


def _actions():
    return pd.DataFrame([dict(symbol=SYMBOL, code=CODE, dividRegistDate=DAYS[index - 1],
        dividOperateDate=DAYS[index], dividPayDate=DAYS[index], dividStockMarketDate='',
        dividCashPsBeforeTax='0.1', dividStocksPs='0.000000', dividReserveToStockPs='',
        bonus_taxable_cash_ps='') for index in (2, 4)])


def _gaps(actions, evidence=()):
    gaps = pd.DataFrame([dict(symbol=SYMBOL, code=CODE, date=row.dividOperateDate,
        previous_observed_close=10.0 if index == 0 else 9.9,
        preclose=9.9 if index == 0 else 9.82, has_reported_distribution=True,
        **{field: row[field] for field in consumer.AMOUNTS[:3]})
        for index, row in actions.iterrows()])
    return reference_concordance(gaps, [], evidence)


def _pair(root, actions, parent=None, selected=()):
    event, reference = root / 'events', root / 'references'
    if parent:
        shutil.copytree(parent[0], event)
        shutil.copytree(parent[1], reference)
    else:
        event.mkdir(parents=True)
        reference.mkdir()
        for name in ('distribution_reference_evidence.json', 'cash_reference_evidence.json',
                     'reviewed_special_references.json'):
            save(reference / name, [])
    actions.to_parquet(event / 'canonical_actions.parquet', index=False)
    _gaps(actions).to_parquet(reference / 'raw_reference_changes.parquet', index=False)
    metadata = dict(status='research_action_events_projected', execution_admitted=False,
        start=DAYS[0], end=DAYS[-1], raw_view='synthetic_raw', source_view='synthetic_terms',
        expected_codes=1, queried=1, unqueried=0, canonical_events=len(actions),
        files={'canonical_actions.parquet': pin(event / 'canonical_actions.parquet')['sha256']})
    if parent:
        amendment = dict(parent_event_view=str(parent[0].resolve()), parent_reference_view=str(parent[1].resolve()),
            events=[list(key) for key in selected], raw_prices_rescanned=False, other_events_changed=False)
        metadata['reviewed_cash_amendment'] = amendment
    (event / 'result.json').write_text(json.dumps(metadata), encoding='utf-8')
    shutil.copy2(event / 'result.json', reference / 'input_events.json')
    refs = dict(status='research_reference_presence_diagnostic', execution_admitted=False,
        event_view=str(event.resolve()), raw_daily_sha256='synthetic_raw', price_view='synthetic_prices',
        canonical_panel_sha256='synthetic_prices', files={name: pin(reference / name)['sha256'] for name in
            ('raw_reference_changes.parquet', 'distribution_reference_evidence.json',
             'cash_reference_evidence.json', 'reviewed_special_references.json', 'input_events.json')})
    if parent:
        refs['reviewed_cash_amendment'] = amendment
    (reference / 'result.json').write_text(json.dumps(refs), encoding='utf-8')
    return event.resolve(), reference.resolve()


def _formula(reference, event, out, actions):
    shutil.copytree(reference, out)
    evidence = [dict(symbol=SYMBOL, code=CODE, dividOperateDate=DAYS[4],
        dividCashPsBeforeTax='0.1', dividStocksPs='0', dividReserveToStockPs='0',
        reference_adjustment_cash_ps='0.08', reference_adjustment_share_ratio='0')]
    (out / 'distribution_reference_evidence.json').write_text(json.dumps(evidence), encoding='utf-8')
    _gaps(actions, evidence).to_parquet(out / 'raw_reference_changes.parquet', index=False)
    value = read(reference / 'result.json')
    value['distribution_amendment'] = dict(parent_reference_view=str(reference.resolve()),
        events=[[CODE, DAYS[4]]], entitlements_changed=False, raw_prices_rescanned=False)
    for name in ('distribution_reference_evidence.json', 'raw_reference_changes.parquet'):
        value['files'][name] = pin(out / name)['sha256']
    (out / 'result.json').write_text(json.dumps(value), encoding='utf-8')
    return out.resolve()


def _account(day=DAYS[1]):
    return dict(day=day, entitlements={f'{SYMBOL}:{DAYS[2]}': 100}, receivables={},
        events=[dict(kind='corporate_action', symbol=SYMBOL, day='2024-06-04'),
                dict(kind='dividend_paid', symbol=SYMBOL, day='2024-06-04', cash=1880)])


def _fixture(tmp_path, monkeypatch):
    old_pair = _pair(tmp_path / 'old', _actions())
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    save(inputs / 'manifest.json', {'synthetic_fixture': True})
    save(inputs / 'cohort.json', {'scope_members': [SYMBOL]})
    batches = {day: dict(day=day, status='complete', reason='monthly_ranking',
        candidates=[dict(symbol=SYMBOL, score=1.0)]) for day in (DAYS[0], DAYS[3])}
    save(inputs / 'batches.json', batches)
    raw_days = ['2024-12-31'] + DAYS
    prices = [10.0, 10.0, 10.0, 9.9, 9.9, 9.82, 9.82, 9.82, 9.82]
    raw = pd.DataFrame([dict(date=day, code=CODE, symbol=SYMBOL, open=str(prices[index]),
        close=str(prices[index]), high=str(prices[index]), low=str(prices[index]),
        preclose=str(prices[index] if index in (3, 5) else prices[max(0, index - 1)]),
        volume='1000000', amount='100000000', tradestatus='1', isST='0', adjustflag='3')
        for index, day in enumerate(raw_days)])
    raw.iloc[:1].to_parquet(inputs / 'context.parquet', index=False)
    raw.iloc[1:].to_parquet(inputs / 'raw_daily.parquet', index=False)
    manifest = dict(source_days=DAYS, start=DAYS[0], end=DAYS[-2], through=DAYS[-1],
        context_day=raw_days[0], next_session_after_year='2025-01-14', raw_rows=len(DAYS))
    config = _config(100000, 5)
    plan = dict(config=config.to_dict(), action_event_view=pin(old_pair[0] / 'result.json'))
    legacy = tmp_path / 'legacy_registration.json'
    save(legacy, {'schema': consumer.previous.METHOD})
    legacy_value = dict(reference_append=dict(new_reference=pin(old_pair[1] / 'result.json')))
    # Only the external annual-input ancestor is mocked; all new registrations,
    # source-pair guards, checkpoints, Engine/Ledger transitions and chained loads run.
    monkeypatch.setattr(consumer.previous, '_load_registration', lambda path:
        (Path(path).resolve(), legacy_value, legacy, inputs, manifest, plan))
    qualification = IpoQualification({SYMBOL: dict(ipo_date='2000-01-01', board='main',
        available_at='2024-01-01T00:00:00+08:00', source_basis='synthetic_listing')},
        tuple(pd.bdate_range('2024-12-20', '2025-01-14').strftime('%Y-%m-%d')), {})
    monkeypatch.setattr(consumer.base_method, '_qualification', lambda *args: qualification)

    def accounting(event_view, reference_view, **kwargs):
        actions = pd.read_parquet(Path(event_view) / 'canonical_actions.parquet').fillna('').to_dict('records')
        universe = pd.DataFrame([dict(symbol=SYMBOL, code=CODE)])
        evidence = read(Path(reference_view) / 'distribution_reference_evidence.json')
        return StockDBActionAccounting(actions, [], evidence, {(CODE, 2025): True}, universe,
            {'rename': None}, kwargs['symbols'], kwargs['raw_days'], kwargs['replay_days'],
            dict(price_source=kwargs['price_source']))
    monkeypatch.setattr(consumer, 'load_action_accounting', accounting)
    parent = consumer._load_parent(legacy)
    checkpoints = []
    for case in consumer.CASES:
        folder = tmp_path / ('prefix_' + case['capital_policy'] + '_' + case['capacity'])
        feed, adapter, execution, _, _, _, _ = consumer._feed(parent, consumer._pair_pin(*old_pair), **case)
        result = baseline_ledger_result(feed, config, DAYS, folder,
            full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
        state = read(folder / 'account.json')
        assert state['day'] == DAYS[1] and result['status'] == 'failed'
        save(folder / 'result.json', dict(schema=consumer.previous.METHOD, **case, registration=pin(legacy),
            inputs=pin(inputs / 'manifest.json'), config=config.to_dict(), ledger_result=result,
            execution_context=consumer.RunContext('standalone_replay', consumer.TradingMode.REPLAY).to_dict(),
            source_blocks=adapter.blocks))
        save(folder / 'decisions.json', adapter.decisions)
        save(folder / 'capital_admission.json', adapter.admission)
        save(folder / 'execution_checks.json', execution.checks)
        save(folder / 'execution_exceptions.json', {'events': [event for event in state['events']
            if event['kind'] in consumer.previous.EXCEPTION_KINDS]})
        checkpoints.append(folder)
    return legacy, parent, old_pair, checkpoints


def test_two_stage_chain_preserves_registered_qty_pending_T1_and_full_engine_account(tmp_path, monkeypatch):
    legacy, parent, old_pair, checkpoints = _fixture(tmp_path, monkeypatch)
    original = {path: (path / 'account.json').read_bytes() for path in checkpoints}
    first = _actions()
    first.loc[0, ['dividStocksPs', 'dividReserveToStockPs']] = ['0', '0']
    first_pair = _pair(tmp_path / 'first', first, old_pair, [(SYMBOL, DAYS[2])])
    first_reg = tmp_path / 'first_registration'
    consumer.register(legacy, first_pair[1], checkpoints, first_reg, event_view=first_pair[0])
    first_results = []
    for case in consumer.CASES:
        folder = tmp_path / ('first_' + case['capital_policy'] + '_' + case['capacity'])
        result = consumer.run(first_reg / 'registration.json', folder, **case)
        assert result['reused_sessions'] == 2 and result['newly_completed_sessions'] == 2
        assert not result['complete_PnL_available'] and result['metrics'] is None
        state = read(folder / 'account.json')
        assert state['day'] == DAYS[3] and state['pending_targets']
        assert state['entitlements'][f'{SYMBOL}:{DAYS[4]}'] > 0
        first_results.append(folder)
    final = first.copy()
    final.loc[1, ['dividStocksPs', 'dividReserveToStockPs']] = ['0', '0']
    second_pair = _pair(tmp_path / 'second', final, first_pair, [(SYMBOL, DAYS[4])])
    final_ref = _formula(second_pair[1], second_pair[0], tmp_path / 'final_reference', final)
    second_reg = tmp_path / 'second_registration'
    registered = consumer.register(first_reg / 'registration.json', final_ref, first_results,
        second_reg, event_view=second_pair[0])
    assert registered['source_change']['kind'] == 'reviewed_cash_amendment_with_distribution_amendment'
    for case in consumer.CASES:
        folder = tmp_path / ('final_' + case['capital_policy'] + '_' + case['capacity'])
        result = consumer.run(second_reg / 'registration.json', folder, **case)
        assert result['complete_PnL_available'] and result['reused_sessions'] == 4
        assert result['newly_completed_sessions'] == 4 and not result['cash_reset']
        full = tmp_path / ('full_' + case['capital_policy'] + '_' + case['capacity'])
        feed, _, _, _, config, _, _ = consumer._feed(parent, consumer._pair_pin(second_pair[0], final_ref), **case)
        baseline_ledger_result(feed, config, DAYS, full, full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
        assert read(folder / 'account.json') == read(full / 'account.json')
        pd.testing.assert_frame_equal(pd.read_csv(folder / 'daily.csv'), pd.read_csv(full / 'daily.csv'), check_exact=True)
        assert read(folder / 'execution_checks.json')['scope'] == 'continuation_only_not_reconstructed_prefix'
    assert all((path / 'account.json').read_bytes() == raw for path, raw in original.items())


def test_reviewed_source_rejects_known_numeric_term_change(tmp_path):
    actions = _actions()
    old = _pair(tmp_path / 'old', actions)
    changed = actions.copy()
    changed.loc[0, ['dividCashPsBeforeTax', 'dividReserveToStockPs']] = ['0.2', '0']
    new = _pair(tmp_path / 'new', changed, old, [(SYMBOL, DAYS[2])])
    with pytest.raises(ValueError, match='Known action terms'):
        consumer.check_source_change(*old, *new, [_account()])


def test_only_unconsumed_event_key_accepts_past_record_and_rejects_economic_repairs():
    action = _actions().iloc[0].to_dict()
    account = _account()
    accepted = consumer.check_unconsumed_action(action, account)
    assert accepted['registered_quantity'] == 100 and accepted['existing_registration_preserved']
    for kind in ('unknown_record', 'unknown_pay', 'past_ex', 'missing_record_quantity', 'receivable', 'paid'):
        target, state = deepcopy(action), deepcopy(account)
        if kind == 'unknown_record': target['dividRegistDate'] = ''
        if kind == 'unknown_pay': target['dividPayDate'] = ''
        if kind == 'past_ex': state['day'] = DAYS[2]
        if kind == 'missing_record_quantity': state['entitlements'] = {}
        if kind == 'receivable': state['receivables'][f'{SYMBOL}:{DAYS[2]}'] = 800
        if kind == 'paid': state['events'].append(dict(kind='dividend_paid', symbol=SYMBOL, day=DAYS[2]))
        with pytest.raises(ValueError):
            consumer.check_unconsumed_action(target, state)


def test_reviewed_pair_rejects_changed_raw_price_despite_valid_new_hash(tmp_path):
    actions = _actions()
    old = _pair(tmp_path / 'old', actions)
    fixed = actions.copy()
    fixed.loc[0, 'dividReserveToStockPs'] = '0'
    new = _pair(tmp_path / 'new', fixed, old, [(SYMBOL, DAYS[2])])
    gaps = pd.read_parquet(new[1] / 'raw_reference_changes.parquet')
    gaps.loc[0, 'preclose'] = 9.91
    gaps.to_parquet(new[1] / 'raw_reference_changes.parquet', index=False)
    value = read(new[1] / 'result.json')
    value['files']['raw_reference_changes.parquet'] = pin(new[1] / 'raw_reference_changes.parquet')['sha256']
    (new[1] / 'result.json').write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='Raw|unrelated'):
        consumer.check_source_change(*old, *new, [_account()])
