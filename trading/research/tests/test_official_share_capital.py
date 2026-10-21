from datetime import datetime, timedelta
from hashlib import sha256
from io import BytesIO
import json
import zipfile

import openpyxl
import pandas as pd
import pytest

from trading.contracts import MARKET_TZ
from trading.research import official_share_capital as module


HEADERS = [*module.REQUIRED_HEADERS, *(f"原列{i}" for i in range(17))]
SOURCE_AT = datetime(2026, 10, 19, 4, 43, 44, tzinfo=MARKET_TZ)
DERIVED_AT = SOURCE_AT + timedelta(minutes=1)


def source(tmp_path, rows, *, broken_dimensions=False):
    directory = tmp_path / "source"
    directory.mkdir()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(HEADERS)
    for row in rows:
        sheet.append([*row, *(f"value{i}" for i in range(17))])
    buffer = BytesIO()
    book.save(buffer)
    response = buffer.getvalue()
    if broken_dimensions:
        new = BytesIO()
        with zipfile.ZipFile(BytesIO(response)) as old, zipfile.ZipFile(new, "w") as rewritten:
            for item in old.infolist():
                content = old.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    import re
                    content = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1"', content)
                rewritten.writestr(item, content)
        response = new.getvalue()
    (directory / "response.bin").write_bytes(response)
    receipt = {"source_url": module.SOURCE_URL,
               "params": {"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"},
               "started_at": (SOURCE_AT - timedelta(seconds=2)).isoformat(),
               "completed_at": SOURCE_AT.isoformat(), "status_code": 200, "http_success": True,
               "execution_admitted": False, "account_accessed": False,
               "byte_count": len(response), "sha256": sha256(response).hexdigest()}
    (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return directory


def test_wrong_excel_dimension_is_reset_and_all_original_columns_preserved(tmp_path, monkeypatch):
    original = source(tmp_path, [["000001", "一", "1991-04-03", "1,000", "500"],
                                 ["000002", "二", "2026-10-20", "2,000", "800"]], broken_dimensions=True)
    monkeypatch.setattr(module, "_now", lambda: DERIVED_AT)
    result = module.normalize(original, tmp_path / "normalized")
    frame = pd.read_parquet(result["files"]["normalized"]["path"])
    quality = json.loads((tmp_path / "normalized" / "quality.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (tmp_path / "normalized" / "original_rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(frame) == len(rows) == 2 and all(len(row["values"]) == 22 for row in rows)
    assert quality["declared_worksheet_dimension"] == "A1:A1"
    assert quality["listing_after_source_local_date_count"] == 1
    assert result["unit"] == "unknown" and result["source_asof"] is None
    assert not result["validated_denominator"] and not result["historical_pit_verified"]
    assert frame.symbol.tolist() == ["000001", "000002"]


def test_comma_integer_explicit_zero_and_blank_are_distinct(tmp_path, monkeypatch):
    original = source(tmp_path, [["000001", "一", "1991-04-03", "19,405,684,991", "0"],
                                 ["000002", "二", "1991-04-03", "", None]])
    monkeypatch.setattr(module, "_now", lambda: DERIVED_AT)
    result = module.normalize(original, tmp_path / "normalized")
    frame = pd.read_parquet(result["files"]["normalized"]["path"])
    assert frame.loc[0, "total_a_reported_integer"] == 19405684991
    assert frame.loc[0, "circulating_a_reported_integer"] == 0
    assert pd.isna(frame.loc[1, "total_a_reported_integer"])
    assert pd.isna(frame.loc[1, "circulating_a_reported_integer"])
    assert frame.loc[0, "total_a_raw"] == "19,405,684,991"
    with pytest.raises(FileExistsError):
        module.normalize(original, tmp_path / "normalized")
    with pytest.raises(ValueError, match="outside"):
        module.normalize(original, original / "child")


def test_duplicate_code_failed_receipt_and_changed_bytes_are_rejected_before_output(tmp_path):
    original = source(tmp_path, [["000001", "一", "1991-04-03", "10", "5"],
                                 ["000001", "一", "1991-04-03", "10", "5"]])
    destination = tmp_path / "normalized"
    with pytest.raises(ValueError, match="Duplicate original"):
        module.normalize(original, destination)
    assert not destination.exists()
    receipt_path = original / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["http_success"] = False
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="Successful official"):
        module.normalize(original, destination)
    receipt["http_success"] = True
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with (original / "response.bin").open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="bytes changed"):
        module.normalize(original, destination)
    assert not destination.exists()


def test_lookup_obeys_both_actual_clocks_and_never_validates_denominator(tmp_path, monkeypatch):
    original = source(tmp_path, [["000001", "一", "1991-04-03", "1,000", "500"]])
    monkeypatch.setattr(module, "_now", lambda: DERIVED_AT)
    module.normalize(original, tmp_path / "normalized")
    snapshot = tmp_path / "normalized" / "result.json"
    before_source = module.lookup(snapshot, ["000001.SZSE"], (SOURCE_AT - timedelta(seconds=1)).isoformat())
    before_derived = module.lookup(snapshot, ["000001.SZSE"], SOURCE_AT.isoformat())
    assert before_source["status"] == "unknown_source_not_available"
    assert before_derived["status"] == "unknown_derived_not_available"
    for observation in (before_source, before_derived):
        assert observation["rows"][0]["total_a_reported_integer"] is None
        assert "total_a_raw" not in observation["rows"][0]
    ready = module.lookup(snapshot, ["000001.SZSE", "000999", "600000.SSE"], DERIVED_AT.isoformat())
    assert ready["rows"][0]["symbol"] == "000001"
    assert ready["rows"][0]["circulating_a_reported_integer"] == 500
    assert ready["rows"][1]["status"] == "unknown_not_observed_in_snapshot"
    assert ready["rows"][2]["status"] == "unknown_outside_source_exchange"
    assert ready["rows"][2]["circulating_a_reported_integer"] is None
    assert not ready["validated_denominator"] and not ready["free_float_verified"]
    with pytest.raises(ValueError, match="timezone-aware"):
        module.lookup(snapshot, ["000001"], "2026-10-19T05:00:00")
    with pytest.raises(ValueError, match="exact original"):
        module.lookup(snapshot, ["bad.SSE"], DERIVED_AT.isoformat())


def test_fixed_collector_saves_single_failure_without_retry_or_normalization(tmp_path, monkeypatch):
    import requests

    calls = []

    def fail(url, **kwargs):
        calls.append((url, kwargs))
        raise requests.Timeout("fixture timeout")

    monkeypatch.setattr(requests, "get", fail)
    out = tmp_path / "failed_source"
    receipt = module.collect(out)
    assert len(calls) == 1
    assert calls[0][0] == module.SOURCE_URL
    assert calls[0][1] == {"params": {"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1",
                                    "random": "0.6935816432433362"},
                            "timeout": (10, 20), "allow_redirects": False,
                            "headers": {"User-Agent": "Mozilla/5.0",
                                        "Referer": "https://www.szse.cn/market/product/stock/list/index.html"}}
    assert receipt["http_success"] is False and receipt["automatic_retry"] is False
    assert json.loads((out / "receipt.json").read_text(encoding="utf-8"))["error"] == "Timeout: fixture timeout"
    assert not (out / "normalized.parquet").exists() and not (out / "response.bin").exists()
