"""Paired score, pre-score registration and shared Engine behavior; synthetic only."""
from copy import deepcopy
from types import SimpleNamespace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.ledger import new_state
from trading.research import leader_52week_monthly as m
from trading.research.stockdb_action_accounting import StockDBActionAccounting


A,B,C = "600001.SSE","600002.SSE","600003.SSE"
QUAL = SimpleNamespace(assess=lambda *args,**kwargs:{"engine_block_reason":None})


def panel():
    days = pd.bdate_range("2023-01-02",periods=270).strftime("%Y-%m-%d").tolist()
    values = np.exp(np.arange(270)*.003+np.sin(np.arange(270))*.0002)
    records = [{"date":day,"code":"sh."+symbol[:6],"close":float(values[i]*(1+j*.1)),
        "high":float(values[i]*(1+j*.1)),"volume":"1000000","amount":"30000000",
        "tradestatus":"1","isST":"0"} for i,day in enumerate(days) for j,symbol in enumerate((A,B,C))]
    frame = pd.DataFrame(records)
    own = {day:rows.assign(symbol=rows.code.map(m._symbol)).set_index("symbol").to_dict("index")
           for day,rows in frame.groupby("date")}
    return days,values,frame,own


def test_common_exact_window_formulas_ties_future_invariance_and_missing_not_shrunk():
    days,values,frame,own = panel()
    day = days[252]
    frame.loc[frame.code.eq("sh.600003") & frame.date.eq(days[200]),"high"] = np.nan
    # Fixed exclusions are original codes and never merged into a survivor.
    extra = frame.loc[frame.code.eq("sh.600001")].assign(code="sz.300114")
    frame = pd.concat([frame,extra],ignore_index=True)
    batches,audit = m.score_panel(frame,[C,B,A,"300114.SZSE"],days,[day],own,QUAL)
    assert batches["high52"][day]["common_eligible_symbols"] == [A,B]
    assert batches["high52"][day]["common_eligible_symbols"] == batches["momentum12_1"][day]["common_eligible_symbols"]
    assert [r["symbol"] for r in batches["high52"][day]["candidates"]] == [A,B]
    assert all(r["score"] == 1 for r in batches["high52"][day]["candidates"])
    assert audit[0]["high52_exact_score1_count"] == 2
    assert np.isclose(batches["momentum12_1"][day]["candidates"][0]["score"],values[231]/values[0]-1)
    changed = frame.copy();changed.loc[changed.date.gt(day),["close","high"]] = 999999
    assert m.score_panel(changed,[C,B,A,"300114.SZSE"],days,[day],own,QUAL) == (batches,audit)
    # Suspension anywhere in the shared253-close requirement invalidates that
    # symbol for both arms; no observed-row shrinking or zero-return filling.
    changed = frame.copy(); changed.loc[changed.code.eq("sh.600001") & changed.date.eq(days[20]),"tradestatus"]="0"
    altered,_ = m.score_panel(changed,[C,B,A],days,[day],own,QUAL)
    assert altered["high52"][day]["common_eligible_symbols"] == [B]
    assert altered["momentum12_1"][day]["common_eligible_symbols"] == [B]
    all_unknown = frame.copy();all_unknown["high"] = np.nan
    unknown,_ = m.score_panel(all_unknown,[A,B,C],days,[day],own,QUAL)
    assert all(unknown[k][day]["status"] == "incomplete" for k in m.ARMS)


