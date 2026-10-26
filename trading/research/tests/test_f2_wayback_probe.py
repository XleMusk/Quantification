from __future__ import annotations

import json
from pathlib import Path

from trading.research.f2_wayback_probe import archive_url, probe_wayback, write_receipt


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
        archive_fetcher=lambda _url: {"http_status": 503, "payload": b"", "error": "fixture failure"},
    )
    assert result["status"] == "BLOCKED_DATA"
    assert result["historical_evidence"]["pit_asof_safe"] is False
    assert "contract:source_file_or_sha256_missing" in result["historical_evidence"]["reason_codes"]


def test_pre_decision_capture_fetches_archived_bytes_and_proves_source_provenance(tmp_path):
    payload = b"archived-pdf-bytes"

    def archive_fetcher(url):
        assert url == archive_url(URL, "20230430120000")
        return {"http_status": 200, "content_type": "application/pdf", "payload": payload,
                "requested_at": "2026-10-26T00:00:00+00:00",
                "retrieved_at": "2026-10-26T00:00:01+00:00", "error": None}

    result = probe_wayback(
        symbol="000002.SZSE", period="2023Q1", publication_date="2023-04-29",
        decision_day="2024-05-01", source_url=URL,
        fetcher=lambda _url: _response([["20230430120000", URL, "200", "DIGEST1"]]),
        archive_fetcher=archive_fetcher,
        archive_payload_path=tmp_path / "archive.pdf",
    )
    row = result["historical_evidence"]
    assert result["archive_payload_path"].endswith("archive.pdf")
    assert row["publication_contract"]["retrieved_asof_proven"] is True
    assert row["publication_contract"]["publication_evidence_reasons"] == ["revision_id_missing"]
    assert row["source_provenance_verified"] is True
    assert row["historical_retrieval_asof_verified"] is True
    assert row["revision_history_verified"] is False
    assert "revision_history_unproven" in row["reason_codes"]
    assert (tmp_path / "archive.pdf").read_bytes() == payload


def test_latest_eligible_capture_is_selected_independent_of_cdx_order(tmp_path):
    payload = b"latest-eligible-pdf"
    captures = [
        ["20230430120000", URL, "200", "OLDER"],
        ["20240430120000", URL, "200", "LATEST_BEFORE"],
        ["20250501120000", URL, "200", "AFTER_CUTOFF"],
    ]

    def archive_fetcher(url):
        assert url == archive_url(URL, "20240430120000")
        return {"http_status": 200, "content_type": "application/pdf", "payload": payload,
                "requested_at": "2026-10-26T00:00:00+00:00",
                "retrieved_at": "2026-10-26T00:00:01+00:00", "error": None}

    result = probe_wayback(
        symbol="000002.SZSE", period="2023Q1", publication_date="2023-04-29",
        decision_day="2024-05-01", source_url=URL,
        fetcher=lambda _url: _response([captures[2], captures[0], captures[1]]),
        archive_fetcher=archive_fetcher,
        archive_payload_path=tmp_path / "archive.pdf",
    )
    assert result["selected_capture"]["timestamp"] == "20240430120000"
    assert result["capture_selection"] == {
        "policy": "latest_on_or_before_decision_day_v1",
        "eligible_count": 2,
        "observed_count": 3,
        "selected_capture_at": "2024-04-30T12:00:00+00:00",
    }


def test_write_receipt_persists_archive_payload_when_capture_is_eligible(tmp_path):
    output = tmp_path / "receipt.json"
    receipt = write_receipt(
        output,
        symbol="000002.SZSE", period="2023Q1", publication_date="2023-04-29",
        decision_day="2024-05-01", source_url=URL,
        fetcher=lambda _url: _response([["20230430120000", URL, "200", "DIGEST1"]]),
        archive_fetcher=lambda _url: {"http_status": 200, "payload": b"payload", "content_type": "application/pdf",
                                      "requested_at": "2026-10-26T00:00:00+00:00",
                                      "retrieved_at": "2026-10-26T00:00:01+00:00", "error": None},
    )
    assert output.is_file()
    assert receipt["archive_payload_path"]
    assert Path(receipt["archive_payload_path"]).is_file()
