"""New pair orchestration/clock fixtures, never saved market prices or PnL."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.research import trend_atr_stop_replay as m


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def source(tmp_path):
    root = tmp_path / "source"
    inputs = {key: str(root / key) for key in ("price_view", "event_view", "reference_view")}
    inputs["listing_master"] = str(root / "master.csv")
    root.mkdir()
    Path(inputs["listing_master"]).write_text("code,ipoDate,outDate\n", encoding="utf-8")
    calendars, sessions = [], []
    closures = ["2024-01-01/2024-01-01", "2024-02-09/2024-02-17", "2024-04-04/2024-04-06",
        "2024-05-01/2024-05-05", "2024-06-10/2024-06-10", "2024-09-15/2024-09-17", "2024-10-01/2024-10-07"]
    for year in (2024, 2025):
        payload = {"schema": "cn_a_share_exchange_calendar_v1", "year": year,
            "closed_ranges": closures if year == 2024 else ["2025-01-01/2025-01-01"],
            "source_status": "official_static_schedule_snapshot"}
        calendars.append(save(root / f"calendar{year}.json", payload))
        sessions += m._calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])[0]
    days = [day for day in sessions if day <= m.END] + [day for day in sessions if day > m.END][:6]
    for folder in inputs.values():
        if not folder.endswith(".csv"):
            Path(folder).mkdir()
    raw = root / "raw"
    raw.mkdir()
    payloads = [(Path(inputs["price_view"]) / "canonical_daily.parquet"), raw / "raw_daily.parquet",
        Path(inputs["event_view"]) / "canonical_actions.parquet", Path(inputs["reference_view"]) / "raw_reference_changes.parquet"]
    for path in payloads:
        path.write_bytes(b"not a parquet: register must only inspect metadata")
    price = {"status": "research_reconstruction_completed", "raw_view": str(raw),
        "canonical_panel_file": payloads[0].name, "canonical_panel_sha256": m.digest(payloads[0])}
    event = {"status": "research_action_events_projected", "raw_view": str(raw), "unqueried": 0,
        "files": {payloads[2].name: m.digest(payloads[2])}}
    reference = {"status": "research_reference_presence_diagnostic", "price_view": inputs["price_view"],
        "event_view": inputs["event_view"], "files": {payloads[3].name: m.digest(payloads[3])}}
    for key, value in zip(("price_view", "event_view", "reference_view"), (price, event, reference)):
        save(Path(inputs[key]) / "result.json", value)
    save(raw / "result.json", {"panel_file": payloads[1].name, "panel_sha256": m.digest(payloads[1])})
    pd.DataFrame({"date": ["2023-12-29"] + days}).to_csv(Path(inputs["event_view"]) / "source_input_calendar.csv", index=False)
    (Path(inputs["event_view"]) / "source_input_universe.csv").write_text("code\nsh.600001\n", encoding="utf-8")
    save(Path(inputs["reference_view"]) / "reviewed_special_references.json", [])
    run = save(root / "run.json", {"id": m.SOURCE_ID, "research_family": m.SOURCE_METHOD,
        "status": "completed_seat_bulk_diagnostic", "execution_admitted": False,
        "protocol": {"stock_inputs": inputs, "portfolio": m.portfolio().to_dict()}})
    return run, calendars


def test_registration_pins_exact_original_inputs_242_plus6_without_loading_market(tmp_path, monkeypatch):
    run, calendars = source(tmp_path)
    monkeypatch.setattr(m, "BulkResearchFeed", lambda **kwargs: pytest.fail("Registration cannot load price matrices"))
    monkeypatch.setattr(m, "BulkTrendFeed", lambda base: pytest.fail("Registration cannot recompute ATR"))
    record = m.register(run, tmp_path / "registered", calendars=calendars)
    assert len(record["source"]["decision_days"]) == 242 and len(record["source"]["tail_days"]) == 6
    assert record["source"]["tail_days"] == ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09"]
    assert record["engine_advanced"] is False and record["prices_or_ATR_loaded"] is False
    assert m._registration(tmp_path / "registered/registration.json") == record
    with pytest.raises(ValueError, match="outside original"):
        m.register(run, run.parent / "forbidden", calendars=calendars)
    run.write_text(run.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source or implementation changed"):
        m._registration(tmp_path / "registered/registration.json")


def test_run_constructs_one_source_and_ATR_then_keeps_two_case_failures_and_nulls(tmp_path, monkeypatch):
    run, calendars = source(tmp_path)
    record = m.register(run, tmp_path / "registered", calendars=calendars)
    calls = {"base": 0, "trend": 0, "close": 0, "cases": []}
    class Base:
        def __init__(self, **kwargs):
            calls["base"] += 1
            assert kwargs == record["source"]["stock_inputs"]
            self.calendar = ["2023-12-29"] + record["source"]["days"]
            self.manifest = {"fixture": True}
        def close(self):
            calls["close"] += 1
    def trend(base):
        calls["trend"] += 1
        return SimpleNamespace(base=base, symbols=["600001.SSE"], data_checks={"fixture": True})
    def case(arm, shared_trend, provider, registration, out):
        calls["cases"].append(arm)
        assert provider.trend is shared_trend
        return {"arm": arm, "status": "partial_or_unclosed_account", "complete_PnL_available": False, "metrics": None,
            "completed_sessions": 12 if arm == "matched_control" else 7,
            "blocking_evidence": {"error": "synthetic_source_unknown"},
            "residual_positions": {"600001.SSE": 100}, "engine_advanced": True, "accounts_created": True}
    monkeypatch.setattr(m, "BulkResearchFeed", Base)
    monkeypatch.setattr(m, "BulkTrendFeed", trend)
    monkeypatch.setattr(m, "_case", case)
    result = m.run(tmp_path / "registered/registration.json", tmp_path / "results")
    assert calls == {"base": 1, "trend": 1, "close": 1, "cases": list(m.ARMS)}
    assert result["status"] == "partial_paired_research_accounts" and result["all_cases_retained"]
    assert [case["metrics"] for case in result["results"]] == [None, None]
    with pytest.raises(FileExistsError):
        m.run(tmp_path / "registered/registration.json", tmp_path / "results")


def test_exact_provider_own_day_mapping_and_same_tail_boundary_without_future_lookup():
    symbol = "600001.SSE"
    days = ["2024-12-31", "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09"]
    seen = []
    class Rows:
        def get(self, day, default=None):
            seen.append(day)
            assert day == days[0], "A D snapshot cannot inspect E prices"
            return {symbol: {"close": 10.}}
    base = SimpleNamespace(day_index={day: index for index, day in enumerate(days)}, rows=Rows(),
        closes=pd.DataFrame(20., index=days, columns=[symbol]))
    trend = SimpleNamespace(base=base, symbols=[symbol], trend={"atr": np.full((7, 1), 2.),
        "regime": np.full((7, 1), 3), "trend_active": np.ones((7, 1), dtype=bool)})
    provider = m.TrendSnapshotProvider(trend, "frozen_fixture_basis")
    snapshot = provider.snapshot(days[0], symbol)
    assert snapshot["raw_to_signal_factor"] == 2. and snapshot["atr"] == 2. and snapshot["regime"] == 3
    assert seen == [days[0]] and provider.snapshot("2024-12-30", symbol) is None
    context = RunContext("tail_fixture", TradingMode.REPLAY)
    trend.signal_frame = lambda day, config, context: SignalFrame.from_legacy(
        [{"symbol": symbol, "score": 1}], day, "original_trend", context)
    window = m._WindowFeed(trend, days[1:])
    assert window.signal_frame(days[0], m.portfolio(), context).ranked_candidates()[0]["symbol"] == symbol
    assert all(isinstance(window.signal_frame(day, m.portfolio(), context), SignalUnavailable) for day in days[1:5])
    assert window.signal_frame(days[5], m.portfolio(), context).signals == ()
    assert isinstance(window.signal_frame(days[6], m.portfolio(), context), SignalUnavailable)


def test_explicit_common_study_exit_survives_unknown_stop_anchor_without_changing_binding(monkeypatch):
    days = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09"]
    context = RunContext("independent_study_end", TradingMode.REPLAY)
    window = m._WindowFeed(SimpleNamespace(), days)
    policy = m.stops.AtrEntryStopPolicy(SimpleNamespace(price_basis_id="frozen_basis", snapshot=lambda day, symbol: None))
    calls = []
    def unknown(frame, day, book, feed):
        calls.append(day)
        return SignalUnavailable("synthetic_unknown_held_entry_anchor")
    monkeypatch.setattr(policy, "filter_frame", unknown)
    feed = m._StudyStopFeed(window, policy)
    binding = deepcopy(feed.close_candidate_policy)
    empty = window.signal_frame(days[4], m.portfolio(), context)
    assert feed.close_signal_frame(days[4], m.portfolio(), context, empty, None) is empty
    assert calls == [] and feed.policy is policy
    other = SignalFrame.from_legacy([], days[3], "ordinary_empty_target", context)
    assert isinstance(feed.close_signal_frame(days[3], m.portfolio(), context, other, None), SignalUnavailable)
    assert calls == [days[3]] and feed.close_candidate_policy == binding
