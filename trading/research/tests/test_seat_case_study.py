import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading.research.seat_case_study import PriceWindow, disclosure_events, _batch_catalog, correct_saved_cases
from trading.research.baostock_daily_slice import digest


def row(key="same", side="buy", buy=10., sell=None):
    return {"raw_id": side + key, "event_key": key, "trade_date": "2026-09-01", "symbol": "000001.SZSE",
        "seat_name": "exact branch", "seat_code": "001", "window_kind": "single", "window_start": "2026-09-01",
        "window_end": "2026-09-01", "reason": side + " reason", "listed_side": side, "buy_cny": buy,
        "sell_cny": sell, "net_cny": None, "period_turnover_cny": 100., "identity_usable": 1, "amount_valid": 1,
        "published_at": None, "retrieved_at": "2026-09-26T18:00:00+08:00", "raw_file": side + ".json.gz"}


def test_identical_buy_sell_listings_not_added_and_unknown_side_not_zero():
    events = disclosure_events([row(), row(side="sell")])
    assert len(events) == 1 and events[0]["buy_cny"] == 10. and events[0]["sell_cny"] is None
    assert events[0]["raw_listing_count"] == 2 and events[0]["published_at"] is None
    assert len(events[0]["reasons"]) == 2 and events[0]["amount_conflict"] is False


def test_conflicting_amounts_retained_and_multiday_not_merged_into_single():
    multi = row(key="multi", buy=30.)
    multi.update(window_kind="multi_up_to_3", window_start=None)
    events = disclosure_events([row(), row(key="different", buy=11.), multi])
    single = [event for event in events if event["window_kind"] == "single"]
    assert len(events) == 3 and all(event["amount_conflict"] for event in single)
    assert {event["buy_cny"] for event in single} == {10., 11.}
    assert next(event for event in events if event["window_kind"] != "single")["window_start"] is None


def test_anonymous_or_multiday_amounts_do_not_assert_same_participant_conflict():
    anonymous = [row(), row(key="other", buy=11.)]
    for item in anonymous:
        item.update(identity_usable=0, seat_name="anonymous institution")
    assert not any(event["amount_conflict"] for event in disclosure_events(anonymous))
    for item in anonymous:
        item.update(identity_usable=1, window_kind="multi_up_to_3", window_start=None)
    assert not any(event["amount_conflict"] for event in disclosure_events(anonymous))


def test_offset_selects_exact_catalog_slice_without_reprocessing_prior_prefix():
    catalog = [{"day": "2026-09-01", "symbol": f"{i:06d}.SZSE"} for i in range(7)]
    selected = _batch_catalog(catalog, 2, 3)
    assert [row["symbol"] for row in selected] == [f"{i:06d}.SZSE" for i in range(2, 5)]
    assert all("not_reprocessed" in row["status"] for row in catalog[:2])
    assert all(row["status"] == "pending_not_processed" for row in catalog[5:])
    assert _batch_catalog(catalog, 0, 2) == catalog[:2]


def test_correction_uses_only_saved_raw_facts_preserves_prices_and_original_files(tmp_path, monkeypatch):
    import trading.research.seat_case_study as module
    original = tmp_path / "original.json"
    case = {"case_id": "fixed", "day": "2026-09-01", "symbol": "000001.SZSE", "stock_disclosures": [],
        "raw_seat_observations": [row(), row(key="other", buy=11.)], "seat_events": [],
        "price_window": {"exact_lags": {}, "unchanged": "original price evidence"}, "review_questions": []}
    for item in case["raw_seat_observations"]:
        item["identity_usable"] = 0
    original.write_text(json.dumps(case))
    before = original.read_bytes()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": module.MANIFEST_SCHEMA, "cases": [{"case_id": "fixed", "day": case["day"],
        "symbol": case["symbol"], "json_file": str(original), "markdown_file": "old.md", "json_sha256": digest(original)}],
        "catalog_file": "original_catalog.csv", "catalog_count": 1, "processed_count": 1, "pending_count": 0,
        "status": "original", "execution_admitted": False}))
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened database")))
    monkeypatch.setattr(module, "PriceWindow", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened prices")))
    corrected = correct_saved_cases(manifest, tmp_path / "corrected")
    saved = json.loads((tmp_path / "corrected/fixed.json").read_text())
    assert original.read_bytes() == before and saved["price_window"] == case["price_window"]
    assert not any(event["amount_conflict"] for event in saved["seat_events"])
    assert corrected["catalog_file"] == "original_catalog.csv" and corrected["database_or_price_source_reopened"] is False


def test_exact_lag_missing_never_uses_next_available_day(tmp_path):
    dates = ["2026-09-01", "2026-09-03"]
    writer = None
    for date in dates:
        frame = pd.DataFrame([{field: "" for field in ("date", "code", "open", "high", "low", "close", "preclose", "volume", "amount",
            "adjustflag", "turn", "tradestatus", "pctChg", "isST")}])
        frame.loc[0, ["date", "code", "adjustflag", "close"]] = [date, "sz.000001", "3", "10" if date == dates[0] else "11"]
        table = pa.Table.from_pandas(frame, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(tmp_path / "raw_daily.parquet", table.schema)
        writer.write_table(table)
    writer.close()
    pd.DataFrame({"date": dates}).to_csv(tmp_path / "input_calendar.csv", index=False)
    (tmp_path / "result.json").write_text(json.dumps({"status": "source_cache_complete", "kind": "stock",
        "execution_admitted": False, "panel_file": "raw_daily.parquet", "rows": 2}))
    price = PriceWindow(tmp_path, ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"])
    window = price.window(dates[0], "000001.SZSE")
    assert window["exact_lags"]["1"]["required_day"] == "2026-09-02"
    assert window["exact_lags"]["1"]["raw"] is window["exact_lags"]["1"]["unadjusted_close_change_pct"] is None
    assert window["exact_lags"]["2"]["unadjusted_close_change_pct"] == 10.000000000000009
    assert all(value["next_available_substituted"] is False for value in window["exact_lags"].values())
