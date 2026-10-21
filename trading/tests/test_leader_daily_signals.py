import json

import pandas as pd
import pytest

from trading.research.leader_daily_signals import (
    FAMILIES, build_leader_daily_batches, produce_leader_daily_batches,
)


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
            "closed_ranges": ["2026-01-01/2026-01-03"], "open_sessions": []}
CODES = ("000001", "300001", "600001")
COHORT = ("000001.SZSE", "300001.SZSE", "600001.SSE")


def _raw(days):
    return pd.DataFrame([
        {"code": code, "date": day, "open": 10., "high": 10.1, "low": 9.9, "close": 10.,
         "volume": 3_000_000, "amount": 30_000_000, "pct_chg": 0., "is_st": False,
         "pre_close": 999.}  # Deliberately invalid legacy reference is never used.
        for code in CODES for day in days
    ])


def _metadata(frame):
    dates = sorted(frame.date.unique().tolist())
    return {"snapshot": "fixture_physical", "manifest": "fixture_manifest", "prefixes": ["0", "3", "6"],
            "partitions": [{"prefix": prefix, "lower": dates[0].replace("-", ""),
                            "upper": dates[-1].replace("-", ""),
                            "observed_dates": sorted(frame.loc[frame.code.str.startswith(prefix), "date"].unique().tolist())}
                           for prefix in ("0", "3", "6")]}


def _set_price(frame, code, day, close, pct, *, opening=None):
    mask = frame.code.eq(code) & frame.date.eq(day)
    frame.loc[mask, "close"] = close
    frame.loc[mask, "open"] = close if opening is None else opening
    frame.loc[mask, "high"] = max(close, close if opening is None else opening) + .1
    frame.loc[mask, "low"] = min(close, close if opening is None else opening) - .1
    frame.loc[mask, "pct_chg"] = pct


def _produce(frame, start, end, cohort=None):
    return build_leader_daily_batches(frame, _metadata(frame), start=start, end=end,
                                      calendar_payload=CALENDAR, declared_cohort=cohort)


def test_stages_use_physical_pct_and_exact_liquidity_boundary():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    frame = _raw(days)
    for day, close in zip(days[1:], (11., 12.1, 13.31)):
        _set_price(frame, "000001", day, close, 10.)
        frame.loc[frame.code.eq("000001") & frame.date.eq(day), "amount"] = 20_000_000
    result = _produce(frame, days[1], days[-1], COHORT)
    for stage, day in enumerate(days[1:], start=1):
        batch = result["batches"][f"stage{stage}_amount_baseline"][day]
        assert batch["status"] == "complete"
        assert [row["symbol"] for row in batch["candidates"]] == ["000001.SZSE"]
        assert batch["candidates"][0]["stage"] == stage
        assert batch["source_verified"] is False and batch["signal_admitted"] is False
    assert result["raw_bars"].pre_close.eq(999).all()
    assert result["raw_bars"].preclose_for_limits.lt(100).all()
    assert result["protocol"]["min_amount_cny"] == 20_000_000


def test_future_rows_new_symbols_and_missing_future_path_do_not_change_past_candidates():
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    original = _raw(days)
    _set_price(original, "000001", days[1], 11., 10.)
    _set_price(original, "000001", days[2], 11., 0.)
    before = _produce(original, days[1], days[-1], COHORT)
    future = _raw(["2026-01-08"])
    future.loc[future.code.eq("000001"), "code"] = "000002"  # Original stock vanishes later.
    extended = pd.concat([original, future], ignore_index=True)
    after = _produce(extended, days[1], "2026-01-08", COHORT)
    for family in FAMILIES:
        for day in days[1:]:
            assert before["batches"][family][day]["candidates"] == after["batches"][family][day]["candidates"]
    assert after["batches"]["stage1_amount_baseline"]["2026-01-06"]["candidates"]
    assert after["coverage_facts"][-1]["declared_cohort_missing_current_raw"] == ["000001.SZSE"]
    assert after["batches"]["stage1_amount_baseline"]["2026-01-08"]["status"] == "incomplete"


def test_complete_empty_and_missing_source_day_are_different_and_gap_run_is_unknown():
    days = ["2026-01-05", "2026-01-06", "2026-01-08"]  # Calendar's Jan7 is absent globally.
    frame = _raw(days)
    _set_price(frame, "000001", "2026-01-06", 11., 10.)
    _set_price(frame, "000001", "2026-01-08", 12.1, 10.)
    result = _produce(frame, "2026-01-05", "2026-01-08", COHORT)
    batches = result["batches"]["stage1_amount_baseline"]
    assert batches["2026-01-05"]["status"] == "complete" and batches["2026-01-05"]["candidates"] == []
    assert "2026-01-07" in result["calendar"]
    assert batches["2026-01-07"]["status"] == "incomplete" and batches["2026-01-07"]["candidates"] == []
    assert all(result["batches"][family]["2026-01-08"]["candidates"] == [] for family in FAMILIES)


