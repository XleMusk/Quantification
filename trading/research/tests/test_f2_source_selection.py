from __future__ import annotations

import json

from trading.research.f2_source_selection import BLOCKED, SCHEMA, SELECTED, build_receipt, select_latest_on_or_before


def test_select_latest_eligible_is_independent_of_input_order():
    captures = [
        {"timestamp": "20250501120000", "digest": "after"},
        {"timestamp": "20230430120000", "digest": "old"},
        {"timestamp": "20240430120000", "digest": "latest"},
    ]
    result = select_latest_on_or_before(
        captures, "2024-05-01", timestamp_key="timestamp", timestamp_format="%Y%m%d%H%M%S")
    assert result["schema"] == SCHEMA
    assert result["eligible_count"] == 2
    assert result["selected"]["digest"] == "latest"
    assert result["observed_after_cutoff_only"] is False


def test_after_cutoff_only_is_explicitly_blocked():
    result = select_latest_on_or_before(
        [{"timestamp": "20250905235023", "digest": "after"}],
        "2024-05-01", timestamp_key="timestamp", timestamp_format="%Y%m%d%H%M%S")
    assert result["eligible_count"] == 0
    assert result["selected"]["digest"] == "after"
    assert result["observed_after_cutoff_only"] is True


def test_same_day_capture_after_decision_time_is_not_eligible():
    result = select_latest_on_or_before(
        [{"timestamp": "20240501150600", "digest": "after"}],
        "2024-05-01", timestamp_key="timestamp", timestamp_format="%Y%m%d%H%M%S",
        cutoff_time="2024-05-01T15:05:00+08:00")
    assert result["eligible_count"] == 0
    assert result["selected"]["digest"] == "after"
    assert result["observed_after_cutoff_only"] is True


def test_selection_receipt_binds_existing_wayback_probe_without_network(tmp_path):
    source = tmp_path / "wayback.json"
    source.write_text(json.dumps({
        "schema": "f2_wayback_probe_v1",
        "symbol": "000002.SZSE",
        "period": "2023Q1",
        "decision_day": "2024-05-01",
        "captures": [{"timestamp": "20250905235023", "digest": "after"}],
    }), encoding="utf-8")
    receipt = build_receipt(source)
    assert receipt["status"] == BLOCKED
    assert receipt["selection"]["eligible_count"] == 0
    assert receipt["execution_admitted"] is False
    assert receipt["factor_computed"] is False


def test_selection_receipt_can_select_predecision_candidate(tmp_path):
    source = tmp_path / "wayback.json"
    source.write_text(json.dumps({
        "schema": "f2_wayback_probe_v1", "decision_day": "2024-05-01",
        "captures": [
            {"timestamp": "20230430120000", "digest": "old"},
            {"timestamp": "20240430120000", "digest": "latest"},
        ],
    }), encoding="utf-8")
    receipt = build_receipt(source)
    assert receipt["status"] == SELECTED
    assert receipt["selection"]["selected"]["digest"] == "latest"
