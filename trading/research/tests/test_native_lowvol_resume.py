"""Synthetic completed prefix; only three new shared-Engine sessions execute."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.ledger import new_state
from trading.research import native_lowvol_replay as m
from trading.research.stockdb_action_accounting import StockDBActionAccounting
from trading.research.stockdb_rolling_low_volatility import portfolio
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return m._reference(path)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    calendar, master = tmp_path / "calendar.json", tmp_path / "master.csv"
    payload = {"year": 2026, "closed_ranges": [], "open_sessions": []}
    write(calendar, payload)
    monkeypatch.setattr(m, "load_calendar", lambda _: payload)
    symbols = ["000001.SZSE", "600018.SSE"]
    pd.DataFrame([{"code": c, "type": "1", "ipoDate": "2000-01-01", "outDate": ""}
        for c in ("sz.000001", "sh.600018")]).to_csv(master, index=False)
    config = portfolio()
    cache = tmp_path / "cache"
    manifest = {"chunks": [], "synthetic_cache_only": True}
    write(cache / "snapshot/manifest.json", manifest)
    decisions = [{"date": day, "variant": "low_volatility_252_20", "decision": "not_month_end",
        "eligible_count": None, "candidate_count": None} for day in ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")]
    pd.DataFrame(decisions).to_csv(cache / "low_volatility_252_20_decisions.csv", index=False)
    pd.DataFrame(columns=["date", "variant", "rank", "symbol", "volatility", "momentum", "median_amount"]).to_csv(cache / "low_volatility_252_20_ranks.csv", index=False)
    run = {"schema": "stockdb_rolling_low_volatility_ledger_v1", "status": "completed_research_only",
        "research_only": True, "execution_admitted": False, "variants": ["low_volatility_252_20"],
        "config": config.to_dict(), "replay_start": "2026-01-01", "replay_end": "2026-10-16",
        "artifacts": str(cache), "sessions": 4, "symbols": 2, "supplemental_raw_view": None,
        "completed_at": "2026-10-16T18:00:00+08:00"}
    write(cache / "run.json", run)
    write(cache / "protocol.json", {**run, "snapshot": str(cache / "snapshot"), "snapshot_manifest": manifest,
        "lookback": 252, "skip": 21, "rank_cadence": "monthly_completed_session_end"})
    pins = [m._reference(cache / name) for name in ("run.json", "protocol.json", "low_volatility_252_20_ranks.csv",
        "low_volatility_252_20_decisions.csv", "snapshot/manifest.json")]
    base, extension = tmp_path / "untouched_base908", tmp_path / "extension"
    prior = {"day": "2026-10-13", "source_view": str(extension), "row_group": 3,
        "source_task": {"original": "same_task"}, "provenance": {"source_view": str(extension),
            "raw_sha256": "a" * 64, "source_available_at": "2026-10-14T12:00:00+08:00", "opened_at": "old"}}
    old_days = pd.bdate_range(end="2026-10-13", periods=185).strftime("%Y-%m-%d").tolist()
    inception_prior = pd.bdate_range(end=old_days[0], periods=2)[0].strftime("%Y-%m-%d")
    state = new_state(config.initial_cash)
    state.update(day="2026-10-13", cash=13_100_000,
        positions={symbols[1]: [{"qty": 900, "available": "2026-10-08"}]}, prices={symbols[1]: 1000})
    state["entitlements"] = {f"synthetic_prior_entitlement_{i}": i for i in range(4114)}
    state["full_exit_lifecycle"] = {"schema_version": 1, "method_id": FULL_EXIT_LIFECYCLE,
        "target_version": 9, "latest_target": {"version": 9, "decided_on": "2026-09-30",
            "decided_at": "2026-09-30T15:05:00+08:00", "execution_not_before": "2026-10-01",
            "source": "research:" + m.METHOD, "weights": {symbols[1]: .04}}, "remaining_full_exits": {}}
    original = tmp_path / "original185"
    registration = {"method_id": m.METHOD, "start": old_days[0], "end": "2026-10-13",
        "config": config.to_dict(), "signal_source": {"run": str(cache / "run.json"), "pins": pins,
            "source_symbols": 2, "scope_basis": "synthetic_full_scope_not_rank_union"},
        "execution_source": {"source_views": [{"source_view": str(base)}, {"source_view": str(extension)}],
            "day_sources": [prior], "requested_symbols": symbols, "requested_days": [inception_prior] + old_days,
            "observed_rows": 370, "missing_stock_day_rows": 0}, "calendar": m._reference(calendar),
        "listing_master": m._reference(master), "action_accounting": {"original": "pinned"},
        "full_exit_lifecycle": FULL_EXIT_LIFECYCLE, "execution_admitted": False}
    write(original / "registration.json", registration)
    write(original / "account.json", state)
    daily = [{"date": day, "equity": 140000., "cash": 131000., "receivables": 0., "holdings": 1,
        "fees": 0., "turnover_value": 0., "new_orders": 0, "stale_symbols": [], "events": [],
        "event_time_frontier": None} for day in old_days]
    pd.DataFrame(daily).to_csv(original / "daily.csv", index=False)
    write(original / "result.json", {"schema": m.METHOD, "status": "completed", "error": None,
        "registration": registration, "completed_sessions": 185, "requested_sessions": 185,
        "metric_basis": "synthetic_native_accounting"})
    prior_raw = pd.DataFrame([{"date": "2026-10-13", "symbol": s, "open": 10., "high": 10.,
        "low": 10., "close": 10., "preclose": 10., "volume": 1000000., "amount": 10000000.,
        "adjustflag": "3", "tradestatus": 1, "isST": 0} for s in symbols])
    calls = []
    def native(views, requested_symbols, days):
        assert views == [str(extension)] and requested_symbols == symbols and days == ["2026-10-13"]
        calls.append("prior13_only")
        value = deepcopy(prior)
        value["provenance"]["opened_at"] = "new_read"
        return prior_raw, {"day_sources": [value]}
    monkeypatch.setattr(m, "load_native_execution_quotes", native)
    folders = []
    for day in ("2026-10-14", "2026-10-15", "2026-10-16"):
        folder = tmp_path / ("strict_" + day)
        folder.mkdir()
        records = [{"date": day, "symbol": s, "code": "sh.600018" if s.endswith("SSE") else "sz.000001",
            **{k: "9.95" if s == symbols[1] and day >= "2026-10-15" else "10.00" for k in ("open", "high", "low", "close", "preclose")},
            "volume": "1000000", "amount": "10000000", "adjustflag": "3", "tradestatus": "1", "isST": "0"} for s in symbols]
        pd.DataFrame(records).to_csv(folder / "raw_daily.csv", index=False)
        folders.append(folder)
    def strict(folder, calendar_ref, fixed):
        frame = pd.read_csv(Path(folder) / "raw_daily.csv", dtype=str, keep_default_na=False)
        day = frame.date.unique().item()
        return {"raw": frame, "proof": {"day": day, "source_folder": str(folder),
            "raw_sha256": m._sha(Path(folder) / "raw_daily.csv"), "calendar_sha256": calendar_ref["sha256"]}}
    monkeypatch.setattr(m, "_strict_source", strict)
    action = {"symbol": symbols[1], "dividRegistDate": "2026-10-14", "dividOperateDate": "2026-10-15",
        "dividPayDate": "2026-10-16", "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.05",
        "dividStocksPs": "0", "dividReserveToStockPs": "0"}
    extension_file = tmp_path / "action_extension.json"
    write(extension_file, {"synthetic_extension": True})
    def accounting(event, reference, extension_path, **kwargs):
        assert extension_path == extension_file
        assert kwargs["raw_days"] == [inception_prior] + old_days + ["2026-10-14", "2026-10-15", "2026-10-16"]
        assert kwargs["replay_days"][0] == old_days[0]
        calls.append("explicit_action_extension")
        universe = pd.DataFrame({"code": ["sz.000001", "sh.600018"], "symbol": symbols})
        return StockDBActionAccounting([action], [], [], {(c, 2026): True for c in universe.code}, universe,
            {"rename": None}, symbols, kwargs["raw_days"], kwargs["replay_days"],
            {"price_source": "independent_baostock_raw", "synthetic_extension": True})
    monkeypatch.setattr(m, "_recent_accounting", accounting)
    monkeypatch.setattr(m, "new_state", lambda *a, **k: pytest.fail("Must not create new cash state"))
    monkeypatch.setattr("trading.research.saved_lowvol_signals.load_saved_lowvol_signals", lambda *a: pytest.fail("Must not rescan scope/saved prefix"))
    return {"saved_run": cache / "run.json", "views": [base, extension], "calendar": calendar, "master": master,
        "original": original, "folders": folders, "extension": extension_file, "state": state,
        "calls": calls, "cache": cache}


def run(inputs, out, **overrides):
    return m.run(inputs["saved_run"], inputs["views"], "base_event", "base_reference", inputs["calendar"],
        inputs["master"], "2026-10-14", "2026-10-16", out, resume_from=inputs["original"],
        raw_closes=overrides.get("folders", inputs["folders"]), recent_action_extension=inputs["extension"])


def test_resume_executes_only_three_new_global_sessions_and_keeps_entitlements_and_csv_prefix(inputs, tmp_path, monkeypatch):
    original, calls = m.advance, []
    def observed(state, feed, day, config, index, **kwargs):
        calls.append((day, index, state["cash"], len(state["entitlements"])))
        return original(state, feed, day, config, index, **kwargs)
    monkeypatch.setattr(m, "advance", observed)
    before = {p: p.read_bytes() for p in inputs["original"].iterdir()}
    out = tmp_path / "continued188"
    result = run(inputs, out)
    assert result["status"] == "completed" and result["reused_sessions"] == 185
    assert result["completed_sessions"] == 188 and result["newly_completed_sessions"] == 3
    assert [(d, i) for d, i, _, _ in calls] == [("2026-10-14", 185), ("2026-10-15", 186), ("2026-10-16", 187)]
    state = json.loads((out / "account.json").read_text())
    assert all(state["entitlements"][k] == v for k, v in inputs["state"]["entitlements"].items())
    assert state["entitlements"]["600018.SSE:2026-10-15"] == 900
    assert state["cash"] == 13_103_600 and state["full_exit_lifecycle"]["target_version"] == 9
    assert sum(l["qty"] for l in state["positions"]["600018.SSE"]) == 900
    assert result["orders"] == result["fills"] == 0 and result["new_cash_account_created"] is False
    assert (out / "daily.csv").read_bytes().startswith(before[inputs["original"] / "daily.csv"])
    assert before == {p: p.read_bytes() for p in inputs["original"].iterdir()}
    assert inputs["calls"] == ["prior13_only", "explicit_action_extension"]


def test_changed_cache_or_wrong_exact_source_order_stops_before_any_engine_session(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "advance", lambda *a, **k: pytest.fail("Bad input must not advance"))
    with pytest.raises(ValueError, match="next-session prefix"):
        run(inputs, tmp_path / "bad_order", folders=list(reversed(inputs["folders"])))
    assert not (tmp_path / "bad_order").exists()
    path = inputs["cache"] / "low_volatility_252_20_ranks.csv"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="pinned.*changed"):
        run(inputs, tmp_path / "changed_cache")
    assert not (tmp_path / "changed_cache").exists()


def test_unknown_new_close_keeps_original_prefix_and_commits_only_prior_successful_new_session(inputs, tmp_path):
    path = inputs["folders"][1] / "raw_daily.csv"
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    raw.loc[raw.symbol.eq("600018.SSE"), "close"] = ""
    raw.to_csv(path, index=False)
    out = tmp_path / "incomplete"
    result = run(inputs, out)
    state = json.loads((out / "account.json").read_text())
    assert result["status"] == "incomplete" and result["completed_sessions"] == 186
    assert result["error"]["day"] == "2026-10-15" and state["day"] == "2026-10-14"
    assert result["failed_trial_committed"] is False and state["cash"] == inputs["state"]["cash"]
    assert (out / "daily.csv").read_bytes().startswith((inputs["original"] / "daily.csv").read_bytes())