def test_independent_arm_contexts_shared_engine_NC_T1_and_persistent_study_exit():
    from trading.research.recent_alpha158_panel_replay import ExactSourceRawFeed
    days=["2025-01-02","2025-01-03","2025-01-06","2025-01-07","2025-01-08","2025-01-09"]
    source=["2024-12-31"]+days
    rows=pd.DataFrame([{"date":d,"code":"sh."+s[:6],"open":"10","high":"10.1","low":"9.9",
        "close":"10","preclose":"10","volume":"1000000","amount":"30000000",
        "tradestatus":"1","isST":"0","adjustflag":"3"} for d in source for s in (A,B)])
    config=m.annual._config(100000,5)
    final=[]
    for arm in m.ARMS:
        context=RunContext(m.METHOD+":2025:"+arm,TradingMode.REPLAY)
        base=ExactSourceRawFeed(rows,source+["2025-01-10"],[A,B])
        base.instruments={s:{"ipoDate":"2000-01-01","outDate":""} for s in (A,B)}
        universe=pd.DataFrame({"symbol":[A,B],"code":["sh.600001","sh.600002"]})
        accounting=StockDBActionAccounting([],[],[],{("sh.600001",2025):True,("sh.600002",2025):True},
            universe,{"rename":None},[A,B],source,days,{"price_source":"independent_baostock_raw"})
        execution=m.HistoricalExecutionPolicy(base,accounting,days,prior_volume_policy=m.PriorVolumeReplayPolicy(context))
        batch={days[0]:{"status":"complete","reason":"monthly_same_pool_ranking","candidates":[
            {"symbol":A,"score":1.},{"symbol":B,"score":.9}]},
            days[2]:{"status":"incomplete","reason":"common_source_unknown_preserves_NO_CHANGE","candidates":[]}}
        adapter=m.PairedMonthlyFeed(execution,batch,config,days[4],days[-1],QUAL,arm)
        assert isinstance(adapter.signal_frame(days[2],config,context),SignalUnavailable)
        # The production runner uses the existing typed qualification loader;
        # this tiny execution check follows the saved AnnualFeed Engine fixture.
        feed=adapter
        state=new_state(config.initial_cash)
        for i,day in enumerate(days):
            m.advance(state,feed,day,config,i,context=context,full_exit_lifecycle=m.RISK_COMPAT_FULL_EXIT_LIFECYCLE)
            if day == days[1]:
                assert sum(l["qty"] for l in state["positions"][A])>0
                assert all(fill["available_day"]==days[2] for fill in state["fills"].values())
            if day == days[2]:
                assert sum(l["qty"] for l in state["positions"][A])>0 and state["pending_targets"] is None
        assert state["close_candidate_policy"]["id"]==m.METHOD+":"+arm
        assert state["prior_volume_execution_policy"]["context"]==context.to_dict()
        assert state["pending_targets"] is None and not state["full_exit_lifecycle"]["remaining_full_exits"]
        assert all(not sum(l["qty"] for l in lots) for lots in state["positions"].values())
        assert all(o["fees"]>0 for o in state["orders"].values())
        final.append(context.account_id)
    assert final[0]!=final[1]


def write(path,value):
    m.save(path,value)
    return m.pin(path)


def test_registration_freezes_two_years_and_both_scores_before_any_score_or_account(tmp_path,monkeypatch):
    from trading.research import low_risk,leader_momentum_benchmark,leader_momentum_capital
    from trading import engine
    monkeypatch.setattr(m,"score_panel",lambda *a,**k: (_ for _ in ()).throw(AssertionError("registration scored")))
    monkeypatch.setattr(m,"advance",lambda *a,**k: (_ for _ in ()).throw(AssertionError("registration advanced")))
    calendar=pd.bdate_range("2023-01-02","2025-01-15").strftime("%Y-%m-%d").tolist()
    rawview,adj=tmp_path/"rawview",tmp_path/"adjusted"
    rawview.mkdir();adj.mkdir()
    calendar_ref=write(rawview/"calendar.json",calendar)
    write(rawview/"plan.json",{"days":calendar,"execution_admitted":False,
        "calendar_source":calendar_ref["path"],"calendar_sha256":calendar_ref["sha256"]})
    schema=pa.schema([("date",pa.string()),("code",pa.string()),("close",pa.string()),("high",pa.string()),
        ("volume",pa.string()),("amount",pa.string()),("tradestatus",pa.string()),("isST",pa.string())])
    with pq.ParquetWriter(adj/"adjusted_daily.parquet",schema) as writer:
        for day in calendar:
            writer.write_table(pa.Table.from_pylist([{"date":day,"code":"sh.600001","close":"10","high":"10",
                "volume":"1","amount":"30000000","tradestatus":"1","isST":"0"}],schema=schema))
    write(adj/"result.json",{"status":"research_reconstruction_completed","scope":"every_pinned_provider_raw_row_no_strategy_universe_selection",
        "panel_file":"adjusted_daily.parquet","panel_sha256":m.digest(adj/"adjusted_daily.parquet"),
        "derived_research_levels":True,"execution_admitted":False,"independent_prices_verified":False,
        "cash_share_actions_verified":False,"raw_view":str(rawview),"dates":len(calendar),"cutoff":calendar[-1],
        "finished_at":"2026-10-17T12:00:00+08:00","source_hashes":{"raw":"fixture","factors":"fixture"}})
    sources=[]
    for year in m.YEARS:
        folder=tmp_path/str(year);folder.mkdir();sources.append(folder)
        files={}
        for name,data in (("cohort.json",{"scope_members":[A,"300114.SZSE","302132.SZSE"]}),
                ("batches.json",{}),("listing_witness.json",{})):
            files[name]=write(folder/name,data)
        for name in ("raw_daily.parquet","context.parquet"):
            pd.DataFrame({"date":[str(year)+"-01-02"],"code":["sh.600001"]}).to_parquet(folder/name,index=False)
            files[name]=m.pin(folder/name)
        plan={"schema":m.annual.METHOD,"spec":m.annual.SPEC,"implementation":m.pin(m.annual.__file__),
            "benchmark_implementation":m.pin(leader_momentum_benchmark.__file__),"feature_implementation":m.pin(low_risk.__file__),
            "capital_implementation":m.pin(leader_momentum_capital.__file__),"close_engine_implementation":m.pin(engine.__file__),
            "master":write(folder/"master.json",{}),"calendars":[write(folder/"calendar.json",{})],
            "action_event_view":write(folder/"events"/"result.json",{}),"action_reference_view":write(folder/"references"/"result.json",{}),
            "config":m.annual._config(100000,5).to_dict()}
        study=write(folder/"study_plan.json",plan)
        write(folder/"manifest.json",{"schema":m.annual.METHOD,"status":"completed_annual_inputs","study_plan":study,
            "files":files,"start":str(year)+"-01-02","end":str(year)+"-01-03","through":str(year)+"-01-15",
            "source_days":[day for day in calendar if str(year)+"-01-02" <= day <= str(year)+"-01-15"],
            "decision_days":[str(year)+"-01-02"],"cohort_count":3,**m.capacity.FLAGS})
    registration=m.register(sources,adj,tmp_path/"registered")
    assert registration["new_scores_computed"] is False and registration["new_accounts_created"] is False
    assert list(registration["studies"])==["2024","2025"] and registration["spec"]["arms"]==list(m.ARMS)
    assert all(v["fixed_excluded_original_members"]==list(m.FIXED_CODE_EXCLUSIONS) for v in registration["studies"].values())
    assert not (tmp_path/"registered"/"batches.json").exists()


