"""Small synthetic shared-Engine mechanics, not strategy performance evidence."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import recent_shortline_panel_replay as replay
from trading.research.baostock_daily_slice import digest
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import _calendar_view


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
SESSIONS, PREVIOUS, _ = _calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14"]
A, B, C = "000001.SZSE", "600001.SSE", "300001.SZSE"


def raw():
    return pd.DataFrame([{"date": day, "code": ("sh." if symbol.startswith("6") else "sz.") + symbol[:6],
        "open": "10", "high": "10.1", "low": "9.9", "close": "10", "preclose": "10", "pctChg": "0",
        "volume": "3000000", "amount": "30000000", "tradestatus": "1", "isST": "0", "adjustflag": "3"}
        for day in DAYS for symbol in (A, B, C)])


def qualification():
    return IpoQualification({symbol: {"ipo_date": "1999-01-01", "available_at": "2026-10-08T12:00:00+08:00",
        "board": "chinext" if symbol.startswith("3") else "main", "source_basis": "synthetic"}
        for symbol in (A, B, C)}, tuple(SESSIONS), {"synthetic": True})


def selected(events=None):
    events = [(DAYS[0], A), (DAYS[1], B), (DAYS[2], A)] if events is None else events
    return pd.DataFrame([{"date": day, "symbol": symbol, "score": 1.0, "stage": 1,
        "event_id": symbol + "|" + day + "|s1", "feature_available_at": "2026-10-15T19:00:00+08:00"}
        for day, symbol in events], columns=["date", "symbol", "score", "stage", "event_id", "feature_available_at"])


def statuses():
    return {day: {"status": "complete", "stage_batches": [{"day": day, "stage": stage,
        "status": "complete", "potential_unknown_stage_members": [], "unknown_fixed_rule_fields": []}
        for stage in (1, 2, 3)]} for day in DAYS}


def case(*, rows=None, bars=None, batches=None, case_index=0):
    return replay._case(selected() if rows is None else rows, raw() if bars is None else bars,
        DAYS, SESSIONS, PREVIOUS, statuses() if batches is None else batches, qualification(),
        replay.PROTOCOL["cases"][case_index], context_anchor={"day": "2026-10-09", "closes": {A: 10., B: 10., C: 10.}})


def test_both_fixed_cases_really_use_engine_next_open_and_always_leave_metrics_null():
    bounded, uncapped = case(), case(case_index=1)
    for result in (bounded, uncapped):
        assert result["status"] == "completed_mechanical_research_only"
        assert [d["status"] for d in result["decisions"]] == ["FULL_TARGET", "FULL_TARGET", "NO_CHANGE"]
        assert result["decisions"][-1]["execution_day"] == "2026-10-15"
        assert result["decisions"][-1]["reasons"] == ["withheld_missing_exact_successor_full_source"]
        assert result["fills"] and {fill["day"] for fill in result["fills"]} <= set(DAYS[1:])
        assert result["account"]["day"] == DAYS[-1]
        assert result["config"] == replay._config(100000, 5).to_dict()
        assert result["five_session_expiry_enabled"] is False
        assert result["metrics"] is None and result["complete_PnL_available"] is False
        assert result["future_path_dropped_rows"] == 0 and result["execution_admitted"] is False
    assert bounded["orders"] == uncapped["orders"] and bounded["fills"] == uncapped["fills"]


def test_one_incomplete_stage_blocks_whole_day_without_clearing_existing_holding():
    batches = statuses()
    batches[DAYS[1]]["status"] = "incomplete"
    batches[DAYS[1]]["stage_batches"][2].update(status="incomplete", potential_unknown_stage_members=[C])
    rows = selected([(DAYS[0], A), (DAYS[1], B)])
    result = case(rows=rows, batches=batches)
    assert [d["status"] for d in result["decisions"]] == ["FULL_TARGET", "NO_CHANGE", "NO_CHANGE"]
    assert result["decisions"][1]["candidates"] == [{"symbol": B, "score": 1.0, "stage": 1}]
    assert "whole_day_stage_membership_or_fixed_rule_fields_incomplete" in result["decisions"][1]["reasons"]
    assert "no_fixed_rule_candidates_after_fixed_capacity_NO_CHANGE" in result["decisions"][2]["reasons"]
    assert {fill["symbol"] for fill in result["fills"]} == {A}
    assert all(fill["side"] == "buy" for fill in result["fills"])
    assert sum(lot["qty"] for lot in result["account"]["positions"][A]) > 0
    assert result["metrics"] is None


def test_no_candidates_is_no_change_with_zero_orders_and_null_not_zero_PnL():
    result = case(rows=selected([]))
    assert not result["orders"] and not result["fills"]
    assert all(row["status"] == "NO_CHANGE" for row in result["decisions"])
    assert result["metrics"] is None and result["complete_PnL_available"] is False


def test_selected_missing_successor_is_preserved_and_never_replaced_by_available_symbol():
    bars = raw().loc[lambda f: ~(f.date.eq(DAYS[1]) & f.code.eq("sz.000001"))]
    result = case(bars=bars)
    first = result["decisions"][0]
    assert first["status"] == "NO_CHANGE" and first["candidates"] == [{"symbol": A, "score": 1.0, "stage": 1}]
    assert any(row["symbol"] == A and row["reason"] == "raw_row_missing" for row in first["raw_issues"])
    assert result["selected_events"][0]["symbol"] == A and result["future_path_dropped_rows"] == 0
    assert not any(fill["day"] == DAYS[1] for fill in result["fills"])


def test_held_reference_unknown_stops_before_committing_session():
    bars = raw()
    bars.loc[bars.code.eq("sz.000001") & bars.date.eq(DAYS[2]), "preclose"] = "9.7"
    result = case(bars=bars)
    assert result["fills"] and result["account"]["day"] == DAYS[1]
    assert result["status"] == "blocked_diagnostic_no_complete_PnL"
    assert result["blocking_evidence"]["session_not_committed"] is True
    assert result["metrics"] is None


def test_rank_ignores_future_labels_and_never_promotes_sixth_after_capacity_gate():
    symbols = [f"00000{i}.SZSE" for i in range(1, 7)]
    frame = pd.DataFrame([{"date": DAYS[0], "symbol": symbol, "stage": 1,
        "event_id": symbol + "|" + DAYS[0] + "|s1", "fixed_rule_pass": True,
        "fixed_rule_score": 1.0, "feature_available_at": "2026-10-15T19:00:00+08:00", "hit_5": i}
        for i, symbol in enumerate(symbols)])
    before = replay._rank(frame)
    frame["hit_5"] = [999, -999, 0, 1, 4, 10000]
    after = replay._rank(frame)
    pd.testing.assert_frame_equal(before, after)
    assert after.symbol.tolist() == symbols[:5] and "hit_5" not in after
    # The cap sees the already-fixed top5; it must not refill using row six.
    capped, dropped = replay._first_seen_cap(after, 1)
    assert capped.symbol.tolist() == symbols[:1] and dropped == 4
    assert symbols[5] not in capped.symbol.tolist()


def input_files(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    panel = tmp_path / "panel"
    panel.mkdir()
    provider_folder = tmp_path / "provider"
    provider_folder.mkdir()
    raw_path = provider_folder / "raw.parquet"
    bars = raw()
    bars.to_parquet(raw_path, index=False)
    proofs = [{"day": day, "source_folder": str(tmp_path / ("source" + day)),
        "raw_daily_sha256": "raw" + day, "source_completed_at": f"2026-10-15T18:00:0{i}+08:00"}
        for i, day in enumerate(DAYS)]
    physical = {"source_proofs": proofs, "history_snapshot": str(tmp_path / "oldhistory"),
        "original_history_manifest_sha256": "oldhistory-pin", "base_history_end": "2026-10-09",
        "recent_raw_observations_file": str(raw_path), "recent_raw_observations_sha256": digest(raw_path)}
    provider_path = provider_folder / "result.json"
    provider_path.write_text(json.dumps(physical), encoding="utf-8")
    q = qualification()
    provider = {"path": str(provider_path), "record": physical, "raw": bars, "days": DAYS,
        "anchor": {"day": "2026-10-09", "closes": {A: 10., B: 10., C: 10.}}, "anchor_proof": {"partitions": []}}
    monkeypatch.setattr(replay, "_provider", lambda *args: provider)
    previews, rows = [], []
    for i, day in enumerate(DAYS):
        folder = tmp_path / ("preview" + str(i))
        folder.mkdir()
        created = f"2026-10-15T19:00:0{i}+08:00"
        feature_path = folder / "current_features.csv"
        feature_path.write_text("synthetic_only\n", encoding="utf-8")
        recent = {"shared_view_id": "one-view", "source_days_used": DAYS[:i+1],
            "source_proofs": proofs[:i+1], "historical_publication_verified": False, "derived_at": created}
        preview = folder / "result.json"
        preview.write_text(json.dumps({"day": day, "schema": "leader_complete_close_preview_v1",
            "status": "complete_source_close_research_preview_only", "created_at": created,
            "recent_source_lineage": recent}), encoding="utf-8")
        previews.append({"preview": str(preview), "preview_sha256": digest(preview),
            "feature_file": str(feature_path), "feature_sha256": digest(feature_path), "recent_source_view": recent,
            "feature_qualification": {"verification": q.verification, "decision_as_of": day + "T15:05:00+08:00"},
            "features_match_current_active_raw": True, "raw_daily_sha256": proofs[i]["raw_daily_sha256"],
            "source_folder": proofs[i]["source_folder"]})
        rows.append({"event_id": A + "|" + day + "|s1", "symbol": A, "date": day, "stage": 1,
            "decision_eligible": True, "feature_available_at": created,
            "amount_rel20_pre": .5, "market_breadth": 0., "fixed_rule_pass": True, "fixed_rule_score": 1.})
    frame = pd.DataFrame(rows)
    frame.to_csv(panel / "fixed_rule_diagnostic.csv", index=False)
    batches = [{"day": day, "stage": stage, "status": "complete", "potential_unknown_stage_members": [],
        "unknown_fixed_rule_fields": [], "known_matching_rows": [
            {"event_id": A + "|" + day + "|s1", "symbol": A, "fixed_rule_score": 1.}] if stage == 1 else [],
        "selection_permitted": False, "unknown_is_empty_target": False} for day in DAYS for stage in (1, 2, 3)]
    batch_path = panel / "candidate_batches.json"
    batch_path.write_text(json.dumps({"schema": "recent_shortline_fixed_rule_diagnostic_batches_v1",
        "future_label_fields_included": False, "batches": batches, **replay.PANEL_FLAGS}), encoding="utf-8")
    record = {"schema": replay.PANEL_SCHEMA, "method_id": replay.PANEL_METHOD,
        "status": "completed_recent_shortline_diagnostics", "created_at": "2026-10-15T20:00:00+08:00",
        "engine_advanced": False, "return_metrics": None, "future_labels_exported_to_candidate_file": False,
        "historical_publication_verified": False, "fixed_rule": replay.PROTOCOL["rule"],
        "ipo_qualification": q.verification, "calendar": str(calendar), "calendar_sha256": digest(calendar),
        "source_days": DAYS, "preview_sources": previews, "original_history_snapshot": physical["history_snapshot"],
        "original_history_manifest_sha256": physical["original_history_manifest_sha256"],
        "base_history_cutoff": physical["base_history_end"], "candidate_batches": str(batch_path),
        "event_rows": 3, "fixed_rule_pass_rows": 3, "incomplete_stage_day_batches": 0, **replay.PANEL_FLAGS}
    (panel / "result.json").write_text(json.dumps(record), encoding="utf-8")
    return panel, provider_folder, calendar, q, record, frame


def test_loader_ignores_label_file_and_extra_future_columns_but_rejects_mixed_source_before_output(tmp_path, monkeypatch):
    panel, provider, calendar, q, record, frame = input_files(tmp_path, monkeypatch)
    (panel / "event_panel.parquet").write_bytes(b"not-readable-and-must-never-be-opened")
    frame["hit_5"] = [0, 1, 1]
    frame.to_csv(panel / "fixed_rule_diagnostic.csv", index=False)
    now = datetime.fromisoformat("2026-10-15T21:00:00+08:00")
    loaded = replay._load(panel, provider, calendar, q, now)
    assert loaded["selected"].symbol.tolist() == [A, A, A]
    assert "hit_5" not in loaded["selected"] and loaded["source"]["future_label_columns_read"] is False
    changed = deepcopy(record)
    changed["preview_sources"][-1]["recent_source_view"]["source_proofs"][-1]["raw_daily_sha256"] = "other-lineage"
    (panel / "result.json").write_text(json.dumps(changed), encoding="utf-8")
    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)
    monkeypatch.setattr(replay, "datetime", FixtureClock)
    monkeypatch.setattr(replay, "load_ipo_qualification", lambda *args, **kwargs: q)
    monkeypatch.setattr(replay, "new_state", lambda *args: pytest.fail("must reject before making a research state"))
    out = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="identical own-dated provider proof prefix"):
        replay.run(panel, provider, calendar, "unused-ipo", "unused-master", out)
    assert not out.exists()


def test_candidate_sidecar_cannot_hide_unknown_batch_membership(tmp_path, monkeypatch):
    panel, provider, calendar, q, _, _ = input_files(tmp_path, monkeypatch)
    path = panel / "candidate_batches.json"
    batches = json.loads(path.read_text(encoding="utf-8"))
    batches["batches"][2]["potential_unknown_stage_members"] = [C]
    path.write_text(json.dumps(batches), encoding="utf-8")
    with pytest.raises(ValueError, match="preserve every unknown"):
        replay._load(panel, provider, calendar, q, datetime.fromisoformat("2026-10-15T21:00:00+08:00"))
