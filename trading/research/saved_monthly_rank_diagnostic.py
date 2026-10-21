"""Diagnose saved common-pool monthly rankings, separately from account PnL.

Scores and their original decision pools are immutable inputs. Only declared
adjusted next-open20 outcome endpoints are read after a new registration.
"""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import factor_families, factors
from .baostock_daily_slice import digest

METHOD = "saved_monthly_common_pool_adjusted_open20_rank_diagnostic_v1"
REGISTRATION_SCHEMA = METHOD + "_registration"
SOURCE_SCORE_SCHEMA = "adjusted_52week_high_vs_matched_12_1_monthly_positive_pool_v1_scores"
ARMS = ("high52", "momentum12_1")
YEARS = (2024, 2025)
HORIZON, MIN_PAIRS = 20, 100
FLAGS = {"research_only":True,"candidate_panel_only":True,"execution_admitted":False,
    "selection_enabled":False,"strategy_validated":False,"returns_reportable":False,
    "account_backtest":False,"model_refit":False,"scores_recomputed":False,
    "signal_generated":False,"orders_generated":False,"ledger_mutated":False,
    "provider_contacted":False,"network_accessed":False,"tradeability_evaluated":False}
SPEC = {"id":METHOD,"years":list(YEARS),"arms":list(ARMS),"min_pairs":MIN_PAIRS,
    "input":"all_symbols_of_saved_both_arm_complete_D_common_pool_not_top5_not_future_label_survivors",
    "label":"adjusted_open[D+21]/adjusted_open[D+1]-1_exact_full_inline_calendar",
    "horizon":HORIZON,"calendar":"unchanged_parent_raw_plan_inline_days_not_current_external_calendar_pointer",
    "endpoint_unknown":"missing_row_inactive_nonpositive_or_nonfinite_volume_or_adjusted_open_remains_NaN_no_date_compression",
    "future_ST":"does_not_remove_D_membership","intermediate_halts":"do_not_change_scheduled_endpoint_dates",
    "ranking":"saved_scores_unchanged_average_ties","IC":"existing_factors.daily_ic_pairwise_finite_Spearman_min100",
    "groups":"existing_rank_group_means_full_D_score_average_pct_rank_Q1_le0.2_Q5_gt0.8_before_label_filter",
    "missing_labels":"reported_by_full_D_pool_and_each_original_group_no_imputation_or_rebinning",
    "ties":"preserve_average_ranks_no_qcut_no_arbitrary_split_report_max_score_and_score1_ties",
    "summary":"date_equal_mean_IC_positive_valid_IC_fraction_Q5_minus_Q1_price_spread_annual_and_all",
    "paired_delta":"high52_IC_minus_momentum12_1_IC_only_same_D_both_finite",
    "nonoverlap":"greedy_earliest_registered_D_keep_if_E_strictly_after_previous_kept_H_fixed_before_label_read",
    "nonoverlap_claim":"descriptive_subset_not_independent_replications",
    "inference":"no_HAC_t_p_value_annualization_or_strategy_return_with22_months",
    "price_return_only":True,"raw_limit_price_proxy":False,"fees_or_fills_evaluated":False,
    "independent_holdout_claimed":False,"future_labels_used_for_score":False}


def now():
    return datetime.now().astimezone().isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def pin(path):
    return {"path":str(Path(path).resolve()),"sha256":digest(path)}


def check(ref):
    path = Path(ref.get("path") or ref["file"]).resolve()
    actual = pin(path)
    if actual["sha256"] != ref["sha256"]:
        raise ValueError("Saved diagnostic input changed: " + str(path))
    return path


def clean(value):
    if isinstance(value,dict): return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v) for v in value]
    if isinstance(value,(np.integer,np.bool_)): return value.item()
    if isinstance(value,(float,np.floating)): return float(value) if np.isfinite(value) else None
    return value


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x",encoding="utf-8") as handle:
        json.dump(clean(value),handle,ensure_ascii=False,indent=2,allow_nan=False)


