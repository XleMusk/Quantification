"""Saved native mechanical selections through the existing Engine/Ledger only."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..contracts import IntentKind, PortfolioIntent, RunContext, SignalFrame, SignalUnavailable, TradingMode, MARKET_TZ
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from ..target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import load_posthoc_listing
from .leader_eight_type_panel_replay import _QualifiedFeed
from .leader_historical_execution_policy import HistoricalExecutionPolicy
from .native_history_chain_source import NativeHistoryChainSource
from .order_capacity_replay import PriorVolumeReplayPolicy
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting


METHOD_ID = 'native_frozen_saved_selection_fixed16pct_shared_engine_replay_v1'
INPUT_SCHEMA = 'native_shortline_selection_replay_inputs_v1'
REGISTRATION_SCHEMA = METHOD_ID + '_registration'
RESULT_SCHEMA = METHOD_ID + '_result'
KINDS = ('lr', 'lgbm')
START, END = '2026-01-05', '2026-09-30'
TAIL = ['2026-10-08', '2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15']
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, model_refit=False,
    future_labels_used_for_score=False, network_accessed=False, returns_reportable=False,
    historical_publication_verified=False, pit_verified=False, corporate_actions_verified=False,
    original_candidate_flags_modified=False, user_account_mutated=False)
SPEC = dict(method_id=METHOD_ID, kinds=list(KINDS), start=START, end=END,
    decision_sessions=181, tail_sessions=TAIL, initial_cash_each=100000,
    target_weight_each=.16, shortage='leave_cash_no_refill_or_upsizing',
    decision='original_saved_FULL_or_NO_CHANGE_no_new_ranking',
    execution='D_close_exact_next_E_open_same_Engine_Ledger_T1_costs',
    expiry=None, holding='daily_full_replacement_NO_CHANGE_preserves_holdings_and_unfinished_exits',
    first_study_exit_decision=TAIL[0], study_exit='first_tail_close_empty_FULL_then_NO_CHANGE_full_exit_lifecycle',
    last_main_target='September30_target_executes_October8_before_study_exit',
    lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE,
    source_cutoff='stop_before_any_session_after_original_action_source_end_no_extension',
    publication='retrospective_saved_selection_diagnostic_not_historical_plan_publication',
    config=_config(100000, 5).to_dict())


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _save(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _pin(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path)}


def _verify(ref):
    path = Path(ref['path']).resolve()
    if digest(path) != ref['sha256']:
        raise ValueError('Registered saved source changed: ' + str(path))
    return path


def _bool(value):
    if isinstance(value, bool):
        return value
    if value in ('True', 'False'):
        return value == 'True'
    raise ValueError('Saved explicit selection boolean required')


def load_selection(evaluation):
    """Read original decisions/selected identities, never probabilities or labels."""
    folder = Path(evaluation).resolve()
    result = _read(folder / 'result.json')
    seal = _read(folder / 'prelabel_seal.json')
    if (result.get('schema') != 'native_frozen_shortline_evaluation_result_v1'
            or result.get('status') != 'completed_retrospective_frozen_stagewise_quality'
            or result.get('event_start') != START or result.get('event_end') != END
            or result.get('execution_admitted') is not False
            or result.get('model_refit') is not False):
        raise ValueError('Original completed frozen evaluation is required')
    refs = {'result': _pin(folder / 'result.json'), 'seal': _pin(folder / 'prelabel_seal.json')}
    for name, key in [('selection.csv', 'selection'), ('daily_decisions.csv', 'daily_decisions')]:
        file = folder / name
        refs[key] = _pin(file)
        if (result['files'].get(name) != refs[key]['sha256']
                or Path(seal[key]['path']).resolve() != file or seal[key]['sha256'] != refs[key]['sha256']):
            raise ValueError('Original pre-label sealed selection or decisions differ')
    decisions = pd.read_csv(folder / 'daily_decisions.csv', dtype={'date': str, 'model_kind': str})
    selected = pd.read_csv(folder / 'selection.csv', dtype={'date': str, 'symbol': str, 'event_id': str})
    required = {'event_id', 'date', 'symbol', 'stage', 'model_kind', 'candidate_selected'}
    if not required <= set(selected) or selected.duplicated(['model_kind', 'event_id']).any():
        raise ValueError('Unique original model/event selection records required')
    if (not set(decisions.model_kind) == set(KINDS)
            or decisions.duplicated(['date', 'model_kind']).any()
            or not decisions.decision.isin(['FULL_TARGET', 'NO_CHANGE']).all()):
        raise ValueError('Exactly two independent complete saved decision streams required')
    days = sorted(decisions.date.unique())
    if (len(days) != 181 or days[0] != START or days[-1] != END
            or any(decisions.loc[decisions.model_kind.eq(k), 'date'].tolist() != days for k in KINDS)
            or not selected.date.isin(days).all() or not selected.model_kind.isin(KINDS).all()):
        raise ValueError('Original 181-day decision prefix must be complete for each model')
    chosen = selected.loc[selected.candidate_selected.map(_bool)].copy()
    plans = {k: {} for k in KINDS}
    for row in decisions.to_dict('records'):
        own = chosen.loc[chosen.date.eq(row['date']) & chosen.model_kind.eq(row['model_kind'])]
        symbols = own.symbol.tolist()
        if (len(symbols) != int(row['selected_count']) or len(symbols) > 5
                or len(symbols) != len(set(symbols))
                or (row['decision'] == 'FULL_TARGET') != bool(symbols)):
            raise ValueError('Saved FULL/NO_CHANGE counts and selected identities disagree')
        plans[row['model_kind']][row['date']] = {**row, 'symbols': symbols}
    return plans, days, refs


def _input_refs(source):
    refs = [source['calendar'], source['master_run'], source['action_event'], source['action_reference']]
    refs += source.get('prior_calendars', []) + source['source_refs']
    for ref in refs:
        _verify(ref)
    return refs


def register(evaluation, raw_inputs, out):
    out, raw_inputs = Path(out).resolve(), Path(raw_inputs).resolve()
    if out.exists():
        raise FileExistsError('Use a new independent registration directory')
    plans, days, refs = load_selection(evaluation)
    source = _read(raw_inputs)
    if (source.get('schema') != INPUT_SCHEMA or source.get('context_day') != '2025-12-31'
            or source.get('next_session_after_tail') != '2026-10-16'
            or set(source['chain']) != {'base_view', 'extension_views', 'close_folders'}):
        raise ValueError('Explicit unchanged native chain and context/successor source contract required')
    dependencies = _input_refs(source)
    calendar = _read(source['calendar']['path'])
    from .leader_daily_signals import _calendar_view
    actual, _, status = _calendar_view(calendar, START, TAIL[-1], [])
    if status != 'pinned_static_exchange_calendar' or actual != days + TAIL:
        raise ValueError('Saved decisions and all six fixed tail sessions must match the exchange calendar')
    events = _read(source['action_event']['path'])
    action_end = events['end']
    if not events['start'] <= START <= action_end:
        raise ValueError('Action source does not cover replay inception')
    protected = [Path(r['path']).resolve().parent for r in dependencies + list(refs.values())]
    if any(out == p or p in out.parents or out in p.parents for p in protected):
        raise ValueError('Output cannot overlap saved source directories')
    registration = dict(schema=REGISTRATION_SCHEMA, method_id=METHOD_ID, spec=SPEC,
        registered_at=_now(), evaluation=str(Path(evaluation).resolve()), saved_selection=refs,
        raw_inputs=_pin(raw_inputs), source_dependencies=dependencies,
        implementation=_pin(__file__), decision_days=days, expected_sessions=days + TAIL,
        action_source_end=action_end, source=source,
        symbols={k: sorted({s for p in plans[k].values() for s in p['symbols']}) for k in KINDS}, **FLAGS)
    out.mkdir(parents=True, exist_ok=False)
    _save(out / 'registration.json', registration)
    return registration


def fixed_weight_policy(intent):
    """Identity-preserving existing Engine reduction, never a new selector."""
    if not isinstance(intent, PortfolioIntent) or intent.kind != IntentKind.FULL_TARGET:
        raise ValueError('Saved selection reduction requires a FULL typed intent')
    if any(weight < .16 for _, weight in intent.weights):
        raise ValueError('The unchanged Engine config cannot enlarge a sub-.16 target')
    return PortfolioIntent.full_target({s: .16 for s, _ in intent.weights}, reason=intent.reason,
        context=intent.context, decided_at=intent.decided_at,
        execution_not_before=intent.execution_not_before, source=intent.source)


class SavedSelectionFeed:
    def __init__(self, execution, plans, config, context, qualification, action_end,
                 *, end=END, tail=TAIL):
        self.execution, self.base, self.plans = execution, execution.base, deepcopy(plans)
        self.config, self.context, self.qualification = config, context, qualification
        self.end, self.tail, self.action_end = end, list(tail), action_end
        self.decisions, self.blocks = [], []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def validate_session(self, state, day):
        if day > self.action_end:
            self.blocks.append(dict(day=day, reason='unsupported_action_source_cutoff',
                original_action_end=self.action_end, session_not_committed=True))
            raise ValueError('unsupported_action_source_cutoff: ' + self.action_end)
        required = {s for s, lots in state['positions'].items() if sum(l['qty'] for l in lots)}
        required.update(state.get('pending_targets') or {})
        issues = self.execution.raw_issues(day, required)
        refs = self.execution.references(day, required)
        ipo = [self.qualification.assess(s, day, decision_as_of=day + 'T09:30:00+08:00') for s in sorted(required)]
        if (issues or any(not self.execution.reference_ok(r) for r in refs)
                or any(r['engine_block_reason'] for r in ipo)):
            self.blocks.append(dict(day=day, reason='saved_native_execution_source_unresolved',
                raw_issues=issues, references=refs, ipo=ipo, session_not_committed=True))
            raise ValueError('Saved native pending/held source unresolved: ' + json.dumps(self.blocks[-1]))
        return self.execution.validate_session(state, day)

    def signal_frame(self, day, config, context):
        if context != self.context or config != self.config or context.mode != TradingMode.REPLAY:
            raise ValueError('Saved selection requires its independent REPLAY account and unchanged config')
        if day == self.tail[0]:
            self.decisions.append(dict(date=day, decision='FULL_EMPTY', reason='fixed_study_end_exit_after_last_target_open'))
            return SignalFrame.from_legacy([], day, 'research:' + METHOD_ID + ':study_end', context)
        if day in self.tail[1:]:
            return SignalUnavailable('fixed_study_end_exit_lifecycle_continues_NO_CHANGE')
        plan = self.plans.get(day)
        if plan is None:
            raise ValueError('Missing original saved decision; do not invent NO_CHANGE')
        self.decisions.append(dict(date=day, decision=plan['decision'], reason=plan['reason'],
            saved_symbols=plan['symbols'], historical_publication_verified=False))
        if plan['decision'] == 'NO_CHANGE':
            return SignalUnavailable(plan['reason'])
        issues = self.execution.raw_issues(day, set(plan['symbols']))
        if issues:
            raise ValueError('Original selected D quotes unavailable: ' + json.dumps(issues))
        # Positive mechanical ranks preserve the already selected source order;
        # no probability/label is loaded or re-ranked by this replay adapter.
        return SignalFrame.from_legacy([{'symbol': s, 'score': float(5 - i)}
            for i, s in enumerate(plan['symbols'])], day, 'research:' + METHOD_ID + ':' + context.account_id, context)


def _raw(source, days, symbols):
    chain = source['chain']
    with NativeHistoryChainSource(chain['base_view'], chain['extension_views'], chain['close_folders'],
            calendar=source['calendar']['path']) as reader:
        return pd.concat([reader.frame(day, symbols) for day in [source['context_day']] + days], ignore_index=True)


def _positions(state):
    return {s: sum(l['qty'] for l in lots) for s, lots in state['positions'].items() if sum(l['qty'] for l in lots)}


def _case(kind, plans, raw, registration, out, qualification):
    config, context = _config(100000, 5), RunContext(METHOD_ID + ':' + kind, TradingMode.REPLAY)
    source, sessions = registration['source'], registration['expected_sessions']
    symbols = registration['symbols'][kind]
    base = ExactSourceRawFeed(raw, [source['context_day']] + sessions + [source['next_session_after_tail']],
        symbols, context_anchor={'day': source['context_day'], 'closes': {}})
    covered = [d for d in sessions if d <= registration['action_source_end']]
    accounting = load_action_accounting(Path(source['action_event']['path']).parent,
        Path(source['action_reference']['path']).parent, symbols=symbols,
        raw_days=[source['context_day']] + covered, replay_days=covered, price_source='independent_baostock_raw')
    prior_volume = PriorVolumeReplayPolicy(context)
    execution = HistoricalExecutionPolicy(base, accounting, covered, prior_volume_policy=prior_volume)
    adapter = SavedSelectionFeed(execution, plans, config, context, qualification, registration['action_source_end'])
    feed = _QualifiedFeed(adapter, qualification)
    state, daily, blocking, main = new_state(100000), [], None, None
    for event in base.manifest.get('historical_price_adjustments', []):
        if event['kind'] in {'rights_issue', 'restructuring_holder_distribution'} and event.get('registration_date') and event['registration_date'] < sessions[0]:
            prefix = 'rights:' if event['kind'] == 'rights_issue' else ''
            state['entitlements'][prefix + event['symbol'] + ':' + event['date']] = 0
    out.mkdir(parents=True, exist_ok=False)
    for index, day in enumerate(sessions):
        trial = deepcopy(state)
        try:
            row = advance(trial, feed, day, config, index, context=context,
                target_policy=fixed_weight_policy, full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
        except (ValueError, KeyError, TypeError, PermissionError) as error:
            blocking = dict(day=day, error=type(error).__name__ + ': ' + str(error), session_not_committed=True)
            _save(out / 'failed_trial.json', trial)
            break
        state, daily = trial, daily + [row]
        if day == END:
            main = dict(source_prefix_completed=True, engine_sessions_committed=len(daily),
                marked_equity=float(row['equity']), marked_NAV=float(row['equity']) / config.initial_cash,
                cash=float(row['cash']), residual_positions=_positions(state),
                pending_targets=deepcopy(state.get('pending_targets')), closed_total_return=None,
                label='main_window_marked_NAV_not_closed_total_return')
    residual = _positions(state)
    exits = (state.get('full_exit_lifecycle') or {}).get('remaining_full_exits', {})
    complete = (blocking is None and len(daily) == len(sessions) and bool(state['fills'])
        and not residual and state.get('pending_targets') is None and not exits)
    result = dict(schema=RESULT_SCHEMA, kind=kind, context=context.to_dict(), config=config.to_dict(),
        status='completed_conditional_replay' if complete else 'incomplete_or_no_fills',
        expected_sessions=len(sessions), engine_sessions_committed=len(daily),
        last_committed_day=state['day'], blocking_evidence=blocking, source_issues=adapter.blocks,
        main_window=main if main else dict(source_prefix_completed=False, marked_NAV=None, closed_total_return=None),
        full_tail=dict(expected_tail=TAIL, expected_through=TAIL[-1], complete_PnL_available=bool(complete),
            metrics=metrics(daily, config.initial_cash) if complete else None),
        residual_positions=residual, residual_full_exits=exits, pending_targets=state.get('pending_targets'),
        parent_orders=len(state['orders']), fill_callbacks=len(state['fills']),
        engine_advanced=bool(daily), paper_orders_generated=bool(state['orders']),
        research_ledger_mutated=bool(daily), new_research_account_created=True,
        action_accounting=accounting.provenance, prior_volume_policy=prior_volume.method_id,
        original_action_source_end=registration['action_source_end'], actual_completed_at=_now(), **FLAGS)
    for name, payload in [('account', state), ('daily', daily), ('orders', list(state['orders'].values())),
            ('fills', list(state['fills'].values())), ('decisions', adapter.decisions),
            ('execution_checks', execution.checks), ('result', result)]:
        _save(out / (name + '.json'), payload)
    return result


def run(registration, out):
    registration, out = Path(registration).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError('Use a new independent replay directory')
    record = _read(registration)
    if record.get('schema') != REGISTRATION_SCHEMA or record.get('spec') != SPEC:
        raise ValueError('Exact independent saved-selection replay registration required')
    dependencies = list(record['saved_selection'].values()) + [record['raw_inputs'], record['implementation']] + record['source_dependencies']
    for ref in dependencies:
        _verify(ref)
        path = Path(ref['path']).resolve()
        parent = path.parent
        overlap = (out == path or out in path.parents) if ref == record['raw_inputs'] else (
            out == parent or parent in out.parents or out in parent.parents)
        if overlap:
            raise ValueError('Replay output must be outside all registered input directories')
    if registration.parent == out or registration.parent in out.parents:
        raise ValueError('Replay output cannot overlap its registration')
    plans, days, saved = load_selection(record['evaluation'])
    if (saved != record['saved_selection'] or days != record['decision_days']
            or record['expected_sessions'] != days + TAIL):
        raise ValueError('Original saved decision prefix changed')
    source = record['source']
    if _read(record['raw_inputs']['path']) != source:
        raise ValueError('Pinned raw-input descriptor differs')
    symbols = sorted({s for kind in KINDS for s in record['symbols'][kind]})
    raw = _raw(source, record['expected_sessions'], symbols)
    qualification = load_posthoc_listing(source['master_run']['path'], source['calendar']['path'],
        observed_at=record['registered_at'], prior_calendars=[r['path'] for r in source.get('prior_calendars', [])])
    out.mkdir(parents=True, exist_ok=False)
    cases = []
    for kind in KINDS:
        try:
            case = _case(kind, plans[kind], raw, record, out / kind, qualification)
        except (ValueError, KeyError, TypeError, PermissionError) as error:
            folder = out / kind
            folder.mkdir(exist_ok=True)
            case = dict(schema=RESULT_SCHEMA, kind=kind, status='case_source_or_setup_failed',
                error=type(error).__name__ + ': ' + str(error), execution_progress_unknown=True,
                engine_advanced=None, paper_orders_generated=None, research_ledger_mutated=None,
                main_window=None, full_tail=dict(complete_PnL_available=False, metrics=None), **FLAGS)
            _save(folder / 'failure.json', case)
        cases.append(case)
    for ref in dependencies:
        _verify(ref)
    result = dict(schema=RESULT_SCHEMA, method_id=METHOD_ID, status='two_independent_saved_selection_replays_saved',
        registered_at=record['registered_at'], actual_completed_at=_now(), registration=_pin(registration),
        case_count=2, cases=cases, expected_decision_days=181, expected_tail=TAIL,
        engine_advanced=True if any(c.get('engine_advanced') is True for c in cases) else (
            None if any(c.get('engine_advanced') is None for c in cases) else False),
        paper_orders_generated=True if any(c.get('paper_orders_generated') is True for c in cases) else (
            None if any(c.get('paper_orders_generated') is None for c in cases) else False),
        research_ledger_mutated=True if any(c.get('research_ledger_mutated') is True for c in cases) else (
            None if any(c.get('research_ledger_mutated') is None for c in cases) else False),
        source_accounting_not_extended=True, original_rule_or_model_changed=False, **FLAGS)
    _save(out / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    reg = sub.add_parser('register')
    reg.add_argument('--evaluation', type=Path, required=True)
    reg.add_argument('--raw-inputs', type=Path, required=True)
    reg.add_argument('--out', type=Path, required=True)
    execute = sub.add_parser('run')
    execute.add_argument('--registration', type=Path, required=True)
    execute.add_argument('--out', type=Path, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    result = {'register': register, 'run': run}[command](**args)
    print(json.dumps({k: result[k] for k in ('schema', 'execution_admitted')}, ensure_ascii=False))
    return 0
