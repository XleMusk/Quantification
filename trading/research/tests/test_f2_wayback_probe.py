from __future__ import annotations

import json

from trading.research.f2_wayback_probe import probe_wayback


URL = "https://static.cninfo.com.cn/finalpage/2023-04-29/1216686497.PDF"


def _response(rows):
    payload = json.dumps([["timestamp", "original", "statuscode", "digest"], *rows]).encode()
    return {"http_status": 200, "payload": payload, "response_sha256": "x", "retrieved_at": "2026-10-26T00:00:00+00:00"}


def test_after_decision_capture_stays_blocked():
    result = probe_wayback(
        symbol="000002.SZSE", period="2023Q1", publication_date="2023-04-29",
        decision_day="2024-05-01", source_url=URL,
        fetcher=lambda _url: _response([["20250905235023", URL, "200", "JETC2ZGDKLIYORYGFKJ5X7DVJDHHPMNB"]]),
    )
    assert result["status"] == "BLOCKED_DATA"
    assert result["captures_on_or_before_decision_day"] == []
    assert "archive_capture_after_decision_only" in result["historical_evidence"]["reason_codes"]
    assert result["factor_computed"] is False and result["account_replay"] is False


def test_pre_decision_capture_still_needs_archived_payload_hash():
    result = probe_wayback(
        symbol="000002.SZSE", period="2023Q1", publication_date="2023-04-29",
        decision_day="2024-05-01", source_url=URL,
        fetcher=lambda _url: _response([["20230430120000", URL, "200", "DIGEST1"]]),
    )
    assert result["status"] == "BLOCKED_DATA"
    assert result["historical_evidence"]["pit_asof_safe"] is False
    assert "contract:source_file_or_sha256_missing" in result["historical_evidence"]["reason_codes"]