def output_directory(out,protected):
    out=Path(out).resolve()
    if out.exists(): raise FileExistsError("Use a new independent diagnostic output directory")
    if any(out==Path(p).resolve() or out in Path(p).resolve().parents or Path(p).resolve() in out.parents for p in protected):
        raise ValueError("Diagnostic output must not overlap saved source or registration directories")
    return out


def label_plan(days,calendar):
    if calendar != sorted(set(calendar)) or any(date.fromisoformat(d).isoformat()!=d for d in calendar):
        raise ValueError("Exact unique chronological inline source calendar required")
    if days != sorted(set(days)) or any(d not in calendar for d in days):
        raise ValueError("Distinct registered score dates on the original calendar required")
    result,last_exit={},None
    for day in days:
        i=calendar.index(day)
        entry=calendar[i+1] if i+1<len(calendar) else None
        exit_day=calendar[i+HORIZON+1] if i+HORIZON+1<len(calendar) else None
        selected=entry is not None and exit_day is not None and (last_exit is None or entry>last_exit)
        result[day]={"source_day":day,"entry_day":entry,"exit_day":exit_day,
            "nonoverlap_anchor":selected,"anchor_basis":"registered_calendar_only_before_label_availability",
            "anchor_reason":"kept_earliest_nonoverlap_interval" if selected else
                "calendar_endpoint_unknown" if entry is None or exit_day is None else "entry_not_strictly_after_previous_kept_exit"}
        if selected: last_exit=exit_day
    return result


def _pool(batches,audit,decision_days):
    if list(batches)!=list(ARMS) or any(list(batches[a])!=decision_days for a in ARMS):
        raise ValueError("Saved paired scores must retain exact two-arm full month-end prefix")
    if [r["day"] for r in audit]!=decision_days:
        raise ValueError("Saved pool audit date prefix changed")
    rows,coverage=[],[]
    for day,audited in zip(decision_days,audit):
        paired=[batches[a][day] for a in ARMS]
        a,b=paired
        if any(a[k]!=b[k] for k in ("status","reason","common_eligible_symbols","eligible_count")):
            raise ValueError("Saved arms do not share the same original complete D pool")
        if audited["status"]!=a["status"] or audited["common_eligible_symbols"]!=a["common_eligible_symbols"]:
            raise ValueError("Saved source pool audit and score batches disagree")
        members=a["common_eligible_symbols"]
        if len(members)!=len(set(members)) or len(members)!=a["eligible_count"]:
            raise ValueError("Saved common pool identities/count changed")
        coverage.append({"date":day,"saved_batch_status":a["status"],"saved_reason":a["reason"],
            "saved_eligible_count":len(members),"diagnostic_pool_count":len(members) if a["status"]=="complete" else None,
            "diagnostic_eligible":a["status"]=="complete","original_pool_was_not_reselected":True})
        if a["status"]!="complete": continue
        keyed=[]
        for arm,batch in zip(ARMS,paired):
            candidates=batch["candidates"]
            identities=[r["symbol"] for r in candidates]
            if len(identities)!=len(set(identities)) or sorted(identities)!=sorted(members):
                raise ValueError("Diagnostic input must contain every saved D member exactly once, not top5")
            column={}
            for row in candidates:
                value=row["score"]
                if type(value) not in (int,float) or not np.isfinite(value):
                    raise ValueError("Saved common-pool score must be finite")
                expected=row["high52"] if arm=="high52" else row["momentum"]
                if value!=expected: raise ValueError("Saved arm score no longer matches its frozen per-symbol value")
                column[row["symbol"]]=row
            keyed.append(column)
        for symbol in members:
            x,y=keyed[0][symbol],keyed[1][symbol]
            if any(x[k]!=y[k] for k in ("high52","momentum","volatility","median_amount")):
                raise ValueError("The paired symbol's saved common features changed across score arms")
            rows.append({"date":day,"symbol":symbol,"high52":x["score"],"momentum12_1":y["score"]})
    frame=pd.DataFrame(rows,columns=["date","symbol",*ARMS]).set_index(["date","symbol"])
    if not frame.index.is_unique: raise ValueError("Duplicate original saved date/security key")
    return frame,coverage


