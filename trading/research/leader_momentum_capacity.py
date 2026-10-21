"""Fresh annual momentum accounts with explicit per-order capacity and exit persistence.

Consumes the unchanged annual inputs. It neither resumes the old failed accounts
nor reconstructs their prefix under a different execution or exit policy.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE
from . import leader_momentum_year as annual
from .experiment import baseline_ledger_result
from .leader_eight_type_historical_native import load_posthoc_listing, with_prior_listing_witness
from .leader_historical_execution_policy import HistoricalExecutionPolicy
from .leader_momentum_benchmark import FLAGS, SPEC as BENCHMARK_SPEC
from .leader_momentum_year import AnnualFeed, _AnnualQualifiedFeed, digest, now, pin, read, save
from .order_capacity_replay import METHOD_ID as CAPACITY_METHOD, PriorVolumeReplayPolicy
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .stockdb_action_accounting import load_action_accounting


METHOD = 'native_12_1_annual_per_order_capacity_persistent_full_exit_v1'
CASES = [dict(capacity=capacity, capital_policy=capital)
         for capital in annual.CAPITAL_POLICIES for capacity in BENCHMARK_SPEC['capacities']]
SPEC = dict(id=METHOD, annual_signal_method=annual.METHOD, annual_signal_spec=annual.SPEC,
    fixed_cases=CASES, capacity_method=CAPACITY_METHOD,
    unknown_prior_volume='block_only_that_order_preserve_unknown_not_zero_no_global_capacity_stop',
    full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE,
    unfinished_full_exits='retry_exact_later_sessions_until_filled_or_replaced_by_new_full_target',
    account='fresh_original_initial_cash_each_case_no_checkpoint_or_old73_session_prefix',
    rule_change='new_execution_and_exit_method_not_original_annual_strategy_completion',
    source_features_ranks_universe_capital_filter_and_fees='unchanged_saved_annual_inputs',
    context='standalone_replay_REPLAY_matching_existing_experiment_runner',
    independent_holdout_claimed=False, parameters_selected_by_return=False,
    old_failed_accounts_modified=False, original_source_rebuilt=False)


def _checked_inputs(inputs):
    inputs = Path(inputs).resolve()
    manifest, plan = read(inputs / 'manifest.json'), read(inputs / 'study_plan.json')
    if (manifest.get('schema') != annual.METHOD or manifest.get('status') != 'completed_annual_inputs'
            or plan.get('schema') != annual.METHOD or plan.get('spec') != annual.SPEC
            or Path(manifest['study_plan']['path']).resolve() != inputs / 'study_plan.json'
            or Path(plan['implementation']['path']).resolve() != Path(annual.__file__).resolve()
            or any(manifest.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError('Exact unchanged completed annual inputs and method required')
    refs = [pin(inputs / 'manifest.json'), manifest['study_plan'], plan['implementation'],
        plan['master'], plan['benchmark_implementation'], plan['feature_implementation'],
        plan['capital_implementation'], plan['close_engine_implementation'],
        *plan['calendars'], plan['action_event_view'], plan['action_reference_view']]
    for name in ('raw_daily.parquet', 'context.parquet', 'batches.json', 'cohort.json', 'listing_witness.json'):
        ref = manifest['files'][name]
        if Path(ref['path']).resolve() != inputs / name:
            raise ValueError('Annual saved input path changed: ' + name)
        refs.append(ref)
    # The saved annual raw/context and ranks are the actual consumed snapshot.
    # Their hashes and the immutable plan retain upstream lineage; this method
    # does not reopen or rehash the unused multi-year native ancestor.
    for ref in refs:
        if digest(ref['path']) != ref['sha256']:
            raise ValueError('Pinned annual source or implementation changed: ' + ref['path'])
    return inputs, manifest, plan, refs


def _qualification(inputs, manifest, plan):
    qualification = load_posthoc_listing(plan['master']['path'], plan['calendars'][0]['path'],
        observed_at=manifest['qualification']['posthoc_observed_at'])
    qualification = with_prior_listing_witness(qualification,
        read(inputs / 'listing_witness.json'), manifest['start'])
    if qualification.verification != manifest['qualification']:
        raise ValueError('Saved annual listing qualification changed')
    return qualification


def _runtime_pins():
    research = Path(__file__).resolve().parent
    return [pin(research / name) for name in (
        'experiment.py', 'order_capacity_replay.py', 'leader_historical_execution_policy.py')
        ] + [pin(research.parent / 'target_exit_lifecycle.py')]


def register(inputs, out):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Capacity registration requires a new directory')
    inputs, manifest, plan, refs = _checked_inputs(inputs)
    _qualification(inputs, manifest, plan)
    registration = dict(schema=METHOD, status='registered_fixed_four_case_research', registered_at=now(),
        spec=SPEC, **FLAGS, inputs=str(inputs), source_pins=refs,
        implementation=pin(__file__), execution_implementations=_runtime_pins(),
        start=manifest['start'], end=manifest['end'], through=manifest['through'],
        source_days=manifest['source_days'], config=plan['config'], fixed_cases=CASES,
        annual_inputs_reused=True, old_failed_accounts_reused=False,
        upstream_native_files_reread=False,
        new_PnL_computed_at_registration=False)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'registration.json', registration)
    return registration


def _load_registration(path):
    path = Path(path).resolve()
    registration = read(path)
    clock = datetime.fromisoformat(registration['registered_at'])
    if (registration.get('schema') != METHOD or registration.get('spec') != SPEC
            or registration.get('fixed_cases') != CASES
            or clock.utcoffset() is None or clock > datetime.now().astimezone()
            or registration.get('implementation') != pin(__file__)
            or any(registration.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError('Exact registered capacity/lifecycle method required')
    for ref in registration['execution_implementations']:
        if digest(ref['path']) != ref['sha256']:
            raise ValueError('Pinned capacity/lifecycle implementation changed: ' + ref['path'])
    inputs, manifest, plan, refs = _checked_inputs(registration['inputs'])
    if (refs != registration['source_pins'] or registration['config'] != plan['config']
            or any(registration[key] != manifest[key] for key in ('start', 'end', 'through', 'source_days'))):
        raise ValueError('Registered annual source or comparison bounds changed')
    return path, registration, inputs, manifest, plan


def run(registration, out, *, capacity, capital_policy):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Capacity replay requires a new account directory')
    registration_path, registered, inputs, manifest, plan = _load_registration(registration)
    if dict(capacity=capacity, capital_policy=capital_policy) not in CASES:
        raise ValueError('Only the registered fixed four cases are supported')
    qualification = _qualification(inputs, manifest, plan)
    members = read(inputs / 'cohort.json')['scope_members']
    days = manifest['source_days']
    raw = pd.concat([pd.read_parquet(inputs / 'context.parquet'),
        pd.read_parquet(inputs / 'raw_daily.parquet')], ignore_index=True)
    if len(raw) != manifest['raw_rows'] + len(raw.loc[raw.date.eq(manifest['context_day'])]):
        raise ValueError('Annual raw row count changed')
    calendar = [manifest['context_day']] + days + [manifest['next_session_after_year']]
    base = ExactSourceRawFeed(raw, calendar, members,
        context_anchor=dict(day=manifest['context_day'], closes={}))
    accounting = load_action_accounting(Path(plan['action_event_view']['path']).parent,
        Path(plan['action_reference_view']['path']).parent, symbols=members,
        raw_days=[manifest['context_day']] + days, replay_days=days,
        price_source='independent_baostock_raw')
    context = RunContext('standalone_replay', TradingMode.REPLAY)
    policy = PriorVolumeReplayPolicy(context)
    execution = HistoricalExecutionPolicy(base, accounting, days, prior_volume_policy=policy)
    config = StrategyConfig(**plan['config']).validate()
    adapter = AnnualFeed(execution, read(inputs / 'batches.json'), config,
        manifest['end'], days[-1], qualification, capacity, capital_policy)
    feed = _AnnualQualifiedFeed(adapter, qualification)
    ledger_result = baseline_ledger_result(feed, config, days, folder,
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    account = read(folder / 'account.json')
    residual = {symbol: sum(lot['qty'] for lot in lots)
        for symbol, lots in account['positions'].items() if sum(lot['qty'] for lot in lots)}
    residual_exits = (account.get('full_exit_lifecycle') or {}).get('remaining_full_exits', {})
    complete = (ledger_result['status'] == 'completed' and bool(account['fills'])
        and not residual and account.get('pending_targets') is None and not residual_exits)
    exceptions = [event for event in account['events'] if event['kind'] in {
        'order_blocked', 'rejected', 'cancelled', 'rebalance_deferred', 'risk_exit_deferred',
        'full_exit_remaining', 'full_exit_completed', 'full_exit_replaced_by_new_target', 'full_target_version'}]
    save(folder / 'execution_exceptions.json', dict(schema=METHOD, events=exceptions,
        counts=dict(Counter(event['kind'] for event in exceptions))))
    save(folder / 'execution_checks.json', execution.checks)
    report = dict(schema=METHOD, status='completed_conditional_replay' if complete else 'incomplete_or_unresolved',
        created_at=now(), registration=pin(registration_path), spec=SPEC, **FLAGS,
        capacity=capacity, capital_policy=capital_policy, config=config.to_dict(),
        cohort_count=len(members), start=manifest['start'], end=manifest['end'], through=manifest['through'],
        inputs=pin(inputs / 'manifest.json'), ledger_result=ledger_result,
        complete_PnL_available=complete, residual_positions=residual, residual_full_exits=residual_exits,
        pending_targets=account.get('pending_targets'),
        metrics={key: value for key, value in ledger_result.items() if key not in
            ('status', 'completed_sessions', 'orders', 'fills', 'reused_sessions',
             'newly_completed_sessions', 'turnover_over_initial')} if complete else None,
        source_blocks=adapter.blocks, action_accounting=accounting.provenance,
        raw_reference_checks=pin(folder / 'execution_checks.json'),
        execution_exceptions=pin(folder / 'execution_exceptions.json'),
        parent_orders=len(account['orders']), fill_callbacks=len(account['fills']),
        execution_context=context.to_dict(), prior_volume_policy=policy.method_id,
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE,
        per_case_independent_cash=True, old_failed_accounts_modified=False,
        old_failed_prefix_reused=False, annual_inputs_rebuilt=False)
    save(folder / 'result.json', report)
    save(folder / 'decisions.json', adapter.decisions)
    save(folder / 'capital_admission.json', adapter.admission)
    return report


replay = run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    registration_parser = commands.add_parser('register')
    for name in ('inputs', 'out'):
        registration_parser.add_argument('--' + name, type=Path, required=True)
    replay_parser = commands.add_parser('replay')
    for name in ('registration', 'out'):
        replay_parser.add_argument('--' + name, type=Path, required=True)
    replay_parser.add_argument('--capacity', choices=BENCHMARK_SPEC['capacities'], required=True)
    replay_parser.add_argument('--capital-policy', choices=annual.CAPITAL_POLICIES, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    result = (register if command == 'register' else run)(**args)
    print(json.dumps({key: result.get(key) for key in ('schema', 'status', 'registered_at',
        'cohort_count', 'complete_PnL_available', 'ledger_result', 'metrics')}, ensure_ascii=False))
    return 0
