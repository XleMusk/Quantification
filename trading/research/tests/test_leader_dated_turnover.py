"""Small synthetic offline sidecars; not market or strategy evidence."""
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import leader_dated_turnover as m

NOW = datetime.fromisoformat("2026-10-15T16:00:00+08:00")


def save_json(path, value):
    path.write_text(json.dumps(value), encoding="utf8")


def inputs(tmp_path):
    cache, close, extra, industry = [tmp_path / name for name in ("cache", "close", "extras", "industry")]
    for folder in (cache, close, extra, industry): folder.mkdir()
    unit=tmp_path/"units.json";save_json(unit,{"turn_unit":"percent","status":"official_provider_document_read","retrieved_at":"2026-10-15T12:15:00+08:00"})
    fields = m.BAOSTOCK_FIELDS.split(",")
    def row(code, day, halted=False):
        return dict(zip(fields, [day, code, "10.0000", "10.0000", "10.0000", "10.0000", "10.0000",
            "" if halted else "100", "" if halted else "1000.0000", "3", "" if halted else "2.000000",
            "0" if halted else "1", "" if halted else "0.000000", "10", "1", "1", "1", "0"]))
    raw = pd.DataFrame([row("sh.600000",m.DAY12),row("sz.000001",m.DAY12,True),row("sh.689009",m.DAY12)])
    raw_file = cache / (m.DAY12 + "_stock.csv"); raw.to_csv(raw_file,index=False)
    task = {"date":m.DAY12,"kind":"stock","method":"query_daily_history_k_AStock","error_code":"0",
            "file":raw_file.name,"sha256":m._digest(raw_file),"quality":{"rows":3}}
    save_json(cache/"plan.json",{"kind":"stock","days":[m.DAY12,m.DAY13],"execution_admitted":False})
    save_json(cache/"receipts.json",{"status":"source_cache_complete","tasks":{m.DAY12:task,m.DAY13:{"date":m.DAY13}},"execution_admitted":False,"updated_at":"2026-10-14T07:00:00+08:00"})
    ordinary = raw.iloc[:2][m.FIELDS.split(",")]
    ordinary.to_csv(close/"raw_daily.csv",index=False)
    pd.DataFrame({"code":["sh.600000","sz.000001"],"tradeStatus":["1","0"]}).to_csv(close/"ordinary_roster.csv",index=False)
    save_json(close/"plan.json",{"schema":"baostock_full_dated_close_inventory_v1","day":m.DAY12,"symbols":["600000.SSE","000001.SZSE"],"ordinary_symbols":2})
    save_json(close/"receipt.json",{"day":m.DAY12,"status":"complete_full_dated_close_inventory_not_admitted","full_dated_roster_inventory_complete":True,"roster_pinned_date":m.DAY12,"execution_admitted":False,"pending_symbols":[],"completed_rows":2,"updated_at":"2026-10-15T10:45:00+08:00","raw_daily_sha256":m._digest(close/"raw_daily.csv")})
    other = pd.DataFrame([row("sh.600000",m.DAY13),row("sz.000001",m.DAY13,True)])
    other["symbol"]=["600000.SSE","000001.SZSE"]
    other["turn_numeric"]=pd.array([2,None],dtype="Float64")
    other.to_parquet(extra/"dated_extras.parquet",index=False)
    (extra/"package").mkdir()
    save_json(extra/"package/receipt.json",{"day":m.DAY13,"status":"complete_full_dated_close_inventory_not_admitted","raw_daily_sha256":"b"*64})
    save_json(extra/"result.json",{"schema":m.EXTRAS_SCHEMA,"day":m.DAY13,"status":"completed_dated_bulk_extras_research_only",**m.EXTRAS_FLAGS,
        "turn_numeric_unit":"percent","turn_scaled":False,"source_inputs_available_at":"2026-10-15T11:00:00+08:00","available_at":"2026-10-15T12:18:00+08:00","actual_derived_at":"2026-10-15T12:18:00+08:00","data_file":str(extra/"dated_extras.parquet"),"data_sha256":m._digest(extra/"dated_extras.parquet"),"ordinary_rows":2,
        "package":str(extra/"package"),"package_receipt_sha256":m._digest(extra/"package/receipt.json"),"unit_evidence_file":str(unit),"unit_evidence_sha256":m._digest(unit),"unit_evidence":m._read(unit)})
    pd.DataFrame({"date":["2026-09-30"],"symbol":["600000.SSE"],"industry":["bank"],"industryClassification":["broad"],"updateDate":["2026-09-28"]}).to_parquet(industry/"2026-09-30.parquet",index=False)
    save_json(industry/"result.json",{"source":"BaoStock.query_stock_industry(date=decision_day)","execution_admitted":False,"entries":[{"date":"2026-09-30","file":"2026-09-30.parquet","rows":1,"retrieved_at":"2026-10-04T03:00:00+08:00"}]})
    return cache,close,extra/"result.json",industry/"2026-09-30.parquet",unit