def _score_source(directory):
    directory=Path(directory).resolve();result=read(directory/"result.json")
    if (result.get("schema")!=SOURCE_SCORE_SCHEMA or result.get("status")!="completed_paired_monthly_scores_only"
            or result.get("year") not in YEARS or any(result.get(k) is not False for k in
                ("execution_admitted","selection_enabled","strategy_validated","model_refit"))):
        raise ValueError("Existing completed unchanged2024/2025 paired score sidecars required")
    refs={"result":pin(directory/"result.json")}
    for key in ("batches","pool_audit","registration"):
        refs[key]=pin(check(result[key]))
    parent=read(refs["registration"]["path"])
    declared=parent["adjusted_source"]
    adjusted_result=read(check(declared["result"]))
    raw_plan=read(check(declared["raw_plan"]))
    if (declared["calendar"]!=raw_plan["days"] or declared["calendar"]!=sorted(set(declared["calendar"]))
            or adjusted_result.get("status")!="research_reconstruction_completed"
            or adjusted_result.get("panel_file")!="adjusted_daily.parquet"
            or adjusted_result["panel_sha256"]!=declared["panel"]["sha256"]
            or Path(declared["panel"]["path"]).resolve()!=Path(declared["directory"]).resolve()/"adjusted_daily.parquet"):
        raise ValueError("Original declared adjusted source and inline calendar changed")
    year=result["year"];study=parent["studies"][str(year)]
    if result["study"]!=study:
        raise ValueError("Saved score study and its original registration differ")
    features,coverage=_pool(read(refs["batches"]["path"]),read(refs["pool_audit"]["path"]),study["decision_days"])
    return {"year":year,"directory":str(directory),"refs":refs,"decision_days":study["decision_days"],
        "input_pool_rows":len(features),"coverage":coverage},declared,features


def register(scores,out):
    """Bind saved ranks and the outcome rule before reading any future endpoint."""
    entries,source,dates={},None,[]
    for directory in scores:
        entry,declared,_=_score_source(directory)
        if str(entry["year"]) in entries: raise ValueError("Each year can be registered only once")
        if source is not None and declared!=source:
            raise ValueError("Both years must bind the same original adjusted source and calendar")
        entries[str(entry["year"])]=entry;source=declared;dates.extend(entry["decision_days"])
    if sorted(map(int,entries))!=list(YEARS):
        raise ValueError("Both exact2024 and2025 score batches must be registered together")
    if len({v["refs"]["registration"]["sha256"] for v in entries.values()})!=1:
        raise ValueError("Both saved years must retain the same original paired-method registration")
    folder=output_directory(out,[*[v["directory"] for v in entries.values()],source["directory"],
        Path(next(iter(entries.values()))["refs"]["registration"]["path"]).parent])
    plan=label_plan(sorted(dates),source["calendar"])
    record={"schema":REGISTRATION_SCHEMA,"status":"registered_before_endpoint_labels",
        "registered_at":now(),"spec":deepcopy(SPEC),"implementation":pin(__file__),
        "reused_function_sources":[pin(factors.__file__),pin(factor_families.__file__)],
        "scores":entries,"adjusted_source_declaration":source,"label_plan":plan,
        "large_adjusted_panel_read_or_rehashed":False,"endpoint_labels_read":False,**FLAGS}
    folder.mkdir(parents=True,exist_ok=False);save(folder/"registration.json",record)
    return record


def _load(registration):
    registration=Path(registration).resolve();record=read(registration)
    if (record.get("schema")!=REGISTRATION_SCHEMA or record.get("spec")!=SPEC
            or record.get("implementation")!=pin(__file__)
            or sorted(map(int,record.get("scores",{})))!=list(YEARS)
            or any(record.get(k) is not v for k,v in FLAGS.items())):
        raise ValueError("Exact registered saved-pool diagnostic method required")
    for ref in record["reused_function_sources"]: check(ref)
    features,coverage=[],[]
    for year in YEARS:
        expected=record["scores"][str(year)]
        entry,source,part=_score_source(expected["directory"])
        if entry!=expected or source!=record["adjusted_source_declaration"]:
            raise ValueError("Saved pool, source or original publication changed after registration")
        features.append(part);coverage.extend(entry["coverage"])
    if label_plan(sorted(record["label_plan"]),record["adjusted_source_declaration"]["calendar"])!=record["label_plan"]:
        raise ValueError("Fixed label or nonoverlap calendar anchors changed")
    return record,pd.concat(features).sort_index(),coverage


