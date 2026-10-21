"""Mechanical listing-window behavior; fixtures are not return evidence."""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import FAMILIES, _calendar_view, build_leader_daily_batches
from trading.research.leader_listing_features import METHOD_ID


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-01-01/2026-01-03", "2026-10-01/2026-10-07"], "open_sessions": []}
SESSIONS, _, _ = _calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
NEW = "301660.SZSE"
IPO_DAYS = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]


def proof(*, include_new=True, new_available="2026-10-12T23:59:59+08:00", symbols=("000001.SZSE", "600001.SSE")):
    records = {symbol: {"ipo_date": "1999-01-01", "available_at": "2026-10-08T12:00:00+08:00",
        "board": "main", "source_basis": "synthetic_master_known_after_retrieval"} for symbol in symbols}
    if include_new:
        records[NEW] = {"ipo_date": "2026-10-13", "available_at": new_available, "board": "chinext",
            "rule_effective_from": "2026-07-06", "source_basis": "synthetic_official_listing"}
    return IpoQualification(records, tuple(SESSIONS), {"synthetic": True, "execution_admitted": False})


def raw(days=IPO_DAYS):
    return pd.DataFrame([dict(code=code, date=day, open=10.0, high=10.1, low=9.9,
        close=10.0, volume=3_000_000, amount=30_000_000.0, pct_chg=0.0, is_st=False, pre_close=999.0)
        for code in ("000001", "301660", "600001") for day in days])


def price(frame, code, day, close, pct, opening=None):
    opening = close if opening is None else opening
    mask = frame.code.eq(code) & frame.date.eq(day)
    frame.loc[mask, ["open", "close", "high", "low", "pct_chg"]] = [opening, close, max(opening, close)+.01, min(opening, close)-.01, pct]


def metadata(frame):
    days = sorted(frame.date.unique())
    return {"snapshot": "synthetic", "prefixes": ["0", "3", "6"], "partitions": [
        {"prefix": prefix, "lower": days[0].replace("-", ""), "upper": days[-1].replace("-", ""),
         "observed_dates": sorted(frame.loc[frame.code.str.startswith(prefix), "date"].unique())}
        for prefix in ("0", "3", "6")]}


def build(frame, *, start="2026-10-19", end="2026-10-21", qualification=None, calendar=CALENDAR):
    symbols = sorted({code + (".SSE" if code.startswith("6") else ".SZSE") for code in frame.code})
    return build_leader_daily_batches(frame, metadata(frame), start=start, end=end,
        calendar_payload=calendar, declared_cohort=symbols, retain_window_features=True,
        ipo_qualification=qualification)


def new_run_frame():
    frame = raw()
    price(frame, "301660", "2026-10-16", 10.0, -1.0)
    for day, close in zip(IPO_DAYS[4:], [12.0, 14.4, 17.28, 20.736]):
        price(frame, "301660", day, close, 20.0)
    return frame


def test_opt_in_excludes_first_five_and_restarts_day6_day7_without_old_default_change():
    frame = new_run_frame()
    before_raw = frame.copy(deep=True)
    legacy = build(frame)
    qualified = build(frame, start=IPO_DAYS[0], end=IPO_DAYS[-1], qualification=proof())
    assert [c["symbol"] for c in legacy["batches"]["stage2_amount_baseline"]["2026-10-20"]["candidates"]] == [NEW]
    features = qualified["window_features"].loc[lambda table: table.symbol.eq(NEW)].set_index("date")
    assert features.loc[IPO_DAYS[:5], "listing_qualification_status"].eq("known_no_limit_listing_window").all()
    assert features.loc[IPO_DAYS[:5], "stage"].isna().all()
    assert features.loc["2026-10-19", "unqualified_stage"] == 1
    assert features.loc["2026-10-20", "unqualified_stage"] == 2
    assert features.loc["2026-10-20", "stage"] == 1 and features.loc["2026-10-21", "stage"] == 2
    for family, day in (("stage1_amount_baseline", "2026-10-20"), ("stage2_amount_baseline", "2026-10-21")):
        batch = qualified["batches"][family][day]
        assert batch["status"] == "complete" and [row["symbol"] for row in batch["candidates"]] == [NEW]
    assert "feature_qualification" not in legacy
    assert qualified["feature_qualification"]["method_id"] == METHOD_ID
    assert qualified["protocol"]["min_amount_cny"] == legacy["protocol"]["min_amount_cny"]
    pd.testing.assert_frame_equal(frame, before_raw)
    pd.testing.assert_frame_equal(qualified["raw_bars"], build(frame, start=IPO_DAYS[0], end=IPO_DAYS[-1])["raw_bars"])


