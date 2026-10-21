"""Fixed decisions and raw/action unknowns, never market-performance evidence."""
from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from trading.research import recent_alpha158_panel_replay as replay
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


def selected(events=None):
    events = events or [(day, symbol, .9 - j * .1) for day in DAYS for j, symbol in enumerate((A, B))]
    return pd.DataFrame([{"date": day, "symbol": symbol, "score": score, "score_seed17": score,
        "score_seed29": score, "score_seed43": score} for day, symbol, score in events])


def proof(missing=()):
    return IpoQualification({symbol: {"ipo_date": "1999-01-01", "available_at": "2026-10-08T12:00:00+08:00",
        "board": "main" if not symbol.startswith("3") else "chinext", "source_basis": "synthetic"}
        for symbol in (A, B, C) if symbol not in missing}, tuple(SESSIONS), {"synthetic": True})


def case(rows=None, bars=None, qualification=None, anchor=None, case_index=0):
    return replay._case(selected() if rows is None else rows, raw() if bars is None else bars, DAYS, PREVIOUS,
        qualification or proof(), replay.PROTOCOL["cases"][case_index],
        context_anchor=anchor if anchor is not None else {"day": "2026-10-09", "closes": {A: 10., B: 10., C: 10.}})


def test_fixed_cases_use_same_engine_next_open_and_withhold_last_score_without_rollforward():
    bounded, uncapped = case(), case(case_index=1)
    for result in (bounded, uncapped):
        assert result["status"] == "completed_conditional_raw_account_diagnostic"
        assert [d["status"] for d in result["decisions"]] == ["FULL_TARGET", "FULL_TARGET", "NO_CHANGE"]
        assert result["decisions"][-1]["reasons"] == ["withheld_missing_exact_successor_full_source"]
        assert result["fills"] and {fill["day"] for fill in result["fills"]} <= set(DAYS[1:])
        assert result["account"]["day"] == DAYS[-1] and len(result["daily"]) == 3
        assert len(result["selected_events"]) == 6 and result["future_path_dropped_rows"] == 0
        assert result["config"] == replay._config(100000, 5).to_dict()
        assert result["metrics"] is not None and result["company_actions_verified"] is False
        assert result["ledger_mutated"] is result["selection_enabled"] is result["execution_admitted"] is False
    assert bounded["metrics"] == uncapped["metrics"]


def test_missing_context_member_preserves_candidates_and_does_not_impute_anchor_or_refill():
    result = case(anchor={"day": "2026-10-09", "closes": {A: 10.}})
    first = result["decisions"][0]
    assert first["status"] == "NO_CHANGE" and {row["symbol"] for row in first["candidates"]} == {A, B}
    assert "current_or_execution_reference_unknown_or_incompatible" in first["reasons"]
    assert result["future_path_dropped_rows"] == 0
    assert all(fill["day"] == "2026-10-14" for fill in result["fills"])


def test_missing_successor_candidate_keeps_full_selection_and_no_fallback_stock():
    rows = selected([(DAYS[0], A, .9), (DAYS[1], B, .95), (DAYS[2], A, .8)])
    bars = raw().loc[lambda f: ~(f.date.eq(DAYS[2]) & f.code.eq("sh.600001"))]
    result = case(rows=rows, bars=bars)
    decision = result["decisions"][1]
    assert decision["status"] == "NO_CHANGE" and decision["candidates"] == [{"symbol": B, "score": .95}]
    assert any(item["reason"] == "raw_row_missing" and item["symbol"] == B for item in decision["raw_issues"])
    assert B in {row["symbol"] for row in result["selected_events"]}
    assert B not in {fill["symbol"] for fill in result["fills"]}
    assert result["future_path_dropped_rows"] == 0


def test_unknown_IPO_is_no_change_not_empty_cash_or_first_seen_listing_date():
    result = case(qualification=proof(missing=(B,)))
    assert not result["orders"] and not result["fills"]
    assert all(row["status"] == "NO_CHANGE" for row in result["decisions"])
    assert all(len(row["candidates"]) == 2 for row in result["decisions"])
    assert "required_IPO_qualification_unknown_or_unsupported_at_own_clock" in result["decisions"][0]["reasons"]
    base = replay.ExactSourceRawFeed(raw(), DAYS, [A, B])
    assert base.instruments[B]["ipoDate"] is None


