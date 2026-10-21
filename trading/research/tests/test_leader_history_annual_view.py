"""Annual saved-result compatibility only; no panels, producer or Engine run."""
from copy import deepcopy
from pathlib import Path

from trading.research import leader_eight_type_history_view as view
from trading.research.leader_eight_type_panel_replay import SCHEMA as REPLAY_SCHEMA
from trading.research.shortline_panel_replay import _config
from trading.research.tests.test_leader_eight_type_history_view import fixture_history, source, save, SELECTED
from trading.research.tests.test_leader_history_ablation_view import saved_ablation


TREND = 'trend_leader__unguarded__uncapped'


def annual_fixture(tmp_path):
    path, record = source(tmp_path / 'annual', '2025-01', view.CASE_IDS, .042)
    original_days = record['source_days']
    replacements = dict(zip(original_days, ['2025-01-02', '2025-01-03']))

    def dated(value):
        if isinstance(value, dict):
            return {replacements.get(key, key): dated(item) for key, item in value.items()}
        if isinstance(value, list):
            return [dated(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    record = dated(record)
    days = ['2025-01-02', '2025-01-03', '2025-12-23', '2025-12-24',
            '2025-12-25', '2025-12-26', '2025-12-29', '2025-12-30', '2025-12-31']
    config = _config(100000, 5).to_dict()
    record.update(schema=REPLAY_SCHEMA, status='completed_research_only_mechanical_comparison',
        start=days[0], end='2025-12-23', source_days=days, config=config, case_count=32,
        scope_members=['600001.SSE', '600002.SSE'])
    for day in days[2:]:
        record['environment_decisions'][day] = deepcopy(record['environment_decisions'][days[0]])
        record['environment_decisions'][day]['day'] = day
    for case in record['cases']:
        case.update(config=deepcopy(config), engine_sessions_committed=len(case['daily']),
            complete_PnL_available=False, metrics=None)
        if case['case_id'] == TREND:
            case.update(metrics={'net_return': .042, 'max_drawdown': -.10}, complete_PnL_available=True)
    save(path, record)
    save(path.parent / 'historical_method.json', {
        'schema': 'leader_eight_type_historical_native_result_v1',
        'method_id': 'fixed_eight_types_observed_native_posthoc_listing_metadata_research_v1',
        'shared_engine_result': str(path.resolve()), 'original_32_protocol_unchanged': True,
        'cases': 32, **view.FLAGS})
    return path, record


def test_independent_year_uses_existing_reader_and_preserves_all_2026_payloads_and_nulls(tmp_path, monkeypatch):
    indexes, old, data, _ = fixture_history(tmp_path)
    view.build(indexes, old)
    ablation, _ = saved_ablation(tmp_path)
    mixed = old.parent / 'old_mixed_2026'
    view.build_ablation(ablation, old, mixed)
    view.publish(mixed, data)
    previous = view.read_history(data, '2026-01', SELECTED)
    old_bytes = {path.relative_to(mixed): path.read_bytes() for path in mixed.rglob('*.json')}
    annual, source_record = annual_fixture(tmp_path)
    output = mixed.parent / 'with_independent_2025'
    original_read = view._read

    def no_old_sources(path):
        assert not Path(path).resolve().is_relative_to((tmp_path / 'sources').resolve())
        assert Path(path).name != 'panels.json'
        return original_read(path)

    monkeypatch.setattr(view, '_read', no_old_sources)
    built = view.build_annual(annual, mixed, output)
    assert built['case_count'] == 64 + 48 + 32
    assert view.publish(output, data)['case_count'] == 144
    assert all((mixed / path).read_bytes() == content for path, content in old_bytes.items())

    def projection_only(path):
        assert Path(path).resolve().is_relative_to(data.resolve())
        return original_read(path)

    monkeypatch.setattr(view, '_read', projection_only)
    result = view.read_history(data, 'annual32_2025', TREND)
    assert result['status'] == 'saved_history_projection'
    annual_window = next(row for row in result['windows'] if row['id'] == 'annual32_2025')
    assert annual_window['period_kind'] == 'independent_annual'
    assert '独立年度' in annual_window['label'] and '2025' in annual_window['label']
    assert annual_window['equity_series_concatenated'] is False
    assert annual_window.get('source_family') is None  # Already-running reader's original32 branch.
    assert annual_window['source_scope_count'] == 2 and annual_window['source_sessions'] == 9
    assert len(annual_window['tail_days']) == 6
    assert result['detail']['summary']['metrics'] == source_record['cases'][view.CASE_IDS.index(TREND)]['metrics']
    assert all(row['metrics'] is None for row in annual_window['case_rows']
               if row['type'] in {'cross_regime_candidate', 'sector_catchup_leader'})
    assert view.read_history(data, '2026-01', SELECTED)['detail'] == previous['detail']
    assert len(result['windows']) == 4
    assert all(result[key] is False for key in ('engine_advanced', 'ledger_mutated', 'profits_recomputed',
        'provider_contacted', 'execution_admitted', 'returns_reportable', 'pit_verified'))


def test_missing_or_running_annual_source_stays_pending_without_projection_or_publication(tmp_path):
    indexes, old, data, _ = fixture_history(tmp_path)
    view.build(indexes, old)
    missing, output = tmp_path / 'not_yet_saved/result.json', old.parent / 'annual_pending'
    pending = view.build_annual(missing, old, output)
    assert pending['status'] == 'pending' and pending['metrics'] is None
    assert pending['projection_created'] is False and not output.exists()
    annual, _ = annual_fixture(tmp_path)
    (annual.parent / 'historical_method.json').unlink()  # Result alone is not terminal native output.
    assert view.main(['--annual-result', str(annual), '--reuse-projection', str(old), '--out', str(output),
        '--publish', '--data-root', str(data)]) == 0
    assert not output.exists() and not (old.parent / 'current.json').exists()
    current = view.read_history(data, '2026-01', SELECTED)
    assert current['detail']['summary']['metrics'] == {'net_return': .0123}
    unknown = view.read_history(data, 'annual32_2025', TREND)
    assert unknown['status'] == 'invalid_selection' and unknown['detail'] is None
    assert not any(row['id'] == 'annual32_2025' for row in unknown['windows'])
