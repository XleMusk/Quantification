from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import baostock_daily_slice as source
from trading.research.leader_daily_signals import _physical_frame
from trading.research.leader_tail_overlay import ADD_DAY, DAYS, apply_tail_overlay, validate_tail_metadata


SYMBOLS = [f"{index:06}.SZSE" for index in range(1, 21)]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "calendar_id": "test", "year": 2026,
                                    "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"anchor": "2025-12-31", "pools": {"common_pre_window_symbol20": SYMBOLS,
                                                                   "common_pre_window_uncapped": SYMBOLS + ["000021.SZSE"]}}), encoding="utf-8")
    scope = {"cohort": str(cohort), "sha256": source.digest(cohort), "pool": "common_pre_window_symbol20", "anchor": "2025-12-31"}
    @contextmanager
    def no_network_session():
        def query(**arguments):
            frame = pd.DataFrame([dict(zip(source.FIELDS.split(","),
                 [day, arguments["code"], "10.0000", "10.1000", "9.9000", "10.0000", "10.0000", "5000040", "50000001.0000", "3", "1", "0.000000", "0"])) for day in DAYS])
            return SimpleNamespace(error_code="0", error_msg="success", get_data=lambda: frame)
        yield SimpleNamespace(query_history_k_data_plus=query)
    monkeypatch.setattr(source.time, "sleep", lambda duration: None)
    package, receipt = source.collect(SYMBOLS, DAYS, calendar, {"sha256": source.WHEEL_SHA256}, tmp_path / "package",
                                      source_scope=scope, session_factory=no_network_session)
    rows = []
    for day in ["2025-12-31", DAYS[0], DAYS[2]]:
        for index in range(1, 22):
            rows.append({"code": f"{index:06}", "date": day, "open": 10., "high": 10.1, "low": 9.9,
                         "close": 10., "pre_close": 10., "volume": 5_000_000, "amount": 50_000_000.,
                         "pct_chg": .0000001, "is_st": False})
    raw = _physical_frame(pd.DataFrame(rows), DAYS[-1])
    bundle = {"raw_bars": raw, "calendar": DAYS, "batches": {"fixed": {DAYS[0]: {"candidates": [{"symbol": SYMBOLS[0]}]}}},
              "candidate_table": pd.DataFrame([{"date": DAYS[0], "symbol": SYMBOLS[0], "score": 1.}]),
              "source_metadata": {"snapshot": "original_frozen_stockdb_snapshot"}}
    return bundle, package


def rewrite_symbol(package, symbol, mutation):
    receipt_path = package / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    entry = receipt["completed"][symbol]
    path = package / entry["raw_file"]
    frame = mutation(pd.read_csv(path, dtype=str, keep_default_na=False))
    frame.to_csv(path, index=False)
    entry["raw_sha256"] = source.digest(path)
    frames = [pd.read_csv(package / receipt["completed"][name]["raw_file"], dtype=str, keep_default_na=False) for name in SYMBOLS]
    pd.concat(frames, ignore_index=True).to_csv(package / "raw_daily.csv", index=False)
    receipt["raw_daily_sha256"] = source.digest(package / "raw_daily.csv")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def test_adds_only_missing_tail_preserving_all_original_rows_and_signal_values(fixture):
    bundle, package = fixture
    raw, candidate_table = bundle["raw_bars"].copy(deep=True), bundle["candidate_table"].copy(deep=True)
    batch_bytes = json.dumps(bundle["batches"], sort_keys=True).encode()
    source_bytes = (package / "raw_daily.csv").read_bytes()
    revised, metadata = apply_tail_overlay(bundle, package)
    assert metadata["added_rows"] == 20 and metadata["existing_rows_replaced"] == 0
    pd.testing.assert_frame_equal(revised["raw_bars"].iloc[:len(raw)], raw)
    pd.testing.assert_frame_equal(bundle["raw_bars"], raw)
    pd.testing.assert_frame_equal(revised["candidate_table"], candidate_table)
    assert revised["batches"] is bundle["batches"] and revised["candidate_table"] is bundle["candidate_table"]
    assert json.dumps(revised["batches"], sort_keys=True).encode() == batch_bytes
    assert (package / "raw_daily.csv").read_bytes() == source_bytes
    added = revised["raw_bars"].loc[revised["raw_bars"].date.eq(ADD_DAY)]
    assert set(added.volume) == {5_000_040} and set(added.amount) == {50_000_001.}
    assert metadata["full_cohort_tail_missing_count"] == 1 and metadata["full_cohort_repair_claimed"] is False
    assert all(row["volume_delta_shares"] == 40 and row["amount_delta_rmb"] == 1 for row in metadata["overlap_comparisons"])
    assert all(row["pct_chg_delta_percentage_points"] == -.0000001 for row in metadata["overlap_comparisons"])
    assert metadata["price_source_mode"] == "mixed_source_research_tail_overlay" and metadata["execution_admitted"] is False


