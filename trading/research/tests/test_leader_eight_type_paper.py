"""Synthetic publication semantics, without a real source publication or account."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import leader_eight_type_paper as m
from trading.research.ipo_qualification import IpoQualification

CALENDAR = {"schema":"cn_a_share_exchange_calendar_v1","year":2026,"closed_ranges":["2026-10-01/2026-10-07"],"open_sessions":[]}
SESSIONS,_,_=m._calendar_view(CALENDAR,"2026-01-01","2026-12-31",[])
NOW=datetime.fromisoformat("2026-10-15T18:00:00+08:00")


def inputs(days=None, count=7):
    days=days or ["2026-10-13","2026-10-14","2026-10-15"]
    symbols=[f"{i:06d}.SZSE" for i in range(1,count+1)]
    raw=pd.DataFrame([{"date":day,"code":"sz."+symbol[:6],"open":"10","high":"10.1","low":"9.9","close":"10","preclose":"10","volume":"1000000","amount":"10000000","pctChg":"0","tradestatus":"1","isST":"0","adjustflag":"3"} for day in days for symbol in symbols])
    panels={day:{"day":day,"available_at":day+"T17:00:00+08:00","environment":{"source_complete":True,"breadth":.5,"median_return_pct":1.},"types":[{"id":identity,"status":"matched","candidates":[{"symbol":symbol,"score":100-i,"date":day,"source_day":day} for i,symbol in enumerate(symbols)],"unresolved_symbols":[]} for identity in m.TYPE_IDS]} for day in days}
    qualification=IpoQualification({symbol:{"ipo_date":"1999-01-01","available_at":"2026-10-08T10:00:00+08:00","board":"main","source_basis":"synthetic"} for symbol in symbols},tuple(SESSIONS),{"synthetic":True})
    return {"raw":raw,"panel_by_day":panels,"source_days":days,"calendar_payload":CALENDAR,"scope_members":symbols,"qualification":qualification,"context_anchor":{"day":"2026-10-12","closes":{s:10. for s in symbols}},"provenance":{}}


def plan(loaded, observed=NOW, recorded=None):
    day,execution,env,prepared=m._prepare(loaded,observed)
    return m._finalize(day,execution,env,prepared,{"input_recorded_at":loaded["panel_by_day"][day]["available_at"],"synthetic":True},recorded or observed)


def case(result, identity="relay_continuation_leader", guard="unguarded",cap="uncapped"):
    return next(row for row in result["cases"] if row["case_id"]==f"{identity}__{guard}__{cap}")


def test_latest_D_fresh_weights_need_no_successor_raw_or_account():
    result=plan(inputs()); row=case(result)
    assert result["case_count"]==32 and result["execution_day_bound"]=="2026-10-16"
    assert row["decision"]=="FULL_TARGET" and len(row["target_weights"])==5
    assert set(row["target_weights"].values())=={.16} and row["target_cash_weight"]==pytest.approx(.2)
    assert all(row["portfolio_intent"]["context"] is None for row in result["cases"])
    assert row["portfolio_intent"]["execution_not_before"]=="2026-10-16"
    assert result["account_created"] is result["engine_advanced"] is result["orders_generated"] is False
    assert result["successor_raw_required"] is result["successor_raw_used"] is False
    assert result["risk_policy_id"]==m.POLICY_ID and row["held_risk_assessment_pending"] is True
    assert row["candidate_reference_and_setup"][0]["reference_close_cny"]==10
    assert row["candidate_reference_and_setup"][0]["reference_is_order_price"] is False


@pytest.mark.parametrize("days,clock,execution",[
    (["2026-10-12","2026-10-13","2026-10-14"],NOW,"2026-10-15"),
    (None,datetime.fromisoformat("2026-10-16T08:00:00+08:00"),"2026-10-16")])
def test_late_or_after_midnight_is_context_only_without_rolling_or_backdating(days,clock,execution):
    result=plan(inputs(days),observed=clock)
    assert result["status"]=="late_context_only" and result["proposed_execution_day"]==execution
    assert result["execution_day_bound"] is None and result["execution_day_rolled_forward"] is False
    assert all(row["portfolio_intent"]["execution_not_before"] is None for row in result["cases"])
    assert result["recorded_at"]==clock.isoformat()


def test_wholecase_unknown_is_NO_CHANGE_not_cash_and_frozen_ranks_not_refilled():
    loaded=inputs(); day=loaded["source_days"][-1]
    for item in loaded["panel_by_day"][day]["types"]:item["unresolved_symbols"]=[loaded["scope_members"][-1]]
    row=case(plan(loaded))
    assert row["decision"]=="NO_CHANGE" and row["target_weights"] is row["target_cash_weight"] is None
    assert row["portfolio_intent"]["execution_not_before"]=="2026-10-16"
    assert row["portfolio_intent"]["weights"]=={} and row["portfolio_intent"]["context"] is None
    assert len(row["original_top5"])==5 and len(row["raw_weights_before_capacity_guard_and_qualification"])==5
    assert "whole_type_membership_unknown" in row["reason"] and row["refill_performed"] is False


def test_trusted_exit_precedes_entry_unknown_without_assuming_empty_holdings():
    loaded=inputs(); day=loaded["source_days"][-1]
    loaded["panel_by_day"][day]["environment"].update(breadth=-.5,median_return_pct=-1.)
    for item in loaded["panel_by_day"][day]["types"]:item.update(status="unknown",unresolved_symbols=list(loaded["scope_members"]))
    loaded["qualification"].records.clear()
    result=plan(loaded); row=case(result,guard="two_close_envguard")
    assert row["decision"]=="EXIT_ALL" and row["target_weights"]=={} and row["target_cash_weight"]==1
    assert row["portfolio_intent"]["kind"]=="FULL_TARGET" and row["entry_unknown_checks_bypassed_for_independent_exit"] is True
    assert row["account_assumed_empty"] is False and row["holdings_binding_pending"] is True
    assert case(result)["decision"]=="NO_CHANGE"


@pytest.mark.parametrize("fault",["IPO","reference","raw","empty","missing_membership"])
def test_current_evidence_failure_preserves_NO_CHANGE_without_replacing_top5(fault):
    loaded=inputs();day=loaded["source_days"][-1];symbol=loaded["scope_members"][0]
    if fault=="IPO":loaded["qualification"].records.pop(symbol)
    if fault=="reference":loaded["raw"].loc[loaded["raw"].date.eq(day)&loaded["raw"].code.eq("sz."+symbol[:6]),"preclose"]="9"
    if fault=="raw":loaded["raw"].loc[loaded["raw"].date.eq(day)&loaded["raw"].code.eq("sz."+symbol[:6]),"close"]=""
    if fault in ("empty","missing_membership"):
        for item in loaded["panel_by_day"][day]["types"]:
            if fault=="empty":item.update(candidates=[],status="not_matched")
            else:item.pop("unresolved_symbols")
    row=case(plan(loaded))
    assert row["decision"]=="NO_CHANGE" and row["target_weights"] is None and row["refill_performed"] is False
    if fault not in ("empty",):assert len(row["original_top5"])==5


def test_capacity_is_frozen_first_seen_before_guard_and_does_not_renormalize():
    days=["2026-10-08","2026-10-09","2026-10-12","2026-10-13","2026-10-14"]
    loaded=inputs(days,count=25)
    for i,day in enumerate(days):
        for item in loaded["panel_by_day"][day]["types"]:item["candidates"]=item["candidates"][i*5:i*5+5]
    result=plan(loaded,observed=datetime.fromisoformat("2026-10-14T18:00:00+08:00"))
    bounded=case(result,cap="causal_first_seen_cap20");unbounded=case(result)
    assert bounded["decision"]=="NO_CHANGE" and bounded["cap_dropped_rows"]==5
    assert unbounded["decision"]=="FULL_TARGET" and set(unbounded["target_weights"].values())=={.16}
    assert bounded["original_top5"]==unbounded["original_top5"]


def test_old_wrong_structural_field_is_unknown_not_zero_or_fictional_held_stop():
    loaded=inputs();day=loaded["source_days"][-1]
    loaded["panel_by_day"][day]["risk_features"]={symbol:{"sma_20":9.} for symbol in loaded["scope_members"]}
    row=case(plan(loaded),identity="trend_leader")
    assert row["held_risk_assessment_pending"] is True
    assert all(value["sma20_observed"] is None for value in row["candidate_reference_and_setup"])


def test_public_writer_creates_only_plan_files_no_replay_state_or_order(tmp_path,monkeypatch):
    loaded=inputs();panel=tmp_path/"input.json";panel.write_text('{"synthetic":true}')
    loaded["provenance"]["panel_sha256"]=m.digest(panel)
    monkeypatch.setattr(m,"load_inputs",lambda *args:loaded)
    monkeypatch.setattr(m,"_source_bindings",lambda *_:{"input_recorded_at":"2026-10-15T17:00:00+08:00","synthetic":True})
    monkeypatch.setattr(m,"_now",lambda:NOW)
    def forbidden(*args,**kwargs):raise AssertionError("No account/replay/Engine allowed")
    from trading import engine,ledger
    from trading.research import leader_eight_type_panel_replay
    monkeypatch.setattr(engine,"advance",forbidden);monkeypatch.setattr(ledger,"new_state",forbidden)
    monkeypatch.setattr(leader_eight_type_panel_replay,"replay_eight_types",forbidden)
    result=m.publish(panel,None,None,tmp_path/"new")
    assert {p.name for p in (tmp_path/"new").iterdir()}=={"paper_plan.json","PLAN.md"}
    assert json.loads((tmp_path/"new/paper_plan.json").read_text(encoding="utf8"))["case_count"]==32
    assert all(row["portfolio_intent"]["context"] is None for row in result["cases"])
    with pytest.raises(FileExistsError):m.publish(panel,None,None,tmp_path/"new")


def test_named_protocol_UTC_and_iso_clocks_include_latest_structural_registration():
    record={"recorded_at_utc":"2026-10-15 09:21:09 UTC",
            "pre_run_clarification":{"recorded_at_utc":"2026-10-15 09:25:01 UTC"},
            "risk_rule_fixed_before_new_actual_run":{"recorded_at_utc":"2026-10-15 09:35:59 UTC"}}
    clocks=m._protocol_clocks(record,NOW)
    assert clocks["risk_rule_fixed_before_new_actual_run.recorded_at_utc"]=="2026-10-15T09:35:59+00:00"
    with pytest.raises(ValueError):m._protocol_clocks(record,datetime.fromisoformat("2026-10-15T17:35:58+08:00"))
    record["recorded_at_utc"]="2026-10-15T17:21:09+08:00"
    assert m._protocol_clocks(record,NOW)["recorded_at_utc"]=="2026-10-15T17:21:09+08:00"
    record["risk_rule_fixed_before_new_actual_run"]["recorded_at_utc"]=None
    with pytest.raises(ValueError):m._protocol_clocks(record,NOW)