def test_real_multiday_contract_kept_halt_null_and_all_roster_no_source_write(tmp_path):
    cache,close,extras,industry,units=inputs(tmp_path)
    originals={p:p.read_bytes() for folder in (cache,close,extras.parent,industry.parent) for p in folder.iterdir() if p.is_file()}
    path,record=m.build(cache,close,extras,industry,tmp_path/"new",now=NOW,unit_evidence=units)
    meta,rows=m.load_dated_turnover(path,m.DAY12,observed_at=NOW)
    assert meta["original_cache_task_count"]==2 and meta["original_date_entry"]["date"]==m.DAY12
    assert meta["excluded_nonordinary_codes"]==["sh.689009"] and len(rows)==2
    assert rows.turn_percent.iloc[0]==2 and pd.isna(rows.turn_percent.iloc[1])
    assert rows.turn_raw.tolist()==["2.000000",""] and pd.isna(rows.industry.iloc[1])
    assert record["available_at"]==NOW.isoformat() and record["single_call_receipt_fabricated"] is False
    meta13,rows13=m.load_dated_turnover(path,m.DAY13,observed_at=NOW)
    assert meta13["data_rederived"] is False and len(rows13)==2 and pd.isna(rows13.turn_percent.iloc[1])
    assert all(p.read_bytes()==raw for p,raw in originals.items())
    batch12=m.load_turnover_source(path,observed_at=NOW)
    batch13=m.load_turnover_source(extras,observed_at=NOW)
    assert batch12["day"]==m.DAY12 and batch13["day"]==m.DAY13
    assert batch12["rows"][1]["turn_percent"] is None and batch13["rows"][1]["turn_percent"] is None
    assert batch12["units_verified"] is True and batch12["unit"]=="percent"
    assert batch12["price_source_raw_sha256"]==meta["own_close_raw_sha256"]
    assert batch13["price_source_raw_sha256"]=="b"*64


@pytest.mark.parametrize("fault",["raw_hash","own_value","date_entry","not_completed","halt_turn","missing_ordinary"])
def test_wrong_raw_date_scope_or_shared_original_values_refused(tmp_path,fault):
    cache,close,extras,industry,units=inputs(tmp_path)
    receipts=m._read(cache/"receipts.json")
    if fault=="raw_hash":receipts["tasks"][m.DAY12]["sha256"]="0"*64
    if fault=="date_entry":receipts["tasks"][m.DAY12]["date"]=m.DAY13
    if fault=="not_completed":receipts["status"]="partial"
    if fault in ("own_value","halt_turn","missing_ordinary"):
        f=cache/(m.DAY12+"_stock.csv");raw=pd.read_csv(f,dtype=str,keep_default_na=False)
        if fault=="own_value":raw.loc[0,"close"]="10.0100"
        if fault=="halt_turn":raw.loc[1,"turn"]="0"
        if fault=="missing_ordinary":raw=raw.drop(index=1)
        raw.to_csv(f,index=False);receipts["tasks"][m.DAY12].update(sha256=m._digest(f),quality={"rows":len(raw)})
    save_json(cache/"receipts.json",receipts)
    with pytest.raises(ValueError):m.build(cache,close,extras,industry,tmp_path/"new",now=NOW,unit_evidence=units)
    assert not (tmp_path/"new").exists()


