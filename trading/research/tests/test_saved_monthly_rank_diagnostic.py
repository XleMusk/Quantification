"""Only small synthetic price endpoints and immutable score pools."""
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading.research import saved_monthly_rank_diagnostic as m


def test_exact_calendar_endpoints_missing_halt_and_ST_do_not_shift_or_select_pool(tmp_path):
    calendar=pd.bdate_range("2024-01-02",periods=40).strftime("%Y-%m-%d").tolist()
    symbols=[f"60000{i}.SSE" for i in range(1,5)]
    panel=tmp_path/"adjusted_daily.parquet"
    schema=pa.schema([(k,pa.string()) for k in ("date","code","open","volume","tradestatus","isST")])
    with pq.ParquetWriter(panel,schema) as writer:
        for i,day in enumerate(calendar):
            rows=[]
            for j,symbol in enumerate(symbols):
                if i==1 and j==2: continue  # No substitution with the next observed date.
                inactive=i==10 and j==0
                rows.append({"date":day,"code":"sh."+symbol[:6],
                    "open":"" if inactive else "20" if i==21 and j==0 else "15" if i==21 and j==1 else "10",
                    "volume":"0" if (i==21 and j==3) or inactive else "1000",
                    "tradestatus":"0" if inactive else "1","isST":"1" if i==21 and j==1 else "0"})
            writer.write_table(pa.Table.from_pylist(rows,schema=schema))
    plans=m.label_plan([calendar[0]],calendar)
    opening,observed,needed=m.endpoint_panel({"calendar":calendar,"panel":{"path":str(panel)}},plans,symbols)
    assert needed==[calendar[1],calendar[21]] and len(opening)==40
    labels=m.factor_families.forward_open_labels(opening,20)
    assert labels.at[calendar[0],symbols[0]]==1.0  # Mid-interval halt not read or used to move dates.
    assert labels.at[calendar[0],symbols[1]]==.5   # Future ST does not remove original D member.
    assert np.isnan(labels.at[calendar[0],symbols[2]]) and np.isnan(labels.at[calendar[0],symbols[3]])
    assert observed[(calendar[21],symbols[3])]["reason"]=="endpoint_volume_nonpositive_or_unknown"


def score_frame(day):
    symbols=[f"{i:06}.SZSE" for i in range(1,201)]
    index=pd.MultiIndex.from_tuples([(day,s) for s in symbols],names=["date","symbol"])
    high=np.r_[np.ones(40),np.linspace(.99,.1,160)]
    return pd.DataFrame({"high52":high,"momentum12_1":np.linspace(.1,1,200)},index=index)


def test_future_label_missing_keeps_full_pool_quintile_membership_and_score1_ties():
    calendar=pd.bdate_range("2024-01-02",periods=100).strftime("%Y-%m-%d").tolist()
    day=calendar[21]
    frame=score_frame(day)
    labels=pd.Series(np.arange(200,dtype=float)/100,index=frame.index)
    labels.iloc[:40]=np.nan
    plans=m.label_plan([day],calendar)
    daily,groups=m.statistics(frame,labels,plans)
    high=next(r for r in groups if r["arm"]=="high52")
    assert high["pool_members"]==200 and high["pairs"]==160 and high["missing_labels"]==40
    assert high["q5_members"]==40 and high["q5_observed_labels"]==0 and high["q5_missing_labels"]==40
    assert high["maximum_score_tie_count"]==high["score1_count"]==40
    assert np.isnan(next(r for r in daily if r["arm"]=="high52")["q5_minus_q1"])
    filled=labels.fillna(.5)
    _,complete=m.statistics(frame,filled,plans)
    original=next(r for r in complete if r["arm"]=="high52")
    assert high["q5_member_symbols"]==original["q5_member_symbols"]


def test_paired_delta_only_same_valid_D_and_nonoverlap_anchors_ignore_label_missingness():
    calendar=pd.bdate_range("2024-01-02",periods=100).strftime("%Y-%m-%d").tolist()
    days=[calendar[0],calendar[20],calendar[42]]
    plans=m.label_plan(days,calendar)
    assert [d for d,p in plans.items() if p["nonoverlap_anchor"]]==[days[0],days[2]]
    frames=[];outcomes=[]
    for i,day in enumerate(days):
        frame=score_frame(day)
        frame["high52"]=1 if i==1 else np.arange(200,dtype=float)+1
        frame["momentum12_1"]=np.arange(200,dtype=float)[::-1]+1
        frames.append(frame)
        outcomes.append(pd.Series(np.nan if i==0 else np.arange(200,dtype=float),index=frame.index))
    features=pd.concat(frames);labels=pd.concat(outcomes)
    daily,_=m.statistics(features,labels,plans)
    summary=m.summaries(daily,plans)
    paired=next(r for r in summary if r["scope"]=="all" and r["subset"]=="all_registered_months"
        and r["arm"]=="paired_high52_minus_momentum12_1")
    assert paired["same_D_both_valid_dates"]==[days[2]] and paired["paired_valid_months"]==1
    assert np.isclose(paired["mean_delta_IC"],2.)
    selected=next(r for r in summary if r["scope"]=="all" and r["subset"]=="fixed_nonoverlap_anchors"
        and r["arm"]=="high52")
    assert selected["registered_dates"]==[days[0],days[2]] and selected["valid_IC_months"]==1
