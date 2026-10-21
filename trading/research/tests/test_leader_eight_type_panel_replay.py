"""Fixed-panel semantics and shared accounting, not profitable-rule tests."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import leader_eight_type_panel_replay as replay
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import _calendar_view
from trading.research.baostock_daily_slice import digest


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
SESSIONS, PREVIOUS, _ = _calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14"]
A, B = "000001.SZSE", "000002.SZSE"


def raw(days=DAYS):
    return pd.DataFrame([{"date": day, "code": "sz." + symbol[:6], "open": "10", "high": "10.1", "low": "9.9",
        "close": "10", "preclose": "10", "pctChg": "0", "volume": "1000000", "amount": "10000000",
        "tradestatus": "1", "isST": "0", "adjustflag": "3"} for day in days for symbol in (A, B)])


def panels(days=DAYS, *, weak=False, unknown=False, empty=False):
    return {day: {"day": day, "available_at": "2026-10-15T12:00:00+08:00" if day < "2026-10-15" else day + "T17:00:00+08:00",
        "environment": {"source_complete": True, "breadth": -.5 if weak else .5,
            "median_return_pct": -1. if weak else 1.},
        "types": [{"id": identity, "status": "unknown" if unknown else "not_matched" if empty else "matched",
            "candidates": [] if empty else [{"symbol": A, "amount_cny": 100000000.}],
            "unresolved_symbols": [B] if unknown else []} for identity in replay.TYPE_IDS]}
        for day in days}


def qualification():
    return IpoQualification({symbol: {"ipo_date": "1999-01-01", "available_at": "2026-10-08T10:00:00+08:00",
        "source_basis": "synthetic", "board": "main"} for symbol in (A, B)}, tuple(SESSIONS), {"synthetic": True})


def invoke(tmp_path, *, daily_panels=None, bars=None, days=DAYS, end=None):
    return replay.replay_eight_types(panel_by_day=panels(days) if daily_panels is None else daily_panels,
        raw=raw(days) if bars is None else bars, source_days=days, calendar_payload=CALENDAR,
        scope_members=[A, B], qualification=qualification(),
        context_anchor={"day": "2026-10-09", "closes": {A: 10., B: 10.}}, start=days[0], end=end or days[-1],
        out=tmp_path / "new_run")


def one_case(*, daily_panels=None, bars=None, days=DAYS, end=None, guard="unguarded"):
    p = panels(days) if daily_panels is None else daily_panels
    paths, _ = replay._rank_path(p, {A, B}, days[0], end or days[-1])
    env = replay._environment(p, SESSIONS, max(item["available_at"] for item in p.values()))
    identity = "relay_continuation_leader"
    return replay._case(raw(days) if bars is None else bars, days, SESSIONS, PREVIOUS, p,
        paths[identity]["causal_first_seen_cap20"], env, {A, B}, qualification(),
        {"day": "2026-10-09", "closes": {A: 10., B: 10.}}, days[0], end or days[-1],
        identity, "causal_first_seen_cap20", guard, "synthetic-eight-type-case")


def test_32_fixed_cases_same_engine_both_guards_caps_missing_tail_has_no_complete_pnl(tmp_path):
    result = invoke(tmp_path)
    assert result["case_count"] == len(result["cases"]) == 32
    assert len({row["case_id"] for row in result["cases"]}) == 32
    assert result["config"] == replay._config(100000, 5).to_dict()
    assert result["protocol"]["case_count"] == 32 and result["case_selection_performed"] is False
    assert result["all_cases_complete_PnL"] is False and result["late_context"] is True
    assert all(row["metrics"] is None and row["status"] == "no_complete_PnL" for row in result["cases"])
    assert any(row["fills"] for row in result["cases"])
    assert all(fill["day"] in DAYS[1:] for row in result["cases"] for fill in row["fills"])
    assert all(row["decisions"][-1]["status"] == "NO_CHANGE" for row in result["cases"])
    assert all(value is expected for key, expected in replay.FLAGS.items() for value in [result[key]])
    assert (tmp_path / "new_run" / result["cases"][0]["case_id"] / "fills.json").is_file()


def test_cap_path_is_first_seen_before_guard_not_rebased_or_refilled():
    days = SESSIONS[SESSIONS.index(DAYS[0]):SESSIONS.index(DAYS[0]) + 5]
    p = panels(days)
    symbols = [f"{i:06d}.SZSE" for i in range(1, 26)]
    for index, day in enumerate(days):
        for item in p[day]["types"]:
            item["candidates"] = [{"symbol": symbol, "score": 100 - i} for i, symbol in enumerate(symbols[index * 5:index * 5 + 5])]
    paths, original = replay._rank_path(p, set(symbols), days[0], days[-1])
    for identity in replay.TYPE_IDS:
        bounded = paths[identity]["causal_first_seen_cap20"][days[-1]]
        assert bounded["candidates"] == [] and bounded["cap_dropped_rows"] == 5
        assert len(bounded["cap_symbols_before"]) == 20
        assert paths[identity]["uncapped"][days[-1]]["candidates"] == original[identity][days[-1]]
        assert bounded["guard_used_for_capacity"] is False


def test_unknown_and_known_empty_type_are_no_change_not_cash(tmp_path):
    unknown = one_case(daily_panels=panels(unknown=True))
    empty = one_case(daily_panels=panels(empty=True))
    assert not unknown["orders"] and not empty["orders"]
    assert all(row["status"] == "NO_CHANGE" for row in unknown["decisions"] + empty["decisions"])
    assert unknown["decisions"][0]["original_top5"]  # keep known matches, do not create a survivor pool
    assert "type_membership_unknown_in_fixed_panel" in unknown["decisions"][0]["reasons"]
    assert "type_empty_or_capacity_filtered_all_no_signal" in empty["decisions"][0]["reasons"]


def test_trusted_exit_precedes_unknown_type_and_sells_only_via_next_engine_open():
    p = panels()
    # Two strength closes confirm the second-day entry. Third day weak exits,
    # with an exact fourth raw session provided for the actual sell.
    days = DAYS + ["2026-10-15"]
    p.update(panels([days[-1]]))
    p[DAYS[-1]]["environment"].update(breadth=-.5, median_return_pct=-1.)
    for item in p[DAYS[-1]]["types"]:
        item.update(status="unknown", unresolved_symbols=[A, B])
    result = one_case(daily_panels=p, days=days, end=DAYS[-1], guard="two_close_envguard")
    decision = result["decisions"][2]
    assert decision["status"] == "EXPLICIT_EXIT_ALL" and decision["reasons"] == []
    buys = [row for row in result["fills"] if row["side"] == "buy"]
    sells = [row for row in result["fills"] if row["side"] == "sell"]
    assert buys and buys[0]["day"] == DAYS[-1]
    assert sells and all(row["day"] == "2026-10-15" for row in sells)


def test_unknown_owned_reference_stops_before_committing_even_trusted_exit():
    bars = raw()
    bars.loc[bars.date.eq(DAYS[2]) & bars.code.eq("sz.000001"), "preclose"] = "9.5"
    result = one_case(bars=bars)
    assert result["blocking_evidence"]["session_not_committed"] is True
    assert result["blocking_evidence"]["day"] == DAYS[2]
    assert result["account"]["day"] == DAYS[1]
    assert result["metrics"] is None and all(row["date"] != DAYS[2] for row in result["daily"])


def test_expiry_remains_independent_when_later_type_observations_unknown():
    days = SESSIONS[SESSIONS.index(DAYS[0]):SESSIONS.index(DAYS[0]) + 8]
    p = panels(days)
    for day in days[1:]:
        for item in p[day]["types"]:
            item.update(status="unknown", unresolved_symbols=[A])
    result = one_case(daily_panels=p, days=days, end=days[1])
    buy = next(row for row in result["fills"] if row["side"] == "buy")
    sell = next(row for row in result["fills"] if row["side"] == "sell")
    assert buy["day"] == days[1] and sell["day"] == days[6]
    assert all(row["status"] == "NO_CHANGE" for row in result["decisions"][1:])
    assert result["complete_PnL_available"] is True and result["metrics"] is not None


def test_engine_preserves_limit_up_buy_failure_no_refill_or_same_day_fill():
    bars = raw()
    bars.loc[bars.code.eq("sz.000001") & bars.date.eq(DAYS[1]), ["open", "high", "low", "close", "preclose"]] = ["11", "11", "11", "11", "10"]
    bars.loc[bars.code.eq("sz.000001") & bars.date.eq(DAYS[2]), ["open", "high", "low", "close", "preclose"]] = ["11", "11.1", "10.9", "11", "11"]
    result = one_case(bars=bars)
    assert result["orders"] and all(row["symbol"] == A for row in result["orders"])
    assert result["decisions"][0]["original_top5"][0]["symbol"] == A
    assert not [fill for fill in result["fills"] if fill["day"] in DAYS[:2]]


def test_structural_break_uses_actual_fill_anchor_and_same_next_open_engine_exit():
    days = DAYS + ["2026-10-15"]
    p = panels(days)
    for day in days:
        p[day]["risk_features"] = {A: {"date": day, "listing_feature_method_id": "leader_listing_window_features_asof_decision_v1",
            "feature_decision_as_of": day + "T15:05:00+08:00", "listing_qualification_status": "supported",
            "limit_up": day == days[0], "stage": 2 if day == days[0] else 0,
            "stage_reference_known": True, "board": "main"}}
        p[day]["risk_feature_scope_complete"] = True
    for day in days[1:]:
        for item in p[day]["types"]:
            item.update(status="unknown", unresolved_symbols=[A])
    result = one_case(daily_panels=p, days=days)
    assessments = [event for row in result["daily"] for event in row["events"] if event["kind"] == "leader_type_risk_assessed"]
    invalid = next(event for event in assessments if event["status"] == "invalid")
    assert invalid["entry"]["fill_day"] == days[1] and invalid["entry"]["decision_day"] == days[0]
    assert invalid["reasons"] == ["current_qualified_limit_explicitly_broken"]
    sells = [fill for fill in result["fills"] if fill["side"] == "sell"]
    assert sells and sells[0]["day"] == days[2]
    assert result["account"]["replay_risk_exit_policy"]["policy_id"].startswith(replay.TYPE_RISK_ID)
    assert result["metrics"] is None  # no complete fixed tail, even with this real mechanical exit


def test_loader_binds_raw_nulls_protocol_and_source_before_research_output(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR))
    bars = raw()
    # Nullable status0 unheld B is retained byte-for-value, not converted to zero.
    bars.loc[bars.code.eq("sz.000002"), ["tradestatus", "volume", "amount", "pctChg"]] = ["0", "", "", ""]
    raw_path = tmp_path / "raw.parquet"
    bars.to_parquet(raw_path, index=False)
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps({"fixed": "thirty_two_cases"}))
    proofs = [{"day": day, "source_folder": str(tmp_path / day), "source_updated_at": "2026-10-15T11:00:00+08:00",
        "raw_daily_sha256": day} for day in DAYS]
    p = panels()
    value = {"schema": replay.INPUT_SCHEMA, "created_at": "2026-10-15T12:30:00+08:00",
        "raw_file": str(raw_path), "raw_sha256": digest(raw_path), "calendar": str(calendar),
        "calendar_sha256": digest(calendar), "source_days": DAYS, "source_proofs": proofs,
        "scope_members": [A, B], "scope_basis": "synthetic_fixed_prior_panel", "scope_frozen_at": "2026-10-15T11:30:00+08:00",
        "history_snapshot": str(tmp_path / "history"), "original_history_manifest_sha256": "anchor",
        "base_history_end": "2026-10-09", "candidate_panel_only": True,
        "protocol_file": str(protocol_path), "protocol_sha256": digest(protocol_path),
        **{key: False for key in ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit", "future_labels_used_for_score")},
        "panels": [{**p[day], "source_proof": proof} for day, proof in zip(DAYS, proofs)]}
    manifest = tmp_path / "panel.json"
    manifest.write_text(json.dumps(value))
    monkeypatch.setattr(replay, "load_complete", lambda folder: (bars.loc[bars.date.eq(folder.rsplit("\\", 1)[-1].rsplit("/", 1)[-1])].reset_index(drop=True), {"folder": folder}))
    monkeypatch.setattr(replay, "source_proof", lambda actual, metadata, h: next(proof for proof in proofs if proof["source_folder"] == metadata["folder"]))
    monkeypatch.setattr(replay, "_context_anchor", lambda v: ({"day": "2026-10-09", "closes": {A: 10., B: 10.}}, {"synthetic": True}))
    monkeypatch.setattr(replay, "load_ipo_qualification", lambda *a, **k: qualification())
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs)
        return kwargs["provenance"]
    monkeypatch.setattr(replay, "replay_eight_types", capture)
    result = replay.run(manifest, "ipo", "master", tmp_path / "new", start=DAYS[0], end=DAYS[-1])
    assert result["external_fixed_protocol"]["sha256"] == digest(protocol_path)
    assert captured["raw"].loc[captured["raw"].code.eq("sz.000002"), "volume"].tolist() == [""] * 3
    protocol_path.write_text("changed")
    with pytest.raises(ValueError, match="protocol bytes"):
        replay.run(manifest, "ipo", "master", tmp_path / "new", start=DAYS[0], end=DAYS[-1])
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("change", ["future_type_clock", "future_label", "missing_type", "missing_panel", "calendar_gap"])
def test_bad_evidence_rejected_before_output(tmp_path, change):
    p, days = panels(), list(DAYS)
    if change == "future_type_clock":
        p[DAYS[0]]["types"][0]["available_at"] = "2099-01-01T18:00:00+08:00"
    elif change == "future_label":
        p[DAYS[0]]["types"][0]["candidates"][0]["hit_5"] = True
    elif change == "missing_type":
        p[DAYS[0]]["types"].pop()
    elif change == "missing_panel":
        p.pop(DAYS[0])
    else:
        days.pop(1)
    with pytest.raises(ValueError):
        invoke(tmp_path, daily_panels=p, days=days)
    assert not (tmp_path / "new_run").exists()