@pytest.mark.parametrize("fault", ["past_window", "missing_day", "overlap_ohlc", "volume_unit", "overlap_scaled_units", "unknown_status", "pct_unit"])
def test_history_overwrite_missing_dates_and_unit_errors_fail_without_mutating_base(fixture, fault):
    bundle, package = fixture
    base = bundle["raw_bars"].copy(deep=True)
    def mutate(frame):
        if fault == "past_window":
            frame.loc[0, "date"] = "2026-10-08"
        elif fault == "missing_day":
            frame = frame.loc[frame.date.ne(ADD_DAY)].copy()
        elif fault == "overlap_ohlc":
            frame.loc[0, "high"] = "11.0000"
        elif fault == "volume_unit":
            frame.loc[1, "volume"] = "50000"
        elif fault == "overlap_scaled_units":
            frame["volume"], frame["amount"] = "50000", "500000"
        elif fault == "unknown_status":
            frame.loc[1, "isST"] = "unknown"
        else:
            frame.loc[1, "pctChg"] = ".01"
        return frame
    rewrite_symbol(package, SYMBOLS[0], mutate)
    with pytest.raises(ValueError):
        apply_tail_overlay(bundle, package)
    pd.testing.assert_frame_equal(bundle["raw_bars"], base)


def test_real_provider_halt_row_is_retained_without_synthetic_quote(fixture):
    bundle, package = fixture
    def halt(frame):
        frame.loc[frame.date.eq(ADD_DAY), ["open", "high", "low", "close", "preclose"]] = "10.0000"
        frame.loc[frame.date.eq(ADD_DAY), ["volume", "amount", "tradestatus"]] = "0"
        return frame
    rewrite_symbol(package, SYMBOLS[0], halt)
    revised, metadata = apply_tail_overlay(bundle, package)
    row = revised["raw_bars"].loc[lambda values: values.date.eq(ADD_DAY) & values.symbol.eq(SYMBOLS[0])].iloc[0]
    assert row.volume == row.amount == 0 and row.close == 10. and row.source_row_valid
    assert not row.active_non_st and metadata["added_rows"] == 20


@pytest.mark.parametrize("base_overlap", [True, False])
def test_nullable_suspended_inventory_is_preserved_but_not_zero_filled_into_raw_bar(fixture, base_overlap):
    bundle, package = fixture
    def halt(frame):
        frame[["open", "high", "low", "close", "preclose"]] = "10.0000"
        frame[["volume", "amount", "pctChg"]] = ""
        frame["tradestatus"], frame["isST"] = "0", "1"
        return frame
    rewrite_symbol(package, SYMBOLS[0], halt)
    existing = bundle["raw_bars"].symbol.eq(SYMBOLS[0]) & bundle["raw_bars"].date.isin([DAYS[0], DAYS[-1]])
    bundle["raw_bars"].loc[existing, ["high", "low"]] = 10.
    bundle["raw_bars"].loc[existing, ["volume", "amount", "pct_chg"]] = 0.
    if not base_overlap:
        bundle["raw_bars"] = bundle["raw_bars"].loc[~existing].reset_index(drop=True)
    source_before = (package / "raw_daily.csv").read_bytes()
    revised, metadata = apply_tail_overlay(bundle, package)
    assert metadata["source_inventory_rows"] == 60 and metadata["source_execution_bar_rows"] == 57
    assert metadata["added_rows"] == 19
    assert metadata["skipped_unusable_tail_rows"] == [{"day": ADD_DAY, "symbol": SYMBOLS[0],
        "reason": "unusable_raw_activity", "source_missing_fields": ["volume", "amount", "pctChg"],
        "tradestatus": "0", "isST": "1", "raw_values_preserved": True, "zero_filled": False}]
    assert revised["raw_bars"].loc[lambda values: values.date.eq(ADD_DAY) & values.symbol.eq(SYMBOLS[0])].empty
    assert (package / "raw_daily.csv").read_bytes() == source_before
    overlaps = [row for row in metadata["overlap_comparisons"] if row["symbol"] == SYMBOLS[0]]
    assert all(row["provider_volume_shares"] is None and row["provider_pct_chg"] is None for row in overlaps)
    assert len(overlaps) == (2 if base_overlap else 0)
    assert len(metadata["absent_nullable_overlap_rows"]) == (0 if base_overlap else 2)
    # JSON has explicit nulls, never NaN standing in for a real zero.
    assert "NaN" not in json.dumps(metadata, allow_nan=False)


def test_undeclared_tail_and_later_raw_tampering_are_rejected(fixture):
    bundle, package = fixture
    wrong = {**bundle, "calendar": [DAYS[0], DAYS[-1]]}
    with pytest.raises(ValueError, match="declared following exchange sessions"):
        apply_tail_overlay(wrong, package)
    revised, metadata = apply_tail_overlay(bundle, package)
    revised["raw_bars"].loc[revised["raw_bars"].date.eq(ADD_DAY), "volume"] = 123.
    with pytest.raises(ValueError, match="actual added raw rows"):
        validate_tail_metadata(revised, metadata)


def test_running_source_and_bad_aggregate_cannot_be_overlaid(fixture):
    bundle, package = fixture
    path = package / "receipt.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    wrong = deepcopy(original)
    wrong["status"] = "running"
    path.write_text(json.dumps(wrong), encoding="utf-8")
    with pytest.raises(ValueError, match="completed exact common20"):
        apply_tail_overlay(bundle, package)
    path.write_text(json.dumps(original), encoding="utf-8")
    aggregate = package / "raw_daily.csv"
    aggregate.write_bytes(aggregate.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="aggregate raw hash changed"):
        apply_tail_overlay(bundle, package)
