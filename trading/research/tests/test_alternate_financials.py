import json

import pytest

from trading.research.alternate_financials import decode_statement


def body(items):
    return json.dumps({"result": {"status": {"code": 0}, "data": {
        "report_count": "1", "report_date": [{"date_value": "20251231", "date_description": "annual", "date_type": 4}],
        "report_list": {"20251231": {"publish_date": "20260420", "update_time": 1780000000,
            "rCurrency": "CNY", "rType": "consolidated", "data_source": "provider", "is_audit": "audited", "data": items}}}}},
        ensure_ascii=False).encode("gb18030")


def test_original_encoding_dates_duplicates_and_unknown_values_are_preserved():
    items = [{"item_field": "ASSETS", "item_title": "\u8d44\u4ea7\u603b\u8ba1", "item_value": "0", "item_display_type": 6},
             {"item_field": "ASSETS", "item_title": "duplicate", "item_value": None},
             {"item_field": "BLANK", "item_title": "blank", "item_value": ""},
             {"item_field": "INVALID", "item_title": "bad", "item_value": "unavailable"}]
    frame, reports, entry = decode_statement(body(items), "600000.SSE", "fzb", "2023-01-01", "2026-09-30")
    assert entry["encoding"] == "gb18030"
    assert frame.item_field.tolist() == ["ASSETS", "ASSETS", "BLANK", "INVALID"]
    assert frame.value_status.tolist() == ["observed_numeric", "null", "blank", "invalid_numeric"]
    assert frame.value_numeric_unscaled.iloc[0] == 0 and frame.value_numeric_unscaled.iloc[1:].isna().all()
    assert reports.publish_day.tolist() == ["2026-04-20"]
    assert reports.provider_update_time.tolist() == [1780000000]
    assert not frame.units_verified.any() and not frame.pit_verified.any()


def test_source_error_and_out_of_period_reports_are_not_fabricated():
    bad = json.dumps({"result": {"status": {"code": 1}, "data": {}}}).encode()
    with pytest.raises(ValueError, match="source error"):
        decode_statement(bad, "600000.SSE", "lrb", "2023-01-01", "2026-09-30")
    items, reports, entry = decode_statement(body([]), "600000.SSE", "llb", "2026-01-01", "2026-09-30")
    assert items.empty and reports.empty and entry["requested_periods"] == 0


def test_older_history_excluded_from_request_is_not_a_missing_target_period():
    payload = json.loads(body([]).decode("gb18030"))
    data = payload["result"]["data"]
    data["report_count"] = "39"
    data["report_date"].append({"date_value": "20221231"})
    encoded = json.dumps(payload).encode()
    _, _, complete = decode_statement(encoded, "600000.SSE", "fzb", "2023-01-01", "2026-09-30")
    assert complete["requested_range_not_truncated"] and complete["earliest_received_report"] == "2022-12-31"
    data["report_date"].pop()
    _, _, truncated = decode_statement(json.dumps(payload).encode(), "600000.SSE", "fzb", "2023-01-01", "2026-09-30")
    assert not truncated["requested_range_not_truncated"]
