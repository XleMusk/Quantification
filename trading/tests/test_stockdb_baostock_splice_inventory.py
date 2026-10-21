from pathlib import Path
import json

import pandas as pd

from trading.research.stockdb_baostock_splice_inventory import build


def test_diagnostic_inventory_keeps_raw_and_derived_provider_separate(tmp_path: Path):
    extension = tmp_path / "extension"
    extension.mkdir()
    panel = extension / "raw_daily.parquet"
    pd.DataFrame([
        {"code": "300114", "date": "2023-12-29", "open": 1, "high": 2, "low": 1,
         "close": 1.5, "pre_close": 1.4, "volume": 10, "amount": 15, "pct_chg": 1,
         "is_st": 0},
    ]).to_parquet(panel, index=False)
    (extension / "manifest.json").write_text(json.dumps({"raw_file": str(panel)}), encoding="utf-8")
    provider = tmp_path / "provider"
    provider_folder = provider / "qlib_provider" / "instruments"
    provider_folder.mkdir(parents=True)
    (provider_folder / "all.txt").write_text("300114.SZSE\t2024-01-02\t2024-01-02\n", encoding="utf-8")
    (provider / "result.json").write_text(json.dumps({
        "status": "research_provider_completed", "execution_admitted": False,
        "provider_folder": str(provider / "qlib_provider"), "calendar": [20240102],
        "dates": 1, "symbols": 1, "rows": 1,
        "fields": ["open", "high", "low", "close", "volume", "vwap", "factor"],
    }), encoding="utf-8")
    output, result = build(extension, provider, out=tmp_path / "out", artifact=tmp_path / "artifact")
    assert result["status"] == "diagnostic_complete"
    assert result["splice_admissible"] is False
    assert result["splice_boundary"]["date_overlap_count"] == 0
    assert result["splice_boundary"]["calendar_gap_count"] == 0
    assert result["existing_provider_modified"] is False
    assert Path(output).is_file()