def test_late_execution_day_metadata_cannot_be_borrowed_at_next_open():
    q = proof()
    records = deepcopy(q.records)
    records[B]["available_at"] = "2026-10-13T12:00:00+08:00"
    result = case(qualification=IpoQualification(records, q.calendar, q.verification))
    assert result["decisions"][0]["status"] == "NO_CHANGE"
    witness = [item for item in result["decisions"][0]["IPO_evidence"] if item["symbol"] == B and item["day"] == DAYS[1]][0]
    assert witness["knowledge_status"] == "unknown" and witness["decision_as_of"].endswith("09:30:00+08:00")


def test_nullable_halt_activity_is_never_filled_with_zero_in_raw_base():
    bars = raw()
    bars.loc[bars.code.eq("sh.600001") & bars.date.eq(DAYS[1]), ["tradestatus", "volume", "amount"]] = ["0", "", ""]
    base = replay.ExactSourceRawFeed(bars, DAYS, [A, B])
    assert base.rows[DAYS[1]][B]["tradestatus"] == 0
    assert np.isnan(base.rows[DAYS[1]][B]["volume"]) and pd.isna(base.volumes.at[DAYS[1], B])
    assert case(bars=bars)["decisions"][0]["status"] == "NO_CHANGE"


def test_unresolved_reference_of_held_member_stops_before_mark_and_has_no_complete_PnL():
    bars = raw()
    bars.loc[bars.code.eq("sz.000001") & bars.date.eq(DAYS[2]), "preclose"] = "9.7"
    result = case(bars=bars)
    assert result["fills"] and result["account"]["day"] == DAYS[1]
    assert result["status"] == "blocked_diagnostic_no_complete_PnL" and result["metrics"] is None
    assert result["blocking_evidence"]["reason"] == "held_raw_or_action_reference_unresolved"
    assert result["blocking_evidence"]["session_not_committed"] is True


def test_future_score_change_cannot_reorder_earlier_decisions_or_refill():
    first = selected([(DAYS[0], A, .8), (DAYS[1], B, .8), (DAYS[2], A, .9)])
    changed = selected([(DAYS[0], A, .8), (DAYS[1], B, .8), (DAYS[2], C, 1.)])
    left, right = case(rows=first), case(rows=changed)
    assert left["decisions"][:2] == right["decisions"][:2]
    assert left["orders"] == right["orders"]


def anchor_files(tmp_path):
    snapshot = tmp_path / "history"
    snapshot.mkdir()
    chunks = []
    for prefix, code in (("0", "000001"), ("3", "300001"), ("6", "600001")):
        path = snapshot / (prefix + ".parquet")
        pd.DataFrame({"code": [code, code], "date": [20261009, 20261013], "close": [10., 999.]}).to_parquet(path)
        chunks.append({"prefix": prefix, "parquet_file": path.name, "parquet_sha256": digest(path),
            "lower_inclusive": "20261008", "upper_inclusive": "20261013", "rows": 2})
    manifest = {"status": "complete_inventory_only", "execution_admitted": False, "chunks": chunks, "chunks_expected": 3}
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {"history_snapshot": str(snapshot), "original_history_manifest_sha256": digest(snapshot / "manifest.json"), "base_history_end": "2026-10-09"}


def test_context_anchor_reads_only_three_bound_partitions_and_exact_date(tmp_path, monkeypatch):
    provider = anchor_files(tmp_path)
    original = pd.read_parquet
    columns = []
    def scoped(*args, **kwargs):
        columns.append(kwargs.get("columns"))
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, "read_parquet", scoped)
    anchor, metadata = replay._context_anchor(provider)
    assert anchor == {"day": "2026-10-09", "closes": {A: 10., B: 10., C: 10.}}
    assert columns == [["code", "date", "close"]] * 3
    assert len(metadata["partitions"]) == 3 and metadata["only_reference_context_not_quote_or_trade_session"] is True


def test_context_anchor_does_not_accept_changed_original_partition(tmp_path):
    provider = anchor_files(tmp_path)
    path = pd.io.common.stringify_path(provider["history_snapshot"]) + "/0.parquet"
    pd.DataFrame({"code": ["000001"], "date": [20261009], "close": [11.]}).to_parquet(path)
    with pytest.raises(ValueError, match="partition hash"):
        replay._context_anchor(provider)


