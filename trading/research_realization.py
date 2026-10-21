"""Read-only views of explicitly linked, already saved research accounts."""
import ast
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from .settings import ROOT, DATA_ROOT
from .portfolio_realization import analyze

SCHEMA = 'saved_research_portfolio_realization_view_v1'
CATALOG = ROOT / 'trading/research/recent_candidates.json'
LAYOUTS = {
    'replay_csv': ('replay.json','daily.csv','completed_sessions'),
    'native_selection_json': ('result.json','daily.json','engine_sessions_committed'),
}


def read_json(path, *, limit=32*1024*1024):
    path=Path(path)
    if path.stat().st_size > limit:
        raise ValueError('保存文件超过只读显示范围')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _sources(catalog):
    entries=[]
    for candidate in read_json(catalog,limit=4*1024*1024)['candidates']:
        for descriptor in (candidate.get('result') or {}).get('portfolio_realization_sources',[]):
            layout=descriptor.get('layout')
            if layout not in LAYOUTS:
                raise ValueError('未知保存账户格式')
            folder=Path(descriptor['folder']).resolve()
            if not folder.is_relative_to((DATA_ROOT/'research').resolve()):
                raise ValueError('保存账户必须位于研究数据目录')
            key=candidate['strategy_id']+'::'+descriptor['key']
            entries.append(dict(case_id=key,label=descriptor['label'],
                candidate_id=candidate['strategy_id'],candidate_label=candidate.get('display_name',candidate['strategy_id']),
                folder=folder,layout=layout,expected_sessions=descriptor['expected_sessions']))
    if len({r['case_id'] for r in entries}) != len(entries):
        raise ValueError('保存账户身份重复')
    return entries


def view(case_id=None, *, catalog=CATALOG):
    entries=_sources(catalog)
    cases=[]
    metadata={}
    for source in entries:
        public={k:source[k] for k in ('case_id','label','candidate_id','candidate_label')}
        public['expected_sessions']=source['expected_sessions']
        try:
            receipt,_,count_field=LAYOUTS[source['layout']]
            record=read_json(source['folder']/receipt,limit=4*1024*1024)
            metadata[source['case_id']]=record
            public.update(status=record['status'],committed_sessions=record.get(count_field),
                last_saved_day=record.get('last_completed_session',record.get('last_committed_day')),
                source_available=True)
        except (OSError,ValueError,KeyError,TypeError) as error:
            public.update(status='source_unavailable',committed_sessions=None,last_saved_day=None,
                source_available=False,error=str(error))
        cases.append(public)
    response=dict(schema=SCHEMA,cases=cases,selected_case_id=case_id,detail=None,
        read_at=datetime.now().astimezone().isoformat(),readonly=True,
        producer_called=False,engine_advanced=False,execution_admitted=False)
    if case_id is None:
        return response
    selected=next((r for r in entries if r['case_id']==case_id),None)
    if selected is None:
        raise ValueError('未登记的研究账户')
    try:
        record=metadata.get(case_id)
        if record is None:
            raise ValueError('该账户的保存凭据尚不可读取')
        if record['status'] not in {'completed','failed','incomplete_or_no_fills','completed_conditional_replay'}:
            raise ValueError('账户尚未形成终态保存记录')
        receipt,daily_file,count_field=LAYOUTS[selected['layout']]
        folder=selected['folder']
        state=read_json(folder/'account.json')
        if selected['layout']=='replay_csv':
            path=folder/daily_file
            if path.stat().st_size>16*1024*1024:
                raise ValueError('日结文件超过只读显示范围')
            frame=pd.read_csv(path,dtype={'date':str})
            if 'stale_symbols' in frame:
                frame['stale_symbols']=frame.stale_symbols.map(ast.literal_eval)
            daily=frame.to_dict('records')
        else:
            daily=read_json(folder/daily_file,limit=16*1024*1024)
        dates=[r['date'] for r in daily]
        if (dates!=sorted(set(dates)) or len(daily)!=record.get(count_field)
                or (dates[-1] if dates else None)!=state.get('day')):
            raise ValueError('账户、日结和提交日数尚未对应，不补造缺行')
        detail=analyze(record['config'],state,daily)
        detail.update(case_id=case_id,label=selected['label'],source_status=record['status'],
            expected_sessions=selected['expected_sessions'],
            full_window_saved=len(daily)==selected['expected_sessions'],
            source_files={'account':str(folder/'account.json'),'daily':str(folder/daily_file),
                'receipt':str(folder/receipt)},
            source_stop=(record.get('error') or (record.get('blocking_evidence') or {}).get('day')))
        response['detail']=detail
        response['detail_status']='available'
    except (OSError,ValueError,KeyError,TypeError,SyntaxError) as error:
        response.update(detail_status='unavailable',error=str(error))
    return response
