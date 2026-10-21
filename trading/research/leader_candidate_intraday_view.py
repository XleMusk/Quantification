"""Join saved candidate references to public snapshots without selecting orders."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import argparse
import json
from pathlib import Path

from .leader_eight_type_paper_view import leader_eight_type_paper_view, read_saved_plan, _stamp, LOCAL
from .leader_cohort_quote_view import leader_cohort_intraday_view

SCHEMA='saved_leader_candidate_intraday_tracking_v1'
FLAGS=dict(research_only=True,candidate_panel_only=True,execution_admitted=False,
    selection_enabled=False,strategy_validated=False,provider_contacted=False,
    account_accessed=False,engine_advanced=False,orders_generated=False,
    ledger_mutated=False,ths_payload_generated=False,broker_interacted=False)


def _positive(value):
    try:
        v=Decimal(str(value))
        return v if v.is_finite() and v>0 else None
    except (InvalidOperation,ValueError,TypeError):
        return None


def _relation(value, line):
    return None if value is None or line is None else 'below' if value<line else 'equal' if value==line else 'above'


def _threshold(type_id, detail):
    saved=detail.get('setup_reference') or {}
    evidence=detail.get('evidence') or {}
    if type_id in {'sector_catchup_leader','turnover_leader'}:
        return 'saved_candidate_setup_low',_positive(saved.get('setup_low_cny'))
    if type_id=='trend_leader':
        return 'saved_sma20_observed_rows',_positive(saved.get('sma20_observed'))
    if type_id=='rebound_repair_candidate':
        return 'saved_exact_previous_open',_positive(evidence.get('previous_open'))
    # The other original rules require a qualified complete ladder, a whole
    # market amount rank or an actual held episode anchor. A price is not one.
    return 'requires_complete_rule_evidence',None


def _unique_map(rows):
    result={}
    for row in rows:
        if row['symbol'] in result: raise ValueError('Duplicate quote identity in saved observation')
        result[row['symbol']]=row
    return result


def build_candidate_tracking(paper,cohort,receipt,now):
    """Keep historical snapshot relations and current freshness separate."""
    clock=_stamp(now)
    raw=_unique_map(receipt.get('quotes',[])); viewed=_unique_map(cohort.get('quotes',[]))
    execution=paper.get('execution_day_bound')
    published=_stamp(paper['recorded_at']) if paper.get('recorded_at') else None
    end=_stamp(receipt['finished_at']) if receipt.get('finished_at') else None
    if end is not None and end>clock: raise ValueError('Saved observation completion is in the future')
    same_day=bool(execution and receipt.get('session_day')==execution
        and (cohort.get('session') or {}).get('session_day')==execution)
    rows=[]; symbols=set(); observed=set(); fresh=set(); changed=set(); below=set()
    for case in paper.get('rows',[]):
        candidates=[]
        for detail in case.get('candidate_details',[]):
            symbol=detail['symbol']; symbols.add(symbol)
            q,v=raw.get(symbol),viewed.get(symbol)
            saved=detail.get('setup_reference') or {}
            reference=_positive(saved.get('reference_close_cny'))
            name,line=_threshold(case['type'],detail)
            tracking=dict(quote_status='missing_from_fixed_cohort',snapshot_price_cny=None,
                source_quote_time=None,observed_at=None,fresh_at_read=False,
                reference_status='unknown',reference_close_cny=float(reference) if reference else None,
                threshold_name=name,threshold_cny=float(line) if line else None,
                snapshot_relation=None,current_relation=None,snapshot_low_relation=None,
                close_confirmation='not_evaluated_intraday_snapshot',held_entry_anchor=None,
                trade_ready=False)
            if q is not None and v is not None:
                tracking.update(quote_status='unknown_saved_quote',source_quote_time=q.get('source_quote_time'),observed_at=q.get('received_at'))
                if not same_day:
                    tracking['quote_status']='different_execution_day'
                elif q.get('current_quote') is True and q.get('source_status')=='00':
                    try:
                        source,received=_stamp(q['source_quote_time']),_stamp(q['received_at'])
                        exact=(v.get('source_quote_time')==q['source_quote_time']
                            and q.get('source_date')==execution
                            and source.date().isoformat()==execution and published is not None
                            and published<=source<=received<=end<=clock
                            and 0<=(received-source).total_seconds()<=120)
                    except (KeyError,ValueError,TypeError):
                        exact=False
                    price,preclose,low=map(_positive,(q.get('last_price_rmb'),q.get('previous_close_price_rmb'),q.get('low_price_rmb')))
                    if exact and price is not None and preclose is not None:
                        observed.add(symbol)
                        is_fresh=(v.get('current_at_read') is True
                            and clock.date().isoformat()==execution
                            and 0<=(clock-source).total_seconds()<=120)
                        if is_fresh: fresh.add(symbol)
                        status='matched' if reference==preclose else 'changed_or_incompatible' if reference else 'unknown'
                        if status=='changed_or_incompatible': changed.add(symbol)
                        relation=_relation(price,line) if status=='matched' else None
                        if relation=='below': below.add(symbol)
                        tracking.update(quote_status='current_public_observation' if is_fresh else 'saved_snapshot_not_current',
                            snapshot_price_cny=float(price),fresh_at_read=is_fresh,reference_status=status,
                            snapshot_relation=relation,current_relation=relation if is_fresh else None,
                            snapshot_low_relation=_relation(low,line) if status=='matched' else None)
                    else:
                        tracking['quote_status']='unknown_quote_time_identity_or_price'
            weights=case.get('target_weights')
            candidates.append(dict(symbol=symbol,name=detail.get('name'),plan_decision=case['decision'],
                target_weight=weights.get(symbol,0) if isinstance(weights,dict) else None,tracking=tracking))
        rows.append(dict(case_id=case['case_id'],type=case['type'],decision=case['decision'],
            target_weights=deepcopy(case.get('target_weights')),candidates=candidates))
    return dict(schema=SCHEMA,status='saved_candidate_quote_observation' if observed else 'no_matching_candidate_observations',
        read_at=clock.isoformat(),paper_source_file=paper.get('source_file'),
        source_close_day=paper.get('source_close_day'),execution_day=execution,
        observation_asof=receipt.get('finished_at'),source_file=cohort.get('source_file'),
        summary=dict(candidate_appearances=sum(len(r['candidates']) for r in rows),unique_candidates=len(symbols),
            observed_unique=len(observed),current_unique=len(fresh),missing_unique=len(symbols-observed),
            reference_changed_unique=len(changed),snapshot_below_saved_line_unique=len(below)),
        rows=rows,held_entry_anchors_bound=False,reference_equality_is_not_action_verification=True,
        complete_current_market_or_theme_scope=False,**FLAGS)


def _exact_candidate_source(data_root,paper,actual):
    """Prefer the same-publication attempt, including its failures, over a subset."""
    root=Path(data_root).resolve()/'research/leader_candidate_observation'
    pointer=root/'latest.json'
    if not pointer.is_file(): return None
    latest=json.loads(pointer.read_text(encoding='utf-8-sig'))
    if latest.get('schema')!='saved_leader_candidate_quote_latest_v1':
        raise ValueError('Unknown saved candidate quote locator')
    ref=latest.get('paper_ref') or {}
    if not paper.get('source_file') or ref.get('file')!=str(Path(paper['source_file']).resolve()):
        return None
    raw=Path(paper['source_file']).read_bytes()
    if ref.get('sha256')!=sha256(raw).hexdigest() or ref.get('byte_count')!=len(raw):
        raise ValueError('Candidate quote attempt refers to a changed publication')
    path=Path(latest['receipt_file']).resolve()
    if path.parent.parent!=root or path.name!='receipt.json':
        raise ValueError('Candidate quote locator escaped its observation directory')
    receipt=json.loads(path.read_text(encoding='utf-8-sig'))
    if (receipt.get('schema')!='saved_leader_candidate_quote_observation_v1'
            or receipt.get('paper_ref')!=ref
            or receipt.get('source_scope')!='exact_saved_eight_type_candidate_union_not_registry_or_current_universe'
            or any(receipt.get(k) is not v for k,v in dict(candidate_panel_only=True,
                execution_admitted=False,selection_enabled=False,strategy_validated=False,orders_generated=False).items())):
        raise ValueError('Candidate quote contract or research boundary changed')
    symbols=sorted({d['symbol'] for row in paper.get('rows',[]) for d in row.get('candidate_details',[])})
    targets=receipt.get('target_symbols',[])
    if (sorted(targets)!=symbols or len(targets)!=len(set(targets))
            or sorted(q['symbol'] for q in receipt.get('quotes',[]))!=symbols):
        raise ValueError('Candidate quote scope differs from the complete original candidate union')
    finished=_stamp(receipt['finished_at'])
    if finished>actual: raise ValueError('Candidate quote receipt is not available yet')
    if receipt.get('status')=='completed_saved_candidate_quote_observation':
        raw_file=Path(receipt['raw_file']).resolve()
        if raw_file.parent!=path.parent or sha256(raw_file.read_bytes()).hexdigest()!=receipt['raw_sha256']:
            raise ValueError('Saved candidate raw response changed')
        from .leader_cohort_observation import parse_batch
        members=[dict(symbol=q['symbol'],name=q['registered_name'],labels=[]) for q in receipt['quotes']]
        parsed,_=parse_batch(raw_file.read_bytes(),members,_stamp(receipt['response_received_at']),
            session=dict(session_start=_stamp(receipt['session_start']),stop_at=_stamp(receipt['stop_at'])),
            registered_at=_stamp(paper['recorded_at']),batch_id=0)
        if parsed!=receipt['quotes']:
            raise ValueError('Candidate quote fields differ from their exact raw response')
    quotes=[]
    for q in receipt['quotes']:
        source=_stamp(q['source_quote_time']) if q.get('source_quote_time') else None
        current=bool(receipt.get('status')=='completed_saved_candidate_quote_observation'
            and q.get('current_quote') is True and source is not None
            and source.date()==actual.date() and 0<=(actual-source).total_seconds()<=120)
        quotes.append(dict(symbol=q['symbol'],source_quote_time=q.get('source_quote_time'),current_at_read=current))
    return dict(source_file=str(path),session={'session_day':receipt['session_day']},quotes=quotes),receipt


def leader_candidate_intraday_view(workspace,data_root,*,now=None,paper=None):
    actual=_stamp(now or datetime.now(LOCAL))
    paper=paper if paper is not None else leader_eight_type_paper_view(workspace,now=actual)
    exact=_exact_candidate_source(data_root,paper,actual)
    if exact is None:
        cohort=leader_cohort_intraday_view(data_root,now=actual)
        source=cohort.get('source_file')
        receipt=json.loads(Path(source).read_text(encoding='utf-8-sig')) if source else {}
    else:
        cohort,receipt=exact
    result=build_candidate_tracking(paper,cohort,receipt,actual)
    result['observation_scope']='exact_saved_candidate_union' if exact else 'fixed_590_proxy_cohort'
    if exact and receipt.get('status')!='completed_saved_candidate_quote_observation':
        result.update(status='unavailable_latest_candidate_attempt',error=receipt.get('error'))
    return result


def main(argv=None):
    from ..settings import DATA_ROOT,ROOT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT)
    parser.add_argument('--data-root',type=Path,default=DATA_ROOT)
    parser.add_argument('--paper',type=Path)
    args=parser.parse_args(argv)
    actual=datetime.now(LOCAL)
    paper=read_saved_plan(args.paper,now=actual) if args.paper else None
    print(json.dumps(leader_candidate_intraday_view(args.workspace,args.data_root,now=actual,paper=paper),ensure_ascii=False,allow_nan=False))
    return 0