def test_changed_external_calendar_pointer_is_observed_not_used_and_inline_footer_conflict_blocks(tmp_path):
    import pytest
    rawview,adj=tmp_path/"raw",tmp_path/"adjusted"
    rawview.mkdir();adj.mkdir()
    days=["2024-01-02","2024-01-03","2024-01-04"]
    pointer=write(rawview/"calendar.json",days)
    plan={"days":days,"execution_admitted":False,"calendar_source":pointer["path"],"calendar_sha256":pointer["sha256"]}
    write(rawview/"plan.json",plan)
    schema=pa.schema([(key,pa.string()) for key in ("date","code","close","high","volume","amount","tradestatus","isST")])
    with pq.ParquetWriter(adj/"adjusted_daily.parquet",schema) as writer:
        for day in days:
            writer.write_table(pa.Table.from_pylist([{"date":day,"code":"sh.600001","close":"10","high":"10",
                "volume":"1000","amount":"30000000","tradestatus":"1","isST":"0"}],schema=schema))
    source={"status":"research_reconstruction_completed","scope":"every_pinned_provider_raw_row_no_strategy_universe_selection",
        "panel_file":"adjusted_daily.parquet","panel_sha256":m.digest(adj/"adjusted_daily.parquet"),
        "derived_research_levels":True,"execution_admitted":False,"independent_prices_verified":False,
        "cash_share_actions_verified":False,"raw_view":str(rawview),"dates":len(days),"cutoff":days[-1],
        "finished_at":"2026-10-17T12:00:00+08:00","source_hashes":{"raw":"fixture","factors":"fixture"}}
    write(adj/"result.json",source)
    # The same external path now names a different saved calendar. Its content
    # must never replace the original inline days or be restored/edited here.
    Path(pointer["path"]).write_text(json.dumps(["2030-01-01"]),encoding="utf-8")
    pointer_before=Path(pointer["path"]).read_bytes()
    actual=m._adjusted(adj,verify_panel=True)
    assert actual["calendar"]==days
    observed=actual["calendar_pointer_observation"]
    assert observed["declared_sha256"]==pointer["sha256"] and observed["observed_sha256"]!=pointer["sha256"]
    assert observed["status"]=="changed_external_pointer_not_used" and observed["used_for_session_generation"] is False
    assert Path(pointer["path"]).read_bytes()==pointer_before
    # Inline declarations alone are insufficient: exact footer dates must agree.
    plan["days"]=["2024-01-02","2024-01-03","2024-01-05"]
    (rawview/"plan.json").write_text(json.dumps(plan),encoding="utf-8")
    source["cutoff"]="2024-01-05"
    (adj/"result.json").write_text(json.dumps(source),encoding="utf-8")
    with pytest.raises(ValueError,match="footer"):
        m._adjusted(adj,verify_panel=True)
