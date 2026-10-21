"""Native32 supplement projection fixtures only; no market sources or Engine."""
from copy import deepcopy
from pathlib import Path

from trading.research import leader_eight_type_history_view as view
from trading.research.leader_eight_type_action_supplement import METHOD, SPEC
from trading.research.tests.test_leader_eight_type_history_view import fixture_history, save, SELECTED
from trading.research.tests.test_leader_history_annual_view import annual_fixture


REPLAYED = ['relay_continuation_leader__unguarded__uncapped',
            'liquidity_capacity_proxy__unguarded__causal_first_seen_cap20',
            'liquidity_capacity_proxy__unguarded__uncapped']


def supplement_fixture(tmp_path):
    original_path, original = annual_fixture(tmp_path)
    old_pair = {key: str((tmp_path / name).resolve()) for key, name in
                (('action_event_view', 'old_events'), ('action_reference_view', 'old_references'))}
    new_pair = {key: str((tmp_path / name).resolve()) for key, name in
                (('action_event_view', 'new_events'), ('action_reference_view', 'new_references'))}
    eligible = [case_id for case_id in view.CASE_IDS if case_id not in REPLAYED
                and not case_id.startswith(('cross_regime_candidate__', 'sector_catchup_leader__'))]
    complete_ids = set(eligible[:15])
    for case in original['cases']:
        complete = case['case_id'] in complete_ids
        case.update(complete_PnL_available=complete,
            status='completed_conditional_mechanical_replay' if complete else 'no_complete_PnL',
            metrics={'net_return': -.031, 'max_drawdown': -.09} if complete else None,
            historical_execution_policy={'accounting': {'event_view': old_pair['action_event_view'],
                'reference_view': old_pair['action_reference_view']}})
    save(original_path, original)
    _, original_ref = view._read(original_path)
    new = deepcopy(original)
    for case in new['cases']:
        replayed = case['case_id'] in REPLAYED
        own_pair = new_pair if replayed else old_pair
        origin = {'mode': 'replayed' if replayed else 'reused', 'original_result': original_ref,
            'original_case_id': case['case_id'], 'action_pair': own_pair,
            'replayed_from_original_cash': replayed, 'checkpoint_recovery_performed': False,
            'late_evidence_not_historical_PIT': replayed}
        if replayed:
            origin['committed_prefix_check'] = {'status': 'exact_original_committed_prefix_preserved'}
            case['historical_execution_policy']['accounting'] = {
                'event_view': new_pair['action_event_view'], 'reference_view': new_pair['action_reference_view']}
        if case['case_id'] == REPLAYED[0]:
            case.update(complete_PnL_available=True, status='completed_conditional_mechanical_replay',
                metrics={'net_return': -.123, 'max_drawdown': -.2})
        case['source_origin'] = origin
    binding = {'replayed_case_ids': REPLAYED,
        'reused_case_ids': [case_id for case_id in view.CASE_IDS if case_id not in REPLAYED],
        'original_pair': {key: {'directory': value} for key, value in old_pair.items()}}
    registration_path = tmp_path / 'registration' / 'registration.json'
    registration = {'schema': METHOD + '_registration', 'method_id': METHOD, 'spec': deepcopy(SPEC),
        'binding': binding, 'case_ids': list(view.CASE_IDS), 'config': deepcopy(original['config']),
        'original_result': original_ref,
        **{key: {'directory': value} for key, value in new_pair.items()}, **view.FLAGS}
    save(registration_path, registration)
    _, registration_ref = view._read(registration_path)
    new.update(schema=METHOD + '_result', method_id=METHOD, spec=deepcopy(SPEC),
        registration=registration_ref, original_result=original_ref, source_action_supplement=binding,
        replayed_case_count=3, reused_case_count=29, new_action_pair_scope='replayed_case_ids_only',
        historical_execution_policy_scope='replayed_case_ids_only', late_evidence_not_historical_PIT=True,
        original_results_modified=False, new_action_pair={key: {'directory': value} for key, value in new_pair.items()})
    result_path = tmp_path / 'supplement' / 'result.json'
    save(result_path, new)
    _, result_ref = view._read(result_path)
    save(result_path.parent / 'progress.json', {'status': 'completed', 'method_id': METHOD,
        'registration': registration_ref, 'active_case': None, 'result': result_ref,
        'completed_cases': [{'case_id': case_id} for case_id in view.CASE_IDS]})
    return original_path, new, result_path


