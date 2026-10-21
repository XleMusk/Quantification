"""Chain source-only continuations of the fixed annual capacity accounts.

Uses the existing reference-append/reviewed-action protocols and checkpoint
runner. A saved registration quantity is retained; past economic effects are
never recalculated or repaired by this consumer.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..symbols import normalize_symbol
from ..target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE
from . import leader_momentum_capacity as base_method
from . import leader_momentum_capacity_continue as previous
from .adjusted_prices import checked_file
from .experiment import baseline_ledger_result, load_replay_checkpoint
from .leader_environment_exit_ablation import _checked_supplement_pair
from .leader_historical_execution_policy import HistoricalExecutionPolicy
from .leader_momentum_benchmark import FLAGS
from .leader_momentum_year import AnnualFeed, _AnnualQualifiedFeed, METHOD as ANNUAL_METHOD, digest, now, pin, read, save
from .order_capacity_replay import PriorVolumeReplayPolicy, validate_binding as validate_capacity_binding
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .stockdb_action_accounting import load_action_accounting


METHOD = 'native_12_1_chained_source_only_checkpoint_continuation_v1'
CASES = deepcopy(previous.CASES)
SPEC = dict(id=METHOD, fixed_cases=CASES,
    parent_methods=[previous.METHOD, METHOD], source_protocols=['distribution_amendment', 'reviewed_cash_amendment'],
    action_change='only_explicit_blank_amount_to_known_with_existing_dates_and_known_terms_unchanged',
    consumed_prefix='future_ex_and_pay_only_existing_record_quantity_retained_no_past_economic_repair',
    continuation='same_checkpoint_Engine_context_calendar_fees_pending_lots_and_cap20_no_cash_reset',
    full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE, independent_holdout_claimed=False,
    parameters_selected_by_return=False, upstream_native_files_reread=False, old_accounts_modified=False)
AMOUNTS = ('dividCashPsBeforeTax', 'dividStocksPs', 'dividReserveToStockPs', 'bonus_taxable_cash_ps')


def check_unconsumed_action(action, account):
    """Allow a known past registration, but never a consumed economic event."""
    last, symbol, ex = account['day'], action['symbol'], action['dividOperateDate']
    record, pay = action.get('dividRegistDate', ''), action.get('dividPayDate', '')
    try:
        for value in (last, record, ex, pay):
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError('Noncanonical action date')
    except (TypeError, ValueError) as error:
        raise ValueError('Source continuation requires known record/ex/payment dates') from error
    if not record < ex <= pay or ex <= last or pay <= last:
        raise ValueError('Unknown or nonfuture economic action dates cannot repair a checkpoint')
    identifier = f'{symbol}:{ex}'
    registered = account['entitlements'].get(identifier)
    if record <= last and (type(registered) is not int or registered < 0):
        raise ValueError('Past registration requires its existing explicit registered quantity')
    if registered is not None and (type(registered) is not int or registered < 0):
        raise ValueError('Invalid saved registered quantity')
    if identifier in account.get('receivables', {}):
        raise ValueError('This event already has an economic receivable; past accounting cannot be repaired')
    for event in account['events']:
        exact_id = event.get('action_id', event.get('identifier')) == identifier
        exact_date = (event.get('symbol') == symbol and
            ((event.get('kind') == 'corporate_action' and event.get('day') == ex)
             or (event.get('kind') == 'dividend_paid' and event.get('day') == pay)))
        if event.get('kind') in {'corporate_action', 'dividend_paid'} and (exact_id or exact_date):
            raise ValueError('This event already has economic action/payment history')
    return dict(event=[symbol, ex], record=record, pay=pay, checkpoint_day=last,
        registered_quantity=registered, existing_registration_preserved=record <= last,
        economic_effects_already_consumed=False)


def _pair_pin(event_view, reference_view):
    result = {}
    for key, folder in (('event', event_view), ('reference', reference_view)):
        folder = Path(folder).resolve()
        value = read(folder / 'result.json')
        result[key] = dict(directory=str(folder), result=pin(folder / 'result.json'),
            files={name: dict(path=str(checked_file(folder, name, sha)), sha256=sha)
                   for name, sha in value['files'].items()})
    return result


def check_source_change(old_event, old_reference, new_event, new_reference, accounts):
    """Accept only the two existing amendment protocols on the exact parent pair."""
    old_event, old_reference, new_event, new_reference = (
        Path(value).resolve() for value in (old_event, old_reference, new_event, new_reference))
    if not accounts:
        raise ValueError('Source changes require saved checkpoint accounts')
    if new_event == old_event:
        proof = previous.check_reference_append(old_reference, new_reference, old_event,
            [account['day'] for account in accounts])
        return dict(kind='distribution_amendment', proof=proof,
            pair=_pair_pin(new_event, new_reference))
    event, reference = read(new_event / 'result.json'), read(new_reference / 'result.json')
    pair_reference, reference_append = new_reference, None
    old_reference_result = read(old_reference / 'result.json')
    if reference['files'].get('distribution_reference_evidence.json') != old_reference_result['files'].get(
            'distribution_reference_evidence.json'):
        # The existing action amendment leaves distribution evidence unchanged.
        # A separately produced final formula append must validate its own exact
        # intermediate parent before the reviewed action-pair check is applied.
        distribution = reference.get('distribution_amendment', {})
        if not distribution.get('parent_reference_view'):
            raise ValueError('Changed distribution evidence requires its existing reference-append protocol')
        pair_reference = Path(distribution['parent_reference_view']).resolve()
        reference_append = previous.check_reference_append(pair_reference, new_reference, new_event,
            [account['day'] for account in accounts])
        reference = read(pair_reference / 'result.json')
    amendment = event.get('reviewed_cash_amendment', {})
    if (Path(amendment.get('parent_event_view', '')).resolve() != old_event
            or Path(amendment.get('parent_reference_view', '')).resolve() != old_reference
            or reference.get('reviewed_cash_amendment') != amendment):
        raise ValueError('Reviewed action amendment must use the exact current parent pair')
    keys = {tuple(key) for key in amendment.get('events', [])}
    if not keys:
        raise ValueError('Explicit reviewed action event keys required')
    if reference_append and {
            (normalize_symbol(code).vt_symbol, ex) for code, ex in reference_append['added_events']} != keys:
        raise ValueError('Compound reference append must address exactly the reviewed action events')
    original = dict(action_event_view=dict(directory=str(old_event)),
        action_reference_view=dict(directory=str(old_reference)))
    # Reuse the existing reviewed amendment's file, coverage, identity and raw-gap checks.
    pair_proof, right, _ = _checked_supplement_pair(original, new_event, pair_reference, keys)
    before = read(old_event / 'result.json')
    left = pd.read_parquet(checked_file(old_event, 'canonical_actions.parquet',
        before['files']['canonical_actions.parquet'])).fillna('')
    indexed = right.set_index(['symbol', 'dividOperateDate'])
    changes, consumption = [], []
    for symbol, ex in sorted(keys):
        rows = left.loc[left.symbol.eq(symbol) & left.dividOperateDate.eq(ex)]
        if len(rows) != 1 or (symbol, ex) not in indexed.index:
            raise ValueError('One unchanged existing canonical action identity is required')
        old, new = rows.iloc[0].to_dict(), indexed.loc[(symbol, ex)].to_dict()
        new.update(symbol=symbol, dividOperateDate=ex)
        changed = []
        for field in AMOUNTS:
            a, b = old.get(field, ''), new.get(field, '')
            if a == b:
                continue
            if a != '' and b != '':
                try:
                    x, y = Decimal(a), Decimal(b)
                    if x.is_finite() and y.is_finite() and x >= 0 and x == y:
                        continue  # Existing numeric terms may retain normalized text.
                except (InvalidOperation, ValueError):
                    pass
            if a != '' or not isinstance(b, str) or b == '':
                raise ValueError('Known action terms cannot change; only blank-to-known amounts are allowed')
            try:
                value = Decimal(b)
                if not value.is_finite() or value < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError) as error:
                raise ValueError('New known action amount must be finite and nonnegative') from error
            changed.append(field)
        if not changed:
            raise ValueError('Every selected event must add an explicit formerly blank amount')
        for account in accounts:
            consumption.append(check_unconsumed_action(new, account))
        changes.append(dict(event=[symbol, ex], fields=changed))
    return dict(kind='reviewed_cash_amendment_with_distribution_amendment' if reference_append
        else 'reviewed_cash_amendment', proof=pair_proof, reference_append=reference_append,
        blank_to_known=changes, consumption=consumption, pair=_pair_pin(new_event, new_reference))


def _runtime_pins():
    research = Path(__file__).resolve().parent
    return [pin(previous.__file__), *base_method._runtime_pins(), pin(base_method.__file__),
        pin(research / 'action_reviewed_amendment.py'), pin(research / 'leader_environment_exit_ablation.py')]


def _load_parent(path, seen=None):
    path = Path(path).resolve()
    value = read(path)
    if value.get('schema') == previous.METHOD:
        parent_path, registered, _, inputs, manifest, plan = previous._load_registration(path)
        return dict(path=parent_path, registration=registered, inputs=inputs, manifest=manifest, plan=plan,
            event_view=Path(plan['action_event_view']['path']).parent,
            reference_view=Path(registered['reference_append']['new_reference']['path']).parent,
            result_schema=previous.METHOD)
    if value.get('schema') == METHOD:
        parent_path, registered, ancestor = _load_registration(path, seen)
        pair = registered['source_change']['pair']
        return dict(path=parent_path, registration=registered, inputs=ancestor['inputs'],
            manifest=ancestor['manifest'], plan=ancestor['plan'],
            event_view=Path(pair['event']['directory']), reference_view=Path(pair['reference']['directory']),
            result_schema=METHOD)
    raise ValueError('Parent must be the original capacity continuation or this source-continuation method')


def _checkpoint_entry(folder, parent):
    folder = Path(folder).resolve()
    result = read(folder / 'result.json')
    manifest, plan = parent['manifest'], parent['plan']
    config = StrategyConfig(**plan['config']).validate()
    case = dict(capacity=result['capacity'], capital_policy=result['capital_policy'])
    if (result.get('schema') != parent['result_schema'] or case not in CASES
            or result.get('registration') != pin(parent['path'])
            or result.get('inputs') != pin(parent['inputs'] / 'manifest.json')
            or result.get('config') != config.to_dict()):
        raise ValueError('Checkpoint must belong to the exact current parent and fixed case')
    saved = load_replay_checkpoint(folder, config, manifest['source_days'],
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    state = saved['state']
    context = RunContext('standalone_replay', TradingMode.REPLAY)
    validate_capacity_binding(state, PriorVolumeReplayPolicy(context), context, config,
        RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    if (state.get('close_candidate_policy') != dict(id=ANNUAL_METHOD, **case)
            or result.get('execution_context') != context.to_dict() or not saved['daily']
            or len(saved['daily']) >= len(manifest['source_days'])):
        raise ValueError('Checkpoint context, policy or unfinished calendar changed')
    entry = dict(directory=str(folder), **case, day=state['day'], completed_sessions=len(saved['daily']),
        files={name: pin(folder / name) for name in previous.CHECKPOINT_FILES})
    return entry, saved


def _entries(paths, parent):
    paths = [Path(path).resolve() for path in paths]
    if len(paths) != 4 or len(set(paths)) != 4:
        raise ValueError('Four distinct current-parent checkpoints are required')
    batches = read(parent['inputs'] / 'batches.json')
    entries, accounts = [], []
    for path in paths:
        entry, saved = _checkpoint_entry(path, parent)
        adapter = SimpleNamespace(end=parent['manifest']['end'], capacity=entry['capacity'],
            capital_policy=entry['capital_policy'])
        previous.restore_feed_prefix(adapter, batches, read(path / 'decisions.json'),
            read(path / 'capital_admission.json'), entry['day'])
        entry['restored_seen_symbols'] = adapter.seen
        entries.append(entry)
        accounts.append(saved['state'])
    if sorted((row['capacity'], row['capital_policy']) for row in entries) != sorted(
            (row['capacity'], row['capital_policy']) for row in CASES):
        raise ValueError('Each fixed case needs exactly one current-parent checkpoint')
    ordered = sorted(zip(entries, accounts), key=lambda item: (item[0]['capital_policy'], item[0]['capacity']))
    return [entry for entry, _ in ordered], [account for _, account in ordered]


def register(parent_registration, reference_view, checkpoint, out, *, event_view=None):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Source-continuation registration requires a new directory')
    parent = _load_parent(parent_registration)
    entries, accounts = _entries(checkpoint, parent)
    change = check_source_change(parent['event_view'], parent['reference_view'],
        event_view or parent['event_view'], reference_view, accounts)
    manifest, plan = parent['manifest'], parent['plan']
    result = dict(schema=METHOD, status='registered_chained_source_only_continuation', registered_at=now(),
        spec=SPEC, **FLAGS, fixed_cases=CASES, parent_registration=pin(parent['path']),
        source_change=change, checkpoints=entries, implementation=pin(__file__), runtime=_runtime_pins(),
        config=plan['config'], start=manifest['start'], end=manifest['end'], through=manifest['through'],
        source_days=manifest['source_days'], new_PnL_computed_at_registration=False)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'registration.json', result)
    return result


def _load_registration(path, seen=None):
    path = Path(path).resolve()
    seen = set() if seen is None else set(seen)
    if path in seen:
        raise ValueError('Source-continuation parent registration cycle')
    seen.add(path)
    value = read(path)
    clock = datetime.fromisoformat(value['registered_at'])
    if (value.get('schema') != METHOD or value.get('spec') != SPEC or value.get('fixed_cases') != CASES
            or value.get('implementation') != pin(__file__) or value.get('runtime') != _runtime_pins()
            or clock.utcoffset() is None or clock > datetime.now().astimezone()
            or any(value.get(key) is not flag for key, flag in FLAGS.items())):
        raise ValueError('Exact chained source-continuation registration required')
    source = value['parent_registration']
    if digest(source['path']) != source['sha256']:
        raise ValueError('Pinned parent registration changed')
    parent = _load_parent(source['path'], seen)
    if (value['config'] != parent['plan']['config'] or any(value[key] != parent['manifest'][key]
            for key in ('start', 'end', 'through', 'source_days'))):
        raise ValueError('Source continuation cannot change strategy, fees or calendar')
    entries, accounts = _entries([row['directory'] for row in value['checkpoints']], parent)
    if entries != value['checkpoints']:
        raise ValueError('Pinned parent checkpoint or feed prefix changed')
    pair = value['source_change']['pair']
    checked = check_source_change(parent['event_view'], parent['reference_view'],
        pair['event']['directory'], pair['reference']['directory'], accounts)
    if json.loads(json.dumps(checked)) != value['source_change']:
        raise ValueError('Registered source amendment changed')
    return path, value, parent


def _feed(parent, pair, capacity, capital_policy):
    inputs, manifest, plan = parent['inputs'], parent['manifest'], parent['plan']
    members, days = read(inputs / 'cohort.json')['scope_members'], manifest['source_days']
    raw = pd.concat([pd.read_parquet(inputs / 'context.parquet'),
        pd.read_parquet(inputs / 'raw_daily.parquet')], ignore_index=True)
    if len(raw) != manifest['raw_rows'] + len(raw.loc[raw.date.eq(manifest['context_day'])]):
        raise ValueError('Pinned annual raw row count changed')
    calendar = [manifest['context_day']] + days + [manifest['next_session_after_year']]
    base = ExactSourceRawFeed(raw, calendar, members,
        context_anchor=dict(day=manifest['context_day'], closes={}))
    accounting = load_action_accounting(pair['event']['directory'], pair['reference']['directory'],
        symbols=members, raw_days=[manifest['context_day']] + days, replay_days=days,
        price_source='independent_baostock_raw')
    context = RunContext('standalone_replay', TradingMode.REPLAY)
    policy = PriorVolumeReplayPolicy(context)
    execution = HistoricalExecutionPolicy(base, accounting, days, prior_volume_policy=policy)
    config = StrategyConfig(**plan['config']).validate()
    batches = read(inputs / 'batches.json')
    qualification = base_method._qualification(inputs, manifest, plan)
    adapter = AnnualFeed(execution, batches, config, manifest['end'], days[-1],
        qualification, capacity, capital_policy)
    return _AnnualQualifiedFeed(adapter, qualification), adapter, execution, accounting, config, batches, members


def run(registration, out, *, capacity, capital_policy):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError('Source continuation requires a new account output')
    path, registered, parent = _load_registration(registration)
    selected = [row for row in registered['checkpoints']
                if row['capacity'] == capacity and row['capital_policy'] == capital_policy]
    if len(selected) != 1:
        raise ValueError('One of the four registered cases is required')
    source = selected[0]
    _, checkpoint = _checkpoint_entry(source['directory'], parent)
    feed, adapter, execution, accounting, config, batches, members = _feed(
        parent, registered['source_change']['pair'], capacity, capital_policy)
    previous.restore_feed_prefix(adapter, batches, read(source['files']['decisions.json']['path']),
        read(source['files']['capital_admission.json']['path']), source['day'])
    if adapter.seen != source['restored_seen_symbols']:
        raise ValueError('Causal capacity prefix changed')
    manifest, days = parent['manifest'], parent['manifest']['source_days']
    ledger_result = baseline_ledger_result(feed, config, days, folder, checkpoint=checkpoint,
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    account, receipt = read(folder / 'account.json'), read(folder / 'replay.json')
    residual = {symbol: sum(lot['qty'] for lot in lots)
        for symbol, lots in account['positions'].items() if sum(lot['qty'] for lot in lots)}
    residual_exits = (account.get('full_exit_lifecycle') or {}).get('remaining_full_exits', {})
    complete = (ledger_result['status'] == 'completed' and receipt['completed_sessions'] == len(days)
        and bool(account['fills']) and not residual and account.get('pending_targets') is None and not residual_exits)
    exceptions = [event for event in account['events'] if event['kind'] in previous.EXCEPTION_KINDS]
    old_exceptions = read(source['files']['execution_exceptions.json']['path'])['events']
    if exceptions[:len(old_exceptions)] != old_exceptions:
        raise ValueError('Original execution-exception prefix changed')
    save(folder / 'execution_exceptions.json', dict(schema=METHOD, scope='full_committed_account_history',
        parent=source['files']['execution_exceptions.json'], events=exceptions,
        counts=dict(Counter(event['kind'] for event in exceptions))))
    save(folder / 'execution_checks.json', dict(schema=METHOD, scope='continuation_only_not_reconstructed_prefix',
        parent=source['files']['execution_checks.json'], prior_last_completed_day=source['day'],
        first_continuation_day=days[source['completed_sessions']], checks=execution.checks))
    prior = read(source['files']['result.json']['path'])
    failures = deepcopy(prior.get('source_failure_history', []))
    if not failures and 'prior_ledger_result' in prior:
        failures.append(dict(ledger_result=prior['prior_ledger_result'], source_blocks=prior.get('prior_source_blocks', []),
            record_basis='retained_ancestor_failure_from_pinned_parent_result'))
    failures.append(dict(result=source['files']['result.json'], ledger_result=prior['ledger_result'],
        source_blocks=prior.get('source_blocks', [])))
    report = dict(schema=METHOD, status='completed_conditional_replay' if complete else 'incomplete_or_unresolved',
        created_at=now(), registration=pin(path), spec=SPEC, **FLAGS, capacity=capacity, capital_policy=capital_policy,
        config=config.to_dict(), cohort_count=len(members), start=manifest['start'], end=manifest['end'],
        through=manifest['through'], inputs=pin(parent['inputs'] / 'manifest.json'), ledger_result=ledger_result,
        complete_PnL_available=complete, metrics={key: value for key, value in ledger_result.items() if key not in
            ('status', 'completed_sessions', 'orders', 'fills', 'reused_sessions', 'newly_completed_sessions',
             'turnover_over_initial')} if complete else None,
        reused_sessions=receipt['reused_sessions'], newly_completed_sessions=receipt['newly_completed_sessions'],
        residual_positions=residual, residual_full_exits=residual_exits, pending_targets=account.get('pending_targets'),
        parent_orders=len(account['orders']), fill_callbacks=len(account['fills']), checkpoint=source,
        source_blocks=adapter.blocks, source_failure_history=failures, action_accounting=accounting.provenance,
        raw_reference_checks=pin(folder / 'execution_checks.json'), execution_exceptions=pin(folder / 'execution_exceptions.json'),
        execution_context=RunContext('standalone_replay', TradingMode.REPLAY).to_dict(),
        full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE, source_change=registered['source_change'],
        checkpoint_accounts_modified=False, cash_reset=False, old_committed_sessions_replayed=False,
        upstream_native_files_reread=False)
    save(folder / 'result.json', report)
    save(folder / 'decisions.json', adapter.decisions)
    save(folder / 'capital_admission.json', adapter.admission)
    return report


replay = run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    registration_parser = commands.add_parser('register')
    for name in ('parent-registration', 'reference-view', 'out'):
        registration_parser.add_argument('--' + name, type=Path, required=True)
    registration_parser.add_argument('--event-view', type=Path)
    registration_parser.add_argument('--checkpoint', type=Path, action='append', required=True)
    replay_parser = commands.add_parser('replay')
    for name in ('registration', 'out'):
        replay_parser.add_argument('--' + name, type=Path, required=True)
    replay_parser.add_argument('--capacity', choices=base_method.annual.BENCHMARK_SPEC['capacities'], required=True)
    replay_parser.add_argument('--capital-policy', choices=base_method.annual.CAPITAL_POLICIES, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    result = (register if command == 'register' else run)(**args)
    print(json.dumps({key: result.get(key) for key in ('schema', 'status', 'registered_at', 'reused_sessions',
        'newly_completed_sessions', 'complete_PnL_available', 'ledger_result', 'metrics')}, ensure_ascii=False))
    return 0
