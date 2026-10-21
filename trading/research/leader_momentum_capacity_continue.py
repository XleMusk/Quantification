"""Continue fixed capacity accounts after an append-only future reference correction.

The existing checkpoint runner retains the full ledger and calendar prefix.
Only independently reviewed reference formulas after every saved close may differ.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE
from . import leader_momentum_capacity as base_method
from .adjusted_prices import checked_file
from .experiment import baseline_ledger_result, load_replay_checkpoint
from .leader_historical_execution_policy import HistoricalExecutionPolicy
from .leader_momentum_benchmark import FLAGS
from .leader_momentum_year import AnnualFeed, _AnnualQualifiedFeed, METHOD as ANNUAL_METHOD, digest, now, pin, read, save
from .order_capacity_replay import PriorVolumeReplayPolicy, validate_binding as validate_capacity_binding
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .stockdb_action_accounting import load_action_accounting


METHOD = 'native_12_1_capacity_source_only_checkpoint_continuation_v1'
CASES = deepcopy(base_method.CASES)
SPEC = dict(id=METHOD, base_method=base_method.METHOD, fixed_cases=CASES,
    change='append_reviewed_reference_formulas_strictly_after_every_checkpoint_close_only',
    entitlements_raw_prices_features_cohort_fees_capital_policy_and_exit_lifecycle='unchanged',
    continuation='existing_full_calendar_checkpoint_runner_no_cash_reset_or_prefix_replay',
    cap20_restore='original_complete_top5_or_saved_complete_pre_cap_capital_admissions',
    capital_prefix_budgets_recomputed=False, full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE,
    independent_holdout_claimed=False, parameters_selected_by_return=False,
    upstream_native_files_reread=False, old_accounts_modified=False)
FORMULA_FIELDS = ('reference_formula_status', 'reference_formula_basis',
    'expected_reference_rmb', 'reference_error_rmb')
CHECKPOINT_FILES = ('result.json', 'account.json', 'replay.json', 'daily.csv',
    'decisions.json', 'capital_admission.json', 'execution_checks.json', 'execution_exceptions.json')
EXCEPTION_KINDS = {'order_blocked', 'rejected', 'cancelled', 'rebalance_deferred',
    'risk_exit_deferred', 'full_exit_remaining', 'full_exit_completed',
    'full_exit_replaced_by_new_target', 'full_target_version'}


def check_reference_append(old_view, new_view, event_view, last_days):
    """Validate the new frozen reference view without rescanning native prices."""
    old_view, new_view, event_view = (Path(value).resolve() for value in (old_view, new_view, event_view))
    old, new = read(old_view / 'result.json'), read(new_view / 'result.json')
    amendment = new.get('distribution_amendment', {})
    last_days = list(last_days)
    if (old_view == new_view or not last_days or any(not isinstance(day, str) for day in last_days)
            or old.get('status') != 'research_reference_presence_diagnostic'
            or new.get('status') != old['status']
            or old.get('execution_admitted') is not False or new.get('execution_admitted') is not False
            or Path(old['event_view']).resolve() != event_view or Path(new['event_view']).resolve() != event_view
            or Path(amendment.get('parent_reference_view', '')).resolve() != old_view
            or amendment.get('entitlements_changed') is not False
            or amendment.get('raw_prices_rescanned') is not False):
        raise ValueError('Only an append-only source reference amendment on the unchanged event view is allowed')
    mutable_files = {'raw_reference_changes.parquet', 'distribution_reference_evidence.json'}
    if not set(old['files']) <= set(new['files']):
        raise ValueError('New reference view lost an original pinned file')
    for name, expected in old['files'].items():
        checked_file(old_view, name, expected)
        if name not in mutable_files and new['files'][name] != expected:
            raise ValueError('Non-formula reference source file changed: ' + name)
    file_pins = {name: dict(path=str(checked_file(new_view, name, expected)), sha256=expected)
                 for name, expected in new['files'].items()}
    old_evidence = read(checked_file(old_view, 'distribution_reference_evidence.json',
        old['files']['distribution_reference_evidence.json']))
    new_evidence = read(file_pins['distribution_reference_evidence.json']['path'])
    if (not isinstance(old_evidence, list) or not isinstance(new_evidence, list)
            or new_evidence[:len(old_evidence)] != old_evidence or len(new_evidence) <= len(old_evidence)):
        raise ValueError('Distribution evidence must preserve the complete original list prefix')
    keys = [(row['code'], row['dividOperateDate']) for row in new_evidence]
    if len(keys) != len(set(keys)):
        raise ValueError('Distribution evidence has duplicate event keys')
    added = new_evidence[len(old_evidence):]
    added_keys = {(row['code'], row['dividOperateDate']) for row in added}
    if (any(ex <= saved_day for _, ex in added_keys for saved_day in last_days)
            or sorted(map(tuple, amendment.get('events', []))) != sorted(added_keys)):
        raise ValueError('Added reference events must be strictly after every checkpoint close')
    old_gaps = pd.read_parquet(checked_file(old_view, 'raw_reference_changes.parquet',
        old['files']['raw_reference_changes.parquet']))
    new_gaps = pd.read_parquet(file_pins['raw_reference_changes.parquet']['path'])
    if (list(new_gaps.columns) != list(old_gaps.columns) or len(new_gaps) != len(old_gaps)
            or not set(FORMULA_FIELDS) <= set(old_gaps.columns)):
        raise ValueError('Frozen raw reference schema or row identities changed')
    immutable = [column for column in old_gaps.columns if column not in FORMULA_FIELDS]
    mask = pd.Series([(code, day) in added_keys for code, day in zip(old_gaps.code, old_gaps.date)],
                     index=old_gaps.index)
    if (set(zip(old_gaps.loc[mask, 'code'], old_gaps.loc[mask, 'date'])) != added_keys
            or not new_gaps[immutable].equals(old_gaps[immutable])
            or not new_gaps.loc[~mask].equals(old_gaps.loc[~mask])):
        raise ValueError('Reference amendment changed raw/non-formula facts or an unselected event')
    return dict(old_reference=pin(old_view / 'result.json'), new_reference=pin(new_view / 'result.json'),
        event_view=str(event_view), events=sorted(added_keys), added_events=sorted(added_keys),
        added_evidence_count=len(added), reference_rows_refreshed=int(mask.sum()), files=file_pins,
        entitlements_changed=False, raw_prices_rescanned=False)


def restore_feed_prefix(adapter, batches, decisions, admissions, last_day):
    """Restore stateful capacity from saved causal traces, never old equity."""
    if not isinstance(decisions, list) or not isinstance(admissions, list):
        raise ValueError('Saved decision and capital-admission list prefixes required')
    if any(row['day'] > last_day for row in decisions + admissions):
        raise ValueError('Only committed decision/admission prefixes may be restored')
    completed = {day: batch for day, batch in batches.items()
                 if day <= last_day and day < adapter.end and batch['status'] == 'complete'}
    saved_admissions = {}
    if adapter.capital_policy == 'known_close_minimum_entry':
        for row in admissions:
            day = row['day']
            if day in saved_admissions or day not in completed:
                raise ValueError('Saved monthly capital admission is duplicate or outside a complete batch')
            saved_admissions[day] = row
        if set(saved_admissions) != set(completed):
            raise ValueError('Every committed complete monthly batch needs its saved capital admission')
    seen = []
    for day in sorted(completed):
        batch = completed[day]
        if adapter.capital_policy == 'known_close_minimum_entry':
            admission = saved_admissions[day]
            if admission['status'] != 'complete':
                continue
            candidates = admission['candidates']
            remaining = iter(batch['candidates'])
            if any(not any(row == original for original in remaining) for row in candidates):
                raise ValueError('Saved capital candidates must preserve the original full ranks')
        else:
            candidates = batch['candidates'][:5]
        if adapter.capacity == 'causal_first_seen_cap20':
            for row in candidates:
                if row['symbol'] not in seen and len(seen) < 20:
                    seen.append(row['symbol'])
    adapter.seen = seen
    adapter.decisions = deepcopy(decisions)
    adapter.admission = deepcopy(admissions)


def _checkpoint_entry(folder, base_registration, inputs, config, days):
    folder = Path(folder).resolve()
    result = read(folder / 'result.json')
    case = dict(capacity=result['capacity'], capital_policy=result['capital_policy'])
    if (result.get('schema') != base_method.METHOD or case not in CASES
            or result.get('registration') != pin(base_registration)
            or result.get('inputs') != pin(inputs / 'manifest.json') or result.get('config') != config.to_dict()):
        raise ValueError('Checkpoint must belong to one exact original registered capacity case')
    checkpoint = load_replay_checkpoint(folder, config, days,
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    state = checkpoint['state']
    context = RunContext('standalone_replay', TradingMode.REPLAY)
    validate_capacity_binding(state, PriorVolumeReplayPolicy(context), context, config,
        RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    if (state.get('close_candidate_policy') != dict(id=ANNUAL_METHOD, **case)
            or result.get('execution_context') != context.to_dict()
            or not checkpoint['daily'] or len(checkpoint['daily']) >= len(days)):
        raise ValueError('Checkpoint context, original close policy or unfinished calendar changed')
    entry = dict(directory=str(folder), **case, day=state['day'],
        completed_sessions=len(checkpoint['daily']), files={name: pin(folder / name) for name in CHECKPOINT_FILES})
    return entry, checkpoint


def _runtime_pins():
    return [pin(base_method.__file__), *base_method._runtime_pins()]


def register(base_registration, reference_view, checkpoint, out):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Continuation registration requires a new directory')
    base_path, _, inputs, manifest, plan = base_method._load_registration(base_registration)
    config = StrategyConfig(**plan['config']).validate()
    paths = [Path(path).resolve() for path in checkpoint]
    if len(paths) != 4 or len(set(paths)) != 4:
        raise ValueError('Exactly four distinct original case checkpoints required')
    batches = read(inputs / 'batches.json')
    entries = []
    for path in paths:
        entry, saved = _checkpoint_entry(path, base_path, inputs, config, manifest['source_days'])
        # Validate that every stateful feed prefix is restorable before registration.
        from types import SimpleNamespace
        adapter = SimpleNamespace(end=manifest['end'], capacity=entry['capacity'], capital_policy=entry['capital_policy'])
        restore_feed_prefix(adapter, batches, read(path / 'decisions.json'),
            read(path / 'capital_admission.json'), entry['day'])
        entry['restored_seen_symbols'] = adapter.seen
        entries.append(entry)
    if sorted((row['capacity'], row['capital_policy']) for row in entries) != sorted(
            (row['capacity'], row['capital_policy']) for row in CASES):
        raise ValueError('Each fixed capacity/capital-policy case needs exactly one checkpoint')
    entries.sort(key=lambda row: (row['capital_policy'], row['capacity']))
    amendment = check_reference_append(Path(plan['action_reference_view']['path']).parent,
        reference_view, Path(plan['action_event_view']['path']).parent, [row['day'] for row in entries])
    registration = dict(schema=METHOD, status='registered_source_only_continuation', registered_at=now(),
        spec=SPEC, **FLAGS, fixed_cases=CASES, base_registration=pin(base_path),
        reference_append=amendment, checkpoints=entries, implementation=pin(__file__),
        execution_implementations=_runtime_pins(), config=config.to_dict(),
        start=manifest['start'], end=manifest['end'], through=manifest['through'],
        source_days=manifest['source_days'], checkpoint_accounts_modified=False,
        new_PnL_computed_at_registration=False, upstream_native_files_reread=False)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'registration.json', registration)
    return registration


def _load_registration(path):
    path = Path(path).resolve()
    registration = read(path)
    clock = datetime.fromisoformat(registration['registered_at'])
    if (registration.get('schema') != METHOD or registration.get('spec') != SPEC
            or registration.get('fixed_cases') != CASES or registration.get('implementation') != pin(__file__)
            or clock.utcoffset() is None or clock > datetime.now().astimezone()
            or registration.get('execution_implementations') != _runtime_pins()
            or any(registration.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError('Exact source-only continuation registration required')
    ref = registration['base_registration']
    if digest(ref['path']) != ref['sha256']:
        raise ValueError('Original capacity registration changed')
    base_path, _, inputs, manifest, plan = base_method._load_registration(ref['path'])
    if (registration['config'] != plan['config'] or any(registration[key] != manifest[key]
            for key in ('start', 'end', 'through', 'source_days'))):
        raise ValueError('Original strategy, fees or full calendar changed')
    entries = registration['checkpoints']
    if sorted((row['capacity'], row['capital_policy']) for row in entries) != sorted(
            (row['capacity'], row['capital_policy']) for row in CASES):
        raise ValueError('Registered checkpoint cases changed')
    for entry in entries:
        for name, source in entry['files'].items():
            if (Path(source['path']).resolve() != Path(entry['directory']) / name
                    or digest(source['path']) != source['sha256']):
                raise ValueError('Pinned original checkpoint changed: ' + source['path'])
        if set(entry['files']) != set(CHECKPOINT_FILES):
            raise ValueError('Complete original checkpoint artifact pins required')
    amendment = registration['reference_append']
    checked = check_reference_append(Path(plan['action_reference_view']['path']).parent,
        Path(amendment['new_reference']['path']).parent,
        Path(plan['action_event_view']['path']).parent, [row['day'] for row in entries])
    # JSON stores event tuples as lists.
    if json.loads(json.dumps(checked)) != amendment:
        raise ValueError('Registered source-only reference amendment changed')
    return path, registration, base_path, inputs, manifest, plan


def run(registration, out, *, capacity, capital_policy):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Continuation replay requires a new output directory')
    registration_path, registered, base_path, inputs, manifest, plan = _load_registration(registration)
    selected = [row for row in registered['checkpoints']
                if row['capacity'] == capacity and row['capital_policy'] == capital_policy]
    if len(selected) != 1:
        raise ValueError('Only one of the registered fixed four cases may be resumed')
    source = selected[0]
    config = StrategyConfig(**plan['config']).validate()
    entry, checkpoint = _checkpoint_entry(source['directory'], base_path, inputs, config, manifest['source_days'])
    if any(entry[key] != source[key] for key in entry):
        raise ValueError('Registered checkpoint prefix changed')
    qualification = base_method._qualification(inputs, manifest, plan)
    members, days = read(inputs / 'cohort.json')['scope_members'], manifest['source_days']
    raw = pd.concat([pd.read_parquet(inputs / 'context.parquet'),
        pd.read_parquet(inputs / 'raw_daily.parquet')], ignore_index=True)
    if len(raw) != manifest['raw_rows'] + len(raw.loc[raw.date.eq(manifest['context_day'])]):
        raise ValueError('Saved annual raw row count changed')
    calendar = [manifest['context_day']] + days + [manifest['next_session_after_year']]
    base = ExactSourceRawFeed(raw, calendar, members,
        context_anchor=dict(day=manifest['context_day'], closes={}))
    reference_view = Path(registered['reference_append']['new_reference']['path']).parent
    accounting = load_action_accounting(Path(plan['action_event_view']['path']).parent, reference_view,
        symbols=members, raw_days=[manifest['context_day']] + days, replay_days=days,
        price_source='independent_baostock_raw')
    context = RunContext('standalone_replay', TradingMode.REPLAY)
    policy = PriorVolumeReplayPolicy(context)
    execution = HistoricalExecutionPolicy(base, accounting, days, prior_volume_policy=policy)
    batches = read(inputs / 'batches.json')
    adapter = AnnualFeed(execution, batches, config, manifest['end'], days[-1],
        qualification, capacity, capital_policy)
    restore_feed_prefix(adapter, batches, read(source['files']['decisions.json']['path']),
        read(source['files']['capital_admission.json']['path']), source['day'])
    if adapter.seen != source['restored_seen_symbols']:
        raise ValueError('Registered causal capacity prefix changed')
    feed = _AnnualQualifiedFeed(adapter, qualification)
    ledger_result = baseline_ledger_result(feed, config, days, folder, checkpoint=checkpoint,
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    account = read(folder / 'account.json')
    residual = {symbol: sum(lot['qty'] for lot in lots)
        for symbol, lots in account['positions'].items() if sum(lot['qty'] for lot in lots)}
    residual_exits = (account.get('full_exit_lifecycle') or {}).get('remaining_full_exits', {})
    complete = (ledger_result['status'] == 'completed'
        and ledger_result.get('completed_sessions') == len(days) and bool(account['fills'])
        and not residual and account.get('pending_targets') is None and not residual_exits)
    exceptions = [event for event in account['events'] if event['kind'] in EXCEPTION_KINDS]
    original_exceptions = read(source['files']['execution_exceptions.json']['path'])['events']
    if exceptions[:len(original_exceptions)] != original_exceptions:
        raise ValueError('Original committed execution-exception prefix changed')
    save(folder / 'execution_exceptions.json', dict(schema=METHOD, scope='full_committed_account_history',
        parent=source['files']['execution_exceptions.json'], events=exceptions,
        counts=dict(Counter(event['kind'] for event in exceptions))))
    save(folder / 'execution_checks.json', dict(schema=METHOD, scope='continuation_checks_only_not_rebuilt_prefix',
        parent=source['files']['execution_checks.json'], prior_last_completed_day=source['day'],
        first_continuation_day=days[source['completed_sessions']], checks=execution.checks))
    prior_result = read(source['files']['result.json']['path'])
    report = dict(schema=METHOD, status='completed_conditional_replay' if complete else 'incomplete_or_unresolved',
        created_at=now(), registration=pin(registration_path), spec=SPEC, **FLAGS,
        capacity=capacity, capital_policy=capital_policy, config=config.to_dict(), cohort_count=len(members),
        start=manifest['start'], end=manifest['end'], through=manifest['through'],
        inputs=pin(inputs / 'manifest.json'), ledger_result=ledger_result, complete_PnL_available=complete,
        metrics={key: value for key, value in ledger_result.items() if key not in
            ('status', 'completed_sessions', 'orders', 'fills', 'reused_sessions',
             'newly_completed_sessions', 'turnover_over_initial')} if complete else None,
        reused_sessions=source['completed_sessions'],
        newly_completed_sessions=read(folder / 'replay.json')['newly_completed_sessions'],
        residual_positions=residual, residual_full_exits=residual_exits, pending_targets=account.get('pending_targets'),
        parent_orders=len(account['orders']), fill_callbacks=len(account['fills']),
        checkpoint=source, source_blocks=adapter.blocks, prior_source_blocks=prior_result.get('source_blocks', []),
        prior_ledger_result=prior_result['ledger_result'], action_accounting=accounting.provenance,
        raw_reference_checks=pin(folder / 'execution_checks.json'),
        execution_exceptions=pin(folder / 'execution_exceptions.json'), execution_context=context.to_dict(),
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE, prior_volume_policy=policy.method_id,
        source_reference_append=registered['reference_append'], checkpoint_accounts_modified=False,
        cash_reset=False, old_committed_sessions_replayed=False, upstream_native_files_reread=False)
    save(folder / 'result.json', report)
    save(folder / 'decisions.json', adapter.decisions)
    save(folder / 'capital_admission.json', adapter.admission)
    return report


replay = run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    registration_parser = commands.add_parser('register')
    for name in ('base-registration', 'reference-view', 'out'):
        registration_parser.add_argument('--' + name, type=Path, required=True)
    registration_parser.add_argument('--checkpoint', type=Path, action='append', required=True)
    replay_parser = commands.add_parser('replay')
    for name in ('registration', 'out'):
        replay_parser.add_argument('--' + name, type=Path, required=True)
    replay_parser.add_argument('--capacity', choices=base_method.annual.BENCHMARK_SPEC['capacities'], required=True)
    replay_parser.add_argument('--capital-policy', choices=base_method.annual.CAPITAL_POLICIES, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    result = (register if command == 'register' else run)(**args)
    print(json.dumps({key: result.get(key) for key in ('schema', 'status', 'registered_at',
        'cohort_count', 'reused_sessions', 'newly_completed_sessions',
        'complete_PnL_available', 'ledger_result', 'metrics')}, ensure_ascii=False))
    return 0