def test_trend_ranking_uses_only_past_60_lag_and_preserves_full_source():
    days = pd.bdate_range("2026-01-05", periods=65).strftime("%Y-%m-%d").tolist()
    frame = _raw(days)
    for code, growth in [("000001", .002), ("600001", .004)]:
        for index, day in enumerate(days):
            _set_price(frame, code, day, 10 * (1 + growth) ** index, growth * 100 if index else 0.)
    result = _produce(frame, days[59], days[61], COHORT)
    batches = result["batches"]["trend_leader_20_60"]
    assert batches[days[59]]["candidates"] == []  # 60 closes cannot provide return lag60.
    assert [row["symbol"] for row in batches[days[60]]["candidates"]] == ["600001.SSE", "000001.SZSE"]
    assert batches[days[60]]["candidates"][0]["score"] > batches[days[60]]["candidates"][1]["score"]
    assert result["first_observed_date"]["000001.SZSE"] == days[0]
    assert result["raw_bars"].date.min() == days[0] and result["raw_bars"].date.max() == days[61]


def test_rebound_is_a_close_available_proxy_and_not_a_reseal_label():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    frame = _raw(days)
    _set_price(frame, "000001", days[1], 11., 10.)
    _set_price(frame, "000001", days[2], 10., (10 / 11 - 1) * 100, opening=10.8)
    _set_price(frame, "000001", days[3], 10.9, 9., opening=10.6)
    result = _produce(frame, days[-1], days[-1], COHORT)
    batch = result["batches"]["daily_rebound_proxy"][days[-1]]
    assert [row["symbol"] for row in batch["candidates"]] == ["000001.SZSE"]
    assert batch["candidates"][0]["stage"] == 0
    assert not set(result["candidate_table"]).intersection({"hit_4", "hit_5", "next_open_clearance", "executable_hit_5"})
    assert batch["market_breadth"]["scope"] == "pinned_provider_observed_universe"
    assert batch["market_breadth"]["verified"] is False


def test_no_cohort_or_invalid_source_cannot_silently_become_complete():
    frame = _raw(["2026-01-05"])
    observed = _produce(frame, "2026-01-05", "2026-01-05")
    assert observed["batches"][FAMILIES[0]]["2026-01-05"]["status"] == "observed_source_complete"
    frame.loc[frame.code.eq("300001"), "pct_chg"] = float("nan")
    incomplete = _produce(frame, "2026-01-05", "2026-01-05", COHORT)
    assert incomplete["batches"][FAMILIES[0]]["2026-01-05"]["status"] == "incomplete"
    assert incomplete["coverage_facts"][0]["invalid_raw_rows"] == 1


def test_loader_reads_pinned_partition_history_without_exposing_future_rows(tmp_path):
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    frame = _raw(days)
    items = []
    for prefix in ("0", "3", "6"):
        part = frame.loc[frame.code.str.startswith(prefix)].copy()
        part["date"] = part.date.str.replace("-", "").astype(int)
        filename = f"2026-01_{prefix}.parquet"
        part.to_parquet(tmp_path / filename, index=False)
        items.append({"prefix": prefix, "lower_inclusive": "20260105", "upper_inclusive": "20260107",
                      "parquet_file": filename, "rows": len(part)})
    (tmp_path / "manifest.json").write_text(json.dumps({"status": "complete_inventory_only", "execution_admitted": False,
        "end": days[-1], "chunks_expected": 3, "chunks": items, "prefixes": ["0", "3", "6"]}), encoding="utf-8")
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps(CALENDAR), encoding="utf-8")
    result = produce_leader_daily_batches(tmp_path, days[1], days[1], calendar_path=calendar_path, declared_cohort=COHORT)
    assert result["raw_bars"].date.min() == days[0] and result["raw_bars"].date.max() == days[1]
    assert result["calendar"] == [days[1]]
    assert result["source_verified"] is False and result["execution_admitted"] is False


def test_duplicate_raw_observations_fail_before_any_batch():
    frame = _raw(["2026-01-05"])
    duplicate = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="Duplicate physical"):
        _produce(duplicate, "2026-01-05", "2026-01-05", COHORT)
