"""Synthetic local prices; no strategy, provider or account execution."""
from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import next_session_preview as m


def test_rolling_preview_uses_bound_exact_day_raw_supplement_not_future_or_old_prices(tmp_path):
    snapshot, supplement, package = [tmp_path / name for name in ("snapshot", "supplement", "package")]
    for folder in (snapshot, supplement, package):
        folder.mkdir()
    pd.DataFrame([
        {"date": 20261013, "code": "000001", "close": 13.0, "volume": 100, "amount": 1300.0, "is_st": False},
        {"date": 20261015, "code": "000002", "close": 20.0, "volume": 100, "amount": 2000.0, "is_st": False},
    ]).to_parquet(snapshot / "chunk.parquet", index=False)
    manifest = {"status": "complete_inventory_only", "execution_admitted": False,
                "chunks": [{"parquet_file": "chunk.parquet"}]}
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    def row(code, day, price, halted=False):
        return {"date": day, "code": code, "open": price, "high": price, "low": price,
                "close": price, "preclose": price, "volume": "" if halted else "100",
                "amount": "" if halted else str(float(price) * 100), "adjustflag": "3",
                "tradestatus": "0" if halted else "1", "pctChg": "0.000000", "isST": "0"}
    rows = [row("sz.000001", "2026-10-15", "15.0000"),
            row("sz.000002", "2026-10-15", "99.0000"),
            row("sz.000003", "2026-10-16", "16.0000"),
            row("sz.000004", "2026-10-13", "14.0000"),
            row("sz.000005", "2026-10-15", "5.0000", halted=True)]
    raw = supplement / "raw_daily.parquet"
    pd.DataFrame(rows).to_parquet(raw, index=False)
    original = package / "raw_daily.csv"
    pd.DataFrame([r for r in rows if r["date"] == "2026-10-15"]).to_csv(original, index=False)
    binding = {"path": str(raw), "rows_added": 3,
               "basis": "local raw_daily.parquet; fills missing StockDB code/date keys only"}
    source = {"supplemental_raw_view": binding, "completed_at": "2026-10-15T15:40:00+08:00"}
    protocol = {"snapshot": str(snapshot), "snapshot_manifest": manifest,
                "supplemental_raw_view": deepcopy(binding)}
    receipt = {"schema": "lowvol_append_only_complete_source_supplement_v1",
               "status": "completed_append_only_research_supplement", "raw_daily_file": str(raw),
               "created_at": "2026-10-15T15:30:00+08:00", "original_full_row_prefix_preserved": True,
               **{key: False for key in ("execution_admitted", "selection_enabled", "strategy_validated",
                    "source_verified", "pit_verified", "corporate_actions_verified", "stockdb_modified", "ledger_mutated")},
               "source_proofs": [{"day": "2026-10-15", "source_folder": str(package),
                    "raw_daily_sha256": sha256(original.read_bytes()).hexdigest(), "read_only_verified": True,
                    "source_inventory_complete": True, "full_dated_roster_inventory_complete": True,
                    "receipt_status": "complete_full_dated_close_inventory_not_admitted"}]}
    receipt_file = supplement / "result.json"
    receipt_file.write_text(json.dumps(receipt), encoding="utf8")
    wanted = {f"00000{i}.SZSE" for i in range(1, 6)}
    base = m._quote_rows(snapshot, "2026-10-15", wanted)
    assert set(base) == {"000002.SZSE"}  # No fallback to snapshot13.
    quotes = m._stockdb_supplement_quotes(source, protocol, snapshot, "2026-10-15", wanted, base)
    assert set(quotes) == {"000001.SZSE", "000002.SZSE", "000005.SZSE"}
    assert quotes["000001.SZSE"]["close_cents"] == 1500
    assert quotes["000001.SZSE"]["trading_date"] == "2026-10-15"
    assert quotes["000001.SZSE"]["price_basis"] == "research_raw_close_rmb"
    assert quotes["000001.SZSE"]["executable_quote_verified"] is False
    assert quotes["000002.SZSE"] == base["000002.SZSE"]  # No overwrite by supplement99.
    assert quotes["000005.SZSE"]["tradestatus"] == 0 and quotes["000005.SZSE"]["amount_rmb"] is None
    assert "000003.SZSE" not in quotes and "000004.SZSE" not in quotes
    wrong = deepcopy(protocol)
    wrong["supplemental_raw_view"]["path"] = str(tmp_path / "different.parquet")
    with pytest.raises(ValueError, match="binding changed"):
        m._stockdb_supplement_quotes(source, wrong, snapshot, "2026-10-15", wanted, base)
    receipt["source_proofs"][0]["day"] = "2026-10-16"
    receipt_file.write_text(json.dumps(receipt), encoding="utf8")
    assert m._stockdb_supplement_quotes(source, protocol, snapshot, "2026-10-15", wanted, base) == base
    receipt["source_proofs"][0]["day"] = "2026-10-15"
    receipt_file.write_text(json.dumps(receipt), encoding="utf8")
    rows[0]["close"] = "15.0100"
    pd.DataFrame(rows).to_parquet(raw, index=False)
    with pytest.raises(ValueError, match="exact original"):
        m._stockdb_supplement_quotes(source, protocol, snapshot, "2026-10-15", wanted, base)
