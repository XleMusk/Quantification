"""Register independent public-seat append methods, without accounts or replay."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from . import seat_hypothesis_dated_inputs as refs
from . import seat_append_dated_inputs as dated
from . import seat_disclosure_catalog as catalog


def register_inputs(original_registration, initial_catalog, calendar, first_decision_day, out):
    now = refs._now()
    base, _, base_refs = refs._registered(original_registration, now)
    initial = catalog.load_catalog(initial_catalog, now=now)
    references = {'original_registration':refs._ref(original_registration),
        'initial_catalog':initial['reference'], 'calendar':refs._ref(calendar),
        'implementation':refs._ref(dated.__file__), 'catalog_implementation':refs._ref(catalog.__file__)}
    files = [r['file'] for r in references.values()] + [r['file'] for r in base_refs] + initial['files']
    target = refs._protect(out,files,[Path(file).resolve().parent for file in files])
    record = {'schema':dated.REGISTRATION_SCHEMA, 'method_id':dated.METHOD_ID,
        'registered_at':now.isoformat(), **references, 'first_decision_day':refs._day(first_decision_day),
        'arm_ids':list(dated.ARMS), 'config':base['config'], 'rule_spec':dated.RULE_SPEC, **dated.FLAGS}
    # Validate session/clock before writing; keep any subsequent diagnostic failure.
    from .exchange_calendar import load_calendar
    from .leader_daily_signals import _calendar_view
    payload=load_calendar(Path(calendar))
    if payload is None: raise ValueError('A pinned exchange calendar is required')
    year=int(payload['year']);sessions,_,status=_calendar_view(payload,f'{year}-01-01',f'{year}-12-31',[])
    first=record['first_decision_day']
    if (status!='pinned_static_exchange_calendar' or first not in sessions
            or first<now.date().isoformat() or sessions.index(first)+1>=len(sessions)):
        raise ValueError('Prospective first D and exact next E are required')
    if refs._stamp(initial['created_at'],now)>now:
        raise ValueError('Initial catalog is not actually available')
    target.parent.mkdir(parents=True,exist_ok=True)
    refs._write(target,record)
    checked=dated.load_registration(target,now=refs._now())
    return {'schema':'public_seat_append_input_registration_receipt_v1',
        'status':'registered_new_append_input_method_only','registration':checked['reference'],
        'first_decision_day':first,'initial_catalog':initial['reference'],
        'source_batch_count':len(initial['batches']),**dated.FLAGS}


def register_forward(dated_registration, ipo_proof, research_master_run, out):
    from . import seat_append_forward as forward
    from . import seat_hypothesis_dated_forward as legacy
    now=refs._now()
    method=dated.load_registration(dated_registration,now=now)
    references={'dated_input_method_registration':method['reference'],
        'initial_catalog':method['record']['initial_catalog'],'calendar':method['calendar'],
        'ipo_evidence':refs._ref(ipo_proof),'research_master_run':refs._ref(research_master_run),
        'implementation':refs._ref(forward.__file__)}
    helpers=[refs._ref(module.__file__) for module in (legacy,refs)]
    qualification_files,_=legacy._qualification_files(ipo_proof,research_master_run)
    files=list(method['files'])+[r['file'] for r in references.values()]+[r['file'] for r in helpers]+qualification_files
    target=refs._protect(out,files,[Path(file).resolve().parent for file in files])
    record={'schema':forward.REGISTRATION_SCHEMA,'method_id':forward.METHOD_ID,
        'registered_at':now.isoformat(),**references,'shared_helper_implementations':helpers,
        'first_decision_day':method['record']['first_decision_day'],
        'arm_ids':list(dated.ARMS),'config':method['config'],'rule_spec':forward.RULE_SPEC,**forward.FLAGS}
    if record['first_decision_day']<now.date().isoformat():
        raise ValueError('Forward registration cannot backfill or roll first D')
    target.parent.mkdir(parents=True,exist_ok=True);refs._write(target,record)
    checked=forward._registration(target,forward._now())
    return {'schema':'public_seat_append_forward_registration_receipt_v1',
        'status':'registered_new_append_four_book_method_only','registration':checked['reference'],
        'first_decision_day':record['first_decision_day'],'arm_ids':record['arm_ids'],
        'account_created':False,'engine_run':False,'orders_generated':False,**forward.FLAGS}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    inp=sub.add_parser('inputs')
    inp.add_argument('--original-registration',required=True,type=Path)
    inp.add_argument('--initial-catalog',required=True,type=Path)
    inp.add_argument('--calendar',required=True,type=Path)
    inp.add_argument('--first-decision-day',required=True)
    inp.add_argument('--out',required=True,type=Path)
    fwd=sub.add_parser('forward')
    fwd.add_argument('--dated-registration',required=True,type=Path)
    fwd.add_argument('--ipo-proof',required=True,type=Path)
    fwd.add_argument('--research-master-run',required=True,type=Path)
    fwd.add_argument('--out',required=True,type=Path)
    args=parser.parse_args(argv)
    result=(register_inputs(args.original_registration,args.initial_catalog,args.calendar,args.first_decision_day,args.out)
        if args.command=='inputs' else register_forward(args.dated_registration,args.ipo_proof,args.research_master_run,args.out))
    print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0


if __name__=='__main__':
    raise SystemExit(main())
