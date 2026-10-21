"""Append saved close inputs to an unchanged annual-quality declaration.

The original price/action prefix is copied, never reconstructed from a newer
source. Later native quotes remain raw. A separately supplied adjusted view
may supply only its own new dates; subsequent raw-only dates have unknown
adjusted prices and cannot masquerade as factor observations.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..symbols import normalize_symbol
from .leader_daily_signals import _calendar_view
from .native_history_chain_source import NativeHistoryChainSource


SCHEMA = 'annual_quality_fixed_prefix_forward_inputs_v1'
METHOD = 'annual_quality_original_prefix_saved_dated_raw_unknown_adjusted_append_v1'
PRICE_FIELDS = ('open', 'high', 'low', 'close', 'preclose')
ACTION_DATES = ('dividRegistDate', 'dividOperateDate', 'dividPayDate', 'dividStockMarketDate')
FLAGS = {'research_only': True, 'candidate_panel_only': True, 'execution_admitted': False,
    'selection_enabled': False, 'strategy_validated': False, 'pit_verified': False,
    'returns_reportable': False, 'provider_contacted': False, 'engine_called': False,
    'ledger_mutated': False, 'account_created': False, 'old_sources_modified': False}


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _digest(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _ref(path, expected=None):
    path = Path(path).resolve()
    value = {'path': str(path), 'sha256': _digest(path)}
    if expected is not None and value['sha256'] != expected:
        raise ValueError('Pinned annual forward source bytes changed: ' + path.name)
    return value


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _table(frame, schema):
    """Unknown new columns remain nullable; original strings are not rescaled."""
    selected = frame.reindex(columns=schema.names).copy()
    for field in schema:
        name = field.name
        selected[name] = selected[name].map(lambda value: None if pd.isna(value) else value)
        if pa.types.is_floating(field.type):
            selected[name] = pd.to_numeric(selected[name], errors='raise')
    return pa.Table.from_pandas(selected, schema=schema, preserve_index=False)


def _project_append(old, incoming, keys, cutoff, date_fields, codes):
    """Keep every original row/column, including original unknowns and failures."""
    seen = set(map(tuple, old[list(keys)].astype(str).itertuples(index=False, name=None)))
    additions, skipped = [], []
    for original in incoming.to_dict('records'):
        row = {key: '' if pd.isna(value) else value for key, value in original.items()}
        if row.get('code') not in codes:
            skipped.append({'reason': 'outside_original_source_scope', 'row': row})
            continue
        key = tuple(str(row.get(field, '')) for field in keys)
        relevant = any(row.get(field, '') > cutoff for field in date_fields)
        if key in seen or not relevant:
            # No new-source revision is allowed to replace even a future row
            # already included by the original declaration.
            continue
        if any(row.get(field, '') and row[field] <= cutoff for field in date_fields):
            skipped.append({'reason': 'would_change_original_economic_prefix', 'row': row})
            continue
        projected = {field: row.get(field) for field in old.columns}
        additions.append(projected)
        seen.add(key)
    return pd.concat([old, pd.DataFrame(additions, columns=old.columns)], ignore_index=True), additions, skipped


def _future_action_rows(event, cutoff, through):
    meta = _read(event / 'result.json')
    files = [event / 'canonical_actions.parquet']
    if (event / 'outside_window_actions.parquet').is_file():
        files.append(event / 'outside_window_actions.parquet')
    frames, refs = [], []
    for path in files:
        refs.append(_ref(path, meta.get('files', {}).get(path.name)))
        frame = pd.read_parquet(path).fillna('')
        relevant = pd.Series(False, index=frame.index)
        for field in ACTION_DATES:
            if field in frame:
                relevant |= frame[field].between(cutoff, through, inclusive='right')
        frame = frame.loc[relevant].copy()
        if len(frame):
            # Window-external source returns retain original provider columns.
            # Canonical fields are identity projections, not filled economics.
            frame['symbol'] = frame.code.map(lambda code: normalize_symbol(code).vt_symbol)
            for field in ('source_code', 'security_id', 'tradable_code', 'registration_code', 'payment_code'):
                if field not in frame:
                    frame[field] = frame.code
            if 'source_symbol' not in frame:
                frame['source_symbol'] = frame.symbol
            if 'identity_origin' not in frame:
                frame['identity_origin'] = 'unresolved_provider_code'
            frames.append(frame)
    combined = (pd.concat(frames, ignore_index=True, sort=False) if frames
        else pd.DataFrame(columns=['code', 'symbol', *ACTION_DATES]))
    if combined.duplicated(['symbol', 'dividOperateDate']).any():
        raise ValueError('Duplicate new action returns require explicit source reconciliation')
    return combined, refs


def _raw_frame(source, day, codes, schema):
    frame = source.frame(day)
    if frame.empty or frame.code.duplicated().any() or not frame.date.eq(day).all():
        raise ValueError('One exact distinct-code source row group required: ' + day)
    excluded = sorted(set(frame.code) - codes)
    selected = frame.loc[frame.code.isin(codes)].copy()
    if selected.empty or not selected.adjustflag.eq('3').all():
        raise ValueError('New raw source must retain its observed raw basis: ' + day)
    fields = frame.attrs.get('provenance', {}).get('original_raw_fields', frame.columns.tolist())
    missing = [field for field in schema.names if field not in frame]
    return selected.reindex(columns=schema.names), excluded, missing, fields


def _unknown_adjusted(raw, schema, rename):
    result = raw.copy(deep=True)
    result['adjustflag'] = '2'
    for field in PRICE_FIELDS:
        result[field] = None
    # Identity annotations are deterministic and remain separate from prices.
    result['security_id'] = result.code
    result['tradable_code'] = result.code
    result['identity_origin'] = 'unresolved_provider_code'
    result['alias_shadow'] = False
    if rename:
        alias = result.code.isin([rename['old'], rename['new']])
        result.loc[alias, 'security_id'] = rename['old']
        result.loc[alias, 'identity_origin'] = 'reviewed_dated_code_change'
        result.loc[result.code.eq(rename['old']), 'alias_shadow'] = True
        result = result.loc[~result.alias_shadow].copy()
    return result.reindex(columns=schema.names)


def _new_reference_rows(previous, current, day, known, schema):
    """Observed raw discrepancies are unresolved unless already saved as such.

    This does not infer an action from a price difference or use price agreement
    to prove a missing term zero. It only supplies the existing BulkFeed stop.
    """
    rows = []
    prior = previous.set_index('code') if previous is not None else pd.DataFrame()
    for row in current.to_dict('records'):
        code = row['code']
        if (code, day) in known or row.get('tradestatus') != '1':
            continue
        preclose = _decimal(row.get('preclose'))
        old = _decimal(prior.at[code, 'close']) if code in prior.index else None
        if preclose is None or old is None:
            reason = 'incremental_exact_previous_raw_reference_unknown'
        elif preclose == old:
            continue
        else:
            reason = 'unresolved_incremental_raw_reference_change'
        item = {field: None for field in schema.names}
        item.update(code=code, date=day, reference_formula_status=reason)
        for field, value in (('close', _decimal(row.get('close'))), ('preclose', preclose),
                ('previous_observed_close', old), ('reference_delta', preclose - old if preclose and old else None)):
            if field in item:
                item[field] = float(value) if value is not None else None
        if 'tradestatus' in item:
            item['tradestatus'] = 1
        if 'symbol' in item:
            item['symbol'] = normalize_symbol(code).vt_symbol
        # Preserve fields used by the existing reference audit when present.
        for field in ('previous_raw_close', 'previous_close', 'raw_previous_close'):
            if field in item:
                item[field] = str(old) if old is not None else None
        for field in ('current_raw_preclose', 'raw_preclose'):
            if field in item:
                item[field] = str(preclose) if preclose is not None else None
        if 'reference_formula_basis' in item:
            item['reference_formula_basis'] = 'new_raw_observation_not_independent_action_evidence'
        rows.append(item)
    return pd.DataFrame(rows, columns=schema.names)


def build(prepared_run, *, native_extension, adjusted_extension, full_closes, action_event,
          action_reference, calendar, out, through=None):
    """Build inputs only; the caller separately advances the original five books."""
    prepared = Path(prepared_run).resolve()
    protocol_path = prepared / 'protocol.json'
    protocol = _read(protocol_path)
    old = protocol['stock_inputs']
    cutoff = protocol['preview_market_day']
    if (protocol.get('definition_id') != 'annual_quality_investment_same_pool_prospective_v1'
            or len(protocol.get('signals', [])) != 5 or protocol.get('execution_admitted') is not False
            or protocol.get('manifest', {}).get('end') != cutoff):
        raise ValueError('Original unchanged five-arm annual declaration required')
    root = Path(out).resolve()
    inputs = [prepared, *[Path(v).resolve() for v in old.values()], Path(native_extension).resolve(),
        Path(adjusted_extension).resolve(), Path(action_event).resolve(), Path(action_reference).resolve(),
        Path(calendar).resolve(), *[Path(v).resolve() for v in full_closes]]
    if root.exists() or any(root == path or root in path.parents or path in root.parents for path in inputs):
        raise ValueError('Annual forward output must be new and separate from every input')
    refs = [_ref(protocol_path), _ref(Path(old['listing_master']))]
    old_price, old_event, old_reference = [Path(old[key]).resolve()
        for key in ('price_view', 'event_view', 'reference_view')]
    price_meta = _read(old_price / 'result.json')
    old_raw = Path(price_meta['raw_view']).resolve()
    raw_file, adjusted_file = old_raw / 'raw_daily.parquet', old_price / 'canonical_daily.parquet'
    refs += [_ref(old_price / 'result.json'), _ref(old_event / 'result.json'),
        _ref(old_reference / 'result.json'), _ref(raw_file, price_meta['source_hashes']['raw']),
        _ref(adjusted_file, price_meta['canonical_panel_sha256'])]
    universe_file = old_event / 'source_input_universe.csv'
    universe = pd.read_csv(universe_file, dtype=str, keep_default_na=False)
    codes = set(universe.code)
    if universe.code.duplicated().any() or len(universe) != protocol['manifest']['source_codes']:
        raise ValueError('Original source membership changed')
    rename = price_meta.get('rename')
    canonical_codes = codes - ({rename['old']} if rename else set())
    if len(canonical_codes) != protocol['manifest']['canonical_codes']:
        raise ValueError('Original canonical scope changed')
    old_days = pd.read_csv(old_event / 'source_input_calendar.csv', dtype=str).date.tolist()
    if old_days != sorted(set(old_days)) or old_days[-1] != cutoff:
        raise ValueError('Original daily prefix must end on its exact preview day')
    calendar_file = Path(calendar).resolve()
    calendar_payload = _read(calendar_file)
    sessions, _, mode = _calendar_view(calendar_payload, cutoff, f"{calendar_payload['year']}-12-31", [])
    if mode != 'pinned_static_exchange_calendar':
        raise ValueError('An exact saved exchange calendar is required')
    action_event, action_reference, adjusted_extension = map(Path,
        (action_event, action_reference, adjusted_extension))
    ae_meta, ar_meta, ext_meta = [_read(path / 'result.json')
        for path in (action_event, action_reference, adjusted_extension)]
    if (ae_meta.get('status') != 'research_action_events_projected' or ae_meta.get('unqueried') != 0
            or ar_meta.get('status') != 'research_reference_presence_diagnostic'
            or Path(ar_meta['event_view']).resolve() != action_event.resolve()
            or Path(ar_meta['price_view']).resolve() != adjusted_extension.resolve()
            or ext_meta.get('status') != 'research_reconstruction_completed'):
        raise ValueError('Explicit completed aligned new action/reference/adjusted inputs required')
    ext_file = adjusted_extension / 'canonical_daily.parquet'
    refs += [_ref(action_event / 'result.json'), _ref(action_reference / 'result.json'),
        _ref(adjusted_extension / 'result.json'), _ref(ext_file, ext_meta['canonical_panel_sha256']),
        _ref(calendar_file), _ref(universe_file)]
    source = NativeHistoryChainSource(old_raw, [native_extension], full_closes, calendar=calendar_file)
    try:
        end = through or source.calendar[-1]
        if end > datetime.now().astimezone().date().isoformat() or end not in source.calendar:
            raise ValueError('Only exact already observed source days may be appended')
        new_days = [day for day in source.calendar if cutoff < day <= end]
        if not new_days or new_days != [day for day in sessions if cutoff < day <= end]:
            raise ValueError('New source days must be the exact consecutive post-declaration prefix')
        if datetime.fromisoformat(protocol['declared_at']).date().isoformat() >= new_days[0]:
            raise ValueError('Cannot backfill a pre-declaration signal')
        raw_pf, adj_pf, ext_pf = [pq.ParquetFile(path) for path in (raw_file, adjusted_file, ext_file)]
        if raw_pf.num_row_groups != len(old_days) or adj_pf.num_row_groups != len(old_days):
            raise ValueError('Original raw and adjusted row-group calendars changed')
        if adj_pf.schema_arrow != ext_pf.schema_arrow:
            raise ValueError('New adjusted source cannot alter the old column schema')
        ext_calendar = pd.read_csv(action_event / 'source_input_calendar.csv', dtype=str).date.tolist()
        if len(ext_calendar) != ext_pf.num_row_groups or ext_calendar[-1] != ext_meta['cutoff']:
            raise ValueError('New adjusted source calendar disagrees with its own price table')
        ext_indices = {day: i for i, day in enumerate(ext_calendar)}
        raw_frames, adj_frames, observations = [], [], []
        previous = raw_pf.read_row_group(len(old_days) - 1).to_pandas()
        ref_schema = pq.read_schema(old_reference / 'raw_reference_changes.parquet')
        old_refs = pd.read_parquet(old_reference / 'raw_reference_changes.parquet')
        current_refs = pd.read_parquet(action_reference / 'raw_reference_changes.parquet')
        refs.append(_ref(action_reference / 'raw_reference_changes.parquet',
            ar_meta.get('files', {}).get('raw_reference_changes.parquet')))
        current_refs = current_refs.loc[current_refs.date.gt(cutoff) & current_refs.date.le(end)]
        combined_refs, appended_refs, skipped_refs = _project_append(old_refs, current_refs,
            ('code', 'date'), cutoff, ('date',), codes)
        known_refs = {(row['code'], row['date']) for row in appended_refs}
        for day in new_days:
            raw, excluded, missing, original_fields = _raw_frame(source, day, codes, raw_pf.schema_arrow)
            if day in ext_indices:
                adjusted = ext_pf.read_row_group(ext_indices[day]).to_pandas()
                adjusted = adjusted.loc[adjusted.code.isin(canonical_codes)].copy()
                if (set(adjusted.code) != set(raw.code) - ({rename['old']} if rename else set())
                        or not adjusted.date.eq(day).all()):
                    raise ValueError('New adjusted source has different exact-day members: ' + day)
                basis = 'saved_provider_factor_derived_research_view_new_day_only'
            else:
                adjusted = _unknown_adjusted(raw, adj_pf.schema_arrow, rename)
                basis = 'no_independent_new_factor_adjusted_prices_unknown'
            new_refs = _new_reference_rows(previous, raw, day, known_refs, ref_schema)
            if len(new_refs):
                combined_refs = pd.concat([combined_refs, new_refs], ignore_index=True)
                known_refs.update(zip(new_refs.code, new_refs.date))
            raw_frames.append(_table(raw, raw_pf.schema_arrow))
            adj_frames.append(_table(adjusted, adj_pf.schema_arrow))
            observations.append({'day': day, 'raw_rows': len(raw), 'canonical_rows': len(adjusted),
                'original_source_fields': list(original_fields), 'unobserved_schema_fields': missing,
                'excluded_new_source_codes': excluded, 'adjusted_basis': basis,
                'unknown_adjusted_close_rows': int(adjusted.close.isna().sum()),
                'source': deepcopy(source.provenance['day_sources'][source.calendar.index(day)]),
                'new_unresolved_raw_references': len(new_refs)})
            previous = raw
        actions, action_refs = _future_action_rows(action_event, cutoff, end)
        refs += action_refs
        old_actions = pd.read_parquet(old_event / 'canonical_actions.parquet')
        combined_actions, appended_actions, skipped_actions = _project_append(old_actions, actions,
            ('symbol', 'dividOperateDate'), cutoff, ('dividRegistDate', 'dividOperateDate'), codes)
        root.mkdir(parents=True, exist_ok=False)
        _write(root / 'attempt.json', {'schema': SCHEMA, 'method_id': METHOD, 'status': 'building',
            'started_at': datetime.now().astimezone().isoformat(), 'refs': refs, **FLAGS})
        raw_out, price_out, event_out, reference_out = [root / name for name in ('raw', 'prices', 'events', 'references')]
        raw_out.mkdir(); price_out.mkdir()
        shutil.copytree(old_event, event_out)
        shutil.copytree(old_reference, reference_out)
        for original, additions, target in ((raw_pf, raw_frames, raw_out / 'raw_daily.parquet'),
                (adj_pf, adj_frames, price_out / 'canonical_daily.parquet')):
            with pq.ParquetWriter(target, original.schema_arrow) as writer:
                for i in range(original.num_row_groups):
                    writer.write_table(original.read_row_group(i))
                for table in additions:
                    writer.write_table(table)
        raw_hash, price_hash = _digest(raw_out / 'raw_daily.parquet'), _digest(price_out / 'canonical_daily.parquet')
        all_days = old_days + new_days
        pd.DataFrame({'date': all_days}).to_csv(event_out / 'source_input_calendar.csv', index=False)
        pd.DataFrame({'date': all_days}).to_csv(raw_out / 'input_calendar.csv', index=False)
        raw_result = {'status': 'source_cache_complete', 'kind': 'stock', 'cutoff': end,
            'expected_days': len(all_days), 'acquired_days': len(all_days), 'missing_days': [],
            'empty_days': [], 'symbols': len(codes), 'rows': raw_pf.metadata.num_rows + sum(t.num_rows for t in raw_frames),
            'panel_file': 'raw_daily.parquet', 'panel_sha256': raw_hash, 'raw_daily_sha256': raw_hash,
            'old_prefix_preserved': True, 'original_source_manifest': _ref(old_raw / 'result.json'), **FLAGS}
        plan = {'kind': 'stock', 'days': all_days, 'requested_start': all_days[0], 'requested_end': end,
            'calendar_sha256': _digest(raw_out / 'input_calendar.csv'), 'method_id': METHOD,
            'original_scope_frozen': True, 'strict_missing_fields_nullable': True, **FLAGS}
        _write(raw_out / 'plan.json', plan)
        raw_result['plan_sha256'] = _digest(raw_out / 'plan.json')
        _write(raw_out / 'result.json', raw_result)
        price_result = {**deepcopy(price_meta), 'raw_view': str(raw_out),
            'new_adjusted_extension_view': str(adjusted_extension.resolve()),
            'new_adjusted_extension_reference': _ref(adjusted_extension / 'result.json'),
            'new_raw_only_unknown_days': [day for day in new_days if day not in ext_indices],
            'dates': len(all_days), 'cutoff': end, 'source_raw_rows': raw_result['rows'],
            'rows': adj_pf.metadata.num_rows + sum(t.num_rows for t in adj_frames),
            'canonical_rows': adj_pf.metadata.num_rows + sum(t.num_rows for t in adj_frames),
            'source_hashes': {**deepcopy(price_meta['source_hashes']), 'raw': raw_hash},
            'panel_file': 'canonical_daily.parquet', 'panel_sha256': price_hash,
            'canonical_panel_sha256': price_hash,
            'canonical_symbols': len(canonical_codes), 'symbols': len(codes),
            'method_id': METHOD, 'new_adjusted_complete_through': min(end, ext_meta['cutoff']),
            'adjusted_prices_after_source_cutoff_unknown': end > ext_meta['cutoff'],
            'original_canonical_price_prefix': _ref(adjusted_file),
            'new_provider_factor_observations_fabricated': False, **FLAGS}
        _write(price_out / 'result.json', price_result)
        # Write with the original Arrow schema, including nullable unknowns.
        pq.write_table(_table(combined_actions, pq.read_schema(old_event / 'canonical_actions.parquet')),
            event_out / 'canonical_actions.parquet')
        pq.write_table(_table(combined_refs, ref_schema), reference_out / 'raw_reference_changes.parquet')
        event_result = {**_read(event_out / 'result.json'), 'raw_view': str(raw_out), 'end': end,
            'canonical_events': len(combined_actions), 'method_id': METHOD,
            'new_window_source_event_view': str(action_event.resolve()),
            'new_window_source_cutoff': ae_meta['end'], 'outside_window_return_rows_projected': True,
            'original_action_economic_prefix_preserved': True, 'new_event_rows': len(appended_actions), **FLAGS}
        for filename in ('canonical_actions.parquet', 'source_input_calendar.csv'):
            event_result['files'][filename] = _digest(event_out / filename)
        _write(event_out / 'result.json', event_result)
        reference_result = {**_read(reference_out / 'result.json'), 'event_view': str(event_out),
            'price_view': str(price_out), 'raw_daily_sha256': raw_hash,
            'canonical_panel_sha256': price_hash, 'dates': len(all_days),
            'raw_reference_changes': len(combined_refs), 'method_id': METHOD,
            'incremental_unknown_references_preserved': True, **FLAGS}
        reference_result['files']['raw_reference_changes.parquet'] = _digest(reference_out / 'raw_reference_changes.parquet')
        _write(reference_out / 'result.json', reference_result)
        _write(root / 'source_observations.json', {'days': observations,
            'new_action_original_rows': actions.astype(object).where(pd.notna(actions), None).to_dict('records'),
            'new_reference_original_rows': current_refs.astype(object).where(pd.notna(current_refs), None).to_dict('records'),
            'unapplied_actions': skipped_actions, 'unapplied_references': skipped_refs,
            'original_past_rows_not_repaired': True, **FLAGS})
        result = {'schema': SCHEMA, 'method_id': METHOD, 'status': 'completed_fixed_prefix_annual_forward_inputs',
            'created_at': datetime.now().astimezone().isoformat(), 'prepared_run': str(prepared),
            'original_declaration': refs[0], 'original_preview_day': cutoff, 'through': end,
            'source_codes': len(codes), 'canonical_codes': len(canonical_codes),
            'original_prefix_sessions': len(old_days), 'appended_sessions': len(new_days),
            'first_new_day': new_days[0], 'new_days': new_days,
            'adjusted_price_feature_coverage_through': min(end, ext_meta['cutoff']),
            'raw_close_coverage_through': end, 'old_features_or_returns_recomputed': False,
            'source_available_at': source.provenance['source_available_at'],
            'source_availability_is_historical_publication': False, 'references': refs,
            'stock_inputs': {'price_view': str(price_out), 'event_view': str(event_out),
                'reference_view': str(reference_out), 'listing_master': old['listing_master']},
            'limitations': ['Original five-arm rules/declaration/financial asof/capital unchanged.',
                'Raw-only later closes have unknown adjusted OHLC, not inferred returns or provider factors.',
                'Source-year replies and late-obtained action rows are not historical PIT; related unknowns stop the existing account.',
                'Scope remains original; new source codes are recorded outside scope, never admitted silently.'], **FLAGS}
        _write(root / 'result.json', result)
        _write(root / 'inputs.json', result['stock_inputs'])
        _write(root / 'attempt.json', {'schema': SCHEMA, 'status': 'completed', 'completed_at': result['created_at'], **FLAGS})
        return root, result
    finally:
        source.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('prepared-run', 'native-extension', 'adjusted-extension', 'action-event',
                 'action-reference', 'calendar', 'out'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--full-close', action='append', type=Path, default=[])
    parser.add_argument('--through')
    args = parser.parse_args(argv)
    directory, result = build(args.prepared_run, native_extension=args.native_extension,
        adjusted_extension=args.adjusted_extension, full_closes=args.full_close,
        action_event=args.action_event, action_reference=args.action_reference,
        calendar=args.calendar, out=args.out, through=args.through)
    print(json.dumps({'result': str(directory / 'result.json'), 'inputs': str(directory / 'inputs.json'),
        'status': result['status'], 'through': result['through'], 'execution_admitted': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