def test_run_saves_both_fixed_cases_and_real_in_memory_accounts_without_store(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    monkeypatch.setattr(replay, "_load", lambda *a: (selected(), raw(), DAYS, SESSIONS, PREVIOUS, {"synthetic": True},
        {"day": "2026-10-09", "closes": {A: 10., B: 10., C: 10.}}))
    monkeypatch.setattr(replay, "load_ipo_qualification", lambda *a, **k: proof())
    result = replay.run("scores", calendar, "ipo", "master", tmp_path / "run")
    assert [row["case"] for row in result["cases"]] == ["causal_first_seen_cap20", "uncapped"]
    assert result["case_selection_performed"] is result["persistent_ledger_mutated"] is False
    for row in result["cases"]:
        path = tmp_path / "run" / row["case"]
        assert all((path / (name + ".json")).is_file() for name in ("orders", "fills", "account", "decisions", "daily"))


def bound_input(tmp_path):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    context = anchor_files(tmp_path)
    folder = tmp_path / "provider"
    folder.mkdir()
    raw_path = folder / "raw.parquet"
    bars = raw()
    bars.to_parquet(raw_path)
    proofs = [{"day": day, "ordinary_inventory_rows": 3, "calendar_sha256": digest(calendar),
        "read_only_verified": True, "source_inventory_complete": True,
        "full_dated_roster_inventory_complete": True, "receipt_status": "complete_full_dated_close_inventory_not_admitted"} for day in DAYS]
    provider = {"schema": replay.PROVIDER_SCHEMA, "provider_method_id": replay.PROVIDER_METHOD,
        "status": "research_provider_completed", "created_at": "2026-10-14T16:00:00+08:00", "recent_source_days": DAYS,
        "calendar_sha256": digest(calendar), "source_proofs": proofs, "raw_prices_imputed": False,
        "recent_source_inventory_complete": True, "recent_raw_original_fields_unchanged": True,
        "recent_raw_observations_file": str(raw_path), "recent_raw_observations_sha256": digest(raw_path),
        "recent_raw_observation_rows": len(bars), **context,
        **{key: False for key in ("model_refit", "model_trained", "execution_admitted", "selection_enabled", "strategy_validated", "future_labels_used_for_score")}}
    (folder / "result.json").write_text(json.dumps(provider), encoding="utf-8")
    batch = tmp_path / "scores"
    batch.mkdir()
    scores = selected([(day, symbol, .9 - j * .1) for day in DAYS for j, symbol in enumerate((A, B, C))]).rename(columns={"score": "rank_mean"})
    scores.to_parquet(batch / "scores.parquet", index=False)
    record = {"schema": "a_share_recent_source_alpha158_frozen_score_batch_v1", "method_id": replay.SCORE_METHOD,
        "status": "completed_research_only_dated_score_batch", "actual_computed_at": "2026-10-14T17:00:00+08:00",
        "provider_view": str(folder), "provider_result_sha256": digest(folder / "result.json"),
        "source_proofs": proofs, "raw_observations": str(raw_path), "raw_observations_sha256": digest(raw_path),
        "score_path": str(batch / "scores.parquet"), "score_dates": DAYS, "score_rows": len(scores),
        **{key: False for key in ("model_refit", "model_trained", "future_labels_used_for_score", "strategy_validated",
            "selection_enabled", "execution_admitted", "ledger_mutated", "target_holdings_generated", "paper_orders_generated", "ths_payload_generated")}}
    (batch / "result.json").write_text(json.dumps(record), encoding="utf-8")
    return batch, calendar


def test_complete_bound_score_raw_calendar_and_anchor_loading_keeps_all_current_members(tmp_path):
    batch, calendar = bound_input(tmp_path)
    candidates, bars, days, _, _, source, anchor = replay._load(batch, calendar)
    assert len(candidates) == len(bars) == 9 and days == DAYS
    assert source["context_anchor"]["valid_anchor_codes"] == 3 and anchor["closes"][A] == 10.
    assert source["score_sha256"] == digest(batch / "scores.parquet")


@pytest.mark.parametrize("change", ["raw_changed", "unsafe_score", "survivor_subset"])
def test_changed_raw_unsafe_flags_or_current_score_scope_subset_are_rejected(tmp_path, change):
    batch, calendar = bound_input(tmp_path)
    path = batch / "result.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if change == "raw_changed":
        pd.DataFrame({"modified": [True]}).to_parquet(record["raw_observations"])
    elif change == "unsafe_score":
        record["execution_admitted"] = True
        path.write_text(json.dumps(record), encoding="utf-8")
    else:
        scores = pd.read_parquet(record["score_path"]).iloc[:-1]
        scores.to_parquet(record["score_path"], index=False)
        record["score_rows"] = len(scores)
        path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        replay._load(batch, calendar)
