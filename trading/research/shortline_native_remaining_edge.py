"""Saved native selections: remaining price advantage after the next open.

This source-only diagnostic never scores, changes selection, runs an account,
or turns a daily price-limit proxy into a fill. It is not a strategy backtest.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ..contracts import MARKET_TZ
from ..rules import execution_rule, quote_value
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import load_posthoc_listing
from .leader_52week_monthly import _adjusted
from .shortline_native_selection_replay import load_selection, START, END
from . import shortline_remaining_edge_stats as statistics
from . import leader_52week_monthly, leader_eight_type_historical_native, shortline_native_selection_replay
from .. import rules


METHOD = 'native_saved_selection_next_open_remaining_price_edge_v1'
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, returns_reportable=False,
    model_refit=False, scores_recomputed=False, labels_read=False,
    account_backtest=False, engine_advanced=False, orders_generated=False,
    original_selection_modified=False, user_account_mutated=False,
    network_accessed=False, pit_verified=False, corporate_actions_verified=False)
SPEC = dict(method_id=METHOD, start=START, cutoff=END, primary_horizon=5,
    secondary_horizon=1, entry='exact_calendar_D_plus_1_open',
    exit='exact_entry_plus_h_sessions_open_T1_or_later_never_next_available',
    formation='all_original_saved_known_stage_events_not_only_selected_or_hit5',
    selection='original_prelabel_sealed_selection_and_NO_CHANGE_unchanged',
    comparator='same_D_same_stage_full_original_pool_including_selected',
    price_return='derived_adjusted_open_F_divided_by_adjusted_open_E_minus1_not_cash_total_return',
    cutoff_policy='all_E_F_and_intermediate_observations_after_September30_unknown_no_October_backfill',
    populations=['endpoint_known', 'entry_exit_proxy_clear'],
    clearance='existing_rules_raw_cent_limits_and_posthoc_IPO_no_cash_lot_queue_or_capacity_test',
    reference='exact_previous_raw_close_equals_own_preclose_otherwise_unknown_no_action_inference',
    intervening_action='any_exact_reference_break_or_missing_row_in_holding_path_makes_clearance_unknown',
    identity='original_codes_no_alias_merge_both_known_300114_302132_ambiguous_endpoints_unknown',
    weighting=['equal_D_stage_baskets', 'equal_D_after_equal_stage_means'],
    fixed_nonoverlap_5='all_181_decision_days_from_first_D_every_fifth_no_origin_shift',
    segments=['all', 'H1', 'H2'], fees_or_slippage_evaluated=False,
    independent_holdout_claimed=False, historical_signal_publication_verified=False,
    event_independence_claimed=False, Sharpe_or_CAGR_reported=False,
    full_market_or_economic_total_return_claimed=False)


def clock():
    return datetime.now(MARKET_TZ).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def pin(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=digest(path))


def check(ref):
    path = Path(ref['path']).resolve()
    if digest(path) != ref['sha256']:
        raise ValueError('Registered remaining-edge input changed: ' + str(path))
    return path


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)


def separate(out, protected):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError('Use a new exclusive remaining-edge output directory')
    for raw in protected:
        path = Path(raw).resolve()
        if out == path or out in path.parents or path in out.parents:
            raise ValueError('New output must not overlap an original source or account')
    return out


def register(evaluation, raw_inputs, adjusted_view, out):
    # load_selection reads identities/decisions only, not probabilities or labels.
    plans, days, selection = load_selection(evaluation)
    source = read(raw_inputs)
    adjusted = _adjusted(adjusted_view, verify_panel=False)
    raw_dir = Path(read(adjusted['result']['path'])['raw_view']).resolve()
    raw_result, raw_plan = read(raw_dir/'result.json'), read(raw_dir/'plan.json')
    if (raw_result.get('old_prefix_preserved') is not True
            or Path(raw_plan['base_view']).resolve() != Path(source['chain']['base_view']).resolve()
            or raw_result['panel_sha256'] != adjusted['source_hashes']['raw']
            or days != [d for d in adjusted['calendar'] if START <= d <= END]):
        raise ValueError('Original native base prefix, derived prices and fixed181 days must agree')
    selection['all_formation_selection'] = pin(Path(evaluation)/'selection.csv')
    refs = dict(raw_inputs=pin(raw_inputs), raw_result=pin(raw_dir/'result.json'),
        raw_plan=pin(raw_dir/'plan.json'), adjusted_result=adjusted['result'],
        master=source['master_run'], calendar=source['calendar'])
    refs.update({'prior_calendar_' + str(i): ref for i, ref in enumerate(source.get('prior_calendars', []))})
    master = read(source['master_run']['path'])
    master_file = Path(source['master_run']['path']).parent/master['master_file']
    refs['master_payload'] = pin(master_file)
    if refs['master_payload']['sha256'] != master['master_sha256']:
        raise ValueError('Original research master payload changed')
    for ref in refs.values():
        check(ref)
    panels = dict(raw=dict(path=str(raw_dir/'raw_daily.parquet'), sha256=raw_result['panel_sha256']),
        adjusted=adjusted['panel'])
    out = separate(out, [Path(evaluation).resolve(), raw_dir, Path(adjusted_view).resolve(),
        Path(raw_inputs).resolve(), master_file.parent])
    record = dict(schema=METHOD+'_registration', method_id=METHOD, spec=SPEC,
        registered_at=clock(), days=days, calendar=adjusted['calendar'],
        evaluation=str(Path(evaluation).resolve()), selection=selection, refs=refs, panels=panels,
        adjusted_declaration=adjusted, implementation=[pin(module) for module in (
            __file__, statistics.__file__, leader_52week_monthly.__file__,
            leader_eight_type_historical_native.__file__, shortline_native_selection_replay.__file__, rules.__file__)],
        historical_published_at=None, large_panels_read_or_hashed=False, **FLAGS)
    out.mkdir(parents=True)
    save(out/'registration.json', record)
    return record


def make_schedule(days, calendar):
    if calendar != sorted(set(calendar)) or any(d not in calendar for d in days):
        raise ValueError('An exact chronological source calendar is required')
    indices = {d: i for i, d in enumerate(calendar)}
    result = {}
    for day in days:
        index = indices[day]
        def exact(offset):
            return calendar[index+offset] if index+offset < len(calendar) else None
        result[day] = dict(entry_day=exact(1), h1_exit_day=exact(2), h5_exit_day=exact(6))
    return result


def _number(row, key):
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def price_open(observation):
    if observation is None:
        return None, 'missing_exact_endpoint'
    raw, adjusted = observation['raw'], observation['adjusted']
    if raw is None or adjusted is None:
        return None, 'missing_raw_or_adjusted_endpoint'
    if str(raw['tradestatus']) != '1' or str(adjusted['tradestatus']) != '1':
        return None, 'inactive_or_unknown_endpoint'
    for own in (raw, adjusted):
        if any((_number(own, key) or 0) <= 0 for key in ('open', 'volume')):
            return None, 'nonpositive_or_unknown_open_or_volume'
    return _number(adjusted, 'open'), None


def reference_reason(raw, previous):
    if raw is None or previous is None:
        return 'missing_exact_raw_reference_row'
    if any((_number(row, key) or 0) <= 0 for row, key in ((raw, 'preclose'), (previous, 'close'))):
        return 'unknown_raw_reference'
    if Decimal(str(raw['preclose'])) != Decimal(str(previous['close'])):
        return 'unresolved_action_or_reference_change'
    return None


def endpoint_proxy(symbol, day, observation, previous, qualification, side):
    if day is None or day > END:
        return 'unknown', 'after_fixed_price_cutoff'
    if symbol in {'300114.SZSE', '302132.SZSE'}:
        return 'unknown', 'known_original_code_identity_ambiguity'
    if observation is None or observation['raw'] is None:
        return 'unknown', 'missing_exact_raw_endpoint'
    raw = observation['raw']
    if str(raw['tradestatus']) == '0':
        return 'proxy_blocked', 'suspended'
    if str(raw['tradestatus']) != '1' or str(raw['isST']) not in {'0', '1'}:
        return 'unknown', 'unknown_native_status'
    if side == 'buy' and str(raw['isST']) == '1':
        return 'proxy_blocked', 'risk_warning_buy_blocked'
    if any((_number(raw, key) or 0) <= 0 for key in ('open', 'preclose', 'volume')):
        return 'unknown', 'unknown_or_nonpositive_open_reference_volume'
    reason = qualification.assess(symbol, day)['engine_block_reason']
    if reason:
        return 'unknown', reason
    reason = reference_reason(raw, previous)
    if reason:
        return 'unknown', reason
    rule = execution_rule(symbol, day)
    lower, upper = rule.limits(raw['preclose'], day, str(raw['isST']) == '1')
    opening = quote_value(raw['open'], rule.price_unit)
    if side == 'buy' and opening >= upper:
        return 'proxy_blocked', 'limit_up_queue_unknown'
    if side == 'sell' and opening <= lower:
        return 'proxy_blocked', 'limit_down_queue_unknown'
    if not lower <= opening <= upper:
        return 'unknown', 'unsupported_limit_exception'
    return 'proxy_clear', 'daily_open_price_proxy_only_not_a_fill'


def _load_endpoints(record, formation, schedule):
    calendar = record['calendar']
    indices = {d: i for i, d in enumerate(calendar)}
    wanted = defaultdict(set)
    for row in formation[['date', 'symbol']].drop_duplicates().to_dict('records'):
        plan = schedule[row['date']]
        end = plan['h5_exit_day']
        # Read only exact rows needed for endpoints and reference continuity.
        last = min(indices.get(end, indices[END]), indices[END])
        for day in calendar[indices[row['date']]:last+1]:
            wanted[day].add(row['symbol'])
    panels = {kind: pq.ParquetFile(ref['path']) for kind, ref in record['panels'].items()}
    data = {}
    try:
        for parquet in panels.values():
            if parquet.metadata.num_row_groups != len(calendar):
                raise ValueError('Original raw/adjusted exact rowgroup calendars differ')
        for day, symbols in sorted(wanted.items()):
            current = {}
            for kind, parquet in panels.items():
                fields = ['date', 'code', 'open', 'volume', 'tradestatus']
                if kind == 'raw':
                    fields += ['close', 'preclose', 'isST']
                group = indices[day]
                stats = parquet.metadata.row_group(group).column(parquet.schema_arrow.names.index('date')).statistics
                if stats is None or stats.min != day or stats.max != day or stats.null_count:
                    raise ValueError('Raw/adjusted rowgroup is not the exact scheduled day')
                frame = parquet.read_row_group(group, columns=fields).to_pandas()
                frame['symbol'] = frame.code.map(lambda code: code[3:]+('.SSE' if code.startswith('sh.') else '.SZSE'))
                frame = frame.loc[frame.symbol.isin(symbols)]
                if frame.symbol.duplicated().any() or not frame.date.eq(day).all():
                    raise ValueError('Endpoint original code/date keys differ')
                current[kind] = {r['symbol']: r for r in frame.to_dict('records')}
            for symbol in symbols:
                data[(day, symbol)] = {k: current[k].get(symbol) for k in panels}
    finally:
        for parquet in panels.values():
            parquet.close()
    return data, dict(rowgroup_days_read=len(wanted), raw_and_adjusted_rowgroups_each=len(wanted),
        exact_symbol_day_observations=len(data), cutoff=END)


def observations(formation, record, schedule, data, qualification):
    rows, calendar = [], record['calendar']
    index = {d: i for i, d in enumerate(calendar)}
    computed = {}
    for row in formation.to_dict('records'):
        day, symbol = row['date'], row['symbol']
        if (day, symbol) in computed:
            rows.append(dict(row, **computed[(day, symbol)]))
            continue
        plan = schedule[day]
        def own(d):
            return data.get((d, symbol)) if d is not None and d <= END else None
        def prior(d):
            return (own(calendar[index[d]-1]) or {}).get('raw') if d in index and index[d] else None
        entry = plan['entry_day']
        entry_status, entry_reason = endpoint_proxy(symbol, entry, own(entry), prior(entry), qualification, 'buy')
        entry_price, price_reason = price_open(own(entry))
        result = dict(**plan, entry_status=entry_status, entry_reason=entry_reason,
            entry_adjusted_open=entry_price, entry_price_reason=price_reason)
        for horizon in (1, 5):
            field = 'h'+str(horizon)
            exit_day = plan[field+'_exit_day']
            status, reason = endpoint_proxy(symbol, exit_day, own(exit_day), prior(exit_day), qualification, 'sell')
            price, price_reason = price_open(own(exit_day))
            breaks = []
            if entry is not None and exit_day is not None and exit_day <= END:
                for d in calendar[index[entry]:index[exit_day]+1]:
                    reference = reference_reason((own(d) or {}).get('raw'), prior(d))
                    if reference:
                        breaks.append(dict(day=d, reason=reference))
                if breaks and status == 'proxy_clear':
                    status, reason = 'unknown', 'intervening_reference_continuity_unknown'
            identity_known = symbol not in {'300114.SZSE', '302132.SZSE'}
            result[field+'_return'] = price/entry_price-1 if (price is not None and entry_price is not None and identity_known) else np.nan
            result[field+'_exit_status'], result[field+'_exit_reason'] = status, reason
            result[field+'_exit_adjusted_open'], result[field+'_exit_price_reason'] = price, price_reason
            result[field+'_reference_breaks'] = json.dumps(breaks, ensure_ascii=False)
        computed[(day, symbol)] = result
        rows.append(dict(row, **result))
    return pd.DataFrame(rows)


def run(registration, out):
    record = read(registration)
    if record.get('schema') != METHOD+'_registration' or record.get('spec') != SPEC:
        raise ValueError('Use the exact independently registered remaining-edge question')
    for ref in [*record['selection'].values(), *record['refs'].values(), *record['implementation']]:
        check(ref)
    plans, days, selection = load_selection(record['evaluation'])
    if days != record['days'] or any(selection[k] != record['selection'][k] for k in selection):
        raise ValueError('Original decisions/selection changed after registration')
    out = separate(out, [Path(registration).resolve().parent,
        *[Path(r['path']).parent for r in record['panels'].values()],
        record['evaluation'], record['refs']['raw_inputs']['path']])
    started = clock()
    signatures = {name: (Path(ref['path']).stat().st_size, Path(ref['path']).stat().st_mtime_ns)
        for name, ref in record['panels'].items()}
    for ref in record['panels'].values():
        check(ref)  # Each large price panel hashed once, only in the real run.
    formation = pd.read_csv(record['selection']['all_formation_selection']['path'],
        dtype={'date': str, 'symbol': str, 'event_id': str})
    formation['candidate_selected'] = formation.candidate_selected.map(
        lambda v: v if isinstance(v, bool) else {'True': True, 'False': False}[v])
    formation = formation[['event_id', 'date', 'symbol', 'stage', 'model_kind', 'candidate_selected']]
    if (formation.duplicated(['model_kind', 'event_id']).any()
            or not formation.date.isin(days).all()
            or not formation.stage.isin([1, 2, 3]).all()):
        raise ValueError('Exact unique original known-stage formation events required')
    for kind, by_day in plans.items():
        for day, plan in by_day.items():
            original = formation.loc[(formation.date == day) & (formation.model_kind == kind) & formation.candidate_selected]
            if sorted(original.symbol) != sorted(plan['symbols']):
                raise ValueError('Formation does not preserve the original FULL/NO_CHANGE selection')
    schedule = make_schedule(days, record['calendar'])
    source = read(record['refs']['raw_inputs']['path'])
    qualification = load_posthoc_listing(source['master_run']['path'], source['calendar']['path'],
        observed_at=record['registered_at'], prior_calendars=[r['path'] for r in source.get('prior_calendars', [])])
    data, source_read = _load_endpoints(record, formation, schedule)
    events = observations(formation, record, schedule, data, qualification)
    result = statistics.analyze(events, days)
    for name, ref in record['panels'].items():
        status = Path(ref['path']).stat()
        if signatures[name] != (status.st_size, status.st_mtime_ns):
            raise ValueError('Price source changed while reading the registered observations')
    out.mkdir(parents=True)
    events.to_parquet(out/'event_observations.parquet', index=False)
    for name in ('detail', 'summary', 'coverage', 'nonoverlap_origins'):
        result[name].to_csv(out/(name+'.csv'), index=False)
    save(out/'summary.json', result['summary'].to_dict('records'))
    save(out/'metadata.json', result['metadata'])
    payload = dict(schema=METHOD+'_result', method_id=METHOD,
        status='completed_source_only_remaining_price_edge_diagnostic', spec=SPEC,
        started_at=started, completed_at=clock(), registered_at=record['registered_at'],
        registration=pin(registration), source_read=source_read,
        formation_model_rows=len(events), unique_events=int(events.event_id.nunique()),
        unique_symbols=int(events.symbol.nunique()), original_selected_counts=events.groupby('model_kind').candidate_selected.sum().to_dict(),
        **FLAGS)
    payload['files'] = {p.name: digest(p) for p in out.iterdir() if p.is_file()}
    save(out/'result.json', payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    reg = sub.add_parser('register')
    for name in ('evaluation', 'raw-inputs', 'adjusted-view', 'out'):
        reg.add_argument('--'+name, type=Path, required=True)
    execute = sub.add_parser('run')
    for name in ('registration', 'out'):
        execute.add_argument('--'+name, type=Path, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    value = {'register': register, 'run': run}[command](**args)
    print(json.dumps({k: value.get(k) for k in ('schema', 'status', 'registered_at', 'execution_admitted')}, ensure_ascii=False))
    return 0