def test_known_unrestricted_streak_does_not_carry_into_ordinary_stage_four_or_five():
    frame = raw()
    close = 10.0
    for day in IPO_DAYS:
        close *= 1.2
        price(frame, "301660", day, close, 20.0)
    result = build(frame, start=IPO_DAYS[0], end=IPO_DAYS[-1], qualification=proof())
    values = result["window_features"].loc[lambda table: table.symbol.eq(NEW)].set_index("date")
    assert values.loc["2026-10-20", "stage"] == 1
    assert values.loc["2026-10-21", "stage"] == 2
    assert values.loc["2026-10-22", "stage"] == 3
    assert values.loc[IPO_DAYS[:5], "stage"].isna().all()


def test_generic_first_five_guard_does_not_invent_an_official_unrestricted_boundary():
    frame = raw()
    price(frame, "000001", "2026-10-19", 11.0, 10.0)
    price(frame, "000001", "2026-10-20", 12.1, 10.0)
    price(frame, "000001", "2026-10-21", 12.1, 0.0)
    price(frame, "000001", "2026-10-22", 13.31, 10.0)
    base = proof()
    records = deepcopy(base.records)
    records["000001.SZSE"] = {"ipo_date": "2026-10-13", "available_at": "2026-10-12T23:59:59+08:00",
        "board": "main", "source_basis": "synthetic_provider_master_without_official_limit_regime"}
    overlay = IpoQualification(records, base.calendar, base.verification)
    result = build(frame, start="2026-10-19", end="2026-10-22", qualification=overlay)
    values = result["window_features"].loc[lambda table: table.symbol.eq("000001.SZSE")].set_index("date")
    assert values.loc["2026-10-19", "listing_qualification_status"] == "known_excluded_listing_window_rule_unverified"
    assert pd.isna(values.loc["2026-10-20", "stage"])
    assert values.loc["2026-10-20", "listing_feature_reason"] == "previous_listing_window_no_limit_rule_unverified"
    assert "000001.SZSE" in result["batches"]["stage1_amount_baseline"]["2026-10-20"]["feature_qualification"]["unknown_symbols"]
    assert values.loc["2026-10-21", "stage"] == 0 and values.loc["2026-10-22", "stage"] == 1


@pytest.mark.parametrize("missing_prior", [True, False])
def test_first_supported_stage_still_requires_exact_previous_raw_and_reference(missing_prior):
    frame = new_run_frame()
    if missing_prior:
        frame = frame.loc[~(frame.code.eq("301660") & frame.date.eq("2026-10-19"))].copy()
    else:
        price(frame, "301660", "2026-10-20", 16.0, 20.0)
    result = build(frame, start="2026-10-20", end="2026-10-20", qualification=proof())
    batch = result["batches"]["stage1_amount_baseline"]["2026-10-20"]
    assert batch["status"] == "incomplete" and batch["candidates"] == []
    assert batch["feature_qualification"]["unknown_symbols"] == [NEW]
    assert batch["feature_qualification"]["source_coverage_status_before_qualification"] == "complete"


def test_unknown_listing_date_is_unknown_potential_membership_not_known_empty_cash():
    result = build(new_run_frame(), qualification=proof(include_new=False))
    for family in FAMILIES[:3]:
        batch = result["batches"][family]["2026-10-20"]
        assert batch["status"] == "incomplete" and batch["candidates"] == []
        assert batch["feature_qualification"]["unknown_symbols"] == [NEW]
        assert batch["feature_qualification"]["qualification_complete"] is False
        assert "NO_CHANGE" in batch["feature_qualification"]["unknown_semantics"]
    features = result["window_features"].loc[lambda table: table.symbol.eq(NEW)]
    assert features.stage.isna().all() and features.listing_qualification_status.eq("unknown").all()


def test_each_decision_cutoff_prevents_future_metadata_and_raw_from_revising_earlier_batches():
    frame = new_run_frame()
    late = proof(new_available="2026-10-20T23:59:59+08:00")
    original = build(frame.loc[frame.date.le("2026-10-20")], start="2026-10-20", end="2026-10-20", qualification=proof(include_new=False))
    extended = build(frame, start="2026-10-20", end="2026-10-21", qualification=late)
    for family in FAMILIES:
        assert original["batches"][family]["2026-10-20"] == extended["batches"][family]["2026-10-20"]
    assert extended["batches"]["stage1_amount_baseline"]["2026-10-20"]["feature_qualification"]["unknown_symbols"] == [NEW]
    assert [row["symbol"] for row in extended["batches"]["stage2_amount_baseline"]["2026-10-21"]["candidates"]] == [NEW]
    new21 = extended["window_features"].loc[lambda table: table.symbol.eq(NEW) & table.date.eq("2026-10-21")].iloc[0]
    assert new21.stage == 2 and new21.feature_decision_as_of == "2026-10-21T15:05:00+08:00"