def test_native32_appends_existing_reader_window_preserves_original_year_months_nulls_and_own_pairs(tmp_path, monkeypatch):
    indexes, monthly, data, _ = fixture_history(tmp_path)
    view.build(indexes, monthly)
    original, source, supplement = supplement_fixture(tmp_path)
    annual = monthly.parent / 'with_annual'
    view.build_annual(original, monthly, annual)
    view.publish(annual, data)
    prior_month = view.read_history(data, '2026-01', SELECTED)['detail']
    prior_annual = view.read_history(data, 'annual32_2025', view.CASE_IDS[0])['detail']
    previous_bytes = {path.relative_to(annual): path.read_bytes() for path in annual.rglob('*.json')}
    output, read = monthly.parent / 'with_native_supplement', view._read

    def no_old_sources(path):
        assert not Path(path).resolve().is_relative_to((tmp_path / 'sources').resolve())
        assert not Path(path).resolve().is_relative_to(original.parent.resolve())
        assert Path(path).suffix != '.parquet' and Path(path).name != 'panels.json'
        return read(path)

    monkeypatch.setattr(view, '_read', no_old_sources)
    built = view.build_native_supplement(supplement, annual, output)
    assert built['case_count'] == 128
    view.publish(output, data)
    assert all((annual / path).read_bytes() == raw for path, raw in previous_bytes.items())

    def only_projection(path):
        assert Path(path).resolve().is_relative_to(data.resolve())
        return read(path)

    monkeypatch.setattr(view, '_read', only_projection)
    result = view.read_history(data, 'native_action_supplement_2025', REPLAYED[0])
    assert result['status'] == 'saved_history_projection'
    window = next(row for row in result['windows'] if row['id'] == 'native_action_supplement_2025')
    assert window.get('source_family') is None
    assert window['source_supplement_counts'] == {'replayed': 3, 'reused': 29}
    assert '同样本后见补证' in window['label'] and '非32个新独立账户' in window['label']
    assert window['independent_account'] is False and window['equity_series_concatenated'] is False
    assert sum(row['metrics'] is not None for row in window['case_rows']) == 16
    assert sum(row['metrics'] is None for row in window['case_rows']) == 16
    rows = {row['case_id']: row for row in window['case_rows']}
    assert rows[REPLAYED[0]]['source_result'] == str(supplement.resolve())
    reused = view.CASE_IDS[0]
    assert rows[reused]['source_result'] == str(original.resolve())
    assert rows[reused]['source_supplement']['mode'] == 'reused'
    assert rows[reused]['own_action_accounting_pair'] != rows[REPLAYED[0]]['own_action_accounting_pair']
    assert result['detail']['summary']['metrics'] == source['cases'][view.CASE_IDS.index(REPLAYED[0])]['metrics']
    assert view.read_history(data, '2026-01', SELECTED)['detail'] == prior_month
    assert view.read_history(data, 'annual32_2025', view.CASE_IDS[0])['detail'] == prior_annual
    assert all(result[key] is False for key in ('engine_advanced', 'ledger_mutated', 'profits_recomputed',
        'provider_contacted', 'execution_admitted', 'returns_reportable', 'pit_verified'))


def test_missing_or_unfinished_native_supplement_stays_pending_and_never_publishes(tmp_path):
    indexes, monthly, data, _ = fixture_history(tmp_path)
    view.build(indexes, monthly)
    missing = tmp_path / 'missing' / 'result.json'
    output = monthly.parent / 'pending_native'
    pending = view.build_native_supplement(missing, monthly, output)
    assert pending['status'] == 'pending' and pending['metrics'] is None and not output.exists()
    _, _, path = supplement_fixture(tmp_path)
    progress, _ = view._read(path.parent / 'progress.json')
    progress['status'] = 'running'
    save(path.parent / 'progress.json', progress)
    assert view.main(['--native-supplement-result', str(path), '--reuse-projection', str(monthly),
        '--out', str(output), '--publish', '--data-root', str(data)]) == 0
    assert not output.exists() and not (monthly.parent / 'current.json').exists()
    current = view.read_history(data, '2026-01', SELECTED)
    assert current['status'] == 'saved_history_projection'
    assert current['detail']['summary']['metrics'] == {'net_return': .0123}
    assert not any(row['id'] == 'native_action_supplement_2025' for row in current['windows'])
