"""One wide snapshot uses its own dated roster; all provider calls are fakes."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import prior_dated_market_quote_snapshot as r


class Clock:
    def __init__(self, value="2026-10-15T13:05:00+08:00"):
        self.value = datetime.fromisoformat(value)

    def now(self):
        return self.value


class Response:
    def __init__(self, raw, status=200):
        self.raw, self.status_code = raw, status
        self.headers = {"Content-Type": "application/javascript"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.raw


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def fixture(tmp_path, monkeypatch, count=205):
    data, package = tmp_path / "data", tmp_path / "source"
    calendar = tmp_path / "calendar.json"
    dump(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                    "closed_ranges": [], "open_sessions": []})
    cal_sha = sha256(calendar.read_bytes()).hexdigest()
    symbols = ["600000.SSE", *[f"{index:06d}.SZSE" for index in range(1, count)]]
    ordinary = pd.DataFrame([{"code": ("sh." if symbol.endswith(".SSE") else "sz.") + symbol[:6],
        "tradeStatus": "0" if index % 100 == 0 else "1", "code_name": f"旧名{index}", "symbol": symbol}
        for index, symbol in enumerate(symbols)])
    raw = pd.DataFrame([{"date": "2026-10-14", "code": row["code"], "open": "10", "high": "11", "low": "9",
        "close": "10", "preclose": "10", "volume": "" if row["tradeStatus"] == "0" else "1000",
        "amount": "" if row["tradeStatus"] == "0" else "10000", "adjustflag": "3", "tradestatus": row["tradeStatus"],
        "pctChg": "" if row["tradeStatus"] == "0" else "0", "isST": "1" if index % 10 == 0 else "0"}
        for index, row in enumerate(ordinary.to_dict("records"))])
    package.mkdir()
    raw.to_csv(package / "raw_daily.csv", index=False)
    ordinary.to_csv(package / "ordinary_roster.csv", index=False)
    roster = package / "roster"
    roster.mkdir()
    ordinary[["code", "tradeStatus", "code_name"]].to_csv(roster / "query_all_stock_2026-10-14.csv", index=False)
    dump(roster / "receipt.json", {"created_at": "2026-10-15T08:36:11+08:00", "completed_at": "2026-10-15T08:36:15+08:00"})
    plan = {"schema": "baostock_full_dated_close_inventory_v1", "day": "2026-10-14", "calendar_sha256": cal_sha,
            "roster_raw_sha256": r.digest(roster / "query_all_stock_2026-10-14.csv"), "ordinary_symbols": count}
    receipt = {"schema": plan["schema"], "day": plan["day"], "status": "complete_full_dated_close_inventory_not_admitted",
               "raw_daily_sha256": r.digest(package / "raw_daily.csv"), "full_dated_roster_inventory_complete": True,
               "created_at": "2026-10-15T09:07:47+08:00", "updated_at": "2026-10-15T09:50:42+08:00"}
    dump(package / "plan.json", plan); dump(package / "receipt.json", receipt)
    metadata = {"plan": plan, "receipt": receipt, "source_folder": str(package), "read_only_verified": True}
    loads = []
    def loaded(path):
        assert path == package
        loads.append(path)
        return deepcopy(metadata), raw.copy(), ordinary.copy()
    monkeypatch.setattr(r, "load_complete_close", loaded)
    registry_path = data / r.REGISTRY_RELATIVE
    registry = {"registered_at": "2026-10-15T11:54:13+08:00", "members": [
        {"symbol": symbol, "name": symbol, "labels": [{"id": "trend_leader"}]} for symbol in symbols[:590]]}
    dump(registry_path, registry)
    registry_sha = r.digest(registry_path)
    monkeypatch.setattr(r, "_registry", lambda path, actual: (deepcopy(registry), registry_sha, [], {}))
    dump(data / r.CONTRACT_RELATIVE, {"schema": "sina_ordinary_SH_SZ_A_price_time_contract_assessment_v1",
                                    "created_at": "2026-10-15T12:41:39+08:00"})
    out = data / "research/market_roster_observation/new_snapshot"
    return {"data": data, "package": package, "calendar": calendar, "out": out, "raw": raw,
            "ordinary": ordinary, "metadata": metadata, "registry": registry, "registry_path": registry_path, "loads": loads}


def transport(clock, *, kind=None, fail_call=2, advance=0):
    calls = []
    def get(endpoint, **kwargs):
        codes = endpoint.removeprefix(r.ENDPOINT).split(",")
        calls.append((clock.now(), codes, kwargs))
        assert kwargs["allow_redirects"] is False and len(codes) <= 100
        if len(calls) == fail_call and kind == "transport":
            raise OSError("fake transport failure")
        when = clock.now() - timedelta(seconds=1)
        lines = []
        for index, code in enumerate(codes):
            if kind == "members" and index == 0:
                continue
            if kind == "members" and index == 1:
                lines.append(f'var hq_str_{code}="";')
                continue
            fields = ["0"] * 34
            timestamp = when - timedelta(seconds=125) if kind == "members" and index == 3 else when
            for field, value in {0: "*ST_当前名", 1: "0.000" if kind == "members" and index == 2 else "10.00100",
                                 2: "9.80000", 3: "10.12340", 4: "10.23450", 5: "9.98110", 8: "000100", 9: "001000.123000",
                                 30: timestamp.strftime("%Y-%m-%d"), 31: timestamp.strftime("%H:%M:%S"),
                                 32: "03" if kind == "members" and index == 2 else "00", 33: ""}.items():
                fields[field] = value
            lines.append(f'var hq_str_{code}="' + ",".join(fields) + '";')
        status = 200
        if len(calls) == fail_call:
            if kind == "duplicate":
                lines.append(lines[0])
            elif kind == "unrequested":
                lines.append('var hq_str_sh999999="";')
            elif kind == "empty":
                lines = []
            elif kind == "non200":
                status = 302
        clock.value += timedelta(seconds=advance)
        return Response("\n".join(lines).encode("gb18030"), status)
    return get, calls


def execute(value, *, clock=None, **kwargs):
    clock = clock or Clock()
    get, calls = transport(clock, **kwargs)
    before = value["registry_path"].read_bytes()
    folder, record = r.run(package=value["package"], calendar=value["calendar"], out=value["out"],
                           data_root=value["data"], _clock=clock.now, _get=get)
    assert value["registry_path"].read_bytes() == before
    return folder, record, calls


def test_lunch_gate_precedes_strict_loader_lock_out_and_latest(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    dump(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026, "closed_ranges": [], "open_sessions": []})
    def forbidden(*args, **kwargs):
        pytest.fail("Lunch must not load sources, lock, create output, or request")
    monkeypatch.setattr(r, "load_complete_close", forbidden)
    monkeypatch.setattr(r, "_collector_lock", forbidden)
    folder, record = r.run(package=tmp_path / "missing", calendar=calendar, out=tmp_path / "badout",
                           data_root=tmp_path / "none", _clock=Clock("2026-10-15T12:59:00+08:00").now, _get=forbidden)
    assert folder is None and record["quote_request_count"] == 0 and not (tmp_path / "none").exists()


def test_5224_targets_keep_original_order_background_and_one_get_per_53_batches(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch, count=5224)
    folder, record, calls = execute(value)
    targets = read(folder / "targets.json")
    assert len(calls) == record["planned_batches"] == record["completed_batches"] == record["quote_request_count"] == 53
    assert [len(call[1]) for call in calls] == [100] * 52 + [24]
    assert len(targets["targets"]) == len(record["quotes"]) == record["target_count"] == 5224
    assert [target["symbol"] for target in targets["targets"]] == value["ordinary"].symbol.tolist()
    assert targets["targets"][0]["symbol"] == "600000.SSE"  # No canonical re-sort.
    assert [target["background_isST"] for target in targets["targets"]] == value["raw"].isST.tolist()
    assert [target["background_trade_status"] for target in targets["targets"]] == value["raw"].tradestatus.tolist()
    assert sum(target["in_fixed_590"] for target in targets["targets"]) == 590
    assert record["source_available_at"] == "2026-10-15T09:50:42+08:00"
    assert record["source_available_at"] != value["metadata"]["receipt"]["created_at"]
    assert record["status"] == r.SUCCESS and record["current_quote_count"] == 5224
    assert len(value["loads"]) == 1
    quote = record["quotes"][0]
    assert quote["last_price_rmb"] == "10.12340" and quote["source_raw_amount"] == "001000.123000"
    assert quote["roster_name"] == "旧名0" and quote["name_changed_from_dated_roster"] is True
    assert "registered_name" not in quote and "frozen_labels" not in quote and "registered_at" not in record
    assert all(record[key] is flag for key, flag in r.FLAGS.items())
    pointer = read(folder.parent / "latest.json")
    assert pointer["schema"] == r.LATEST_SCHEMA and pointer["receipt_file"] == str(folder / "receipt.json")
    assert pointer["targets_sha256"] == r.digest(folder / "targets.json")
    for batch in record["batches"]:
        assert r.digest(batch["raw_file"]) == batch["raw_sha256"]


def test_individual_missing_empty_halt_and_stale_keep_the_full_denominator(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    _, record, calls = execute(value, kind="members")
    assert record["status"] == r.SUCCESS and len(calls) == 3
    assert record["target_count"] == len(record["quotes"]) == 205 and record["unknown_quote_count"] == 12
    assert [quote["status"] for quote in record["quotes"][:4]] == ["unknown_missing_assignment", "unknown_empty_payload",
        "unknown_nonregular_provider_status", "unknown_source_age_over_120_seconds"]
    assert record["quotes"][2]["open_price_rmb"] == "0.000"


@pytest.mark.parametrize("kind", ["transport", "duplicate", "unrequested", "non200", "empty"])
def test_batch_failure_stops_remaining_and_latest_never_reverts_to_old_success(tmp_path, monkeypatch, kind):
    value = fixture(tmp_path, monkeypatch)
    dump(value["out"].parent / "latest.json", {"old": "success"})
    folder, record, calls = execute(value, kind=kind)
    assert record["status"] != r.SUCCESS and len(calls) == 2
    assert record["completed_batches"] == 1 and record["all_batches_completed"] is False
    assert len(record["quotes"]) == 205
    assert all(quote["status"] == "unknown_failed_batch" for quote in record["quotes"][100:200])
    assert all(quote["status"] == "unknown_not_attempted" for quote in record["quotes"][200:])
    pointer = read(folder.parent / "latest.json")
    assert pointer["snapshot_status"] == record["status"] and pointer["receipt_file"] == str(folder / "receipt.json")
    if kind != "transport":
        assert record["batches"][1]["raw_sha256"] and record["response_received_at"]


@pytest.mark.parametrize("problem", ["not_previous", "calendar_mismatch", "future_completion"])
def test_source_previous_session_calendar_and_actual_completion_are_mandatory(tmp_path, monkeypatch, problem):
    value = fixture(tmp_path, monkeypatch)
    if problem == "not_previous":
        value["metadata"]["plan"]["day"] = "2026-10-13"
    elif problem == "calendar_mismatch":
        value["metadata"]["plan"]["calendar_sha256"] = "wrong"
    else:
        value["metadata"]["receipt"]["updated_at"] = "2026-10-15T14:00:00+08:00"
    with pytest.raises(ValueError):
        execute(value)
    assert not value["out"].exists() and not (value["out"].parent / "latest.json").exists()


def test_slot_crossing_stops_after_raw_is_preserved(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    _, record, calls = execute(value, clock=Clock("2026-10-15T14:59:59+08:00"), advance=2)
    assert len(calls) == 1 and record["status"] != r.SUCCESS and record["completed_batches"] == 0
    assert record["batches"][0]["raw_sha256"]


def test_existing_out_and_another_wide_worker_are_rejected_without_http(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    get, calls = transport(Clock())
    with r._collector_lock(value["out"].parent):
        with pytest.raises(RuntimeError, match="busy"):
            r.run(package=value["package"], calendar=value["calendar"], out=value["out"], data_root=value["data"], _clock=Clock().now, _get=get)
    value["out"].mkdir()
    with pytest.raises(ValueError, match="already exists"):
        r.run(package=value["package"], calendar=value["calendar"], out=value["out"], data_root=value["data"], _clock=Clock().now, _get=get)
    assert calls == [] and not value["loads"]


def test_out_must_be_a_new_single_child_of_its_own_fixed_root(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    for out in (tmp_path / "elsewhere", value["out"] / "nested"):
        with pytest.raises(ValueError, match="directly under"):
            r.run(package=value["package"], calendar=value["calendar"], out=out, data_root=value["data"], _clock=Clock().now)
    assert not value["loads"]


def test_pure_parser_registration_vocabulary_is_removed_from_wide_roster_observation():
    target = {"symbol": "000001.SZSE", "roster_name": "旧名"}
    quote = r._member(r._member_input(target), 0)
    quote.update(status="unknown_source_time_precedes_registration", unknown_reason="source_time_precedes_registration")
    adapted = r._adapt(quote, target)
    assert adapted["status"] == "unknown_source_time_precedes_roster_availability"
    assert adapted["unknown_reason"] == "source_time_precedes_roster_availability"
    assert "registered_name" not in adapted and "frozen_labels" not in adapted
