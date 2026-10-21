"""Fixed market-model momentum comparison through the existing stock Engine.

This daily, single-price-index adaptation is not the paper's monthly FF3 model.
No source collection, old-model refit, account promotion or live execution.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..ledger import Ledger
from .baostock_daily_slice import digest
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result
from .high_volume_attention import read, save
from .low_risk import LowRiskFeed, features, portfolio
from .market_residual_momentum import score
from .monthly_timing import month_ends

METHOD = 'market_model_504d_residual_momentum_matched_three_arm_v1'
ARMS = ('raw', 'standardized', 'residual_std')
END = '2026-09-30'
SOURCE = 'https://repub.eur.nl/pub/22252/ResidualMomentum-2011.pdf'
FLAGS = dict(candidate_panel_only=True, strategy_validated=False, selection_enabled=False,
    execution_admitted=False, returns_reportable=False, pit_verified=False,
    independent_holdout=False, model_refit=False, old_models_modified=False,
    future_labels_used_for_score=False, network_accessed=False)
SPEC = dict(id=METHOD, end=END, arms=list(ARMS), source=SOURCE,
    hypothesis='stock_specific_trend_after_removing_market_comovement_improves_momentum',
    paper='36_month_monthly_FF3_regressions_and_11_month_standardized_residuals',
    adaptation='504_exact_daily_price_returns_intercept_OLS_on_CSI300_price_return_no_Rf_SMB_HML',
    formation='231_daily_returns_Dminus251_through_Dminus21_skip_last21',
    controls='raw_P_Dminus21_over_P_Dminus252_minus1_and_same_formation_mean_over_sample_std',
    residual='same_formation_mean_r_minus_alpha_minus_beta_market_over_its_sample_std',
    common_pool='original_low_risk_pool_intersect_complete504_return_and_three_score_known',
    no_imputation=True, missing_market='whole_batch_NO_CHANGE',
    decisions='confirmed_month_end_with_covered_following_session',
    selection='original_top20_retain30_equal4percent_max80percent_no_sign_gate_no_refill',
    execution='unchanged_Engine_Ledger_next_raw_open_T1_lots_fees_limits_participation',
    terminal='mark_last_close_no_forced_sale_no_missing_endpoint_zero',
    unknown_actions='stop_affected_account_only_no_notice_chasing',
    evidence='inspected_historical_development_adaptation_not_independent_or_paper_replication',
    thresholds_or_windows_selected_by_return=False)


def now():
    return datetime.now().astimezone().isoformat()


def separate(out, paths):
    out = Path(out).resolve()
    if any(out == p or out in p.parents or p in out.parents for p in map(lambda v: Path(v).resolve(), paths)):
        raise ValueError('Research output cannot overlap source inputs')


def index_close(folder):
    folder = Path(folder)
    manifest = read(folder / 'manifest.json')
    receipt = manifest['receipts']['sh.000300']
    if manifest['end'] != END or manifest['source'] != 'BaoStock':
        raise ValueError('Require the explicitly chosen saved index source through September30')
    for name, key in (('file', 'sha256'), ('raw_file', 'raw_sha256')):
        if digest(folder / receipt[name]) != receipt[key]:
            raise ValueError('Saved CSI300 source receipt changed')
    frame = pd.read_parquet(folder / receipt['file'])
    dates = frame.date.tolist()
    if (len(frame) != receipt['rows'] or dates != sorted(set(dates)) or dates[-1] != END
            or not frame.code.eq('sh.000300').all()):
        raise ValueError('CSI300 exact source dates/identity changed')
    closes = pd.to_numeric(frame.close, errors='coerce')
    if not (np.isfinite(closes) & closes.gt(0)).all():
        raise ValueError('CSI300 close unknown; no filling')
    return pd.Series(closes.to_numpy(), index=dates), manifest


def register(source_registration, index_folder, out):
    original = read(source_registration)
    if original.get('schema') != 'own50_activity_conditioned_momentum_fixed20session_v1_registration':
        raise ValueError('Use the original frozen complete source locator')
    market, manifest = index_close(index_folder)
    inputs = original['inputs']
    calendar = pd.read_csv(Path(inputs['event_view']) / 'source_input_calendar.csv', dtype=str).date.tolist()
    if calendar != market.index.tolist() or len(calendar) < 505:
        raise ValueError('Stock/index calendars must be exact; intersection cannot hide missing dates')
    refs = dict(original['refs'])
    refs['source_registration'] = dict(path=str(Path(source_registration).resolve()), sha256=digest(source_registration))
    for name in ('manifest.json', 'sh000300.parquet', 'sh000300_response.csv'):
        path = Path(index_folder).resolve() / name
        refs['index:' + name] = dict(path=str(path), sha256=digest(path))
    for ref in refs.values():
        if digest(ref['path']) != ref['sha256']:
            raise ValueError('Existing source locator changed')
    separate(out, [source_registration, index_folder, *[inputs[k] for k in
        ('price_view', 'event_view', 'reference_view', 'raw_view', 'listing_master')]])
    Path(out).mkdir(parents=True, exist_ok=False)
    record = dict(schema=METHOD + '_registration', registered_at=now(), spec=SPEC,
        config=portfolio().to_dict(), start=calendar[504], end=END, sessions=len(calendar)-504,
        decision_days=[d for d in month_ends(calendar, END) if d >= calendar[504]],
        inputs=inputs, refs=refs, index_folder=str(Path(index_folder).resolve()),
        index_limitations=manifest['limitations'], implementations={str(p.resolve()):digest(p) for p in
            (Path(__file__), Path(__file__).with_name('market_residual_momentum.py'))}, **FLAGS)
    save(Path(out) / 'registration.json', record)
    return record


class SavedMarketMomentumFeed:
    def __init__(self, base, batches, arm, config):
        self.base, self.batches, self.arm, self.config = base, batches, arm, config
        self.audit = []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config or self.arm not in ARMS:
            raise ValueError('Fixed independent research account required')
        batch = self.batches.get(day)
        if batch is None or batch['status'] != 'complete':
            reason = 'not_covered_month_end' if batch is None else batch['reason']
            self.audit.append(dict(date=day, intent='NO_CHANGE', reason=reason))
            return SignalUnavailable(reason)
        rows = batch['targets'][self.arm]
        self.audit.append(dict(date=day, intent='FULL_TARGET', candidates=len(rows)))
        return SignalFrame.from_legacy(rows, day, 'research:' + METHOD + ':' + self.arm, context)


def run(registration_path, out):
    out = Path(out).resolve()
    registration = read(registration_path)
    if (registration.get('schema') != METHOD + '_registration' or registration['spec'] != SPEC
            or registration['config'] != portfolio().to_dict()):
        raise ValueError('Fixed method and portfolio registration required')
    for ref in registration['refs'].values():
        if digest(ref['path']) != ref['sha256']:
            raise ValueError('Registered source changed')
    for path, value in registration['implementations'].items():
        if digest(path) != value:
            raise ValueError('Registered implementation changed')
    inputs = registration['inputs']
    separate(out, [registration_path, registration['index_folder'], *[inputs[k] for k in
        ('price_view','raw_view','event_view','reference_view','listing_master')]])
    out.mkdir(parents=True, exist_ok=False)
    record = dict(schema=METHOD, status='running', started_at=now(), registration=str(registration_path),
        registration_sha256=digest(registration_path), start=registration['start'], end=END,
        sessions=registration['sessions'], config=portfolio().to_dict(), cases=[], **FLAGS)
    save(out/'started.json', record)
    base = None
    try:
        base = BulkResearchFeed(**{k:inputs[k] for k in ('price_view','event_view','reference_view','listing_master')})
        market, _ = index_close(registration['index_folder'])
        if base.calendar != market.index.tolist() or len(base.symbols) != inputs['canonical_codes']:
            raise ValueError('Exact source scope/calendar changed')
        dates = [d for d in base.calendar if registration['start'] <= d <= END]
        days = registration['decision_days']
        print(json.dumps(dict(stage='source_loaded', sessions=len(dates), source_symbols=len(base.symbols), decision_days=len(days))),flush=True)
        config = portfolio()
        values = features(base, END, feature_days=days)
        pool_feed = LowRiskFeed(base, values, 'momentum_12_1_20', END, diagnostic_extension=True)
        closes = base.closes.where(base.volumes.gt(0))
        batches, scored = {}, []
        for day in days:
            pool = pool_feed.pool(day, config)
            if isinstance(pool, SignalUnavailable):
                batches[day] = dict(status='incomplete',reason=pool.reason,targets=None)
                continue
            result = score(closes, market, day)
            if result['status'] == 'unavailable':
                batches[day] = dict(status='incomplete',reason=result['reason'],targets=None)
                continue
            known = {r['symbol']:r for r in result['rows']}
            common = [{**r,**known[r['symbol']]} for r in pool if r['symbol'] in known]
            scored.extend(dict(date=day,**r) for r in common)
            targets={}
            for arm in ARMS:
                ranked=sorted(common,key=lambda r:(-r[arm],r['symbol']))
                targets[arm]=[dict(symbol=r['symbol'],score=r[arm],momentum=r['raw'],volatility=r['volatility'],
                    median_amount=r['median_amount'],alpha=r['alpha'],beta=r['beta']) for r in ranked[:config.retain_rank]]
            batches[day]=dict(status='complete',original_pool=len(pool),common_pool=len(common),
                unknown_count=len(pool)-len(common),unknown_reasons=dict(Counter(result['unknown'].get(r['symbol'],'score_unavailable') for r in pool if r['symbol'] not in known)),targets=targets)
        save(out/'batches.json',batches)
        pd.DataFrame(scored).to_parquet(out/'scores.parquet',index=False)
        save(out/'score_publication.json',dict(published_at=now(),historical_signal_publication=False,
            files={name:dict(path=str(out/name),sha256=digest(out/name)) for name in ('batches.json','scores.parquet')},**FLAGS))
        print(json.dumps(dict(stage='formation_saved',rows=len(scored),complete_batches=sum(b['status']=='complete' for b in batches.values()))),flush=True)
        for arm in ARMS:
            feed=SavedMarketMomentumFeed(base,batches,arm,config)
            result=baseline_ledger_result(feed,config,dates,out/arm)
            save(out/arm/'decision_audit.json',feed.audit)
            state=read(out/arm/'account.json')
            book=Ledger(state,config)
            result.update(arm=arm,account=str(out/arm/'account.json'),cumulative_orders=len(state['orders']),
                cumulative_fills=len(state['fills']),residual_positions={s:book.quantity(s) for s in state['positions'] if book.quantity(s)>0},
                full_period_metrics_available=result['status']=='completed' and bool(state['fills']))
            if not result['full_period_metrics_available']:
                result.update(net_return=None,max_drawdown=None,sharpe_zero_rf_252=None,annualized_volatility=None,worst_day=None)
            record['cases'].append(result)
            print(json.dumps(dict(stage='account_terminal',arm=arm,status=result['status'],sessions=result['completed_sessions'],fills=len(state['fills']))),flush=True)
        record.update(status='completed_fixed_three_arm_research',completed_at=now(),formation_rows=len(scored),
            formation_days=len(days),unique_scored_symbols=len({r['symbol'] for r in scored}),
            full_paired_account_comparison=all(r['full_period_metrics_available'] for r in record['cases']),
            marked_returns_not_liquidated=True,price_index_not_hedge_or_execution_asset=True,
            action_notices_downloaded=0,source_limitations=base.manifest['limitations'])
        save(out/'result.json',record)
        return record
    except Exception as error:
        record.update(status='failed_research_command',completed_at=now(),error=f'{type(error).__name__}: {error}')
        save(out/'failure.json',record)
        raise
    finally:
        if base is not None:
            base.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='operation',required=True)
    reg=subs.add_parser('register')
    reg.add_argument('--source-registration',required=True)
    reg.add_argument('--index-folder',required=True)
    reg.add_argument('--out',required=True)
    execute=subs.add_parser('run')
    execute.add_argument('--registration',required=True)
    execute.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    value=register(args.source_registration,args.index_folder,args.out) if args.operation=='register' else run(args.registration,args.out)
    print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
