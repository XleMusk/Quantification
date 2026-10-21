"""Bounded saved small-JSON projections; no real files or runtime refreshes."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from trading import research_evidence_catalog as m


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    return {"path":str(path.resolve()),"sha256":sha256(path.read_bytes()).hexdigest()}


def fixture(tmp_path):
    project,data=tmp_path/"project",tmp_path/"data"
    saved_at="2026-10-18T23:03:43+08:00"
    days=[f"{year}-{month:02}-28" for year in (2024,2025) for month in range(1,12)]
    coverage=[{"date":day,"label_rows":2200 if i<21 else 4683,
        "observed_labels":2069 if i==0 else 2200 if i<21 else 4683} for i,day in enumerate(days)]
    summaries=[]
    for scope in ("2024","2025","all"):
        for subset in ("all_registered_months","fixed_nonoverlap_anchors"):
            for arm in ("high52","momentum12_1","paired_high52_minus_momentum12_1"):
                summaries.append({"scope":scope,"subset":subset,"arm":arm,
                    "registered_month_count":22 if scope=="all" else 11,"valid_IC_months":22 if scope=="all" else 11,
                    "mean_rank_IC":-.014 if arm=="high52" else -.058,"positive_IC_fraction":.45,
                    "mean_Q5_minus_Q1_price_spread":None if scope=="2025" and subset=="fixed_nonoverlap_anchors" else -.009,
                    "valid_group_spread_months":11,"paired_valid_months":11,"mean_delta_IC":.044})
    rank_dir=data/"research/ranking"
    refs={name:write(rank_dir/name,value) for name,value in (("coverage.json",coverage),("summary.json",summaries))}
    ranking={"schema":m.RANK_SCHEMA,"status":"completed_saved_pool_rank_diagnostic","created_at":saved_at,
        "registered_months":22,"label_rows":50883,"outputs":refs,"account_backtest":False,
        "nonoverlap_retained_dates":days[::2],"candidate_panel_only":True,"execution_admitted":False,
        "selection_enabled":False,"strategy_validated":False,"returns_reportable":False}
    rank=rank_dir/"result.json";write(rank,ranking)
    cases=[]
    for year in (2024,2025):
        for arm in ("high52","momentum12_1"):
            complete=year==2024 and arm=="momentum12_1"
            cases.append({"year":year,"arm":arm,"status":"completed_conditional_replay" if complete else "incomplete_or_no_fills",
                "engine_sessions_committed":242 if complete else 85,"complete_PnL_available":complete,
                "metrics":{"net_return":-.12,"max_drawdown":-.31,"fees":693.36,"final_equity":88000} if complete else None,
                "residual_positions":{} if complete else {"600001.SSE":100,"000002.SZSE":200},
                "residual_full_exits":{},"stopped_day":None if complete else str(year)+"-05-15",
                "source_issues":[] if complete else [{"symbol":"600001.SSE","day":str(year)+"-05-15","status":"reference_mismatch"}],
                "parent_orders":22,"fill_callbacks":40,"result_path":str(data/f"accounts/{year}/{arm}/result.json")})
    comparison={"created_at":"2026-10-18T22:48:49+08:00","cases":cases,
        "candidate_panel_only":True,"execution_admitted":False,"selection_enabled":False,
        "strategy_validated":False,"returns_reportable":False}
    account=project/"outputs/comparison.json";write(account,comparison)
    write(project/m.POSTRUN,{"registered_months":22,"stock_month_rows":50883,"observed_price_labels":50752,
        "missing_price_labels":131,"unique_symbols":4941,"execution_admitted":False,"selection_enabled":False,
        "strategy_validated":False,"account_backtest":False})
    candidates=[]
    for identity in m.TITLES:
        row={"strategy_id":identity,"original_period":"2024/2025已检查时期","A_share_reproduction_status":"saved_unvalidated",
            "known_gaps":["不将诊断等同成交收益"],"next_test":"沿固定研究路线继续",
            "source_review":"来源范围说明","source":["https://example.org/paper",str(project/"outputs/report.md")]}
        if identity==m.PAIRED_ID: row["result"]={"ranking_diagnostic":str(rank),"account_comparison":str(account)}
        candidates.append(row)
    registry={"reviewed_at":"2026-10-14","candidates":candidates}
    registry_path=project/"trading/research/recent_candidates.json";write(registry_path,registry)
    return {"project":project,"data":data,"registry":registry,"registry_path":registry_path,
        "rank":rank,"ranking":ranking,"account":account,"summary":rank_dir/"summary.json"}


def test_actual_style_19_projection_keeps_negative_null_scope_and_saved_dates(tmp_path,monkeypatch):
    b=fixture(tmp_path)
    before={p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result=m.read_catalog(b["project"],b["data"],now=datetime.fromisoformat("2026-10-19T10:00:00+08:00"))
    assert result["candidate_count"]==19 and result["served_at"]=="2026-10-19T10:00:00+08:00"
    candidate=result["candidates"][0]
    assert candidate["title"]=="52周高点接近度与月动量对照" and candidate["period"]=="2024/2025已检查时期"
    rank=candidate["ranking_diagnostic"]
    assert rank["status"]=="saved" and rank["saved_at"]!=result["served_at"]
    assert {k:rank["coverage"][k] for k in ("score_months","unique_symbols","label_rows","observed_labels","missing_labels")}=={
        "score_months":22,"unique_symbols":4941,"label_rows":50883,"observed_labels":50752,"missing_labels":131}
    assert rank["coverage"]["unique_symbols_provenance"]["path"]==str((b["project"]/m.POSTRUN).resolve())
    assert any(r["mean_rank_IC"]<0 for r in rank["summaries"])
    assert any(r["mean_Q5_minus_Q1_price_spread"] is None for r in rank["summaries"])
    accounts=candidate["account_comparison"]
    assert accounts["complete_cases"]==1 and accounts["unresolved_cases"]==3
    assert accounts["cases"][0]["metrics"] is None and accounts["cases"][0]["residual_share_count"]==300
    assert accounts["cases"][1]["metrics"]["net_return"]==-.12 and accounts["cases"][1]["metrics"]["max_drawdown"]==-.31
    assert accounts["cases"][1]["residual_security_count"]==0
    assert before=={p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    from trading import api
    monkeypatch.setattr(api.settings,"ROOT",b["project"]);monkeypatch.setattr(api.settings,"DATA_ROOT",b["data"])
    assert api.research_candidate_evidence()["candidate_count"]==19


def test_corrupt_missing_and_conflicting_optional_metadata_are_isolated(tmp_path):
    b=fixture(tmp_path)
    summary_bytes=b["summary"].read_bytes()
    b["summary"].write_text("{}",encoding="utf-8")
    value=m.read_catalog(b["project"],b["data"])
    first=value["candidates"][0]
    assert first["ranking_diagnostic"]["status"]=="unknown" and first["account_comparison"]["status"]=="saved"
    assert value["candidate_count"]==19 and value["candidates"][1]["status"]=="saved_registry_entry"
    b["summary"].write_bytes(summary_bytes);b["account"].unlink()
    metadata=readback=b["project"]/m.POSTRUN
    altered=json.loads(metadata.read_text(encoding="utf-8"));altered["observed_price_labels"]-=1
    metadata.write_text(json.dumps(altered),encoding="utf-8")
    first=m.read_catalog(b["project"],b["data"])["candidates"][0]
    assert first["ranking_diagnostic"]["status"]=="saved" and first["account_comparison"]["status"]=="unknown"
    assert first["ranking_diagnostic"]["coverage"]["unique_symbols"] is None
    assert first["ranking_diagnostic"]["coverage"]["observed_labels"]==50752


def test_outside_source_reference_rejected_without_writes_parquet_or_producer(tmp_path,monkeypatch):
    b=fixture(tmp_path)
    outside=tmp_path/"outside.json";write(outside,{"not_allowed":True})
    altered=deepcopy(b["ranking"])
    altered["outputs"]["summary.json"]={"path":str(outside),"sha256":sha256(outside.read_bytes()).hexdigest()}
    b["rank"].write_text(json.dumps(altered),encoding="utf-8")
    original_read=Path.read_bytes
    def guarded_read(path):
        assert path!=outside and path.suffix==".json", "GET attempted outside or binary evidence read"
        return original_read(path)
    def no_write(*args,**kwargs): raise AssertionError("GET attempted write or producer")
    monkeypatch.setattr(Path,"read_bytes",guarded_read)
    for name in ("write_bytes","write_text","mkdir","unlink","replace"):
        monkeypatch.setattr(Path,name,no_write)
    result=m.read_catalog(b["project"],b["data"])
    first=result["candidates"][0]
    assert first["ranking_diagnostic"]["status"]=="unknown"
    assert first["ranking_diagnostic"]["reason"]=="evidence_path_outside_allowed_roots"
    assert first["account_comparison"]["status"]=="saved" and result["candidate_count"]==19
    assert result["producer_called"] is False and result["files_written"] is False
