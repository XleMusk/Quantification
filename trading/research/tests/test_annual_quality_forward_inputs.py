"""Tiny saved inputs only; no historical feature, strategy or account replay."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from trading.research import annual_quality_forward_inputs as m
from trading.research.prospective_research import validate_extension


OLD = ['2026-09-29', '2026-09-30']
NEW = ['2026-10-08', '2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16']
A, B, C = 'sh.600001', 'sz.000002', 'sz.301660'
FIELDS = BAOSTOCK_FIELDS.split(',')


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def rows(day, extra=False):
    result = []
    for code in ([A, B, C] if extra else [A, B]):
        row = {field: '' for field in FIELDS}
        row.update(date=day, code=code, open='10', high='10.1', low='9.9', close='10', preclose='10',
            volume='1000000', amount='10000000', adjustflag='3', tradestatus='1', isST='0')
        if day == NEW[1] and code == B:
            row['preclose'] = '9.8'
        result.append(row)
    return pd.DataFrame(result, columns=FIELDS)


def adjusted(frame):
    result = frame.copy()
    result['adjustflag'] = '2'
    result['factor_effective_date'] = OLD[0]
    result['source_forward_factor'] = '1'
    result['security_id'] = result.code
    result['tradable_code'] = result.code
    result['identity_origin'] = 'unresolved_provider_code'
    result['alias_shadow'] = False
    return result


def panel(path, frames):
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.Table.from_pandas(frames[0], preserve_index=False).schema
    with pq.ParquetWriter(path, schema) as writer:
        for frame in frames:
            writer.write_table(pa.Table.from_pandas(frame, schema=schema, preserve_index=False))


@pytest.fixture
def source(tmp_path, monkeypatch):
    raw, price, event, reference, ext, new_event, new_ref = [tmp_path / name for name in
        ('old_raw', 'old_price', 'old_event', 'old_ref', 'ext_price', 'new_event', 'new_ref')]
    for path in (raw, price, event, reference, ext, new_event, new_ref):
        path.mkdir()
    panel(raw / 'raw_daily.parquet', [rows(day) for day in OLD])
    panel(price / 'canonical_daily.parquet', [adjusted(rows(day)) for day in OLD])
    panel(ext / 'canonical_daily.parquet', [adjusted(rows(day, extra=True)) for day in OLD + NEW[:4]])
    pd.DataFrame({'code': [A, B]}).to_csv(event / 'source_input_universe.csv', index=False)
    pd.DataFrame({'date': OLD}).to_csv(event / 'source_input_calendar.csv', index=False)
    pd.DataFrame({'date': OLD + NEW[:4]}).to_csv(new_event / 'source_input_calendar.csv', index=False)
    master = tmp_path / 'master.csv'
    pd.DataFrame({'code': [A, B], 'ipoDate': ['2000-01-01'] * 2}).to_csv(master, index=False)
    actions = pd.DataFrame([{'code': A, 'symbol': '600001.SSE', 'dividRegistDate': OLD[0],
        'dividOperateDate': OLD[1], 'dividPayDate': OLD[1], 'dividStockMarketDate': '',
        'dividCashPsBeforeTax': '0.1', 'dividStocksPs': '0', 'dividReserveToStockPs': '',
        'reference_adjustment_cash_ps': '', 'source_pages': ''}])
    actions.to_parquet(event / 'canonical_actions.parquet', index=False)
    changed = actions.copy()
    changed.loc[0, 'dividCashPsBeforeTax'] = '0.8'
    new = {'code': B, 'symbol': '000002.SZSE', 'dividRegistDate': NEW[0],
        'dividOperateDate': NEW[1], 'dividPayDate': NEW[1], 'dividStockMarketDate': '',
        'dividCashPsBeforeTax': '0.2', 'dividStocksPs': '0', 'dividReserveToStockPs': '0'}
    pd.concat([changed.drop(columns=['reference_adjustment_cash_ps', 'source_pages']),
        pd.DataFrame([new])], ignore_index=True).to_parquet(new_event / 'canonical_actions.parquet', index=False)
    outside = {**new, 'code': A, 'symbol': '600001.SSE', 'dividRegistDate': NEW[4],
        'dividOperateDate': NEW[5], 'dividPayDate': NEW[5], 'dividCashPsBeforeTax': ''}
    pd.DataFrame([outside]).to_parquet(new_event / 'outside_window_actions.parquet', index=False)
    refs = pd.DataFrame([{'code': A, 'date': OLD[1], 'reference_formula_status': 'unresolved_blank_terms',
        'previous_observed_close': 10.0, 'preclose': 9.9, 'close': 10.0,
        'reference_delta': -.1, 'tradestatus': 1, 'reference_formula_basis': '', 'reference_error_rmb': .1}])
    refs.to_parquet(reference / 'raw_reference_changes.parquet', index=False)
    pd.DataFrame([{**refs.iloc[0].to_dict(), 'code': B, 'date': NEW[1],
        'reference_formula_status': 'unresolved_blank_terms', 'reference_error_rmb': float('nan')}]).to_parquet(new_ref / 'raw_reference_changes.parquet', index=False)
    write(reference / 'reviewed_special_references.json', [{'synthetic_original_unknown': True}])
    write(raw / 'result.json', {'status': 'source_cache_complete'})
    old_price = {'status': 'research_reconstruction_completed', 'raw_view': str(raw),
        'canonical_panel_sha256': m._digest(price / 'canonical_daily.parquet'),
        'source_hashes': {'raw': m._digest(raw / 'raw_daily.parquet')}, 'cutoff': OLD[-1]}
    write(price / 'result.json', old_price)
    old_event = {'status': 'research_action_events_projected', 'unqueried': 0,
        'raw_view': str(raw), 'end': OLD[-1], 'files': {}}
    write(event / 'result.json', old_event)
    write(reference / 'result.json', {'status': 'research_reference_presence_diagnostic',
        'event_view': str(event), 'price_view': str(price), 'files': {}})
    write(ext / 'result.json', {'status': 'research_reconstruction_completed', 'cutoff': NEW[3],
        'canonical_panel_sha256': m._digest(ext / 'canonical_daily.parquet')})
    write(new_event / 'result.json', {'status': 'research_action_events_projected', 'unqueried': 0,
        'end': NEW[3], 'files': {}})
    write(new_ref / 'result.json', {'status': 'research_reference_presence_diagnostic',
        'event_view': str(new_event), 'price_view': str(ext)})
    manifest = {'scope': 'full_source_sh_sz_research_only', 'source_codes': 2, 'canonical_codes': 2,
        'unsupported_instruments': {}, 'selection_date': '2023-01-01', 'end': OLD[-1],
        'price_view': str(price), 'raw_view': str(raw), 'event_view': str(event),
        'reference_view': str(reference), 'listing_master': str(master)}
    prepared = tmp_path / 'prepared'
    write(prepared / 'protocol.json', {'definition_id': 'annual_quality_investment_same_pool_prospective_v1',
        'execution_admitted': False, 'declared_at': '2026-10-05T06:11:12+08:00',
        'preview_market_day': OLD[-1], 'signals': list('abcde'), 'manifest': manifest,
        'stock_inputs': {key: manifest[key] for key in ('price_view', 'event_view', 'reference_view', 'listing_master')}})
    calendar = tmp_path / 'calendar.json'
    write(calendar, {'schema': 'cn_a_share_exchange_calendar_v1', 'year': 2026,
        'closed_ranges': ['2026-10-01/2026-10-07'], 'open_sessions': []})

    class Native:
        def __init__(self, base, extensions, full_closes, *, calendar):
            self.calendar = OLD + NEW
            self.provenance = {'source_available_at': '2026-10-17T18:00:00+08:00',
                'day_sources': [{'day': day, 'source_available_at': '2026-10-17T18:00:00+08:00'} for day in self.calendar]}

        def frame(self, day):
            frame = rows(day, extra=True)
            if day in NEW[4:]:
                frame = frame.drop(columns=['turn', 'peTTM', 'pbMRQ', 'psTTM', 'pcfNcfTTM'])
            frame.attrs['provenance'] = {'original_raw_fields': frame.columns.tolist()}
            return frame

        def close(self):
            pass

    monkeypatch.setattr(m, 'NativeHistoryChainSource', Native)
    kwargs = dict(prepared_run=prepared, native_extension=tmp_path / 'native_extension',
        adjusted_extension=ext, full_closes=[tmp_path / 'strict_close'], action_event=new_event,
        action_reference=new_ref, calendar=calendar, out=tmp_path / 'out', through=NEW[-1])
    return kwargs, manifest


def test_exact_prefix_scoped_new_raw_unknown_adjusted_and_original_action_schema(source):
    kwargs, before = source
    old_refs = (Path(before['reference_view']) / 'reviewed_special_references.json').read_bytes()
    directory, result = m.build(**kwargs)
    inputs = result['stock_inputs']
    after = {**before, **inputs, 'raw_view': str(directory / 'raw'), 'end': NEW[-1],
        'replay_only': True, 'execution_admitted': False}
    validate_extension(before, after, OLD[-1])
    assert result['new_days'] == NEW and result['source_codes'] == result['canonical_codes'] == 2
    assert result['raw_close_coverage_through'] == NEW[-1]
    assert result['adjusted_price_feature_coverage_through'] == NEW[3]
    raw = pq.ParquetFile(directory / 'raw' / 'raw_daily.parquet')
    adj = pq.ParquetFile(directory / 'prices' / 'canonical_daily.parquet')
    assert raw.num_row_groups == adj.num_row_groups == len(OLD + NEW)
    last = raw.read_row_group(len(OLD + NEW) - 1).to_pandas()
    last_adj = adj.read_row_group(len(OLD + NEW) - 1).to_pandas()
    assert set(last.code) == {A, B} and last.turn.isna().all() and last.close.eq('10').all()
    assert last_adj[list(m.PRICE_FIELDS)].isna().all().all()
    assert last_adj.volume.eq('1000000').all() and last_adj.source_forward_factor.isna().all()
    action = pd.read_parquet(directory / 'events' / 'canonical_actions.parquet')
    assert len(action) == 3 and action.iloc[0].dividCashPsBeforeTax == '0.1'
    assert action.loc[action.dividOperateDate.eq(NEW[5]), 'dividCashPsBeforeTax'].iloc[0] == ''
    assert (directory / 'references' / 'reviewed_special_references.json').read_bytes() == old_refs
    observed = m._read(directory / 'source_observations.json')
    assert all(row['excluded_new_source_codes'] == [C] for row in observed['days'])
    assert observed['new_reference_original_rows'][0]['reference_error_rmb'] is None
    assert not result['engine_called'] and not result['account_created']
    # Prefix filtering must ignore later null-induced pandas dtype changes,
    # while a real historical numeric revision still fails the same guard.
    ref_path = directory / 'references' / 'raw_reference_changes.parquet'
    changed = pq.read_table(ref_path)
    frame = changed.to_pandas()
    frame.loc[frame.date.eq(OLD[-1]), 'reference_error_rmb'] = .2
    pq.write_table(pa.Table.from_pandas(frame, schema=changed.schema, preserve_index=False), ref_path)
    with pytest.raises(ValueError, match='Past price references'):
        validate_extension(before, after, OLD[-1])


def test_changed_original_price_or_adjusted_source_rejects_before_output(source):
    kwargs, before = source
    path = Path(kwargs['adjusted_extension']) / 'canonical_daily.parquet'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='source bytes changed'):
        m.build(**kwargs)
    assert not kwargs['out'].exists()


def test_unknown_reference_is_saved_without_zero_or_independent_match_inference(source):
    kwargs, _ = source
    # Force a second new raw discrepancy, without changing existing saved refs.
    factory = m.NativeHistoryChainSource

    class Changed(factory):
        def frame(self, day):
            frame = super().frame(day)
            if day == NEW[4]:
                frame.loc[frame.code.eq(A), 'preclose'] = '9.5'
            return frame

    m.NativeHistoryChainSource = Changed
    try:
        directory, _ = m.build(**kwargs)
    finally:
        m.NativeHistoryChainSource = factory
    refs = pd.read_parquet(directory / 'references' / 'raw_reference_changes.parquet')
    row = refs.loc[refs.code.eq(A) & refs.date.eq(NEW[4])].iloc[0]
    assert row.reference_formula_status == 'unresolved_incremental_raw_reference_change'
    assert row.preclose == 9.5 and row.previous_observed_close == 10.0


def test_output_cannot_overlap_original_declaration_or_roll_to_unobserved_day(source):
    kwargs, _ = source
    original = kwargs['out']
    kwargs['out'] = kwargs['prepared_run'] / 'new_input'
    with pytest.raises(ValueError, match='separate'):
        m.build(**kwargs)
    kwargs['out'] = original
    kwargs['through'] = '2026-10-19'
    with pytest.raises(ValueError, match='already observed'):
        m.build(**kwargs)
    assert not original.exists()
