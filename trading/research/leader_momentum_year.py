"""Annual native momentum inputs, independent of eight-type panel construction.

The existing experiment.replay/Engine/Ledger owns every account transition.
Only the initial observed universe and saved monthly candidate feed are new.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import StrategyConfig
from ..contracts import SignalFrame, TradingMode
from ..rules import stock_board
from .baostock_daily_slice import digest
from .baostock_native_history import RAW_FIELDS
from .experiment import baseline_ledger_result
from .leader_eight_type_historical_native import _physical_native, load_posthoc_listing, with_prior_listing_witness
from .leader_eight_type_panel_replay import _QualifiedFeed, _calendar_view
from .leader_historical_execution_policy import HistoricalExecutionPolicy
from .leader_momentum_benchmark import FLAGS, SPEC as BENCHMARK_SPEC, MonthlyFeed, monthly_batch
from .low_risk import WINDOW, features
from .monthly_timing import month_ends
from .native_history_source import NativeHistorySource
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting


METHOD = 'native_12_1_annual_first_day_known_cohort_v1'
CAPITAL_POLICIES = ('original', 'known_close_minimum_entry')
SPEC = dict(id=METHOD, benchmark=BENCHMARK_SPEC,
    universe='first_D_supported_native_rows_active_non_ST_61_prior_valid_observations_including_D_and_qualified_listing',
    cohort_count='past_native_active_non_ST_valid_count_saturated_at61_not_future_membership',
    raw_features='unchanged_low_risk_native_raw_252_21_volatility_median_amount',
    capital_policies=list(CAPITAL_POLICIES), first_month_warmup='first_month_end_requires252_prior_source_sessions',
    capital_clock='own_D_close_shared_Engine_book_after_open_fills_marks_and_actions',
    annual_cash_reset='independent_research_accounts_not_continuous_or_comparable_as_one_multiyear_account',
    six_tail_sessions=True, parameters_selected_by_return=False, independent_holdout_claimed=False)


def now():
    return datetime.now().astimezone().isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def pin(path):
    return dict(path=str(Path(path).resolve()), sha256=digest(path))


def schedule(payload, source_calendar, start, end, through):
    year = int(payload['year'])
    sessions, _, status = _calendar_view(payload, f'{year}-01-01', f'{year}-12-31', [])
    if status != 'pinned_static_exchange_calendar' or any(day not in sessions for day in (start, end, through)):
        raise ValueError('Explicit same-year official study dates required')
    days = [day for day in sessions if start <= day <= through]
    if ([day for day in source_calendar if start <= day <= through] != days
            or len([day for day in days if day > end]) != 6 or not start < end < through):
        raise ValueError('Exact continuous native source and exactly six fixed tail sessions required')
    decision_days = [day for day in month_ends(sessions, end) if start <= day < end]
    if not decision_days or source_calendar.index(decision_days[0]) < WINDOW:
        raise ValueError('First monthly decision lacks its exact252-session native prefix')
    return sessions, days, decision_days


def full_monthly_batch(day, members, values, rows, qualification):
    """Reuse every original eligibility calculation; retain full ranks for capital admission."""
    eligible, exclusions, finite = [], Counter(), 0
    for symbol in members:
        batch = monthly_batch(day, [symbol], values, rows, qualification)
        finite += batch.get('finite_feature_count', 0)
        exclusions.update(batch.get('exclusions', {}))
        eligible.extend(batch['candidates'])
    eligible.sort(key=lambda row: (-row['score'], row['symbol']))
    return dict(day=day, status='complete' if finite else 'incomplete', candidates=eligible,
        eligible_count=len(eligible), finite_feature_count=finite, exclusions=dict(exclusions),
        reason='monthly_ranking' if eligible else 'known_empty_eligible_pool' if finite else 'monthly_features_unavailable')


def build(native_view, calendar, next_calendar, master_run, action_event_view, action_reference_view,
          start, end, through, out):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Annual inputs require a new directory')
    payload, successor_payload = read(calendar), read(next_calendar)
    successor_year = int(successor_payload['year'])
    if successor_year != int(payload['year']) + 1:
        raise ValueError('An explicit immediately following calendar is required')
    successor_sessions, _, status = _calendar_view(successor_payload,
        f'{successor_year}-01-01', f'{successor_year}-12-31', [])
    if status != 'pinned_static_exchange_calendar':
        raise ValueError('The successor calendar must be pinned, not a weekday fallback')
    source = NativeHistorySource(native_view)
    writer = None
    try:
        source_calendar = source.calendar
        sessions, days, decision_days = schedule(payload, source_calendar, start, end, through)
        first_index = source_calendar.index(start)
        if not first_index:
            raise ValueError('An exact native predecessor context is required')
        context_day = source_calendar[first_index - 1]
        observed_at = now()
        qualification = load_posthoc_listing(master_run, calendar, observed_at=observed_at)
        first_raw = source.frame(start)
        supported, outside = [], []
        for symbol in first_raw.symbol:
            try:
                stock_board(symbol)
                supported.append(symbol)
            except ValueError:
                outside.append(symbol)
        if not supported:
            raise ValueError('No supported first-day observed stock identities')
        first_physical = _physical_native(first_raw.loc[first_raw.symbol.isin(supported)])
        active = set(first_physical.loc[first_physical.active_non_st, 'symbol'])
        counts = dict.fromkeys(supported, 0)
        witness = {symbol: [] for symbol in supported}
        history_begin = source_calendar.index(decision_days[0]) - WINDOW
        feature_days = [day for day in source_calendar[history_begin:] if day <= end]
        matrices = {key: pd.DataFrame(np.nan, index=feature_days, columns=supported)
                    for key in ('closes', 'volumes', 'amounts')}
        folder.mkdir(parents=True, exist_ok=False)
        plan = dict(schema=METHOD, specified_at=now(), spec=SPEC, **FLAGS,
            start=start, end=end, through=through, config=_config(100000, 5).to_dict(),
            calendars=[pin(calendar), pin(next_calendar)], master=pin(master_run),
            action_event_view=pin(Path(action_event_view) / 'result.json'),
            action_reference_view=pin(Path(action_reference_view) / 'result.json'),
            native_source=source.provenance, implementation=pin(__file__),
            benchmark_implementation=pin(Path(__file__).with_name('leader_momentum_benchmark.py')),
            feature_implementation=pin(Path(__file__).with_name('low_risk.py')),
            capital_implementation=pin(Path(__file__).with_name('leader_momentum_capital.py')))
        plan['close_engine_implementation'] = pin(Path(__file__).parent.parent / 'engine.py')
        save(folder / 'study_plan.json', plan)
        rows_by_month, raw_rows, read_count = {}, 0, 0
        for day in source_calendar:
            if day > through:
                break
            frame = source.frame(day, supported)
            read_count += 1
            if day <= start:
                unresolved = [symbol for symbol, count in counts.items() if count < 61]
                valid = _physical_native(frame.loc[frame.symbol.isin(unresolved)]) if unresolved else None
                if valid is not None:
                    for symbol in valid.loc[valid.active_non_st, 'symbol']:
                        counts[symbol] += 1
                if day < start:
                    close = pd.to_numeric(frame.close, errors='coerce')
                    for symbol in frame.loc[frame.tradestatus.eq('1') & np.isfinite(close) & close.gt(0), 'symbol']:
                        record = qualification.records.get(symbol)
                        if record and day >= record['ipo_date'] and len(witness[symbol]) < 5:
                            witness[symbol].append(day)
            if day in matrices['closes'].index:
                indexed = frame.set_index('symbol', drop=False)
                for key, field in (('closes', 'close'), ('volumes', 'volume'), ('amounts', 'amount')):
                    matrices[key].loc[day, indexed.index] = pd.to_numeric(indexed[field], errors='coerce').to_numpy()
                if day in decision_days:
                    rows_by_month[day] = indexed.to_dict('index')
            native = frame[list(RAW_FIELDS) + ['symbol']].copy()
            native.attrs.clear()
            if day == context_day:
                native.to_parquet(folder / 'context.parquet', index=False)
            if day in days:
                table = pa.Table.from_pandas(native, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(folder / 'raw_daily.parquet', table.schema)
                writer.write_table(table)
                raw_rows += len(frame)
        writer.close()
        writer = None
        witness = {symbol: values for symbol, values in witness.items() if len(values) == 5}
        qualification = with_prior_listing_witness(qualification, witness, start)
        members, excluded = [], []
        for symbol in supported:
            reasons = []
            if symbol not in active:
                reasons.append('first_D_not_valid_active_non_ST')
            if counts[symbol] < 61:
                reasons.append('past_valid_observation_count_below61')
            proof = qualification.assess(symbol, start, decision_as_of=start + 'T15:05:00+08:00')
            if proof['engine_block_reason'] is not None:
                reasons.append('first_D_listing_qualification_unsupported')
            if reasons:
                excluded.append(dict(symbol=symbol, reasons=reasons, known_count_lower_bound=counts[symbol]))
            else:
                members.append(symbol)
        if not members:
            raise ValueError('No known first-day cohort; not an all-cash strategy result')
        values = features(SimpleNamespace(**{key: frame[members] for key, frame in matrices.items()},
            calendar=source_calendar), end, feature_days=decision_days)
        batches = {day: full_monthly_batch(day, members, values, rows_by_month[day], qualification)
                   for day in decision_days}
        save(folder / 'batches.json', batches)
        selection = dict(first_D=start, observed_supported_count=len(supported), scope_members=members,
            excluded=excluded, outside_supported_codes=outside, valid_count_saturated_at61=counts,
            raw_identity_aliases_merged=False, future_membership_used=False)
        save(folder / 'cohort.json', selection)
        save(folder / 'listing_witness.json', witness)
        source._unchanged()
        manifest = dict(schema=METHOD, status='completed_annual_inputs', created_at=now(), **FLAGS,
            study_plan=pin(folder / 'study_plan.json'), source_days=days, decision_days=decision_days,
            start=start, end=end, through=through, context_day=context_day,
            source_rows_read_once=read_count, original914_feature_panel_rebuilt=False,
            feature_source_sessions=len(feature_days), cohort_count=len(members), raw_rows=raw_rows,
            study_sessions=sessions, next_session_after_year=successor_sessions[0],
            qualification=qualification.verification,
            files={name: pin(folder / name) for name in ('raw_daily.parquet', 'context.parquet',
                'batches.json', 'cohort.json', 'listing_witness.json')})
        save(folder / 'manifest.json', manifest)
        return manifest
    finally:
        if writer is not None:
            writer.close()
        source.close()


class AnnualFeed(MonthlyFeed):
    def __init__(self, execution, batches, config, end, final_day, qualification, capacity, capital_policy):
        super().__init__(execution, deepcopy(batches), config, end, final_day)
        self.original_batches = deepcopy(batches)
        self.qualification, self.capacity, self.capital_policy = qualification, capacity, capital_policy
        self.state, self.seen, self.admission = None, [], []
        self.blocks = []
        self.close_candidate_policy = dict(id=METHOD, capacity=capacity, capital_policy=capital_policy)

    def validate_session(self, state, day):
        self.state = state
        required = {s for s, lots in state['positions'].items() if sum(lot['qty'] for lot in lots)}
        required.update(state.get('pending_targets') or {})
        issues, refs = self.execution.raw_issues(day, required), self.execution.references(day, required)
        ipo = [self.qualification.assess(s, day, decision_as_of=day + 'T09:30:00+08:00') for s in sorted(required)]
        if issues or any(not self.execution.reference_ok(row) for row in refs) or any(row['engine_block_reason'] for row in ipo):
            self.blocks.append(dict(day=day, raw_issues=issues, reference_evidence=refs, ipo_evidence=ipo,
                                   session_not_committed=True))
            raise ValueError('Annual owned/pending native source unresolved: ' + json.dumps(self.blocks[-1]))
        return self.execution.validate_session(state, day)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config:
            raise ValueError('Annual feed requires its registered REPLAY config')
        batch = self.original_batches.get(day)
        if batch and batch['status'] == 'complete' and day < self.end:
            return SignalFrame.from_legacy(batch['candidates'], day, 'research:' + BENCHMARK_SPEC['id'], context)
        return super().signal_frame(day, config, context)

    def close_signal_frame(self, day, config, context, signal_batch, book):
        """Filter original ranks using the shared book after this D's marks/actions."""
        batch = self.original_batches.get(day)
        if batch and batch['status'] == 'complete' and day < self.end:
            original = batch['candidates'][:5]
            if self.capital_policy == 'known_close_minimum_entry':
                from .leader_momentum_capital import affordable_ranked_candidates
                equity = book.equity()
                quantities = {s: book.quantity(s) for s in book.state['positions']}
                result = affordable_ranked_candidates(batch['candidates'], equity, quantities,
                    self.base.rows.get(day, {}), config, day)
                self.admission.append(dict(day=day, close_equity_cents=equity, **result))
                original = result['candidates']
                effective = dict(batch, status=result['status'], candidates=original, reason=result['reason'])
            else:
                effective = dict(batch, candidates=original)
            if effective['status'] == 'complete' and self.capacity == 'causal_first_seen_cap20':
                for row in original:
                    if row['symbol'] not in self.seen and len(self.seen) < 20:
                        self.seen.append(row['symbol'])
                effective['candidates'] = [row for row in original if row['symbol'] in self.seen]
                if original and not effective['candidates']:
                    effective.update(status='incomplete', reason='capacity_filtered_all_NO_CHANGE')
            self.batches[day] = effective
            return super().signal_frame(day, config, context)
        return signal_batch


