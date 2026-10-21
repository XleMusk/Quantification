"""New multi-year boundaries using tiny saved caches and the existing Engine."""
import json

import pandas as pd
import pytest

from trading.research import native_lowvol_replay as replay
from trading.research import saved_lowvol_signals as saved
from trading.research.baostock_daily_slice import digest
from trading.research.stockdb_action_accounting import StockDBActionAccounting
from trading.research.stockdb_rolling_low_volatility import portfolio


def write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def calendars(tmp_path):
    return [write(tmp_path / f"calendar{year}.json", {
        "schema": "cn_a_share_exchange_calendar_v1", "year": year,
        "closed_ranges": [f"{year}-01-01/{year}-01-01"], "open_sessions": []})
        for year in (2025, 2026)]


@pytest.fixture
def cache(tmp_path):
    snapshot, artifacts = tmp_path / "snapshot", tmp_path / "artifacts"
    snapshot.mkdir()
    artifacts.mkdir()
    bars = snapshot / "bars.parquet"
    pd.DataFrame({"code": ["000001", "600000", "000002"]}).to_parquet(bars, index=False)
    manifest = {"status": "complete_inventory_only", "chunks_expected": 1,
        "execution_admitted": False, "chunks": [{"parquet_file": bars.name, "parquet_sha256": digest(bars)}]}
    write(snapshot / "manifest.json", manifest)
    days = ["2025-12-30", "2025-12-31", "2026-01-02", "2026-01-05"]
    run = {"schema": "stockdb_rolling_low_volatility_ledger_v1", "status": "completed_research_only",
        "research_only": True, "execution_admitted": False, "variants": [saved.VARIANT],
        "config": portfolio().to_dict(), "replay_start": days[0], "replay_end": days[-1],
        "symbols": 3, "sessions": len(days), "supplemental_raw_view": None, "artifacts": str(artifacts)}
    run_path = write(tmp_path / "run.json", run)
    write(artifacts / "protocol.json", {**run, "snapshot": str(snapshot), "snapshot_manifest": manifest,
        "lookback": 252, "skip": 21, "rank_cadence": "monthly_completed_session_end"})
    pd.DataFrame([{"date": "2025-12-31", "variant": saved.VARIANT, "rank": index + 1,
        "symbol": symbol, "volatility": volatility, "momentum": .2, "median_amount": 1e8}
        for index, (symbol, volatility) in enumerate([("600000.SSE", .4), ("000001.SZSE", .2)])]
        ).to_csv(artifacts / f"{saved.VARIANT}_ranks.csv", index=False)
    pd.DataFrame([{"date": day, "variant": saved.VARIANT,
        "decision": "monthly_ranking" if day == "2025-12-31" else "not_month_end",
        "eligible_count": 2 if day == "2025-12-31" else None,
        "candidate_count": 2 if day == "2025-12-31" else None} for day in days]
        ).to_csv(artifacts / f"{saved.VARIANT}_decisions.csv", index=False)
    return run_path, days


def test_multiyear_calendar_pins_closed_days_open_overrides_and_exact_year_boundary(tmp_path):
    prior, current = calendars(tmp_path)
    payload = json.loads(prior.read_text(encoding="utf-8"))
    payload["open_sessions"] = ["2025-12-27"]
    write(prior, payload)
    fixed, proof = replay._multiyear_calendar(current, [prior], "2025-12-30", "2026-01-05")
    assert fixed == sorted(set(fixed)) and "2025-12-27" in fixed
    assert "2026-01-01" not in fixed
    assert fixed[fixed.index("2025-12-31") + 1] == "2026-01-02"
    assert fixed[fixed.index("2025-12-30") - 1] == "2025-12-29"
    assert proof["years"] == [2025, 2026] and proof["missing_years_inferred"] is False
    assert proof["files"] == [{**replay._reference(prior), "year": 2025},
                              {**replay._reference(current), "year": 2026}]


def test_multiyear_calendar_rejects_missing_duplicate_and_wrong_years(tmp_path):
    prior, current = calendars(tmp_path)
    year2024 = write(tmp_path / "calendar2024.json", {
        "schema": "cn_a_share_exchange_calendar_v1", "year": 2024,
        "closed_ranges": [], "open_sessions": []})
    for preceding, last in (([prior, prior], current), ([year2024], current), ([prior], prior)):
        with pytest.raises(ValueError, match="unique|continuous|matching"):
            replay._multiyear_calendar(last, preceding, "2025-12-30", "2026-01-05")
    malformed = write(tmp_path / "bad_dates.json", {
        "schema": "cn_a_share_exchange_calendar_v1", "year": 2025,
        "closed_ranges": ["2024-12-31/2024-12-31"], "open_sessions": []})
    with pytest.raises(ValueError, match="declared year"):
        replay._multiyear_calendar(current, [malformed], "2025-12-30", "2026-01-05")


def test_legacy_single_year_default_stays_strict_without_inception_context(tmp_path, cache):
    prior, current = calendars(tmp_path)
    run_path, days = cache
    fixed, _ = replay._multiyear_calendar(current, [prior], days[0], days[-1])
    with pytest.raises(ValueError, match="preceding original observed"):
        saved.load_saved_lowvol_signals(run_path, days[0], days[-1], fixed)
    with pytest.raises(ValueError, match="One explicit ordered replay year"):
        replay.run(run_path, ["native"], "events", "refs", current, "master", days[0], days[-1], tmp_path / "legacy")
    assert not (tmp_path / "legacy").exists()
    # Existing same-year calls still use the original observed previous decision.
    result = saved.load_saved_lowvol_signals(run_path, "2026-01-02", days[-1], fixed)
    assert result.preceding_session == "2025-12-31"
    assert result.provenance["preceding_session_basis"] == "exact_previous_original_observed_decision_day"
    assert "inception_context_method" not in result.provenance


