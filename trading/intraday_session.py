"""Explicit open/close commits over the existing unbound simulation Ledger.

The first consumer is the native PreopenPortfolioIntent path. Existing end-of-day
accounts keep their method; this never migrates or submits to a broker account.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import math

from . import engine
from .contracts import MARKET_TZ, IntentKind, PreopenPortfolioIntent, RunContext, TradingMode
from .ledger import Ledger

METHOD = 'native_preopen_shared_engine_two_phase_simulation_v1'
DAILY_SOURCE_POLICY = 'native_verified_daily_projection_v1'
DAILY_METHOD = 'native_preopen_daily_source_two_phase_simulation_v1'


def _timestamp(value, day):
    if not isinstance(value, str):
        raise ValueError('Phase timestamps must be explicit ISO strings')
    try:
        stamp = datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError as error:
        raise ValueError('Invalid phase timestamp') from error
    if stamp.tzinfo is None:
        raise ValueError('Phase timestamp needs a timezone')
    stamp = stamp.astimezone(MARKET_TZ)
    if stamp.date().isoformat() != day:
        raise ValueError('Phase observation must belong to the exact session')
    return stamp


def _clock(feed, day, at, source_ref, phase, context):
    stamp = _timestamp(at, day)
    if not isinstance(source_ref, str) or not source_ref or source_ref != getattr(feed, f'intraday_{phase}_source_ref', None):
        raise ValueError('Exact phase source reference required')
    observed = _timestamp(getattr(feed, f'intraday_{phase}_observed_at', None), day)
    current = engine._published_now().astimezone(MARKET_TZ)
    if stamp > current or observed > current:
        raise ValueError('Future phase/quote clock cannot be consumed')
    if phase == 'opening' and context.mode == TradingMode.PAPER:
        if current.date().isoformat() != day or (current-observed).total_seconds()>90:
            raise ValueError('PAPER opening requires a fresh contemporaneous quote')
    clock = lambda x: (x.hour, x.minute, x.second, x.microsecond)
    if observed > stamp:
        raise ValueError('Quote evidence is not yet available at the phase commit')
    if phase == 'opening':
        for value in (stamp, observed):
            if not ((9,30,0,0) <= clock(value) <= (11,30,0,0)
                    or (13,0,0,0) <= clock(value) <= (15,0,0,0)):
                raise ValueError('Opening evidence requires a continuous-trading timestamp')
        if (stamp-observed).total_seconds() > 90:
            raise ValueError('Opening quote snapshot is stale')
    elif clock(stamp) < (15,0,0,0) or clock(observed) < (15,0,0,0):
        raise ValueError('Closing valuation needs actual post-close evidence')
    return stamp.isoformat(), observed.isoformat()


def _quote_provenance(feed, phase, observed):
    kind=feed.intraday_phase_binding['quote_source_kind']
    available=getattr(feed,f'intraday_{phase}_source_available_at',None)
    if kind=='observed_market_snapshot':
        if not isinstance(available,str):
            raise ValueError('Observed quote evidence needs its actual source availability clock')
        clock=datetime.fromisoformat(available.replace('Z','+00:00'))
        if (clock.tzinfo is None or clock.date().isoformat()!=datetime.fromisoformat(observed).date().isoformat()
                or clock>datetime.fromisoformat(observed)):
            raise ValueError('Quote source was not available at the stated observation')
    return dict(quote_source_kind=kind,source_available_at=available,
        quote_clock_is_actual_observation=kind=='observed_market_snapshot',
        quote_clock_semantics=('actual_observation' if kind=='observed_market_snapshot'
            else 'historical_open_or_close_reference_time_not_actual_receipt'))


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _binding(feed, config, context):
    if not isinstance(context, RunContext) or context.mode not in {TradingMode.REPLAY, TradingMode.PAPER}:
        raise ValueError('Explicit unbound REPLAY/PAPER context required')
    config.validate()
    declared = deepcopy(getattr(feed, 'intraday_phase_binding', None))
    if (not isinstance(declared, dict) or declared.get('schema_version') != 1
            or not isinstance(declared.get('source_id'), str) or not declared['source_id']
            or declared.get('quote_source_kind') not in {'synthetic_fixture','replay_raw_open','observed_market_snapshot'}):
        raise ValueError('Explicit phase feed binding required')
    if context.mode == TradingMode.PAPER and declared['quote_source_kind'] != 'observed_market_snapshot':
        raise ValueError('Historical reconstructed/synthetic quotes cannot become current PAPER evidence')
    if getattr(feed, 'manifest', {}).get('replay_only') and context.mode != TradingMode.REPLAY:
        raise ValueError('Research market source is REPLAY only')
    if any(getattr(feed, name, None) is not None for name in
           ('entry_risk_policy','prior_volume_policy','rights_replay_policy','close_candidate_policy')):
        raise ValueError('Unsupported policy needs its own explicit two-phase consumer')
    # Freeze source semantics across an open checkpoint and the matching close.
    frozen = dict(profiles=getattr(feed,'instrument_profiles',{}), instruments=feed.instruments,
        actions=getattr(feed,'actions',[]),
        historical_price_adjustments=getattr(feed,'manifest',{}).get('historical_price_adjustments',[]))
    policy = declared.get('daily_source_policy')
    if policy is not None and policy != DAILY_SOURCE_POLICY:
        raise ValueError('Unknown daily phase-source policy')
    return dict(method_id=DAILY_METHOD if policy else METHOD, context=context.to_dict(), config=config.to_dict(), feed=declared,
        calendar_sha256=_digest(feed.calendar), source_semantics_sha256=_digest(frozen))


def _check_account(state, binding):
    if state.get('broker_binding'):
        raise PermissionError('A broker-bound account cannot use simulated phase execution')
    previous = state.get('intraday_session_method')
    if previous is not None and previous != binding:
        # New dated, independently validated quote/qualification projections may
        # follow a closed daily account. The complete binding remains exact
        # inside its opening marker, so this never permits a mid-session swap.
        static = lambda value: {k:v for k,v in value.items() if k != 'source_semantics_sha256'}
        daily_update = (binding['method_id'] == DAILY_METHOD
            and previous.get('method_id') == DAILY_METHOD
            and static(previous) == static(binding)
            and state.get('intraday_open_session') is None)
        if not daily_update:
            raise ValueError('Two-phase account source/config/context changed')
    if state['day'] is not None and previous is None:
        raise ValueError('Existing EOD accounts cannot silently migrate into the two-phase method')
    if any(state.get(name) is not None for name in ('pending_targets','pending_risk_exits',
            'pending_published_close_intent','full_exit_lifecycle','lot_allocation',
            'replay_risk_exit_policy','published_close_target_policy','published_close_router_policy')):
        raise ValueError('Legacy pending or overlay cannot be mixed with explicit preopen phases')
    if state.get('commission_deadband'):
        raise ValueError('Legacy commission overlay is not this phase method')


def _phase_hook(feed, phase, state, day):
    hook = getattr(feed, f'validate_{phase}_session', None)
    if not callable(hook):
        raise ValueError('Phase-specific source/qualification/action validation is required')
    hook(deepcopy(state), day)


def opening(state, feed, day, config, envelope, *, context, opened_at, source_ref):
    """Commit only opening fills/fees/cash/T1; do not read E close or E signals."""
    binding = _binding(feed, config, context)
    _check_account(state,binding)
    if state.get('intraday_open_session') is not None:
        raise ValueError('Opening already committed; restore this checkpoint and close it once')
    if state['day'] is not None and day <= state['day']:
        raise ValueError('Opening cannot repeat or reverse an account session')
    if not isinstance(envelope,PreopenPortfolioIntent):
        raise TypeError('Explicit published preopen envelope required')
    stamp, observed = _clock(feed,day,opened_at,source_ref,'opening',context)
    provenance=_quote_provenance(feed,'opening',observed)
    receipt, preopen_binding = engine._preopen_frontier(state,envelope,day,feed,config,context,opening_only=True)
    if datetime.fromisoformat(envelope.published_at.replace('Z','+00:00')) > datetime.fromisoformat(stamp):
        raise ValueError('Preopen target was not yet published')
    _phase_hook(feed,'opening',state,day)
    targets = dict(envelope.intent.weights) if envelope.intent.kind == IntentKind.FULL_TARGET else {}
    guard = getattr(feed,'validate_preopen_targets',None)
    if targets and guard is not None:
        guard(day,targets)
    trial = deepcopy(state)
    view = getattr(feed,'research_corporate_action_view',None)
    if view is not None or 'research_corporate_action_binding' in trial:
        from .research.research_corporate_action_view import validate_research_action_binding
        action_binding = validate_research_action_binding(trial,view,context,config,day=day)
        if action_binding is not None:
            trial['research_corporate_action_binding']=action_binding
    rows = feed.rows.get(day,{})
    book = Ledger(trial,config,context,instrument_profiles=getattr(feed,'instrument_profiles',None))
    # Delisting and quote-unit checks remain independent of E's future close.
    for symbol in trial['positions']:
        if book.quantity(symbol):
            out = feed.instruments[symbol].get('outDate')
            if out and day >= out:
                raise ValueError('Unresolved delisted holding requires settlement')
    for symbol,unit in trial.get('price_units',{}).items():
        if book.rule(symbol,day).price_unit != unit:
            raise ValueError('Account quote units changed')
    before_orders,before_events=len(trial['orders']),len(trial['events'])
    trial['preopen_execution_binding']=preopen_binding
    engine._execute_session_opening(book,feed,day,rows,before_events=before_events,
        preopen_intent=envelope,preopen_receipt=receipt)
    if trial['cash'] < 0 or any(book.quantity(s)<0 for s in trial['positions']):
        raise ValueError('Ledger invariant violated')
    marker=dict(day=day,opened_at=stamp,observed_at=observed,source_ref=source_ref,
        preopen_frontier=receipt,before_orders=before_orders,before_events=before_events,
        method_binding=binding,action_binding=deepcopy(trial.get('research_corporate_action_binding')),
        quote_provenance=provenance,committed_at=engine._published_now().isoformat())
    trial['intraday_session_method']=binding
    trial['intraday_open_session']=marker
    result=dict(day=day,phase='opening_committed',opened_at=stamp,quote_observed_at=observed,
        source_ref=source_ref,account_close_day=trial['day'],cash_cents=trial['cash'],
        opening_marked_equity_cents=book.equity(),new_orders=len(trial['orders'])-before_orders,
        fills=[deepcopy(v) for v in trial['fills'].values() if v['day']==day],
        closing_equity=None,execution_admitted=False,broker_observation=False,**provenance)
    state.clear();state.update(trial)
    return result


def closing(state, feed, day, config, *, context, closed_at, source_ref):
    """Commit the matching close valuation; never resubmit opening orders."""
    binding=_binding(feed,config,context)
    _check_account(state,binding)
    marker=state.get('intraday_open_session')
    if not isinstance(marker,dict) or marker['day']!=day or marker['method_binding']!=binding:
        raise ValueError('Matching durable opening checkpoint required')
    stamp,observed=_clock(feed,day,closed_at,source_ref,'closing',context)
    provenance=_quote_provenance(feed,'closing',observed)
    if datetime.fromisoformat(stamp) <= datetime.fromisoformat(marker['opened_at']):
        raise ValueError('Closing must follow the committed opening')
    _phase_hook(feed,'closing',state,day)
    trial=deepcopy(state)
    view=getattr(feed,'research_corporate_action_view',None)
    if view is not None or 'research_corporate_action_binding' in trial:
        from .research.research_corporate_action_view import validate_research_action_binding
        action_binding=validate_research_action_binding(trial,view,context,config,day=day)
        if action_binding!=marker['action_binding']:
            raise ValueError('Corporate-action source changed after opening')
    rows=feed.rows.get(day,{})
    book=Ledger(trial,config,context,instrument_profiles=getattr(feed,'instrument_profiles',None))
    for symbol in trial['positions']:
        if not book.quantity(symbol):
            continue
        row=rows.get(symbol)
        if row is None or not math.isfinite(row.get('close',float('nan'))) or row['close']<=0:
            raise ValueError('Unpriceable held security at close; retain the opening checkpoint')
    stale=engine._mark_session_close(book,feed,day,rows,view)
    trial['day']=day
    trial['last_preopen_intent']=marker['preopen_frontier']
    if trial['cash']<0 or any(book.quantity(s)<0 for s in trial['positions']):
        raise ValueError('Ledger invariant violated')
    fills=[f for f in trial['fills'].values() if f['day']==day]
    result=dict(date=day,equity=book.equity()/100,cash=trial['cash']/100,
        receivables=sum(trial['receivables'].values())/100,
        holdings=sum(book.quantity(s)>0 for s in trial['positions']),fees=sum(f['fees'] for f in fills)/100,
        turnover_value=sum(f.get('notional',f['qty']*f['price']) for f in fills)/100,
        new_orders=len(trial['orders'])-marker['before_orders'],stale_symbols=stale,
        events=trial['events'][marker['before_events']:],event_time_frontier=None,
        preopen_intent_frontier=marker['preopen_frontier'])
    trial['last_intraday_session']=dict(day=day,opened_at=marker['opened_at'],closed_at=stamp,
        opening_source_ref=marker['source_ref'],closing_source_ref=source_ref,
        closing_observed_at=observed,quote_source_kind=binding['feed']['quote_source_kind'],
        closing_quote_provenance=provenance,committed_at=engine._published_now().isoformat())
    trial.pop('intraday_open_session')
    state.clear();state.update(trial)
    return result