class _AnnualQualifiedFeed(_QualifiedFeed):
    def close_signal_frame(self, day, config, context, signal_batch, book):
        return self.base.close_signal_frame(day, config, context, signal_batch, book)


def run(inputs, out, *, capacity, capital_policy):
    folder, inputs = Path(out).resolve(), Path(inputs).resolve()
    if folder.exists():
        raise FileExistsError('Annual account output already exists')
    manifest, plan = read(inputs / 'manifest.json'), read(inputs / 'study_plan.json')
    if (manifest.get('schema') != METHOD or plan.get('spec') != SPEC
            or digest(__file__) != plan['implementation']['sha256']):
        raise ValueError('Exact annual method registration required')
    for ref in [manifest['study_plan'], plan['master'], plan['benchmark_implementation'],
                plan['feature_implementation'], plan['capital_implementation'], plan['close_engine_implementation'],
                *plan['calendars'], plan['action_event_view'],
                plan['action_reference_view'], *manifest['files'].values()]:
        if digest(ref['path']) != ref['sha256']:
            raise ValueError('Pinned annual source or implementation changed: ' + ref['path'])
    if capacity not in BENCHMARK_SPEC['capacities'] or capital_policy not in CAPITAL_POLICIES:
        raise ValueError('Only the fixed four annual cases are admitted for this comparison')
    cohort, witness = read(inputs / 'cohort.json'), read(inputs / 'listing_witness.json')
    qualification = load_posthoc_listing(plan['master']['path'], plan['calendars'][0]['path'],
        observed_at=manifest['qualification']['posthoc_observed_at'])
    qualification = with_prior_listing_witness(qualification, witness, manifest['start'])
    if qualification.verification != manifest['qualification']:
        raise ValueError('Saved listing qualification changed')
    members, days = cohort['scope_members'], manifest['source_days']
    raw = pd.concat([pd.read_parquet(inputs / 'context.parquet'), pd.read_parquet(inputs / 'raw_daily.parquet')], ignore_index=True)
    if len(raw) != manifest['raw_rows'] + len(raw.loc[raw.date.eq(manifest['context_day'])]):
        raise ValueError('Annual raw row count changed')
    calendar = [manifest['context_day']] + days + [manifest['next_session_after_year']]
    base = ExactSourceRawFeed(raw, calendar, members, context_anchor=dict(day=manifest['context_day'], closes={}))
    accounting = load_action_accounting(Path(plan['action_event_view']['path']).parent,
        Path(plan['action_reference_view']['path']).parent, symbols=members,
        raw_days=[manifest['context_day']] + days, replay_days=days, price_source='independent_baostock_raw')
    execution = HistoricalExecutionPolicy(base, accounting, days)
    config = StrategyConfig(**plan['config']).validate()
    adapter = AnnualFeed(execution, read(inputs / 'batches.json'), config, manifest['end'], days[-1],
        qualification, capacity, capital_policy)
    feed = _AnnualQualifiedFeed(adapter, qualification)
    result = baseline_ledger_result(feed, config, days, folder)
    account = read(folder / 'account.json')
    residual = {s: sum(lot['qty'] for lot in lots) for s, lots in account['positions'].items() if sum(lot['qty'] for lot in lots)}
    complete = result['status'] == 'completed' and bool(account['fills']) and not residual
    report = dict(schema=METHOD, created_at=now(), inputs=pin(inputs / 'manifest.json'), spec=SPEC, **FLAGS,
        capacity=capacity, capital_policy=capital_policy, config=config.to_dict(), cohort_count=len(members),
        start=manifest['start'], end=manifest['end'], through=manifest['through'],
        ledger_result=result, complete_PnL_available=complete, residual_positions=residual,
        metrics={key: value for key, value in result.items() if key not in
            ('status', 'orders', 'fills', 'reused_sessions', 'newly_completed_sessions', 'turnover_over_initial')}
            if complete else None,
        source_blocks=adapter.blocks, parent_orders=len(account['orders']), fill_callbacks=len(account['fills']),
        benchmark_vs_capital_is_same_fixed_scores=True, per_case_independent_cash=True)
    save(folder / 'result.json', report)
    save(folder / 'decisions.json', adapter.decisions)
    save(folder / 'capital_admission.json', adapter.admission)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build_parser = commands.add_parser('inputs')
    for name in ('native-view', 'calendar', 'next-calendar', 'master-run', 'action-event-view', 'action-reference-view', 'out'):
        build_parser.add_argument('--' + name, type=Path, required=True)
    for name in ('start', 'end', 'through'):
        build_parser.add_argument('--' + name, required=True)
    replay_parser = commands.add_parser('replay')
    for name in ('inputs', 'out'):
        replay_parser.add_argument('--' + name, type=Path, required=True)
    replay_parser.add_argument('--capacity', choices=BENCHMARK_SPEC['capacities'], required=True)
    replay_parser.add_argument('--capital-policy', choices=CAPITAL_POLICIES, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    result = (build if command == 'inputs' else run)(**args)
    print(json.dumps({key: result.get(key) for key in ('schema', 'status', 'cohort_count',
        'complete_PnL_available', 'ledger_result', 'metrics')}, ensure_ascii=False))
    return 0
