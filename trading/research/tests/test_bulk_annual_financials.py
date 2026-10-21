import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import bulk_annual_financials as annual
from trading.research.bulk_annual_financials import decode_page


def response(rows, count=None):
    count = len(rows) if count is None else count
    return json.dumps({"success": True, "result": {"count": count, "pages": (count + 499) // 500,
                                                 "data": rows}}).encode()


def test_original_fields_known_zero_and_null_are_retained():
    row = {"SECURITY_CODE": "600000", "REPORT_DATE": "2025-12-31 00:00:00",
           "NOTICE_DATE": "2026-04-20 00:00:00", "TOTAL_ASSETS": 0, "TOTAL_EQUITY": None}
    frame, meta = decode_page(response([row]), "2025-12-31", 1)
    assert meta == {"count": 1, "pages": 1, "rows": 1}
    assert frame.TOTAL_ASSETS.iloc[0] == 0
    assert frame.TOTAL_EQUITY.isna().all()
    assert frame.NOTICE_DATE.iloc[0] == row["NOTICE_DATE"]
    with pytest.raises(ValueError, match="incomplete"):
        decode_page(response([row], count=2), "2025-12-31", 1)
    with pytest.raises(ValueError, match="Duplicate"):
        decode_page(response([row, row]), "2025-12-31", 1)


def test_empty_source_period_is_not_a_rejected_request():
    frame, meta = decode_page(response([]), "2025-12-31", 1)
    assert frame.empty and meta == {"count": 0, "pages": 0, "rows": 0}
    with pytest.raises(ValueError, match="rejected"):
        decode_page(json.dumps({"success": False, "message": "unavailable"}).encode(), "2025-12-31", 1)


def test_empty_groups_finish_queries_without_fabricating_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(annual, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(annual, "ROOT", tmp_path / "repository")
    monkeypatch.setattr(annual.time, "sleep", lambda delay: None)
    root = annual.create(["2025-12-31"])
    reply = SimpleNamespace(content=response([]), url=annual.ENDPOINT, raise_for_status=lambda: None)
    _, result = annual.collect(root, max_pages=3, request_get=lambda *args, **kwargs: reply)
    coverage = pd.read_csv(root / "statement_coverage.csv")
    assert result["status"] == "completed_candidate_cache"
    assert coverage.status.eq("empty_source_period").all()
    assert coverage.rows.eq(0).all()
    assert not coverage.execution_admitted.any()
