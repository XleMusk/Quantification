"""Opt-in residual full-exit handling for the integrated dated catch-up method.

The existing Engine still owns every order/fill/share. Only this new method
keeps an already requested full exit after a blocked/partial opening attempt.
The original 32 books and their policies retain their prior behavior.
"""
from copy import deepcopy
from dataclasses import replace

from ..contracts import IntentKind, SignalUnavailable
from ..engine import ReplayRiskExitIntent
from .leader_published_risk_exit import PublishedCloseLeaderTypeRiskExitPolicy, RULE_SPEC as BASE_RULES
from .shortline_decision_v2 import fixed_exit_policy
from .shortline_decision_v3 import _known_raw

TARGET_POLICY_ID='dated_catchup_published_target_sticky_reduction_v1'
RISK_POLICY_ID='dated_catchup_structure_expiry_sticky_queue_v1'
RULE_SPEC={**BASE_RULES,'id':RISK_POLICY_ID,'target_policy_id':TARGET_POLICY_ID,
    'scope':'new_dated_catchup_method_only_original_32_unchanged',
    'residual_full_exit':'confirmed_risk_or_actual_zero_target_retry_until_actual_flat',
    'unknown_or_recovered_structure':'does_not_cancel_existing_full_exit',
    'new_target_reincludes_residual':'remove_weight_until_actual_flat_no_renormalization',
    'partial_reweight_or_old_buy':'not_promoted_to_sticky_full_exit_or_retried_buy',
    'reentry_after_flat':'subsequent_close_may_publish_new_entry',
    'original_entry_structure_and_five_session_thresholds_changed':False,
    'execution_lifecycle_changed':True,'signal_only_comparison':False}


def _quantity(state,symbol):
    return sum(lot['qty'] for lot in state['positions'].get(symbol,[]))


def _captured_exits(state):
    held={s for s in state['positions'] if _quantity(state,s)>0}
    symbols=set((state.get('pending_risk_exits') or {}).get('symbols',[]))
    targets=state.get('pending_targets')
    if targets is not None:
        symbols.update(s for s in held if targets.get(s,0)<=0)
    return frozenset(symbols & held)


def _remaining(state,day,captured):
    symbols=set(captured)
    for event in state['events']:
        if (event.get('day')==day and event.get('kind')=='target_execution_diagnostic'
            and event.get('engine_target_quantity')==0 and event.get('held_before',0)>0
            and event.get('held_after',0)>0):
            symbols.add(event['symbol'])
    return tuple(sorted(s for s in symbols if _quantity(state,s)>0))


class DatedCatchupTargetReduction:
    policy_id=TARGET_POLICY_ID

    def __init__(self,feed,state,config):
        self.state=state
        self.captured=_captured_exits(state)
        self.expiry=fixed_exit_policy(feed,state,config)

    def __call__(self,intent):
        reduced=self.expiry(intent)
        if reduced.kind!=IntentKind.FULL_TARGET:
            return reduced
        remaining=set(_remaining(self.state,intent.decided_at[:10],self.captured))
        weights=tuple((s,w) for s,w in reduced.weights if s not in remaining)
        return reduced if weights==reduced.weights else replace(reduced,weights=weights,
            reason='dated_catchup_existing_full_exit_residual_no_reentry_until_flat')


class DatedCatchupRiskPolicy(PublishedCloseLeaderTypeRiskExitPolicy):
    def __init__(self,type_id,*,state,**kwargs):
        if type_id!='sector_catchup_leader':
            raise ValueError('Dated catchup execution policy is scoped to its one signal type')
        super().__init__(type_id,**kwargs)
        self.policy_id=RISK_POLICY_ID+':'+type_id
        self.captured=_captured_exits(state)
        self.original_risk=deepcopy(state.get('pending_risk_exits'))

    def __call__(self,day,book,feed):
        current=super().__call__(day,book,feed)
        remaining=_remaining(book.state,day,self.captured)
        if not remaining:
            return current
        if isinstance(current,SignalUnavailable):
            raise ValueError('Residual full exit cannot be discarded with unavailable risk evidence: '+current.reason)
        indices={d:i for i,d in enumerate(feed.calendar)}
        if day not in indices or indices[day]+1>=len(feed.calendar):
            raise ValueError('Residual full exit requires the exact next exchange session')
        if any(not _known_raw(feed.rows.get(day,{}).get(s)) for s in remaining):
            raise ValueError('Residual full exit needs known current raw/status; no price imputation')
        symbols=tuple(sorted(set(remaining)|set(current.symbols if current else ())))
        following=feed.calendar[indices[day]+1]
        book.emit(day,'dated_catchup_full_exit_residual_requeued',symbols=list(remaining),
            remaining_quantities={s:book.quantity(s) for s in remaining},
            original_risk_receipt=self.original_risk,execution_not_before=following,
            old_buy_order_retried=False,current_unknown_does_not_cancel=True)
        return ReplayRiskExitIntent(book.context,day,following,symbols,
            'research:dated_catchup_residual_full_exit',
            'already_requested_full_exit_remaining_until_actual_flat')


def risk_policy(identity,*,state,**kwargs):
    return DatedCatchupRiskPolicy(identity,state=state,**kwargs)


def target_policy(feed,state,config):
    return DatedCatchupTargetReduction(feed,state,config)