def _provider_code(symbol):
    if not isinstance(symbol,str) or len(symbol)!=11 or symbol[6:] not in (".SSE",".SZSE"):
        # SSE identifiers contain ten characters, SZSE identifiers eleven.
        if not isinstance(symbol,str) or not (len(symbol)==10 and symbol[6:]==".SSE"):
            raise ValueError("Exact original saved SH/SZ stock identity required")
    if not symbol[:6].isdigit(): raise ValueError("Malformed saved original stock code")
    return ("sh." if symbol.endswith(".SSE") else "sz.")+symbol[:6]


def endpoint_panel(source,plans,symbols):
    """Only scheduled E/H rowgroups; all other exact calendar rows remain NaN."""
    calendar=source["calendar"]
    opening=pd.DataFrame(np.nan,index=calendar,columns=symbols,dtype=float)
    needed=sorted({p[k] for p in plans.values() for k in ("entry_day","exit_day") if p[k] is not None})
    codes={_provider_code(s):s for s in symbols}
    parquet=pq.ParquetFile(source["panel"]["path"])
    names=parquet.schema_arrow.names
    if not {"date","code","open","volume","tradestatus"}<=set(names) or parquet.metadata.num_row_groups!=len(calendar):
        raise ValueError("Original adjusted endpoint fields and full rowgroup calendar required")
    observations={}
    for day in needed:
        group=calendar.index(day)
        stats=parquet.metadata.row_group(group).column(names.index("date")).statistics
        if stats is None or stats.min!=day or stats.max!=day or stats.null_count!=0:
            raise ValueError("Scheduled endpoint rowgroup no longer matches exact source calendar")
        frame=parquet.read_row_group(group,columns=["date","code","open","volume","tradestatus"]).to_pandas()
        if not frame.date.eq(day).all(): raise ValueError("Wrong dated adjusted endpoint rows")
        frame=frame.loc[frame.code.isin(codes)].copy()
        if frame.code.duplicated().any(): raise ValueError("Duplicate exact original-code endpoint")
        frame["open_numeric"]=pd.to_numeric(frame.open,errors="coerce")
        frame["volume_numeric"]=pd.to_numeric(frame.volume,errors="coerce")
        for row in frame.to_dict("records"):
            symbol=codes[row["code"]]
            reason=("endpoint_inactive_or_status_unknown" if row["tradestatus"]!="1" else
                "endpoint_volume_nonpositive_or_unknown" if not np.isfinite(row["volume_numeric"]) or row["volume_numeric"]<=0 else
                "endpoint_open_nonpositive_or_unknown" if not np.isfinite(row["open_numeric"]) or row["open_numeric"]<=0 else None)
            observations[(day,symbol)]={"reason":reason,"adjusted_open":float(row["open_numeric"]) if reason is None else None}
            if reason is None: opening.at[day,symbol]=row["open_numeric"]
    return opening,observations,needed


