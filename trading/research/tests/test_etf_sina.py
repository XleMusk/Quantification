from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research.full_market_etf import (
    SOURCES, collect, create, normalize_sina_history, sina_payload,
)


def test_payload_accepts_recorded_comment_but_rejects_executable_suffix():
    assert sina_payload('var KLC_K2_sh510300="abc==";\n/* checksum */') == "abc=="
    with pytest.raises(ValueError):
        sina_payload('var KLC_K2_sh510300="abc"; alert(1);')
    with pytest.raises(ValueError):
        sina_payload('var payload={"data":[]};')


def history():
    return pd.DataFrame({
        "date": ["2022-12-30T00:00:00Z", "2026-09-24T00:00:00Z", "2026-09-29T00:00:00Z"],
        "open": [2.0] * 3, "high": [2.1] * 3, "low": [1.9] * 3,
        "close": [2.0] * 3, "volume": [123] * 3,
    })


def test_sina_period_is_pinned_without_fabricated_amount_or_adjustment():
    frame = normalize_sina_history(history(), "510300")
    assert frame.date.tolist() == ["20260924"]
    assert frame.symbol.tolist() == ["510300"]
    assert "amount" not in frame and "adjustflag" not in frame


@pytest.mark.parametrize("column,value", [("close", float("inf")), ("volume", -1), ("high", 1.8)])
def test_sina_invalid_values_rejected(column, value):
    frame = history()
    frame.loc[1, column] = value
    with pytest.raises(ValueError):
        normalize_sina_history(frame, "510300")


def test_sina_source_master_reuse_and_collector_do_not_change_eastmoney(tmp_path, monkeypatch):
    original = tmp_path / "eastmoney"
    original.mkdir()
    source = b"recorded name master"
    (original / "stockdb_names_5.msgpack").write_bytes(source)
    master = {"candidate_codes": ["510300"], "count": 1, "raw_sources": [
        {"file": "stockdb_names_5.msgpack", "sha256": sha256(source).hexdigest(), "rows": 1}],
        "historical_membership_validated": False}
    raw = json.dumps(master).encode()
    (original / "master.json").write_bytes(raw)
    old_state = json.dumps({"master_sha256": sha256(raw).hexdigest(), "receipts": {}}).encode()
    (original / "status.json").write_bytes(old_state)
    folder = create(tmp_path / "sina", provider="sina-ohlcv", master_folder=original)
    received = normalize_sina_history(history(), "510300")
    calls = []

    def fetch(code, target, start, end):
        calls.append((code, start, end))
        (target / f"{code}_response.js").write_bytes(b"archived response")
        return code, received, None

    monkeypatch.setattr("trading.research.full_market_etf.fetch_sina_one", fetch)
    monkeypatch.setattr("trading.research.full_market_etf.fetch_one",
                        lambda _: pytest.fail("Sina collection must not call Eastmoney"))
    result = collect(folder, 1, workers=1, request_interval=0)
    assert calls == [("510300", "20230101", "20260924")]
    assert result["source"] == SOURCES["sina-ohlcv"]
    assert result["acquired_symbols"] == 1 and not result["execution_admitted"]
    assert not result["adjustment_verified"] and not result["volume_unit_verified"]
    assert result["receipts"]["510300"]["raw_sha256"] == sha256(b"archived response").hexdigest()
    assert (original / "status.json").read_bytes() == old_state
    collect(folder, 1, workers=1, request_interval=0)
    assert len(calls) == 1


def test_sina_collection_cannot_be_relabeled_as_adjusted(tmp_path):
    raw = json.dumps({"candidate_codes": []}).encode()
    (tmp_path / "master.json").write_bytes(raw)
    (tmp_path / "status.json").write_text(json.dumps({
        "master_sha256": sha256(raw).hexdigest(), "receipts": {},
        "provider": "sina-ohlcv", "source": SOURCES["sina-ohlcv"], "adjustment": "qfq",
    }))
    with pytest.raises(ValueError, match="separate unverified"):
        collect(tmp_path, 1)
