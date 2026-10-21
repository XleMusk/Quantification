"""Tiny synthetic selection/unknown and source-contract checks, not market evidence."""
import json

import pandas as pd
import pytest

from trading.research.leader_posthoc_industry import load


def saved(tmp_path):
    entries = []
    for day, returned, industry in [("2026-01-05", "2026-01-05", "bank"),
                                    ("2026-01-12", "2026-01-11", "")]:
        name = day + ".parquet"
        pd.DataFrame({"date": [day], "symbol": ["000001.SZSE"],
            "updateDate": [returned], "industry": [industry],
            "industryClassification": ["CSRC"]}).to_parquet(tmp_path / name, index=False)
        entries.append({"date": day, "rows": 1, "file": name,
            "retrieved_at": "2026-10-04T03:35:15+08:00", "update_dates": [returned]})
    record = {"status": "completed_requested_industry_history",
        "source": "BaoStock.query_stock_industry(date=decision_day)",
        "requested_dates": [entry["date"] for entry in entries], "entries": entries,
        "revision_safe_pit_verified": False, "execution_admitted": False}
    (tmp_path / "result.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def test_backward_snapshot_keeps_real_clock_returned_date_and_unknown(tmp_path):
    saved(tmp_path)
    provider = load(tmp_path, "2026-10-17T00:00:00+08:00")
    assert provider("2026-01-02") is None
    first = provider("2026-01-09")
    assert first["snapshot_date"] == "2026-01-05" and first["membership"] == {"000001.SZSE": "bank"}
    latest = provider("2026-09-30")
    assert latest["snapshot_date"] == "2026-01-12" and latest["source_update_dates"] == ["2026-01-11"]
    assert latest["membership"] == {"000001.SZSE": None}
    assert latest["actual_available_at"] == "2026-10-04T03:35:15+08:00" and "available_at" not in latest
    latest["membership"]["000001.SZSE"] = "not persistent"
    assert provider("2026-01-12")["membership"]["000001.SZSE"] is None
    assert provider.provenance["snapshot_count"] == 2
    assert all(value["source_sha256"] for value in provider.provenance["source_entries"])


def test_future_receipt_or_returned_date_mismatch_is_rejected(tmp_path):
    record = saved(tmp_path)
    with pytest.raises(ValueError, match="actually retrieved"):
        load(tmp_path, "2026-01-05T18:00:00+08:00")
    record["entries"][1]["update_dates"] = ["2026-01-12"]
    (tmp_path / "result.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="Returned provider update dates"):
        load(tmp_path, "2026-10-17T00:00:00+08:00")
