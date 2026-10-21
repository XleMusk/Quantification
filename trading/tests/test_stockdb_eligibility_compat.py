import json
from hashlib import sha256

import pandas as pd

from trading.research import stockdb_eligibility_compat as module


def _snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    days = pd.bdate_range("2026-09-01", periods=20)
    rows = []
    for day in days:
        for code, st in (("000001", False), ("600000", False)):
            rows.append({
                "code": code, "date": int(day.strftime("%Y%m%d")),
                "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.2,
                "volume": 3_000_000, "amount": 30_000_000.0,
                "pct_chg": 1.0, "is_st": st, "pre_close": 10.0,
            })
    chunk = snapshot / "chunk.parquet"
    pd.DataFrame(rows).to_parquet(chunk, index=False)
    manifest = {
        "status": "complete_inventory_only", "execution_admitted": False,
        "chunks_expected": 1, "chunks": [{
            "parquet_file": chunk.name,
            "parquet_sha256": sha256(chunk.read_bytes()).hexdigest(),
        }],
    }
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    master = tmp_path / "listing.csv"
    pd.DataFrame([
        {"code": "sz.000001", "ipoDate": "2020-01-01", "outDate": ""},
        {"code": "sh.600000", "ipoDate": "2020-01-01", "outDate": ""},
    ]).to_csv(master, index=False)
    return snapshot, master


def test_eligibility_receipt_is_research_only_and_measures_fields(tmp_path):
    snapshot, master = _snapshot(tmp_path)
    path, result = module.build(snapshot, master, tmp_path / "out")
    assert result["status"] == "completed_research_only_eligibility_compatibility"
    assert result["sessions"] == 20
    assert result["symbols"] == 2
    assert result["latest_compatibility_known_rows"] == 2
    assert result["latest_eligible_proxy_rows"] == 2
    assert result["latest_observed_eligible_proxy_rows"] == 0
    assert result["tradestatus_field"]["present"] is False
    assert result["ranking_generated"] is False
    assert result["target_holdings_generated"] is False
    assert result["paper_orders_generated"] is False
    assert (tmp_path / "out" / "receipt.json").is_file()
    assert (tmp_path / "out" / "REPORT.md").is_file()


def test_eligibility_receipt_without_listing_master_keeps_eligibility_unknown(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    _, result = module.build(snapshot, None, tmp_path / "out")
    assert result["latest_listing_known_rows"] == 0
    assert result["latest_compatibility_known_rows"] == 0
    assert result["latest_eligible_proxy_rows"] == 0
    assert result["latest_observed_eligible_proxy_rows"] == 0
