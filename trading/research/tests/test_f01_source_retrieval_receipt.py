from __future__ import annotations

import json
from pathlib import Path
import hashlib
import json

from trading.research.f01_source_retrieval_receipt import build_retrieval_receipt


def test_retrieval_receipt_records_hash_match_without_pit_promotion() -> None:
    scoped = Path("outputs/financials/f01_scoped_field_coverage_20261024.json")
    rows = json.loads(Path(
        r"D:\QuantificationData\trading\research\financials\20260930_162448_8be1e603\field_checks\20260930_q1_three_issuers\source_receipts.json"
    ).read_text(encoding="utf-8"))
    by_url = {row["source"]: row for row in rows}

    def fake_fetch(url: str, *, timeout: float) -> dict[str, object]:
        payload = Path(by_url[url]["pdf"]).read_bytes()
        return {"requested_at": "2026-10-24T12:00:00+08:00", "retrieved_at": "2026-10-24T12:00:01+08:00",
                "http_status": 200, "content_type": "application/pdf", "content_length": len(payload),
                "response_sha256": hashlib.sha256(payload).hexdigest(), "error": None}

    result = build_retrieval_receipt(scoped, fetcher=fake_fetch, market_snapshot=None)
    assert result["status"] == "NETWORK_RETRIEVED_HASH_MATCH_PIT_PENDING"
    assert result["summary"]["retrieved_source_hash_match_rows"] == 3
    assert result["rows"][0]["revision_id"]
    assert result["rows"][0]["historical_asof_verified"] is False
    assert result["summary"]["market_join_verified"] is False
    assert result["execution_admitted"] is False