def test_rebound_D_minus2_first_five_is_known_excluded_but_supported_event_can_match():
    frame = new_run_frame()
    price(frame, "301660", "2026-10-20", 11.4, -5.0, opening=12.0)
    price(frame, "301660", "2026-10-21", 12.54, 10.0, opening=11.8)
    legacy = build(frame, start="2026-10-21", end="2026-10-21")
    assert [row["symbol"] for row in legacy["batches"]["daily_rebound_proxy"]["2026-10-21"]["candidates"]] == [NEW]
    new = build(frame, start="2026-10-21", end="2026-10-21", qualification=proof())
    assert new["batches"]["daily_rebound_proxy"]["2026-10-21"]["candidates"] == []
    feature = new["window_features"].loc[lambda table: table.symbol.eq(NEW)].iloc[0]
    assert not feature.daily_rebound and feature.unqualified_daily_rebound
    assert feature.rebound_feature_reason == "D_minus2_known_no_limit_listing_window"
    # Move the same causal pattern one session later so D-2 is the sixth,
    # supported ordinary limit event rather than an unrestricted IPO session.
    frame = new_run_frame()
    price(frame, "301660", "2026-10-21", 13.68, -5.0, opening=14.4)
    price(frame, "301660", "2026-10-22", 15.048, 10.0, opening=14.0)
    new = build(frame, start="2026-10-22", end="2026-10-22", qualification=proof())
    assert [row["symbol"] for row in new["batches"]["daily_rebound_proxy"]["2026-10-22"]["candidates"]] == [NEW]


def test_rebound_missing_D_minus2_remains_unknown_despite_old_False():
    frame = new_run_frame()
    price(frame, "301660", "2026-10-21", 13.68, -5.0, opening=14.4)
    price(frame, "301660", "2026-10-22", 15.048, 10.0, opening=14.0)
    frame = frame.loc[~(frame.code.eq("301660") & frame.date.eq("2026-10-20"))].copy()
    result = build(frame, start="2026-10-22", end="2026-10-22", qualification=proof())
    batch = result["batches"]["daily_rebound_proxy"]["2026-10-22"]
    assert batch["status"] == "incomplete" and batch["feature_qualification"]["unknown_symbols"] == [NEW]
    assert batch["feature_qualification"]["unknown_reasons_by_symbol"][NEW] == "missing_exact_D_minus2_bar_or_reference"


def test_old_stock_rules_and_observed_row_trend_keep_default_results_when_qualification_known():
    days = [day for day in SESSIONS if "2026-06-01" <= day <= "2026-10-14"]
    frame = raw(days).replace({"code": {"301660": "300001"}})
    close = 10.0
    for index, day in enumerate(days):
        close *= 1.004
        price(frame, "000001", day, close, .4 if index else 0.0)
    # A normal pre-decision limit followed by repair exercises the unchanged
    # old-stock stage and rebound definitions, in addition to the trend path.
    price(frame, "600001", "2026-10-12", 11.0, 10.0)
    price(frame, "600001", "2026-10-13", 10.0, (10/11-1)*100, opening=10.8)
    price(frame, "600001", "2026-10-14", 10.9, 9.0, opening=10.6)
    overlay = proof(include_new=False, symbols=("000001.SZSE", "300001.SZSE", "600001.SSE"))
    records_before = deepcopy(overlay.records)
    default = build(frame, start="2026-10-12", end="2026-10-14")
    qualified = build(frame, start="2026-10-12", end="2026-10-14", qualification=overlay)
    for family in FAMILIES:
        for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
            assert default["batches"][family][day]["candidates"] == qualified["batches"][family][day]["candidates"]
    assert overlay.records == records_before
    assert qualified["batches"]["trend_leader_20_60"]["2026-10-14"]["candidates"]
    assert all(qualified[key] is False for key in ("selection_enabled", "signal_admitted", "execution_admitted", "model_refit", "future_labels_used_for_score"))


def test_unknown_listing_blocks_only_the_actual_potential_family_instead_of_all_empty_rules():
    days = [day for day in SESSIONS if "2026-06-01" <= day <= "2026-10-14"]
    frame = raw(days).replace({"code": {"301660": "300001"}})
    close = 10.0
    for index, day in enumerate(days):
        close *= 1.004
        price(frame, "000001", day, close, .4 if index else 0.0)
    overlay = proof(include_new=False, symbols=("300001.SZSE", "600001.SSE"))
    result = build(frame, start="2026-10-14", end="2026-10-14", qualification=overlay)
    trend = result["batches"]["trend_leader_20_60"]["2026-10-14"]
    assert trend["candidates"] == [] and trend["status"] == "incomplete"
    assert trend["feature_qualification"]["unknown_symbols"] == ["000001.SZSE"]
    for family in FAMILIES[:3]:
        batch = result["batches"][family]["2026-10-14"]
        assert batch["status"] == "complete" and batch["feature_qualification"]["unknown_symbols"] == []


def test_opt_in_requires_matching_full_calendar_and_a_typed_qualification():
    frame = new_run_frame()
    with pytest.raises(ValueError, match="full pinned"):
        build(frame, qualification=proof(), calendar=None)
    with pytest.raises(ValueError, match="typed"):
        build(frame, qualification={"ipo_date": "2026-10-13"})
    other = IpoQualification(proof().records, tuple(SESSIONS[:-1]), {"synthetic": True})
    with pytest.raises(ValueError, match="match exactly"):
        build(frame, qualification=other)
