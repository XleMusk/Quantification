from contextlib import contextmanager
from datetime import datetime
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import baostock_full_close as source
from trading.research.tests.test_baostock_full_close import setup, SDK, NOW

HISTORICAL_DAY = "2026-10-12"


def test_explicit_closed_historical_scope_rejects_future_intraday_and_non_sessions(setup):
    calendar, _ = setup
    record = source.historical_closed_scope(HISTORICAL_DAY, calendar, NOW)
    assert record["acquisition_mode"] == source.HISTORICAL_MODE
    assert record["roster_requested_day"] == HISTORICAL_DAY
    for day in ("2026-10-11", "2026-10-15", "2026-10-16"):
        with pytest.raises(ValueError):
            source.historical_closed_scope(day, calendar, NOW)
    with pytest.raises(ValueError, match="aware"):
        source.historical_closed_scope(HISTORICAL_DAY, calendar, datetime(2026, 10, 15, 8))
    payload=json.loads(calendar.read_text())
    payload["closed_ranges"]=["2026-10-12/2026-10-12"]
    calendar.write_text(json.dumps(payload),encoding="utf-8")
    with pytest.raises(ValueError,match="not a pinned"):
        source.historical_closed_scope(HISTORICAL_DAY,calendar,NOW)


def test_historical_mode_fixes_exact_target_full_roster_and_preserves_nullable_halt(setup,tmp_path,monkeypatch):
    calendar, _ = setup
    destination=tmp_path/'historical_job'
    roster_calls, price_calls = [], []
    def fake_probe(day,wheel,folder,**options):
        roster_calls.append(day)
        folder.mkdir(parents=True)
        raw=pd.DataFrame([['sz.000001','1','observed'],['sz.000026','0','observed'],['sh.000001','1','index']],
                         columns=['code','tradeStatus','code_name'])
        path=folder/f'query_all_stock_{day}.csv';raw.to_csv(path,index=False)
        receipt={'schema':'baostock_historical_all_stock_status_probe_v1','day':day,'response_day':day,
            'method':'query_all_stock','kwargs':{'day':day},'status':'completed_provider_status_list','quality_issues':[],
            'provider_error_code':'0','fields_returned':list(raw),'sdk':SDK,'execution_admitted':False,'price_bars_generated':False,
            'raw_csv':str(path),'raw_sha256':source.daily.digest(path),'rows_preserved':len(raw),'historical_bounded_collection_feasible':True}
        (folder/'receipt.json').write_text(json.dumps(receipt),encoding='utf-8')
        return receipt
    @contextmanager
    def session():
        def query(**arguments):
            price_calls.append(arguments)
            halted=arguments['code']=='sz.000026'
            row=[HISTORICAL_DAY,arguments['code'],'10','10','10','10','10','' if halted else '100',
                '' if halted else '1000','3','0' if halted else '1','' if halted else '0','0']
            frame=pd.DataFrame([row],columns=source.daily.FIELDS.split(','))
            return SimpleNamespace(error_code='0',error_msg='success',get_data=lambda:frame)
        yield SimpleNamespace(query_history_k_data_plus=query)
    monkeypatch.setattr(source.daily.time,'sleep',lambda _:None)
    with pytest.raises(ValueError,match='latest actually closed'):
        source.collect(HISTORICAL_DAY,calendar,SDK,destination,now=NOW,roster_probe=fake_probe,session_factory=session)
    assert roster_calls==[] and not destination.exists()
    folder,receipt=source.collect(HISTORICAL_DAY,calendar,SDK,destination,now=NOW,
        historical_research_repair=True,roster_probe=fake_probe,session_factory=session)
    assert roster_calls==[HISTORICAL_DAY] and len(price_calls)==2
    assert all(call['start_date']==call['end_date']==HISTORICAL_DAY and call['adjustflag']=='3' for call in price_calls)
    assert receipt['ordinary_symbols']==2 and receipt['status']=='complete_full_dated_close_inventory_not_admitted'
    assert receipt['nullable_suspension_rows']==1 and receipt['execution_admitted'] is False
    request=json.loads((folder/'historical_repair_manifest.json').read_text())
    assert request['day']==request['roster_requested_day']==HISTORICAL_DAY and request['source_verified'] is False
    raw,metadata=source.load_complete(folder)
    assert len(raw)==2 and metadata['plan']['acquisition_mode']==source.HISTORICAL_MODE
    assert raw.loc[raw.code.eq('sz.000026'),'volume'].item()==''
    with pytest.raises(ValueError,match='scope'):
        source.collect(HISTORICAL_DAY,calendar,SDK,folder,now=NOW,session_factory=session)


def test_later_cached_roster_cannot_substitute_historical_day(setup):
    calendar,folder=setup
    before=source.daily.digest(folder/'roster/receipt.json')
    with pytest.raises(ValueError,match='exact successful'):
        source.collect(HISTORICAL_DAY,calendar,SDK,folder,now=NOW,historical_research_repair=True,
            roster_probe=lambda *args,**kwargs:pytest.fail('No cache reacquisition'),
            session_factory=lambda:pytest.fail('No price query on wrong-day roster'))
    assert source.daily.digest(folder/'roster/receipt.json')==before
    assert not (folder/'plan.json').exists()