def test_first_saved_source_context_uses_exact_predecessor_without_reranking_or_old_signal(tmp_path, cache):
    prior, current = calendars(tmp_path)
    run_path, days = cache
    fixed, _ = replay._multiyear_calendar(current, [prior], days[0], days[-1])
    files = [run_path, *list((tmp_path / "artifacts").iterdir()), *list((tmp_path / "snapshot").iterdir())]
    before = {path: path.read_bytes() for path in files}
    result = saved.load_saved_lowvol_signals(run_path, days[0], days[-1], fixed,
        inception_context=saved.MULTIYEAR_INCEPTION_CONTEXT)
    assert result.preceding_session == "2025-12-29"
    assert result.symbols == ["000001.SZSE", "000002.SZSE", "600000.SSE"]
    assert [row["symbol"] for row in result.signals["2025-12-31"]] == ["600000.SSE", "000001.SZSE"]
    assert [row["score"] for row in result.signals["2025-12-31"]] == [-.4, -.2]
    assert result.preceding_session not in result.signals and result.preceding_session not in result.decisions
    assert result.provenance["feature_recomputed"] is result.provenance["ranks_recomputed"] is False
    assert result.provenance["prior_pending_signal_invented"] is False
    assert {path: path.read_bytes() for path in files} == before
    with pytest.raises(ValueError, match="explicit multi-year"):
        saved.load_saved_lowvol_signals(run_path, days[0], "2025-12-31", fixed,
            inception_context=saved.MULTIYEAR_INCEPTION_CONTEXT)


def test_multiyear_engine_keeps_december_target_into_january_and_rejects_resume(tmp_path, monkeypatch, cache):
    prior, current = calendars(tmp_path)
    run_path, days = cache
    symbols = ["000001.SZSE", "000002.SZSE", "600000.SSE"]
    raw_days = ["2025-12-29"] + days
    raw = pd.DataFrame([{"date": day, "symbol": symbol,
        **{field: 10. for field in ("open", "high", "low", "close", "preclose")},
        "volume": 1e8, "amount": 1e9, "tradestatus": 1, "isST": 0, "adjustflag": "3"}
        for day in raw_days for symbol in symbols])
    universe = pd.DataFrame({"code": ["sz.000001", "sz.000002", "sh.600000"], "symbol": symbols})
    master = tmp_path / "master.csv"
    pd.DataFrame([{"code": code, "type": "1", "ipoDate": "1991-04-03", "outDate": ""}
        for code in universe.code]).to_csv(master, index=False)
    calls = []
    def quotes(views, requested_symbols, requested_days):
        assert views == ["native"] and requested_symbols == symbols and requested_days == raw_days
        calls.append("native_exact_predecessor_and_all_sessions")
        return raw.copy(), {"fixture": True}
    def actions(event_view, reference_view, **kwargs):
        assert (event_view, reference_view) == ("events", "references")
        assert kwargs == {"symbols": symbols, "raw_days": raw_days, "replay_days": days,
                          "price_source": "independent_baostock_raw"}
        return StockDBActionAccounting([], [], [],
            {(code, year): True for code in universe.code for year in (2025, 2026)},
            universe, {"rename": None}, symbols, raw_days, days, {"price_source": "independent_baostock_raw"})
    monkeypatch.setattr(replay, "load_native_execution_quotes", quotes)
    monkeypatch.setattr(replay, "load_action_accounting", actions)
    out = tmp_path / "multi"
    result = replay.run(run_path, ["native"], "events", "references", current, master,
        days[0], days[-1], out, prior_calendars=[prior])
    assert result["schema"] == replay.MULTIYEAR_METHOD and result["status"] == "completed"
    assert result["completed_sessions"] == 4 and result["orders"] == 2
    daily = pd.read_csv(out / "daily.csv")
    assert daily.date.tolist() == days and daily.iloc[0].cash == portfolio().initial_cash
    fills = pd.read_csv(out / "fills.csv")
    assert fills.day.unique().tolist() == ["2026-01-02"] and fills.qty.sum() == 1000
    assert calls == ["native_exact_predecessor_and_all_sessions"]
    state = json.loads((out / "account.json").read_text(encoding="utf-8"))
    assert set(state["positions"]) == {"000001.SZSE", "600000.SSE"}
    registration = result["registration"]
    assert registration["config"] == portfolio().to_dict()
    assert registration["calendar_method"]["years"] == [2025, 2026]
    assert registration["continuation_supported"] is registration["account_year_boundary_reset"] is False
    assert registration["signal_source"]["prior_pending_signal_invented"] is False
    assert state["full_exit_lifecycle"]["latest_target"]["source"] == "research:" + replay.MULTIYEAR_METHOD
    resume_out = tmp_path.parent / (tmp_path.name + "_resume")
    for kwargs in ({"prior_calendars": [prior]}, {}):
        with pytest.raises(ValueError, match="continuation is not implemented"):
            replay.run(run_path, ["native"], "events", "references", current, master,
                days[0], days[-1], resume_out, resume_from=out, **kwargs)
    assert not resume_out.exists()
    monkeypatch.setattr(replay, "load_native_execution_quotes", lambda *args:
        (raw.loc[raw.date.ne(raw_days[0])].copy(), {"fixture": True}))
    with pytest.raises(ValueError, match="real native rows"):
        replay.run(run_path, ["native"], "events", "references", current, master,
            days[0], days[-1], tmp_path / "missing_context", prior_calendars=[prior])
    assert not (tmp_path / "missing_context").exists()
