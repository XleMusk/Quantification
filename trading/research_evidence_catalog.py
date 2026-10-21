"""Read bounded saved candidate evidence without running research or accounts."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

SCHEMA = "research_candidate_evidence_catalog_v1"
PAIRED_ID = "adjusted_52week_high_vs_matched_12_1_monthly_positive_pool_v1"
NATIVE_ID = "native_frozen_stage123_hit5_retrospective_quality_v1"
RANK_SCHEMA = "saved_monthly_common_pool_adjusted_open20_rank_diagnostic_v1"
POSTRUN = Path("outputs/leader_52week_monthly/20261018_pool_rank20_v1/actual_readback.json")
FLAGS = {"research_only":True,"candidate_panel_only":True,"execution_admitted":False,
    "selection_enabled":False,"strategy_validated":False,"returns_reportable":False,
    "account_backtest":False,"automatic_promotion":False,"producer_called":False,
    "engine_advanced":False,"account_created":False,"ledger_mutated":False,
    "training_started":False,"network_accessed":False,"files_written":False}
TITLES = {
    PAIRED_ID:"52周高点接近度与月动量对照",
    "native_12_1_monthly_leader_common_universe_benchmark_v1":"原价12-1月动量基准",
    "alpha158_lightgbm_fixed_five_name_baseline_v1":"Alpha158与LightGBM固定模型基准",
    "stock_defensive_fixed_40_20_20_v1":"股债金固定配比对照",
    "low_risk_price_volume_ablation_v1":"低波动与动量三规则对照",
    "three_asset_equal_risk_1y_v1":"股债金等风险配置改编",
    "three_asset_class_own_total_return_10m_v1":"股债金总回报十月均线择时",
    "six_equity_etf_own_total_return_10m_v1":"六只股票ETF总回报均线择时",
    "csi300_monthly_variance_local_v1":"月动量与沪深300波动率仓位控制",
    "monthly_momentum_csi300_10m_local_v2":"沪深300均线过滤月动量",
    "MASTER_AAAI2024_local_adaptation_candidate":"MASTER本地模型改编对照",
    "TSFM_China_JEF2024_pending_fundamentals":"中国因子择时文献候选（未运行）",
    "V61C_turnover_size_PIT_reproduction_candidate":"V61C低换手小市值候选（未运行）",
    "emotion_core_replenishment_v2":"连板延续与情绪风格假说",
    "full_source_public_seat_donchian_v1":"公开席位事件与唐奇安趋势对照",
    "lhb_identifiable_buy_concentration_v1":"龙虎榜席位集中度对照（含旧口径）",
    "shortline_fixed_emotion_rule_v1":"固定情绪量价规则对照",
    "long_term_owner_quality_v1":"长期持有风格质量因子改编",
    "native_fixed_eight_leaders_environment_comparison_v1":"八类龙头与市场环境对照"}
SUMMARY_FIELDS = ("scope","subset","arm","registered_month_count","valid_IC_months",
    "mean_rank_IC","positive_IC_fraction","valid_group_spread_months","mean_Q5_minus_Q1_price_spread",
    "paired_valid_months","mean_delta_IC","same_D_both_valid_dates")


def _path(value,roots):
    if not isinstance(value,(str,Path)) or "://" in str(value):
        raise ValueError("evidence_path_not_local")
    path=Path(value).resolve()
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("evidence_path_outside_allowed_roots")
    return path


def _read(value,roots,expected=None):
    path=_path(value,roots)
    if path.suffix.lower()!=".json": raise ValueError("only_saved_small_JSON_evidence_allowed")
    if path.stat().st_size>4*1024*1024: raise ValueError("saved_evidence_exceeds_small_JSON_bound")
    data=path.read_bytes();ref={"path":str(path),"sha256":sha256(data).hexdigest()}
    if expected is not None:
        declared=_path(expected.get("path") or expected["file"],roots)
        if declared!=path or ref["sha256"]!=expected["sha256"]:
            raise ValueError("saved_payload_hash_or_path_changed")
    def invalid(value): raise ValueError("nonfinite_saved_JSON_value")
    return json.loads(data.decode("utf-8-sig"),parse_constant=invalid),ref


def _boundaries(value):
    if (value.get("candidate_panel_only") is not True or any(value.get(k) is not False for k in
            ("execution_admitted","selection_enabled","strategy_validated","returns_reportable"))):
        raise ValueError("saved_research_boundaries_changed")


def _count(value):
    if type(value) is not int or value<0: raise ValueError("saved_count_unknown_or_invalid")
    return value


def _unknown(reason):
    return {"status":"unknown","reason":str(reason),"saved_at":None,"source":None}


def _ranking(path,roots,project_root):
    result,ref=_read(path,roots);_boundaries(result)
    if (result.get("schema")!=RANK_SCHEMA or result.get("status")!="completed_saved_pool_rank_diagnostic"
            or result.get("account_backtest") is not False):
        raise ValueError("saved_ranking_diagnostic_not_complete_or_wrong_method")
    payloads={}
    for name in ("summary.json","coverage.json"):
        declared=result["outputs"][name]
        sibling=Path(ref["path"]).parent/name
        payload,proof=_read(declared.get("path") or declared["file"],roots,expected=declared)
        if Path(proof["path"])!=sibling or not isinstance(payload,list):
            raise ValueError("ranking_payload_not_its_exact_saved_sibling")
        payloads[name]=(payload,proof)
    coverage=payloads["coverage.json"][0]
    dates=[row["date"] for row in coverage]
    if len(dates)!=len(set(dates)) or len(dates)!=_count(result["registered_months"]):
        raise ValueError("ranking_coverage_months_disagree")
    label_rows=sum(_count(row["label_rows"]) for row in coverage)
    observed=sum(_count(row["observed_labels"]) for row in coverage)
    if label_rows!=_count(result["label_rows"]) or observed>label_rows:
        raise ValueError("ranking_coverage_rows_disagree")
    projected={"score_months":len(coverage),"unique_symbols":None,"label_rows":label_rows,
        "observed_labels":observed,"missing_labels":label_rows-observed,
        "unique_symbols_provenance":None,"unique_symbols_reason":"not_saved_in_small_metadata"}
    # Optional existing post-run metadata, never a labels.parquet read on GET.
    try:
        postrun,proof=_read(project_root/POSTRUN,roots)
        matching={"registered_months":len(coverage),"stock_month_rows":label_rows,
            "observed_price_labels":observed,"missing_price_labels":label_rows-observed}
        if any(postrun.get(k)!=v for k,v in matching.items()):
            raise ValueError("saved_postrun_metadata_count_conflict")
        if any(postrun.get(k) is not False for k in ("execution_admitted","selection_enabled","strategy_validated","account_backtest")):
            raise ValueError("saved_postrun_metadata_boundaries_changed")
        unique=_count(postrun["unique_symbols"])
        if unique>label_rows: raise ValueError("saved_postrun_unique_count_invalid")
        projected.update(unique_symbols=unique,unique_symbols_reason=None,
            unique_symbols_provenance={**proof,"basis":"existing_postrun_metadata_four_coverage_counts_concordant_no_parquet_read_this_GET"})
    except (OSError,UnicodeError,ValueError,KeyError,TypeError) as error:
        projected["unique_symbols_reason"]=str(error)
    summaries=[];keys=set()
    for row in payloads["summary.json"][0]:
        key=(row["scope"],row["subset"],row["arm"])
        if (key in keys or key[0] not in {"2024","2025","all"}
                or key[1] not in {"all_registered_months","fixed_nonoverlap_anchors"}
                or key[2] not in {"high52","momentum12_1","paired_high52_minus_momentum12_1"}):
            raise ValueError("saved_ranking_summary_identity_changed")
        keys.add(key);summaries.append({k:deepcopy(row.get(k)) for k in SUMMARY_FIELDS})
    if len(keys)!=18: raise ValueError("saved_ranking_summary_has_missing_fixed_rows")
    return {"status":"saved","reason":None,"saved_at":result.get("created_at"),"source":ref,
        "coverage":projected,"summaries":summaries,
        "nonoverlap_retained_dates":deepcopy(result["nonoverlap_retained_dates"]),
        "payload_refs":[payloads[k][1] for k in ("summary.json","coverage.json")],
        "metric_units":{"IC":"dimensionless","group_spread":"return_fraction_not_account_PnL"},
        "price_diagnostics_are_account_returns":False,"tradeability_evaluated":False}


def _account_case(row,roots):
    if type(row.get("complete_PnL_available")) is not bool:
        raise ValueError("saved_account_completion_unknown")
    complete=row["complete_PnL_available"]
    metrics=row.get("metrics")
    if complete and not isinstance(metrics,dict): raise ValueError("completed_account_metrics_not_saved")
    residual=row.get("residual_positions")
    if residual is not None and (not isinstance(residual,dict) or any(type(q) is not int or q<0 for q in residual.values())):
        raise ValueError("saved_residual_quantities_unknown")
    result_path=str(_path(row["result_path"],roots)) if row.get("result_path") else None
    return {"year":row["year"],"arm":row["arm"],"status":row.get("status"),"evidence_status":"saved",
        "reason":None,"engine_sessions_committed":row.get("engine_sessions_committed"),
        "complete_PnL_available":complete,"metrics":deepcopy(metrics) if complete else None,
        "residual_positions":deepcopy(residual),"residual_security_count":sum(q>0 for q in residual.values()) if residual is not None else None,
        "residual_share_count":sum(residual.values()) if residual is not None else None,
        "residual_full_exits":deepcopy(row.get("residual_full_exits")),"stopped_day":row.get("stopped_day"),
        "source_issues":deepcopy(row.get("source_issues")),"parent_orders":row.get("parent_orders"),
        "fill_callbacks":row.get("fill_callbacks"),"result_path":result_path}


def _accounts(path,roots):
    record,ref=_read(path,roots);_boundaries(record)
    if not isinstance(record.get("cases"),list): raise ValueError("saved_account_comparison_cases_missing")
    output=[]
    for year in (2024,2025):
        for arm in ("high52","momentum12_1"):
            choices=[r for r in record["cases"] if r.get("year")==year and r.get("arm")==arm]
            try:
                if len(choices)!=1: raise ValueError("saved_account_case_missing_or_duplicated")
                output.append(_account_case(choices[0],roots))
            except (ValueError,KeyError,TypeError) as error:
                output.append({"year":year,"arm":arm,"status":"unknown","evidence_status":"unknown",
                    "reason":str(error),"engine_sessions_committed":None,"complete_PnL_available":None,
                    "metrics":None,"residual_positions":None,"residual_security_count":None,"residual_share_count":None,
                    "source_issues":None,"stopped_day":None,"parent_orders":None,"fill_callbacks":None,"result_path":None})
    return {"status":"saved" if all(r["evidence_status"]=="saved" for r in output) else "partial",
        "reason":None,"saved_at":record.get("created_at"),"source":ref,"cases":output,
        "complete_cases":sum(r["complete_PnL_available"] is True for r in output),
        "unresolved_cases":sum(r["complete_PnL_available"] is False for r in output),
        "unknown_cases":sum(r["evidence_status"]=="unknown" for r in output),
        "independent_books_are_personal_positions":False,"metrics_are_conditional_research_only":True}


def _sources(values,roots):
    if not isinstance(values,list): return []
    result=[]
    for value in values:
        if not isinstance(value,str): continue
        if value.startswith(("https://","http://")): result.append(value)
        else: result.append(str(_path(value,roots)))
    return result


def _native_unknown(reason,section):
    result={**_unknown(reason),"rows":None}
    if section=="quality":
        result.update(coverage=None,model_available_at=None,event_start=None,event_end=None,label_witness_cutoff=None,saved_engine_advanced=None)
    elif section=="accounts":
        result.update(saved_engine_advanced=None,saved_paper_orders_generated=None)
    else:
        result["saved_accounts_advanced"]=None
    return result


def _linked_readback(path,roots,result_ref):
    saved,ref=_read(path,roots)
    if (not saved.get("result") or _path(saved["result"],roots)!=Path(result_ref["path"])
            or saved.get("result_sha256")!=result_ref["sha256"]):
        raise ValueError("native_saved_readback_result_binding_changed")
    return saved,ref


def _native_quality(source,roots):
    result,ref=_read(source["evidence"],roots);_boundaries(result)
    if (result.get("schema")!="native_frozen_shortline_evaluation_result_v1"
            or result.get("status")!="completed_retrospective_frozen_stagewise_quality"
            or result.get("engine_advanced") is not False or result.get("model_refit") is not False):
        raise ValueError("native_quality_source_method_or_phase_changed")
    file=Path(ref["path"]).parent/"quality.json"
    rows,payload_ref=_read(file,roots,expected={"path":str(file),"sha256":result["files"]["quality.json"]})
    if (not isinstance(rows,list) or len(rows)!=6
            or {(r["model_kind"],r["stage"]) for r in rows}!={(k,s) for k in ("lr","lgbm") for s in (1,2,3)}):
        raise ValueError("native_quality_six_fixed_rows_missing_or_duplicated")
    summary=result["summary"]
    total=_count(summary["unique_event_rows"]);mature=_count(summary["evaluated_unique_events"])
    if mature>total: raise ValueError("native_quality_maturity_counts_conflict")
    for kind in ("lr","lgbm"):
        own=[r for r in rows if r["model_kind"]==kind]
        if (sum(_count(r["event_rows"]) for r in own)!=total
                or sum(_count(r["evaluable_rows"]) for r in own)!=mature
                or sum(_count(r["unknown_rows"]) for r in own)!=total-mature):
            raise ValueError("native_quality_stage_coverage_conflict")
        for row in own:
            if row["event_rows"]!=row["evaluable_rows"]+row["unknown_rows"]:
                raise ValueError("native_quality_known_unknown_counts_conflict")
            if not isinstance(row.get("metrics"),dict): raise ValueError("native_quality_metrics_unknown")
    readback,readback_ref=_linked_readback(source["actual_readback"],roots,ref)
    observed=readback["summary"]
    if any(observed.get(key)!=summary.get(key) for key in
            ("unique_event_rows","evaluated_unique_events","registered_days")):
        raise ValueError("native_quality_readback_coverage_conflict")
    unique=_count(readback["unique_symbols"])
    if unique>total: raise ValueError("native_quality_unique_symbol_count_conflict")
    return {"status":"saved","reason":None,"source":ref,"saved_at":result.get("completed_at"),
        "model_available_at":result.get("model_available_at"),"event_start":result.get("event_start"),
        "event_end":result.get("event_end"),"label_witness_cutoff":result.get("label_witness_cutoff"),
        "coverage":{"registered_days":_count(summary["registered_days"]),"unique_events":total,
            "mature_events":mature,"unknown_events":total-mature,"unique_symbols":unique},
        "rows":deepcopy(rows),"payload_refs":[payload_ref,readback_ref],
        "saved_engine_advanced":result["engine_advanced"],
        "retrospective_not_historical_publication":True,"two_models_share_the_same_events":True}


def _native_issues(values):
    if not isinstance(values,list): return None
    result=[]
    for row in values:
        day=row.get("day")
        result.append({"day":day,"reason":row.get("reason"),"symbol":row.get("symbol"),"status":row.get("status")})
        for item in row.get("raw_issues",[]):
            result.append({"day":item.get("day",day),"symbol":item.get("symbol"),
                "reason":item.get("reason"),"status":item.get("status")})
        for item in row.get("references",[]):
            if item.get("status") not in {"reference_match","no_reported_event_reference_continuous"}:
                result.append({"day":item.get("day",day),"symbol":item.get("symbol"),
                    "reason":"reference_evidence_unresolved","status":item.get("status")})
        for item in row.get("ipo",[]):
            if item.get("engine_block_reason"):
                result.append({"day":item.get("day",day),"symbol":item.get("symbol"),
                    "reason":item["engine_block_reason"],"status":item.get("knowledge_status")})
    return result


def _native_accounts(source,roots):
    result,ref=_read(source["evidence"],roots);_boundaries(result)
    if (result.get("schema")!="native_frozen_saved_selection_fixed16pct_shared_engine_replay_v1_result"
            or result.get("case_count")!=2 or not isinstance(result.get("cases"),list)
            or result.get("engine_advanced") is not True or result.get("paper_orders_generated") is not True
            or result.get("user_account_mutated") is not False):
        raise ValueError("native_accounts_source_method_or_count_changed")
    readback,readback_ref=_linked_readback(source["actual_readback"],roots,ref)
    checks=readback.get("execution_checks",{}).get("cases")
    if not isinstance(checks,list): raise ValueError("native_account_saved_fee_checks_missing")
    rows=[]
    for kind in ("lr","lgbm"):
        own=[r for r in result["cases"] if r.get("kind")==kind]
        observed=[r for r in checks if r.get("model_kind")==kind]
        if len(own)!=1 or len(observed)!=1: raise ValueError("native_account_identity_missing_or_duplicated")
        row,check=own[0],observed[0]
        if any(check.get(k)!=row.get(k) for k in
                ("engine_sessions_committed","last_committed_day","parent_orders","fill_callbacks")):
            raise ValueError("native_account_saved_readback_counts_conflict")
        if row.get("full_tail",{}).get("complete_PnL_available") is not False or row["full_tail"].get("metrics") is not None:
            raise ValueError("native_partial_account_evidence_contract_changed")
        fees=check.get("fees_rmb")
        amount=None
        if fees is not None:
            if (not isinstance(fees,dict) or not all(k in fees for k in
                    ("sum_parent_order_fees","sum_fill_callback_fees","sum_saved_daily_fees"))):
                raise ValueError("native_account_saved_fee_amount_unknown")
            numbers=[fees[k] for k in ("sum_parent_order_fees","sum_fill_callback_fees","sum_saved_daily_fees")]
            if any(type(n) not in (int,float) or n<0 for n in numbers) or max(numbers)-min(numbers)>1e-8:
                raise ValueError("native_account_saved_fee_totals_conflict")
            amount=numbers[0]
        residual=row.get("residual_positions")
        if residual is not None and (not isinstance(residual,dict) or any(type(q) is not int or q<0 for q in residual.values())):
            raise ValueError("native_account_residual_quantities_unknown")
        rows.append({"model_kind":kind,"committed_sessions":row.get("engine_sessions_committed"),
            "last_committed_day":row.get("last_committed_day"),"blocked_day":(row.get("blocking_evidence") or {}).get("day"),
            "parent_orders":row.get("parent_orders"),"fill_callbacks":row.get("fill_callbacks"),"fees":amount,
            "residual_positions":deepcopy(residual),"pending_targets":deepcopy(row.get("pending_targets")),
            "complete_PnL_available":False,"metrics":None,"main_window":deepcopy(row.get("main_window")),
            "source_issues":_native_issues(row.get("source_issues"))})
    return {"status":"saved","reason":None,"source":ref,"saved_at":result.get("actual_completed_at"),
        "rows":rows,"payload_refs":[readback_ref],"fees_unit":"RMB",
        "saved_engine_advanced":result["engine_advanced"],"saved_paper_orders_generated":result["paper_orders_generated"],
        "callbacks_are_not_independent_trades":True,"independent_research_accounts":True}


def _native_attribution(source,roots):
    result,ref=_read(source["result"],roots)
    if (result.get("schema")!="native_saved_selection_execution_attribution_v1"
            or result.get("candidate_panel_only") is not True
            or any(result.get(k) is not False for k in
                ("execution_admitted","selection_enabled","strategy_validated","returns_calculated","models_rerun","accounts_advanced"))):
        raise ValueError("native_saved_attribution_method_or_boundaries_changed")
    rows=[]
    states=("buy_order_filled","buy_blocked","execution_session_unavailable","no_buy_attempt_recorded")
    for kind in ("lr","lgbm"):
        matches=[r for r in result["cases"] if r.get("model_kind")==kind]
        if len(matches)!=1: raise ValueError("native_attribution_fixed_model_missing_or_duplicated")
        own=matches[0];categories=own["categories"]
        if len(categories)!=4 or {r["execution_status"] for r in categories}!=set(states):
            raise ValueError("native_attribution_four_saved_categories_required")
        groups={r["execution_status"]:r for r in categories}
        for row in categories:
            size=_count(row["selected_slots"])
            known,unknown=_count(row["known_label_slots"]),_count(row["unknown_label_slots"])
            if (known+unknown!=size or _count(row["hit5_slots"])>known
                    or own["execution_status_counts"].get(row["execution_status"],0)!=size):
                raise ValueError("native_attribution_saved_category_counts_conflict")
        if sum(r["selected_slots"] for r in categories)!=_count(own["selected_slots"]):
            raise ValueError("native_attribution_selected_count_conflict")
        rows.append({"model_kind":kind,"selected_slots":own["selected_slots"],
            "filled_slots":groups[states[0]]["selected_slots"],"blocked_slots":groups[states[1]]["selected_slots"],
            "unavailable_slots":groups[states[2]]["selected_slots"],"no_attempt_slots":groups[states[3]]["selected_slots"],
            "known_label_slots":sum(r["known_label_slots"] for r in categories),
            "known_hit5_slots":sum(r["hit5_slots"] for r in categories),
            "hit5_filled":groups[states[0]]["hit5_slots"],"hit5_blocked":groups[states[1]]["hit5_slots"],
            "hit5_unavailable":groups[states[2]]["hit5_slots"],"categories":deepcopy(categories)})
    return {"status":"saved","reason":None,"source":ref,"saved_at":result.get("completed_at"),
        "rows":rows,"saved_accounts_advanced":result["accounts_advanced"],
        "selected_slots_are_not_independent_trades":True,"unavailable_is_not_a_failed_order":True}


def _native_evidence(source,roots):
    result=source.get("result") or {}
    engine=result.get("saved_selection_engine_replay") or {}
    output={}
    for section,reader,record in (("quality",_native_quality,result),("accounts",_native_accounts,engine),
            ("attribution",_native_attribution,engine.get("execution_attribution") or {})):
        try: output[section]=reader(record,roots)
        except (OSError,UnicodeError,ValueError,KeyError,TypeError,AttributeError) as error:
            output[section]=_native_unknown(error,section)
    return output


def read_catalog(project_root,data_root,*,now=None):
    """GET projection only: fixed registry, exact declared small saved evidence."""
    project_root,data_root=Path(project_root).resolve(),Path(data_root).resolve()
    roots=(project_root,data_root)
    current=now or datetime.now().astimezone()
    base={"schema":SCHEMA,"status":"saved_research_candidate_evidence","served_at":current.isoformat(),
        "registry":None,"candidate_count":0,"candidates":[],"cross_sample_ranking":None,**FLAGS}
    try:
        registry,ref=_read(project_root/"trading/research/recent_candidates.json",roots)
        if not isinstance(registry.get("candidates"),list): raise ValueError("candidate_registry_list_missing")
    except (OSError,UnicodeError,ValueError,KeyError,TypeError) as error:
        return {**base,"status":"unavailable","reason":str(error)}
    base["registry"]={**ref,"reviewed_at":registry.get("reviewed_at")}
    seen=set()
    for source in registry["candidates"]:
        strategy=source.get("strategy_id") if isinstance(source,dict) else None
        if not isinstance(strategy,str): strategy=None
        title=(source.get("display_name") or "首二三板至五板：原生冻结模型证据") if strategy==NATIVE_ID else TITLES.get(strategy,strategy or "未知研究候选")
        row={"id":strategy,"title":title,
            "reviewed_at":source.get("reviewed_at") if isinstance(source,dict) else None,
            "status":"saved_registry_entry","reproduction_status":None,"period":None,
            "known_gaps":None,"next_test":None,"source_review":None,"source":[],
            "ranking_diagnostic":None,"account_comparison":None,**FLAGS}
        try:
            if not isinstance(strategy,str) or strategy in seen: raise ValueError("candidate_identity_missing_or_duplicated")
            seen.add(strategy)
            if any(source.get(k,False) is not False for k in ("execution_admitted","selection_enabled","strategy_validated","returns_reportable")):
                raise ValueError("candidate_registry_research_boundaries_changed")
            row.update(reproduction_status=source.get("A_share_reproduction_status"),period=source.get("original_period"),
                known_gaps=deepcopy(source.get("known_gaps")),next_test=source.get("next_test"),
                source_review=source.get("source_review"),source=_sources(source.get("source"),roots))
            if strategy==PAIRED_ID:
                for key,reader in (("ranking_diagnostic",lambda p:_ranking(p,roots,project_root)),
                                   ("account_comparison",lambda p:_accounts(p,roots))):
                    try: row[key]=reader(source["result"][key])
                    except (OSError,UnicodeError,ValueError,KeyError,TypeError) as error: row[key]=_unknown(error)
                row["status"]="saved_evidence" if all(row[k]["status"]=="saved" for k in
                    ("ranking_diagnostic","account_comparison")) else "partial_evidence"
            elif strategy==NATIVE_ID:
                row["native_evidence"]=_native_evidence(source,roots)
                row["status"]="saved_evidence" if all(r["status"]=="saved" for r in row["native_evidence"].values()) else "partial_evidence"
        except (OSError,UnicodeError,ValueError,KeyError,TypeError) as error:
            row.update(status="unknown",reason=str(error))
        base["candidates"].append(row)
    base["candidate_count"]=len(base["candidates"])
    return base