def statistics(features,label,plans):
    """Original full-pool memberships survive endpoint attrition and ties."""
    ic=factors.daily_ic(features,label,min_pairs=MIN_PAIRS)
    shared=factor_families.rank_group_means(features,label,min_pairs=MIN_PAIRS)
    indexed={(str(r["date"]),r["factor"]):r for r in shared.to_dict("records")}
    daily,groups=[],[]
    for day,frame in features.groupby(level="date",sort=True):
        y=label.reindex(frame.index)
        ranks=frame.rank(pct=True)
        for arm in ARMS:
            series=frame[arm];member_mask=np.isfinite(series)
            pair_mask=member_mask & np.isfinite(y)
            masks={"q1":member_mask & ranks[arm].le(.2),"q5":member_mask & ranks[arm].gt(.8)}
            row=indexed[(str(day),arm)]
            group={"date":str(day),"arm":arm,"pool_members":int(member_mask.sum()),
                "pairs":int(pair_mask.sum()),"missing_labels":int((member_mask & ~np.isfinite(y)).sum()),
                "unique_scores":int(series.nunique()),"maximum_score":float(series.max()) if len(series) else None,
                "maximum_score_tie_count":int(series.eq(series.max()).sum()),"score1_count":int(series.eq(1).sum()),
                "membership_basis":"full_saved_D_pool_average_pct_rank_before_future_labels"}
            for name,mask in masks.items():
                group[name+"_members"]=int(mask.sum())
                group[name+"_observed_labels"]=int((mask & np.isfinite(y)).sum())
                group[name+"_missing_labels"]=int((mask & ~np.isfinite(y)).sum())
                group[name+"_mean_observed_label"]=y.loc[mask].mean()
                group[name+"_member_symbols"]=frame.index.get_level_values("symbol")[mask].tolist()
            groups.append(group)
            daily.append({"date":str(day),"year":int(str(day)[:4]),"arm":arm,
                "rank_ic":ic.at[day,arm],"pool_members":group["pool_members"],"pairs":group["pairs"],
                "missing_labels":group["missing_labels"],"q1_excess":row["q1_excess"],"q5_excess":row["q5_excess"],
                "q5_minus_q1":row["q5_excess"]-row["q1_excess"],
                "nonoverlap_anchor":plans[str(day)]["nonoverlap_anchor"]})
    return daily,groups


def summaries(daily,plans):
    frame=pd.DataFrame(daily)
    result=[]
    dates=sorted(plans)
    scopes={str(year):[d for d in dates if d[:4]==str(year)] for year in YEARS}
    scopes["all"]=dates
    for scope,scope_dates in scopes.items():
        for subset in ("all_registered_months","fixed_nonoverlap_anchors"):
            retained=[d for d in scope_dates if subset=="all_registered_months" or plans[d]["nonoverlap_anchor"]]
            part=frame.loc[frame.date.isin(retained)] if not frame.empty else frame
            for arm in ARMS:
                rows=part.loc[part.arm.eq(arm)] if not part.empty else part
                values=rows.rank_ic.dropna() if not rows.empty else pd.Series(dtype=float)
                spreads=rows.q5_minus_q1.dropna() if not rows.empty else pd.Series(dtype=float)
                result.append({"scope":scope,"subset":subset,"arm":arm,"registered_dates":retained,
                    "registered_month_count":len(retained),"observed_score_months":len(rows),"valid_IC_months":len(values),
                    "mean_rank_IC":values.mean(),"positive_IC_fraction":values.gt(0).mean() if len(values) else None,
                    "valid_group_spread_months":len(spreads),"mean_Q5_minus_Q1_price_spread":spreads.mean(),
                    "independent_replications_claimed":False,"strategy_return":None})
            paired=part.pivot(index="date",columns="arm",values="rank_ic") if not part.empty else pd.DataFrame(columns=ARMS)
            paired=paired.reindex(columns=ARMS).dropna(subset=list(ARMS))
            delta=paired.high52-paired.momentum12_1
            result.append({"scope":scope,"subset":subset,"arm":"paired_high52_minus_momentum12_1",
                "registered_dates":retained,"same_D_both_valid_dates":paired.index.tolist(),
                "paired_valid_months":len(delta),"mean_delta_IC":delta.mean(),
                "positive_delta_IC_fraction":delta.gt(0).mean() if len(delta) else None,
                "same_D_delta_IC":[{"date":str(d),"delta_IC":float(v)} for d,v in delta.items()],
                "independent_replications_claimed":False,"strategy_return":None})
    return result