def test_reader_denies_backdating_unsafe_flags_changed_bytes_and_missing_date(tmp_path):
    cache,close,extras,industry,units=inputs(tmp_path)
    path,record=m.build(cache,close,extras,industry,tmp_path/"new",now=NOW,unit_evidence=units)
    with pytest.raises(ValueError):m.load_dated_turnover(path,m.DAY12,observed_at="2026-10-15T15:59:00+08:00")
    with pytest.raises(KeyError):m.load_dated_turnover(path,"2026-10-14",observed_at=NOW)
    record["execution_admitted"]=True;save_json(path,record)
    with pytest.raises(ValueError):m.load_dated_turnover(path,m.DAY12,observed_at=NOW)
    record["execution_admitted"]=False;save_json(path,record)
    with (path.parent/"20261012_turnover.parquet").open("ab") as handle:handle.write(b"tamper")
    with pytest.raises(ValueError):m.load_dated_turnover(path,m.DAY12,observed_at=NOW)


def typed15(tmp_path):
    """A synthetic complete new-day extras package, never a market artifact."""
    _,_,path,_,_=inputs(tmp_path)
    record=m._read(path)
    data=pd.read_parquet(record["data_file"])
    data["date"]="2026-10-15"
    data.to_parquet(record["data_file"],index=False)
    receipt_path=path.parent/"package/receipt.json"
    receipt=m._read(receipt_path);receipt["day"]="2026-10-15";save_json(receipt_path,receipt)
    record.update(day="2026-10-15",source_inputs_available_at="2026-10-15T15:10:00+08:00",
        actual_derived_at="2026-10-15T15:20:00+08:00",available_at="2026-10-15T15:20:00+08:00",
        data_sha256=m._digest(record["data_file"]),package_receipt_sha256=m._digest(receipt_path))
    save_json(path,record)
    return path,record


def test_complete_typed_new_day_is_loaded_without_relabeling_or_rederivation(tmp_path):
    path,record=typed15(tmp_path)
    before={p:p.read_bytes() for p in (path, path.parent/"dated_extras.parquet", path.parent/"package/receipt.json")}
    batch=m.load_turnover_source(path,observed_at=NOW)
    explicit=m.load_turnover_source(path,observed_at=NOW,day="2026-10-15")
    assert batch==explicit and batch["day"]=="2026-10-15"
    assert {r["date"] for r in batch["rows"]}=={"2026-10-15"}
    assert [r["turn_percent"] for r in batch["rows"]]==[2.0,None]
    assert batch["available_at"]==record["available_at"] and batch["unit"]=="percent"
    assert batch["units_verified"] is True and batch["sourceproof"]["data_rederived"] is False
    assert batch["price_source_raw_sha256"]=="b"*64 and batch["execution_admitted"] is False
    assert all(p.read_bytes()==raw for p,raw in before.items())
    with pytest.raises(ValueError):m.load_turnover_source(path,observed_at=NOW,day=m.DAY13)
    with pytest.raises(ValueError):m._existing13(path,NOW)


@pytest.mark.parametrize("fault",["future_source_day","future_available_at","future_derived_at"])
def test_new_typed_day_refuses_future_source_or_actual_clock(tmp_path,fault):
    path,record=typed15(tmp_path)
    if fault=="future_source_day":
        record["day"]="2026-10-16"
        rows=pd.read_parquet(record["data_file"]);rows["date"]=record["day"];rows.to_parquet(record["data_file"],index=False)
        record["data_sha256"]=m._digest(record["data_file"])
        receipt_path=path.parent/"package/receipt.json"
        receipt=m._read(receipt_path);receipt["day"]=record["day"];save_json(receipt_path,receipt)
        record["package_receipt_sha256"]=m._digest(receipt_path)
    elif fault=="future_available_at":record["available_at"]="2026-10-15T17:00:00+08:00"
    else:record["actual_derived_at"]="2026-10-15T17:00:00+08:00"
    save_json(path,record)
    with pytest.raises(ValueError):m.load_turnover_source(path,observed_at=NOW)
