from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import fund_statistics as source


def row():
    return {"SEC_CODE": "510300", "TX_DATE": "20260119", "DATE_TYPE": "D",
            "OPEN_PRICE": "4.737", "CLOSE_PRICE": "4.738", "HIGH_PRICE": "4.77",
            "LOW_PRICE": "4.72", "TRADE_VOL": "291343.99", "TRADE_AMT": "1379257.64"}


def provider():
    return {"code": "sh.510300", "date": "2026-01-19", "open": "4.7370", "close": "4.7380",
            "high": "4.7690", "low": "4.7180", "volume": "2913439864", "amount": "13792576409.0000"}


def panel(folder):
    folder.mkdir()
    pd.DataFrame([provider()]).to_parquet(folder / "raw_daily.parquet", index=False)
    raw = (folder / "raw_daily.parquet").read_bytes()
    (folder / "result.json").write_text(json.dumps({"status": "source_cache_complete",
                                                    "raw_daily_sha256": sha256(raw).hexdigest()}))
    return folder


def test_public_units_and_precision_do_not_rewrite_provider_quotes():
    raw = provider()
    result = source.compare(raw, row())
    assert result["volume"]["difference_base_units"] == "-36.00"
    assert result["amount"]["difference_base_units"] == "9.0000"
    assert all(item["within_rounding_interval"] for item in result.values())
    assert result["close"]["exact_match"]
    assert not result["high"]["exact_match"] and not result["low"]["exact_match"]
    assert raw == provider()


def test_activity_unit_or_price_conflict_is_not_rounded_away():
    raw = provider()
    raw.update(volume="29134398", high="4.80")
    result = source.compare(raw, row())
    assert not result["volume"]["within_rounding_interval"]
    assert not result["high"]["within_rounding_interval"]


@pytest.mark.parametrize("field,value", [("SEC_CODE", "510050"), ("TX_DATE", "20260120"),
                                         ("DATE_TYPE", "M"), ("TRADE_VOL", "NaN"),
                                         ("TRADE_AMT", "-1")])
def test_conflicting_or_invalid_statistics_refused(field, value):
    official = row()
    official[field] = value
    with pytest.raises(ValueError):
        source.validate({"sqlId": source.SQL_ID, "result": [official]}, "510300", "2026-01-19")


def test_saved_frozen_panel_and_raw_source_consumed_without_admission(tmp_path, monkeypatch):
    view = panel(tmp_path / "view")
    raw = json.dumps({"sqlId": source.SQL_ID, "result": [row()]}).encode()
    monkeypatch.setattr(source, "fetch", lambda session, params: raw)
    folder, result = source.collect(["510300"], ["2026-01-19"], view, tmp_path / "comparison", object())
    assert result["observed_pairs"] == 1 and result["conflicts"] == []
    assert not result["execution_admitted"] and not result["strategy_validated"]
    assert (folder / "510300_2026-01-19.json").read_bytes() == raw
    assert pd.read_parquet(view / "raw_daily.parquet").iloc[0].to_dict() == provider()
    assert not (folder / "actions.csv").exists()


def test_empty_statistics_is_missing_not_zero_or_admitted(tmp_path, monkeypatch):
    view = panel(tmp_path / "view")
    monkeypatch.setattr(source, "fetch", lambda session, params: json.dumps({"sqlId": source.SQL_ID, "result": []}).encode())
    _, result = source.collect(["510300"], ["2026-01-19"], view, tmp_path / "empty", object())
    assert result["missing_pairs"] == 1 and result["observed_pairs"] == 0
    assert result["comparisons"] == [{"code": "510300", "date": "2026-01-19", "source_status": "missing"}]


def test_raw_panel_hash_and_sample_bound_are_enforced_before_network(tmp_path):
    view = panel(tmp_path / "view")
    with pytest.raises(ValueError, match="32"):
        source.collect([f"510{number:03}" for number in range(33)], ["2026-01-19"], view, tmp_path / "large")
    (view / "raw_daily.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        source.collect(["510300"], ["2026-01-19"], view, tmp_path / "changed")
    assert not (tmp_path / "changed").exists()