def run(registration,out):
    record,features,coverage=_load(registration)
    source=record["adjusted_source_declaration"]
    folder=output_directory(out,[Path(registration).resolve().parent,source["directory"],
        *[item["directory"] for item in record["scores"].values()]])
    # This is the sole large-panel hash in a run; no ancestor/factor/raw scans.
    check(source["panel"])
    symbols=sorted(features.index.get_level_values("symbol").unique())
    opening,observations,needed=endpoint_panel(source,record["label_plan"],symbols)
    wide=factor_families.forward_open_labels(opening,HORIZON)
    labels,rows=[],[]
    for (day,symbol),score in features.iterrows():
        plan=record["label_plan"][str(day)]
        value=wide.at[day,symbol]
        endpoints={}
        for name,key in (("entry","entry_day"),("exit","exit_day")):
            endpoint=observations.get((plan[key],symbol))
            endpoints[name+"_reason"]=("calendar_endpoint_unknown" if plan[key] is None else
                endpoint["reason"] if endpoint is not None else "endpoint_original_code_row_missing")
        rows.append({"date":day,"symbol":symbol,**score.to_dict(),"entry_day":plan["entry_day"],"exit_day":plan["exit_day"],
            "label_open20":value,**endpoints,"nonoverlap_anchor":plan["nonoverlap_anchor"]})
        labels.append(value)
    label=pd.Series(labels,index=features.index,dtype=float)
    daily,groups=statistics(features,label,record["label_plan"])
    seen={(r["date"],r["arm"]) for r in daily}
    for item in coverage:
        for arm in ARMS:
            if (item["date"],arm) not in seen:
                daily.append({"date":item["date"],"year":int(item["date"][:4]),"arm":arm,"rank_ic":np.nan,
                    "pool_members":item["diagnostic_pool_count"],"pairs":None,"missing_labels":None,
                    "q1_excess":np.nan,"q5_excess":np.nan,"q5_minus_q1":np.nan,
                    "nonoverlap_anchor":record["label_plan"][item["date"]]["nonoverlap_anchor"],
                    "reason":"saved_incomplete_or_empty_pool_no_valid_IC"})
    daily.sort(key=lambda r:(r["date"],ARMS.index(r["arm"])))
    for item in coverage:
        part=[r for r in rows if r["date"]==item["date"]]
        item.update(label_rows=len(part),observed_labels=sum(np.isfinite(r["label_open20"]) for r in part),
            endpoint_missing_counts=dict(Counter(r[k] for r in part for k in ("entry_reason","exit_reason") if r[k] is not None)),
            **record["label_plan"][item["date"]])
    summary=summaries(daily,record["label_plan"])
    folder.mkdir(parents=True,exist_ok=False)
    pd.DataFrame(rows).to_parquet(folder/"labels.parquet",index=False)
    for name,value in (("coverage",coverage),("IC",daily),("groups",groups),("summary",summary)):
        save(folder/(name+".json"),value)
    result={"schema":METHOD,"status":"completed_saved_pool_rank_diagnostic","created_at":now(),
        "registration":pin(registration),"spec":deepcopy(SPEC),"source_clock_basis":"actual_saved_2026_snapshot_not_historical_publication",
        "large_panel_fullhash_count":1,"adjusted_rowgroups_read":len(needed),"endpoint_days_read":needed,
        "registered_months":len(record["label_plan"]),"label_rows":len(rows),
        "nonoverlap_retained_dates":[d for d,p in record["label_plan"].items() if p["nonoverlap_anchor"]],
        "outputs":{name:pin(folder/name) for name in ("labels.parquet","coverage.json","IC.json","groups.json","summary.json")},
        "old_scores_or_accounts_modified":False,"raw_limit_or_actual_fills_evaluated":False,
        "HAC_t_p_values_computed":False,"annualized_or_strategy_return":None,**FLAGS}
    save(folder/"result.json",result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    reg=commands.add_parser("register");reg.add_argument("--scores",type=Path,action="append",required=True)
    reg.add_argument("--out",type=Path,required=True)
    consume=commands.add_parser("run");consume.add_argument("--registration",type=Path,required=True)
    consume.add_argument("--out",type=Path,required=True)
    args=vars(parser.parse_args(argv));command=args.pop("command")
    result=(register if command=="register" else run)(**args)
    print(json.dumps(clean({k:result.get(k) for k in ("schema","status","registered_at","registered_months","label_rows","adjusted_rowgroups_read")}),ensure_ascii=False,allow_nan=False))
    return 0
