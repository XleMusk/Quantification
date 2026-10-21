"""Dated native plans plus saved contemporaneous quotes for unbound PAPER.

No provider, model, Engine or account is started by these readers. The import
command reuses the existing raw-RMB CSV parser; it does not turn a public quote
into BrokerQuote, nor attest that a manually supplied source is authentic.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import argparse
import json
import math
from pathlib import Path

import pandas as pd

from . import engine
from .config import StrategyConfig
from .contracts import MARKET_TZ, TradingMode
from .intraday_session import DAILY_SOURCE_POLICY
from .rules import stock_board
from .ths.quotes import build_quote_observation
from .research.exchange_calendar import load_calendar
from .research.ipo_qualification import load_ipo_qualification
from .research.leader_daily_signals import _calendar_view
from .research.shortline_native_preopen import load_preopen_plan, QUALIFIED_POLICY_ID
from .research.shortline_native_preopen_forward import _source, _panel_link, _qualification_link, _read, _stamp
from .research.shortline_decision_v3 import _known_raw

QUOTE_SCHEMA = 'native_intraday_quote_observation_v1'
PRICE_METHOD = 'fresh_observed_price_proxy_not_session_open_or_guaranteed_fill'


def _now():
    return datetime.now(MARKET_TZ)


def _clock(value):
    return _stamp(value)


def _ref(path):
    return _read(path)[1]


def _risk_warning_check(source, prices, observed):
    from .research.current_risk_warning_source import load
    snapshot = load(source, as_of=observed)
    if snapshot['status'] != 'saved_same_day_official_positive_risk_warning_report':
        raise ValueError('Same-day complete official risk-warning evidence is unavailable')
    positive = set(snapshot['ordinary_risk_symbols'])
    conflicts = sorted(symbol for symbol, row in prices.items() if symbol in positive and row.get('is_st') is False)
    if conflicts:
        raise ValueError('Quote non-ST claim conflicts with the official risk-warning report: ' + ','.join(conflicts))
    return dict(receipt_ref=_ref_bytes(Path(source) / 'receipt.json'), source_directory=str(Path(source).resolve()),
        source_available_at=snapshot['source_available_at'],
        positive_quote_symbols=sorted(symbol for symbol in prices if symbol in positive),
        non_ST_or_tradable_inference_admitted=False, original_quote_prices_and_status_unchanged=True)


def import_quote(csv_file, *, source, source_kind, quote_asof, trading_day, phase, out, now=None,
        risk_warning_source=None):
    """Save a new explicit observation; never overwrite or infer missing status."""
    actual = _clock(now) if now is not None else _now()
    asof = _clock(quote_asof)
    if (phase not in {'opening', 'closing'} or source_kind not in {'public_export','readonly_terminal_export'}
            or actual.date().isoformat() != trading_day or asof.date().isoformat() != trading_day
            or asof > actual or (phase=='opening' and (actual-asof).total_seconds()>90)):
        raise ValueError('A same-session observation is required; opening must be no older than 90 seconds')
    csv_file, out = Path(csv_file).resolve(), Path(out).resolve()
    if out == csv_file or out.exists():
        raise FileExistsError('Quote output must be new and separate from its source')
    before = _ref_bytes(csv_file)
    data = build_quote_observation(csv_file, source=source, observed_at=asof.isoformat(), trading_date=trading_day)
    for symbol, row in data['prices'].items():
        stock_board(symbol)
        if 'tradable' not in row or (phase == 'opening' and not {'preclose_rmb','is_st'} <= row.keys()):
            raise ValueError('Explicit current suspension/ST/reference facts required: '+symbol)
    risk_check = _risk_warning_check(risk_warning_source, data['prices'], actual) if risk_warning_source is not None else None
    record = dict(schema=QUOTE_SCHEMA, phase=phase, trading_day=trading_day,
        source=str(source), source_kind=source_kind, quote_asof=asof.isoformat(),
        observed_at=actual.isoformat(), source_available_at=actual.isoformat(),
        price_basis='raw_rmb', execution_price_method=PRICE_METHOD,
        source_csv=before, prices=data['prices'], provider_authenticated=False,
        broker_quote=False, execution_admitted=False, selection_enabled=False,
        strategy_validated=False, candidate_panel_only=True)
    if risk_check is not None:
        record['official_positive_risk_warning_check'] = risk_check
    if _ref_bytes(csv_file) != before:
        raise ValueError('Quote CSV changed while being imported')
    out.parent.mkdir(parents=True, exist_ok=True)
    from .research.intraday_native_rehearsal import _save
    _save(out, record)
    return record


def _ref_bytes(path):
    path=Path(path).resolve(); raw=path.read_bytes()
    return dict(file=str(path), sha256=sha256(raw).hexdigest(), byte_count=len(raw))


def load_quote(path, phase, day, now):
    record, reference = _read(path)
    if (record.get('schema') != QUOTE_SCHEMA or record.get('phase') != phase
            or record.get('trading_day') != day or record.get('price_basis') != 'raw_rmb'
            or record.get('execution_price_method') != PRICE_METHOD
            or record.get('source_kind') not in {'public_export','readonly_terminal_export'}
            or record.get('broker_quote') is not False or record.get('execution_admitted') is not False
            or record.get('provider_authenticated') is not False
            or record.get('selection_enabled') is not False or record.get('strategy_validated') is not False
            or record.get('candidate_panel_only') is not True
            or not record.get('source')):
        raise ValueError('Explicit saved public/terminal PAPER observation required')
    observed, available, asof = map(_clock, (record['observed_at'], record['source_available_at'], record['quote_asof']))
    if (any(t.date().isoformat()!=day or t > now for t in (observed, available, asof))
            or not asof <= available == observed
            or (phase=='opening' and (observed-asof).total_seconds()>90)):
        raise ValueError('Quote actual observation/availability clock is invalid')
    if phase == 'opening':
        if (now-asof).total_seconds()>90:
            raise ValueError('Opening quote is stale at consumption')
        for t in (asof,observed):
            if not ((9,30) <= (t.hour,t.minute) <= (11,30) or (13,0) <= (t.hour,t.minute) < (15,0)):
                raise ValueError('Opening quote must be observed during continuous trading')
    elif any((t.hour,t.minute,t.second)<(15,0,0) for t in (asof,observed)):
        raise ValueError('Closing quote must be observed after the actual close')
    csv_file=Path(record['source_csv']['file']).resolve()
    if _ref_bytes(csv_file) != record['source_csv']:
        raise ValueError('Saved quote raw CSV changed')
    parsed=build_quote_observation(csv_file, source=record['source'],observed_at=asof.isoformat(),trading_date=day)
    if parsed['prices'] != record['prices']:
        raise ValueError('Saved normalized quote differs from original raw CSV')
    check = record.get('official_positive_risk_warning_check')
    if check is not None and _risk_warning_check(check['source_directory'], parsed['prices'], observed) != check:
        raise ValueError('Bound official positive risk-warning evidence changed')
    return record, reference


@dataclass
class NativeInputs:
    loaded: dict
    config: StrategyConfig
    calendar: list
    proof: dict
    input_paths: list
    prior: dict
    qualification: object
    now: datetime

    def feed(self, quote_path, phase, context):
        if context.mode != TradingMode.PAPER:
            raise ValueError('The daily native quote consumer is PAPER only')
        record, ref=load_quote(quote_path,phase,self.loaded['execution_day'],self.now)
        return NativeQuoteFeed(self,record,ref,phase,context)


class NativeQuoteFeed:
    def __init__(self, prepared, quote, quote_ref, phase, context):
        self.prepared, self.quote, self.phase, self.context=prepared,quote,phase,context
        self.calendar=list(prepared.calendar)
        self.symbols=sorted(set(prepared.prior['original_rows']) | set(prepared.qualification.records))
        self.instruments={s:dict(ipoDate=prepared.qualification.records.get(s,{}).get('ipo_date'),outDate=None)
            for s in self.symbols}
        self.instrument_profiles={}; self.actions=[]
        self.manifest=dict(replay_only=False, execution_admitted=False,
            paper_quote_method=PRICE_METHOD, research_only=True,
            corporate_actions_verified=False,pit_verified=False, historical_price_adjustments=[])
        self.intraday_phase_binding=dict(schema_version=1, source_id='saved_native_daily_quote_projection_v1',
            quote_source_kind='observed_market_snapshot', daily_source_policy=DAILY_SOURCE_POLICY,
            policy_id=prepared.loaded['record']['policy_id'],
            provider_source=quote['source'],provider_kind=quote['source_kind'],
            execution_price_method=PRICE_METHOD)
        self.quote_ref=quote_ref
        self.input_paths=[*prepared.input_paths, Path(quote_ref['file']), Path(quote['source_csv']['file'])]
        if quote.get('official_positive_risk_warning_check'):
            self.input_paths.append(Path(quote['official_positive_risk_warning_check']['source_directory']))
        setattr(self,'intraday_'+phase+'_source_ref',quote_ref['file']+'#sha256='+quote_ref['sha256'])
        setattr(self,'intraday_'+phase+'_observed_at',quote['observed_at'])
        setattr(self,'intraday_'+phase+'_source_available_at',quote['source_available_at'])
        d,e=prepared.loaded['source_day'],prepared.loaded['execution_day']
        self.volumes=pd.DataFrame([{s:float(row['volume']) if row['volume'] not in ('',None) else float('nan')
            for s,row in prepared.prior['original_rows'].items()}],index=[d])
        self.rows={e:{}}
        for symbol,row in quote['prices'].items():
            stock_board(symbol)
            if 'tradable' not in row or type(row['tradable']) is not bool:
                raise ValueError('Missing explicit current suspension status: '+symbol)
            value=dict(tradestatus=int(row['tradable']))
            if phase=='opening':
                if not {'preclose_rmb','is_st'}<=row.keys() or type(row['is_st']) is not bool:
                    raise ValueError('Missing explicit ST or reference status: '+symbol)
                value.update(open=float(row['price_rmb']),preclose=float(row['preclose_rmb']),
                    isST=int(row['is_st']),adjustflag='3')
            else:
                value.update(close=float(row['price_rmb']))
            self.rows[e][symbol]=value

    def next_day(self, day):
        i=self.calendar.index(day)
        if i+1>=len(self.calendar): raise ValueError('Exact following session unavailable')
        return self.calendar[i+1]

    def _validate(self,state,day,phase):
        loaded=self.prepared.loaded
        if day!=loaded['execution_day'] or phase!=self.phase:
            raise ValueError('Quote/plan exact phase or session changed')
        if phase=='opening' and (engine._published_now().astimezone(MARKET_TZ)-_clock(self.quote['quote_asof'])).total_seconds()>90:
            raise ValueError('Quote aged before its opening commit')
        required={s for s,lots in state['positions'].items() if sum(v['qty'] for v in lots)}
        if phase=='opening':
            required.update(s for s,w in (loaded['decisions'][self.context.account_id.rsplit(':',1)[-1]]['weights'] or {}).items() if w>0)
        for symbol in sorted(required):
            if symbol not in self.instruments or symbol not in self.rows[day]:
                raise ValueError('Missing exact current holding/target quote: '+symbol)
            if phase=='closing': continue
            previous=self.prepared.prior['original_rows'].get(symbol)
            canonical=self.prepared.prior['canonical_rows'].get(symbol)
            if not _known_raw(canonical):
                raise ValueError('Prior dated raw quality unknown: '+symbol)
            row=self.quote['prices'][symbol]
            if Decimal(previous['close'])!=Decimal(row['preclose_rmb']):
                raise ValueError('Dclose/Epreclose unresolved; no action/reference imputation: '+symbol)
            assessment=self.prepared.qualification.assess(symbol,day,decision_as_of=day+'T09:30:00+08:00')
            if assessment['engine_block_reason'] is not None:
                raise ValueError('Listing evidence unknown or excluded at exact opening: '+symbol)
            # Default Engine listing rules remain active; this feed has no
            # research_listing_limit_reason hook and never relaxes that guard.

    def validate_opening_session(self,state,day): self._validate(state,day,'opening')
    def validate_closing_session(self,state,day): self._validate(state,day,'closing')


def prepare_inputs(plan, prior_package, *, ipo_proof=None, master_run=None, now=None):
    actual=_clock(now) if now is not None else _now()
    loaded=load_preopen_plan(plan)
    calendar_ref=loaded['calendar']; payload=load_calendar(Path(calendar_ref['file']))
    year=payload['year']
    sessions,previous,status=_calendar_view(payload,f'{year}-01-01',f'{year}-12-31',[])
    d,e=loaded['source_day'],loaded['execution_day']
    if status!='pinned_static_exchange_calendar' or previous.get(e)!=d:
        raise ValueError('Exact prior/next exchange sessions required')
    original=loaded['registration'].get('qualification_inputs')
    if original is not None:
        ipo_proof=ipo_proof or original['ipo_evidence']['file']
        master_run=master_run or (original['research_master_run'] or {}).get('file')
    if ipo_proof is None:
        raise ValueError('An explicit saved listing-evidence source is required')
    qualification=load_ipo_qualification(ipo_proof,calendar_ref['file'],master_run=master_run)
    if list(qualification.calendar)!=sessions:
        raise ValueError('Listing source calendar differs')
    qualification_inputs=dict(ipo_evidence=_ref(ipo_proof),research_master_run=_ref(master_run) if master_run else None)
    if loaded['record']['policy_id']==QUALIFIED_POLICY_ID:
        _qualification_link(loaded,qualification,qualification_inputs)
    panel=_panel_link(loaded,actual)
    prior=_source(prior_package,calendar_ref,sessions,actual)
    if prior['proof']['day']!=d or any(prior['proof'][k]!=v for k,v in panel['source_close_proof'].items()):
        raise ValueError('D raw source differs from the published score features')
    prior['canonical_rows']=prior['canonical'].set_index('symbol').to_dict('index')
    for row in prior['canonical_rows'].values():
        for field in ('tradestatus','isST'):
            value=row.get(field)
            row[field]=int(value) if value in (0,1,'0','1') else None
    models=loaded['registration']['frozen_model_refs']
    for ref in models.values():
        if _ref_bytes(ref['manifest_file'])['sha256']!=ref['manifest_sha256']:
            raise ValueError('Frozen model generation changed')
    config=StrategyConfig(**loaded['record']['spec']['config']).validate()
    proof=dict(plan_ref=loaded['plan_ref'],calendar_ref=calendar_ref,model_manifest=models,
        source_day=d,execution_day=e,source_close_proof=prior['proof'],
        qualification_inputs=qualification_inputs,qualification_verification=qualification.verification,
        feature_metadata=panel,policy_id=loaded['record']['policy_id'],
        company_actions_complete=False,returns_reportable=False)
    paths=[Path(loaded['plan_ref']['file']),Path(calendar_ref['file']),Path(prior_package)/'raw_daily.csv',
        Path(ipo_proof),*[Path(ref['manifest_file']) for ref in models.values()]]
    if master_run: paths.append(Path(master_run))
    # Retain all direct and indirect source package paths already validated by
    # the existing loaders; the consumer uses these only to prevent overwrites.
    def walk(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {'file','source_file','raw_path','source_receipt','manifest_file','master_file'} and isinstance(item,str):
                    p=Path(item)
                    if p.is_absolute(): paths.append(p)
                walk(item)
        elif isinstance(value,list):
            for item in value: walk(item)
    walk(loaded); walk(qualification.verification); walk(panel)
    return NativeInputs(loaded,config,sessions,proof,list(dict.fromkeys(p.resolve() for p in paths)),prior,qualification,actual)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    q=sub.add_parser('import-quote')
    q.add_argument('--csv',required=True,type=Path); q.add_argument('--source',required=True)
    q.add_argument('--source-kind',required=True,choices=['public_export','readonly_terminal_export'])
    q.add_argument('--quote-asof',required=True); q.add_argument('--day',required=True)
    q.add_argument('--phase',required=True,choices=['opening','closing']);q.add_argument('--out',type=Path,required=True)
    q.add_argument('--risk-warning-source',type=Path,
        help='Explicit saved official positive report; rejects contradictory non-ST claims without filling missing status')
    inspect=sub.add_parser('inspect')
    inspect.add_argument('--plan',type=Path,required=True); inspect.add_argument('--prior-package',type=Path,required=True)
    inspect.add_argument('--ipo-proof',type=Path);inspect.add_argument('--master-run',type=Path)
    args=parser.parse_args(argv)
    if args.command=='import-quote':
        r=import_quote(args.csv,source=args.source,source_kind=args.source_kind,quote_asof=args.quote_asof,
            trading_day=args.day,phase=args.phase,out=args.out,risk_warning_source=args.risk_warning_source)
        print(json.dumps(dict(schema=r['schema'],trading_day=r['trading_day'],phase=r['phase'],quote_count=len(r['prices']),
            observed_at=r['observed_at'],broker_quote=False,execution_admitted=False),ensure_ascii=False))
    else:
        p=prepare_inputs(args.plan,args.prior_package,ipo_proof=args.ipo_proof,master_run=args.master_run)
        print(json.dumps(dict(schema='native_intraday_inputs_readiness_v1',source_day=p.loaded['source_day'],
            execution_day=p.loaded['execution_day'],plan_ref=p.proof['plan_ref'],source_rows=len(p.prior['original_rows']),
            observations_read=0,account_created=False,engine_advanced=False,execution_admitted=False),ensure_ascii=False))
    return 0
