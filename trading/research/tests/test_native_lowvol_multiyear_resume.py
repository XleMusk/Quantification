"""Synthetic stopped-account recovery through the original shared Engine."""
from copy import deepcopy
import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.research import native_lowvol_replay as replay
from trading.research import saved_lowvol_signals as saved
from trading.research.baostock_daily_slice import digest
from trading.research.baostock_native_history import RAW_FIELDS
from trading.research.native_history_source import NativeHistorySource
from trading.research.stockdb_rolling_low_volatility import portfolio


DAYS = ["2025-12-30", "2025-12-31", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
RAW_DAYS = ["2025-12-29"] + DAYS
SYMBOLS = ["000001.SZSE", "000002.SZSE", "600000.SSE"]
CODES = ["sz.000001", "sz.000002", "sh.600000"]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def action_pair(root, *, future_reference=False, cash="0.10"):
    events, references, source = [root / name for name in ("events", "references", "source")]
    for directory in (events, references, source):
        directory.mkdir(parents=True)
    source_result = {"status": "source_actions_complete", "raw_view": str(root / "raw_identity"),
                     "raw_daily_sha256": "a" * 64}
    write(source / "result.json", source_result)
    write(events / "source_result.json", source_result)
    write(events / "price_identity.json", {"status": "derived_research_levels", "rename": None,
        "raw_view": source_result["raw_view"]})
    pd.DataFrame({"code": CODES, "symbol": SYMBOLS}).to_csv(events / "source_input_universe.csv", index=False)
    pd.DataFrame({"date": RAW_DAYS}).to_csv(events / "source_input_calendar.csv", index=False)
    pd.DataFrame([{"code": code, "symbol": symbol, "year": year, "queried": True,
        "rows": 1, "empty_response": False, "source_file": "synthetic.csv", "source_sha256": "b" * 64}
        for code, symbol in zip(CODES, SYMBOLS) for year in (2025, 2026)]
        ).to_csv(events / "input_coverage.csv", index=False)
    action = {"code": "sh.600000", "symbol": "600000.SSE", "dividRegistDate": DAYS[2],
        "dividOperateDate": DAYS[3], "dividPayDate": DAYS[5], "dividStockMarketDate": DAYS[5],
        "dividCashPsBeforeTax": cash, "dividStocksPs": "0", "dividReserveToStockPs": "0"}
    pd.DataFrame([action]).to_parquet(events / "canonical_actions.parquet", index=False)
    event_result = {"status": "research_action_events_projected", "source_status": "source_actions_complete",
        "execution_admitted": False, "source_view": str(source), "raw_view": source_result["raw_view"],
        "start": RAW_DAYS[0], "end": DAYS[-1], "expected_codes": 3, "queried": 6, "unqueried": 0,
        "canonical_events": 1, "files": {path.name: digest(path) for path in events.iterdir()}}
    write(events / "result.json", event_result)
    (references / "input_events.json").write_bytes((events / "result.json").read_bytes())
    write(references / "input_prices.json", {"status": "research_reconstruction_completed",
        "raw_view": source_result["raw_view"], "source_hashes": {"raw": "a" * 64},
        "canonical_panel_sha256": "c" * 64})
    special = [{"symbol": "000001.SZSE", "date": DAYS[4], "kind": "restructuring",
        "shares_before": "100", "debt_shares": "10", "investor_shares": "0", "holder_shares": "0",
        "debt_cash_equivalent": "45", "investor_cash": "0", "cash_dividend": "0"}] if future_reference else []
    write(references / "reviewed_special_references.json", special)
    for name in ("cash_reference_evidence.json", "distribution_reference_evidence.json"):
        write(references / name, [])
    write(references / "result.json", {"status": "research_reference_presence_diagnostic",
        "execution_admitted": False, "event_view": str(events), "raw_daily_sha256": "a" * 64,
        "canonical_panel_sha256": "c" * 64,
        "files": {path.name: digest(path) for path in references.iterdir()}})
    return events, references


@pytest.fixture
def stopped(tmp_path):
    """Six sessions, including a cash receivable and one unresolved future reference."""
    snapshots, artifacts, native = [tmp_path / name for name in ("snapshot", "artifacts", "native")]
    for directory in (snapshots, artifacts, native):
        directory.mkdir()
    bars = snapshots / "bars.parquet"
    pd.DataFrame({"code": ["000001", "000002", "600000"]}).to_parquet(bars, index=False)
    manifest = {"status": "complete_inventory_only", "execution_admitted": False,
        "chunks_expected": 1, "chunks": [{"parquet_file": bars.name, "parquet_sha256": digest(bars)}]}
    write(snapshots / "manifest.json", manifest)
    run = {"schema": "stockdb_rolling_low_volatility_ledger_v1", "status": "completed_research_only",
        "research_only": True, "execution_admitted": False, "variants": [saved.VARIANT],
        "config": portfolio().to_dict(), "replay_start": DAYS[0], "replay_end": DAYS[-1],
        "completed_at": "2026-01-07T18:00:00+08:00", "symbols": 3, "sessions": len(DAYS),
        "supplemental_raw_view": None, "artifacts": str(artifacts)}
    signal_directory = tmp_path / "signal_cache"
    signal_directory.mkdir()
    run_path = write(signal_directory / "run.json", run)
    write(artifacts / "protocol.json", {**run, "snapshot": str(snapshots), "snapshot_manifest": manifest,
        "lookback": 252, "skip": 21, "rank_cadence": "monthly_completed_session_end"})
    pd.DataFrame([{"date": DAYS[1], "variant": saved.VARIANT, "rank": index + 1,
        "symbol": symbol, "volatility": .1 + index * .01, "momentum": .2, "median_amount": 1e8}
        for index, symbol in enumerate((SYMBOLS[0], SYMBOLS[2]))]
        ).to_csv(artifacts / f"{saved.VARIANT}_ranks.csv", index=False)
    pd.DataFrame([{"date": day, "variant": saved.VARIANT,
        "decision": "monthly_ranking" if day == DAYS[1] else "not_month_end",
        "eligible_count": 2 if day == DAYS[1] else None,
        "candidate_count": 2 if day == DAYS[1] else None} for day in DAYS]
        ).to_csv(artifacts / f"{saved.VARIANT}_decisions.csv", index=False)
    calendars = [write(tmp_path / f"calendar{year}.json", {
        "schema": "cn_a_share_exchange_calendar_v1", "year": year,
        "closed_ranges": [f"{year}-01-01/{year}-01-01"], "open_sessions": []}) for year in (2025, 2026)]
    master = tmp_path / "master.csv"
    pd.DataFrame([{"code": code, "type": "1", "ipoDate": "1991-04-03", "outDate": ""}
        for code in CODES]).to_csv(master, index=False)
    native_calendar = tmp_path / "native_calendar.csv"
    pd.DataFrame({"date": RAW_DAYS}).to_csv(native_calendar, index=False)
    tasks, groups = {}, []
    for day in RAW_DAYS:
        rows = []
        for code in CODES:
            price = "9.9" if code == "sh.600000" and day >= DAYS[3] else (
                "9.5" if code == "sz.000001" and day >= DAYS[4] else "10")
            row = {field: "" for field in RAW_FIELDS}
            row.update(date=day, code=code, open=price, high=price, low=price, close=price,
                preclose=price, volume="100000000", amount="1000000000", adjustflag="3",
                tradestatus="1", isST="0")
            rows.append(row)
        groups.append(rows)
        path = native / f"{day}_stock.csv"
        pd.DataFrame(rows, columns=RAW_FIELDS).to_csv(path, index=False)
        tasks[day] = {"date": day, "kind": "stock", "method": "query_daily_history_k_AStock",
            "file": path.name, "sha256": digest(path), "error_code": "0", "quality": {"rows": 3}}
    schema = pa.schema([(field, pa.string()) for field in RAW_FIELDS])
    with pq.ParquetWriter(native / "raw_daily.parquet", schema) as writer:
        for rows in groups:
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
    write(native / "plan.json", {"kind": "stock", "days": RAW_DAYS,
        "requested_start": RAW_DAYS[0], "requested_end": RAW_DAYS[-1],
        "calendar_source": str(native_calendar), "calendar_sha256": digest(native_calendar),
        "execution_admitted": False, "historical_universe_independently_verified": False})
    write(native / "receipts.json", {"status": "source_cache_complete", "execution_admitted": False,
        "expected_days": len(RAW_DAYS), "updated_at": "2026-01-07T18:00:00+08:00", "tasks": tasks, "failures": []})
    write(native / "result.json", {"status": "source_cache_complete", "kind": "stock",
        "execution_admitted": False, "strategy_validated": False, "panel_file": "raw_daily.parquet",
        "missing_days": [], "expected_days": len(RAW_DAYS), "acquired_days": len(RAW_DAYS),
        "rows": len(RAW_DAYS) * 3, "symbols": 3, "raw_daily_sha256": digest(native / "raw_daily.parquet"),
        "panel_sha256": digest(native / "raw_daily.parquet"), "plan_sha256": digest(native / "plan.json"),
        "receipts_sha256": digest(native / "receipts.json")})
    old_pair = action_pair(tmp_path / "old_pair")
    old = tmp_path / "stopped"
    original_args = (run_path, [native], *old_pair, calendars[1], master, DAYS[0], DAYS[-1])
    result = replay.run(*original_args, old, prior_calendars=[calendars[0]])
    assert result["status"] == "incomplete" and result["completed_sessions"] == 4
    assert result["error"]["day"] == DAYS[4] and "missing_source_event" in result["error"]["message"]
    state = read(old / "account.json")
    assert state["receivables"] and state["positions"]
    new_pair = action_pair(tmp_path / "new_pair", future_reference=True)
    args = (run_path, [native], *new_pair, calendars[1], master, DAYS[4], DAYS[-1])
    return {"args": args, "old_args": original_args, "old": old, "prior": calendars[0],
        "state": state, "old_pair": old_pair, "native": native, "calendars": calendars,
        "original_files": {path: path.read_bytes() for path in old.iterdir()}}


def test_resume_reuses_committed_account_receivable_full_scope_and_only_remaining_sessions(stopped, tmp_path, monkeypatch):
    from trading.research import native_lowvol_multiyear_resume as recovery
    accesses, indices = [], []
    real_frame, real_advance = NativeHistorySource.frame, recovery.advance
    def frame(source, day, symbols):
        accesses.append((day, list(symbols)))
        return real_frame(source, day, symbols)
    def advance(state, feed, day, config, index, **kwargs):
        indices.append(index)
        return real_advance(state, feed, day, config, index, **kwargs)
    monkeypatch.setattr(NativeHistorySource, "frame", frame)
    monkeypatch.setattr(recovery, "advance", advance)
    monkeypatch.setattr(replay, "new_state", lambda *a, **k: pytest.fail("Recovery must not reset cash"))
    output = tmp_path / "continued"
    result = recovery.resume(*stopped["args"], output, resume_from=stopped["old"], prior_calendars=[stopped["prior"]])
    assert result["status"] == "completed" and result["completed_sessions"] == len(DAYS)
    assert result["reused_sessions"] == 4 and result["newly_completed_sessions"] == 2
    assert indices == [4, 5]
    assert accesses == [(day, SYMBOLS) for day in DAYS[3:]]
    after = read(output / "account.json")
    old = stopped["state"]
    assert after["day"] == DAYS[-1] and after["positions"] == old["positions"]
    assert after["cash"] == old["cash"] + sum(old["receivables"].values())
    assert after["receivables"] == {} and after["entitlements"] == old["entitlements"]
    assert after["orders"] == old["orders"] and after["fills"] == old["fills"]
    assert (output / "daily.csv").read_bytes().startswith(stopped["original_files"][stopped["old"] / "daily.csv"])
    assert (output / "prior_action_reference_checks.csv").read_bytes() == stopped["original_files"][stopped["old"] / "action_reference_checks.csv"]
    assert all(path.read_bytes() == data for path, data in stopped["original_files"].items())
    assert result["execution_admitted"] is result["returns_reportable"] is False


def test_readonly_first_session_probe_preserves_unknown_reference_without_advance_or_output(stopped, tmp_path, monkeypatch):
    from trading.research import native_lowvol_multiyear_resume as recovery
    accesses = []
    real_frame = NativeHistorySource.frame
    def frame(source, day, symbols):
        accesses.append(day)
        return real_frame(source, day, symbols)
    monkeypatch.setattr(NativeHistorySource, "frame", frame)
    monkeypatch.setattr(recovery, "advance", lambda *a, **k: pytest.fail("Read-only inspection cannot advance"))
    args = list(stopped["args"])
    args[2:4] = stopped["old_pair"]
    output = tmp_path / "never_created"
    result = replay.run(*args, output, resume_stopped_multiyear=stopped["old"],
        prior_calendars=[stopped["prior"]], inspect_only=True)
    assert result["status"] == "blocked_stopped_multiyear"
    assert result["first_session_readiness_only"] is True
    assert "missing_source_event" in json.dumps(result)
    assert accesses == DAYS[3:5] and not output.exists()
    assert all(path.read_bytes() == data for path, data in stopped["original_files"].items())


@pytest.mark.parametrize("fault", ["calendar", "config", "source", "economic_past", "overlap", "skip_day",
    "daily_cash", "engine_pin", "result_bool"])
def test_recovery_rejects_changed_inputs_or_output_overlap_without_account_write(stopped, tmp_path, fault):
    from trading.research import native_lowvol_multiyear_resume as recovery
    args = list(stopped["args"])
    output = tmp_path / "refused"
    if fault == "calendar":
        payload = read(stopped["prior"])
        payload["open_sessions"] = ["2025-12-27"]
        write(stopped["prior"], payload)
    elif fault == "config":
        payload = read(args[0])
        payload["config"]["holdings"] += 1
        write(args[0], payload)
    elif fault == "source":
        args[1] = [tmp_path / "replacement_native"]
    elif fault == "economic_past":
        args[2:4] = action_pair(tmp_path / "past_changed", future_reference=True, cash="0.20")
    elif fault == "overlap":
        output = stopped["old"] / "child"
    elif fault == "skip_day":
        args[6] = DAYS[-1]
    elif fault == "daily_cash":
        path = stopped["old"] / "daily.csv"
        daily = pd.read_csv(path)
        daily.loc[daily.index[-1], "cash"] += .01
        daily.to_csv(path, index=False)
    elif fault == "engine_pin":
        path = stopped["old"] / "registration.json"
        registration = read(path)
        engine_pin = next(ref for ref in registration["implementation"] if ref["path"].endswith("engine.py"))
        engine_pin["sha256"] = "0" * 64
        write(path, registration)
        path = stopped["old"] / "result.json"
        result = read(path)
        result["registration"] = deepcopy(registration)
        write(path, result)
    elif fault == "result_bool":
        path = stopped["old"] / "result.json"
        result = read(path)
        result["execution_admitted"] = 0
        write(path, result)
    # The corruption is intentional input; recovery must cause no further write.
    if fault in {"daily_cash", "engine_pin", "result_bool"}:
        stopped["original_files"] = {path: path.read_bytes() for path in stopped["old"].iterdir()}
    expected = {"daily_cash": "committed account/daily prefix", "engine_pin": "pinned.*input changed",
        "result_bool": "stopped unadmitted multi-year"}.get(fault)
    with pytest.raises((ValueError, FileExistsError), match=expected):
        recovery.resume(*args, output, resume_from=stopped["old"], prior_calendars=[stopped["prior"]])
    assert not output.exists()
    assert all(path.read_bytes() == data for path, data in stopped["original_files"].items())
