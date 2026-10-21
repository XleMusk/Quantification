"""Fixed native 12-1 monthly benchmark on a saved leader research universe.

Reuses low_risk features/rank and the historical native execution policy. This
is a whole-strategy comparison, not a pure signal ablation or live signal.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ..contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from .baostock_daily_slice import digest
from .leader_eight_type_panel_replay import _QualifiedFeed, _calendar_view
from .leader_historical_execution_policy import HistoricalExecutionPolicy
from .leader_trend_eligible_scope import _load_registration as load_trend_registration
from .low_risk import WINDOW, features, rank
from .monthly_timing import month_ends
from .native_history_source import NativeHistorySource
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .stockdb_action_accounting import load_action_accounting


METHOD = 'native_12_1_monthly_leader_common_universe_benchmark_v1'
FLAGS = dict(candidate_panel_only=True, strategy_validated=False, selection_enabled=False,
    execution_admitted=False, returns_reportable=False, model_refit=False,
    future_labels_used_for_score=False, pit_verified=False)
SPEC = dict(id=METHOD, signal='reuse_low_risk_features_and_rank_12_1',
    signal_price_policy='explicit_unadjusted_native_close_not_original_adjusted_low_risk_replication',
    feature_eligibility='finite_252_session_volatility_positive_12_1_and_20_session_median_amount',
    minimum_median_amount_cny=20000000, selection='monthly_top5_score_desc_symbol_ties_no_extra_retention',
    initial_account='same_saved_leader_cash_and_config_no_pre_window_signal_or_positions',
    entry='exact_next_open_shared_engine', regular_exit='next_monthly_target_replacement',
    environment_filter=False, five_session_expiry=False, leader_structural_exit=False,
    study_exit='end_decision_day_and_tail_except_last_emit_empty_full_targets_until_terminal_no_change',
    capacities=['causal_first_seen_cap20', 'uncapped'], capacity_refill=False,
    comparison='whole_strategy_not_signal_only', independent_holdout_claimed=False,
    parameters_selected_by_return=False, historical_publication_verified=False)


def now():
    return datetime.now().astimezone().isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def monthly_batch(day, members, values, rows, qualification):
    """Use own-D features/status only; missing batches never mean liquidation."""
    if any(day not in frame.index for frame in values.values()):
        return dict(status='incomplete', candidates=[], reason='monthly_feature_batch_missing')
    observed = 0
    pool, exclusions = [], Counter()
    for symbol in members:
        fields = {key: float(frame.at[day, symbol]) for key, frame in values.items()}
        if not np.isfinite(list(fields.values())).all():
            exclusions['nonfinite_required_history_features'] += 1
            continue
        observed += 1
        raw = rows.get(symbol)
        if (raw is None or raw.get('tradestatus') != '1' or raw.get('isST') != '0'
                or not np.isfinite(float(raw['volume'])) or float(raw['volume']) <= 0):
            exclusions['own_day_raw_activity_or_status_unavailable'] += 1
            continue
        if fields['volatility'] <= 0 or fields['median_amount'] < SPEC['minimum_median_amount_cny']:
            exclusions['fixed_volatility_or_liquidity_condition'] += 1
            continue
        proof = qualification.assess(symbol, day, decision_as_of=day + 'T15:05:00+08:00')
        if proof['engine_block_reason'] is not None:
            exclusions['own_day_listing_qualification_unsupported'] += 1
            continue
        pool.append(dict(symbol=symbol, **fields))
    ordered = rank(pool, 'momentum_12_1_20')[:5]
    return dict(day=day, status='complete' if observed else 'incomplete', candidates=ordered,
        eligible_count=len(pool), finite_feature_count=observed, exclusions=dict(exclusions),
        reason='monthly_ranking' if ordered else 'known_empty_eligible_pool' if observed else 'monthly_features_unavailable')


def build(trend_registration, native_view, out):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Benchmark inputs require a new output directory')
    trend_registration = Path(trend_registration).resolve()
    trend, loaded, manifest = load_trend_registration(trend_registration)
    members, start, end = trend['scope_members'], trend['start'], trend['end']
    source = NativeHistorySource(native_view)
    try:
        calendar = source.calendar
        first, last = calendar.index(start), calendar.index(end)
        if first < WINDOW or manifest['source_days'][-1] not in calendar:
            raise ValueError('Exact native 252-session prefix and study/tail source required')
        history_days = calendar[first - WINDOW:last + 1]
        decision_days = [day for day in month_ends(calendar, end) if start <= day < end]
        if not decision_days:
            raise ValueError('The fixed comparison window has no monthly decision')
        folder.mkdir(parents=True, exist_ok=False)
        registration = dict(method_id=METHOD, registered_at=now(), spec=SPEC, **FLAGS,
            trend_registration=str(trend_registration), trend_registration_sha256=digest(trend_registration),
            common_scope_members=members, config=trend['config'], start=start, end=end,
            source_days=trend['source_days'], history_days=history_days, decision_days=decision_days,
            native_source=source.provenance, implementation_sha256=digest(__file__),
            feature_implementation_sha256=digest(Path(__file__).with_name('low_risk.py')))
        save(folder / 'registration.json', registration)
        matrices = {key: pd.DataFrame(np.nan, index=history_days, columns=members)
                    for key in ('closes', 'volumes', 'amounts')}
        own_rows, reads = {}, []
        for day in history_days:
            frame = source.frame(day, members).set_index('symbol', drop=False)
            for key, field in (('closes', 'close'), ('volumes', 'volume'), ('amounts', 'amount')):
                matrices[key].loc[day, frame.index] = pd.to_numeric(frame[field], errors='coerce').to_numpy()
            if day in decision_days:
                own_rows[day] = frame.to_dict('index')
            reads.append(dict(day=day, observed_rows=len(frame), requested_symbols=len(members)))
        values = features(SimpleNamespace(**matrices, calendar=calendar), end, feature_days=decision_days)
        batches = {day: monthly_batch(day, members, values, own_rows[day], loaded['qualification'])
                   for day in decision_days}
        source._unchanged()
        save(folder / 'batches.json', batches)
        save(folder / 'history_reads.json', reads)
        result = dict(schema=METHOD, status='completed_benchmark_inputs', created_at=now(), **FLAGS,
            registration=str(folder / 'registration.json'), registration_sha256=digest(folder / 'registration.json'),
            batches=str(folder / 'batches.json'), batches_sha256=digest(folder / 'batches.json'),
            history_reads=str(folder / 'history_reads.json'), history_reads_sha256=digest(folder / 'history_reads.json'),
            history_sessions=len(history_days), monthly_batches=len(batches), common_scope_count=len(members),
            source_opened_once=True, original_914_panel_rebuilt=False, strategy_or_account_run=False)
        save(folder / 'result.json', result)
        return result
    finally:
        source.close()


class MonthlyFeed:
    def __init__(self, execution, batches, config, end, final_day):
        self.execution, self.base, self.batches = execution, execution.base, batches
        self.config, self.end, self.final_day = config, end, final_day
        self.decisions = []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config:
            raise ValueError('Benchmark requires its saved config and REPLAY context')
        batch = self.batches.get(day)
        if self.end <= day < self.final_day:
            candidates, reason = [], 'study_exit_only'
        elif batch is None or batch['status'] != 'complete':
            reason = 'not_month_end_or_terminal_no_change' if batch is None else batch['reason']
            self.decisions.append(dict(day=day, status='NO_CHANGE', reason=reason))
            return SignalUnavailable(reason)
        else:
            candidates, reason = batch['candidates'], batch['reason']
            required = {row['symbol'] for row in candidates}
            issues = self.execution.raw_issues(day, required)
            references = self.execution.references(day, required)
            if issues or any(not self.execution.reference_ok(row) for row in references):
                self.decisions.append(dict(day=day, status='NO_CHANGE', reason='own_D_candidate_source_unknown',
                    raw_issues=issues, reference_evidence=references))
                return SignalUnavailable('own_D_candidate_source_unknown')
        self.decisions.append(dict(day=day, status='FULL_TARGET', reason=reason,
            candidates=deepcopy(candidates)))
        return SignalFrame.from_legacy(candidates, day, 'research:' + METHOD, context)


def capacity_batches(batches, capacity):
    if capacity not in SPEC['capacities']:
        raise ValueError('Only the two original leader capacity treatments are supported')
    output, seen = deepcopy(batches), []
    for day in sorted(output):
        batch = output[day]
        batch['original_top5'] = deepcopy(batch['candidates'])
        if capacity == 'uncapped' or batch['status'] != 'complete':
            continue
        for row in batch['original_top5']:
            if row['symbol'] not in seen and len(seen) < 20:
                seen.append(row['symbol'])
        batch['candidates'] = [row for row in batch['original_top5'] if row['symbol'] in seen]
        batch['causal_seen_symbols'] = list(seen)
        if batch['original_top5'] and not batch['candidates']:
            batch.update(status='incomplete', reason='capacity_filtered_all_NO_CHANGE')
    return output


def run(inputs, out, *, capacity):
    folder, inputs = Path(out).resolve(), Path(inputs).resolve()
    if folder.exists():
        raise FileExistsError('Benchmark replay requires a new output directory')
    built, registration = read(inputs / 'result.json'), read(inputs / 'registration.json')
    if (built.get('schema') != METHOD or registration.get('spec') != SPEC
            or digest(built['registration']) != built['registration_sha256']
            or digest(built['batches']) != built['batches_sha256']
            or digest(registration['trend_registration']) != registration['trend_registration_sha256']
            or digest(__file__) != registration['implementation_sha256']
            or digest(Path(__file__).with_name('low_risk.py')) != registration['feature_implementation_sha256']):
        raise ValueError('Exact saved benchmark source and method required')
    trend, loaded, manifest = load_trend_registration(registration['trend_registration'])
    if (trend['scope_members'] != registration['common_scope_members']
            or trend['config'] != registration['config']):
        raise ValueError('Common leader universe/config changed')
    context_raw = pd.read_parquet(manifest['context_raw_file'])
    days = loaded['source_days']
    sessions, _, _ = _calendar_view(loaded['calendar_payload'], '2026-01-01', '2026-12-31', [])
    following = sessions[sessions.index(days[-1]) + 1]
    raw = pd.concat([context_raw, loaded['raw']], ignore_index=True)
    base = ExactSourceRawFeed(raw, [manifest['context_anchor']['day']] + days + [following],
        trend['scope_members'], context_anchor=loaded['context_anchor'])
    accounting = load_action_accounting(trend['action_event_view']['directory'],
        trend['action_reference_view']['directory'], symbols=trend['scope_members'],
        raw_days=[manifest['context_anchor']['day']] + days, replay_days=days,
        price_source='independent_baostock_raw')
    execution = HistoricalExecutionPolicy(base, accounting, days)
    from ..config import StrategyConfig
    config = StrategyConfig(**trend['config']).validate()
    batches = capacity_batches(read(built['batches']), capacity)
    adapter = MonthlyFeed(execution, batches, config, trend['end'], days[-1])
    feed = _QualifiedFeed(adapter, loaded['qualification'])
    context = RunContext(METHOD + ':' + capacity + ':' + folder.name, TradingMode.REPLAY)
    state, daily, blocked = new_state(config.initial_cash), [], None
    folder.mkdir(parents=True, exist_ok=False)
    for index, day in enumerate(days):
        trial = deepcopy(state)
        try:
            required = {s for s, lots in state['positions'].items() if sum(lot['qty'] for lot in lots)}
            required.update(state.get('pending_targets') or {})
            issues, refs = execution.raw_issues(day, required), execution.references(day, required)
            ipo = [loaded['qualification'].assess(s, day, decision_as_of=day + 'T09:30:00+08:00')
                   for s in sorted(required)]
            if issues or any(not execution.reference_ok(row) for row in refs) or any(row['engine_block_reason'] for row in ipo):
                blocked = dict(day=day, reason='owned_or_pending_source_unknown', raw_issues=issues,
                    reference_evidence=refs, ipo_evidence=ipo, session_not_committed=True)
                break
            row = advance(trial, feed, day, config, index, context=context)
        except (ValueError, KeyError) as error:
            blocked = dict(day=day, reason='shared_engine_blocked', error=f'{type(error).__name__}: {error}',
                           session_not_committed=True)
            save(folder / 'failed_trial.json', trial)
            break
        state, daily = trial, daily + [row]
    residual = {s: sum(lot['qty'] for lot in lots) for s, lots in state['positions'].items() if sum(lot['qty'] for lot in lots)}
    complete = blocked is None and len(daily) == len(days) and not residual and bool(state['fills'])
    result = dict(schema=METHOD, status='completed_conditional_replay' if complete else 'incomplete_or_no_fills',
        created_at=now(), inputs=str(inputs), inputs_sha256=digest(inputs / 'result.json'), spec=SPEC, **FLAGS,
        start=trend['start'], end=trend['end'], source_days=days, capacity=capacity,
        common_scope_count=len(trend['scope_members']),
        config=config.to_dict(), engine_sessions_committed=len(daily), complete_PnL_available=complete,
        metrics=metrics(daily, config.initial_cash) if complete else None,
        blocking_evidence=blocked, residual_positions=residual, pending_targets=state.get('pending_targets'),
        parent_orders=len(state['orders']), fill_callbacks=len(state['fills']),
        action_accounting=accounting.provenance, old_leader_cases_replayed=False,
        price_signal_actions_adjusted=False, historical_publication_verified=False,
        champion_method_reproduction_claimed=False, comparison_kind='whole_strategy_not_pure_signal')
    for name, value in (('result', result), ('account', state), ('daily', daily),
                        ('decisions', adapter.decisions), ('orders', list(state['orders'].values())),
                        ('fills', list(state['fills'].values()))):
        save(folder / (name + '.json'), value)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build_parser = sub.add_parser('inputs')
    for key in ('trend-registration', 'native-view', 'out'):
        build_parser.add_argument('--' + key, type=Path, required=True)
    replay_parser = sub.add_parser('replay')
    for key in ('inputs', 'out'):
        replay_parser.add_argument('--' + key, type=Path, required=True)
    replay_parser.add_argument('--capacity', choices=SPEC['capacities'], required=True)
    args = parser.parse_args(argv)
    result = (build(args.trend_registration, args.native_view, args.out) if args.command == 'inputs'
              else run(args.inputs, args.out, capacity=args.capacity))
    print(json.dumps({key: result.get(key) for key in ('status', 'history_sessions', 'monthly_batches',
        'engine_sessions_committed', 'complete_PnL_available', 'metrics', 'blocking_evidence')}, ensure_ascii=False))
    return 0
